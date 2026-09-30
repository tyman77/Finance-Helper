"""Code United statement lines with Claude: evidence in, judgment out.

The rules engine (enrich.py) is a stack of brittle joins — booking name to
traveler map to schedule row to project code to state to airport table — and
every join it gets wrong becomes another special case. This module flips the
architecture: code GATHERS the evidence (schedule rows, hotel stays, Ramp
memos, history, the active project list) and Claude weighs it, returning a
structured coding per line with a confidence and a one-line reason.

Guardrails stay deterministic and outside the model: the project must come
from the provided active list (anything else is dropped to a suggestion),
the department-required floor and the posted ledger are enforced at post
time as always, and every line still lands in the human review UI.

Runs only when ANTHROPIC_API_KEY is set; FINANCE_HELPER_LLM_CODER=0 turns
it off (rules-engine coding is always computed first and stays in place
wherever Claude is unsure or unavailable). Model via TRAVEL_CODER_MODEL
(default claude-opus-5 — one statement a month; judgment beats pennies),
effort via TRAVEL_CODER_EFFORT (default medium).
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from . import project_resolver

DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You code airline statement lines to GL accounts, departments, and job (project) numbers for Summit Integrated Systems, an AV integration company headquartered in Denver (home airport DEN). You receive one JSON payload: the company's active projects (code and name, names usually carry a city/state), the department list, and the statement lines with the evidence gathered for each traveler.

House rules:
- Project work travel (installs, engineering visits, project management trips) -> account 52200 with the project number and the traveler's department.
- Overhead travel with no project (HQ visits, training, conferences, recruiting, internal meetings) -> account 71000 with the traveler's department and NO project.
- Baggage fees, seat fees, and change fees belong to the same trip as the traveler's flight on or near that date - code them to the same project/account.
- A negative amount is a refund/credit - code it where the original charge went.
- Choose projects ONLY from the provided active project list. Never invent a code.

How to weigh evidence:
- The flight route is ground truth for where the traveler actually went. Match route cities/states against project locations using real geography, including metro areas that cross state lines (e.g. MCI serves the whole Kansas City metro including Overland Park KS; DCA/IAD serve DC/MD/VA).
- Projects may carry a "location" — the customer's actual city and state. A city-level match between the flight destination and a project's location is the strongest geographic signal; prefer it over a mere state match from the project name.
- The crew schedule says what job the traveler was assigned around those dates; hotel stays naming the traveler and Ramp per-diems corroborate. A per-diem's own accounting coding is strong evidence: one carrying a project pins the trip's job, and one coded to an overhead GL category (71040 OH - Meals / Per Diem) with no project says the trip was overhead (sales/HQ), not project work — code the flight 71000 with the traveler's department. When signals conflict, prefer the combination consistent with the route and dates, and say why in one short sentence.
- Historical projects are weak evidence on their own - use them to break ties, not to override the route.
- coding_notes, when present, are standing instructions from the finance team. They outrank every other heuristic and any pattern you might infer.
- past_corrections are lines you previously coded wrong, with what you said and the human's correct answer. Never repeat those mistakes, and generalize from them (a corrected traveler/route pattern likely applies to that traveler's similar trips). confirmed_examples are past codings a human verified - trustworthy precedent.

Confidence:
- high: route, dates, and at least one assignment signal agree.
- medium: one solid signal with nothing contradicting it.
- low: evidence is missing or conflicting - leave project empty, put the plausible codes in candidates, and say what would decide it in reason.

For every input line return: line (its index), gl_account, department (code from the list, or the traveler's hint), project (code or empty), candidates (comma-separated codes when low, else empty), confidence, reason (one short sentence a reviewer can act on)."""


class LineCoding(BaseModel):
    line: int
    gl_account: str
    department: str
    project: str
    candidates: str
    confidence: Literal["high", "medium", "low"]
    reason: str


class TravelCoding(BaseModel):
    lines: list[LineCoding]


def enabled() -> bool:
    if (os.environ.get("FINANCE_HELPER_LLM_CODER") or "").lower() in ("0", "false", "off"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.environ.get("TRAVEL_CODER_MODEL") or DEFAULT_MODEL


def _client():
    import anthropic
    # Explicit timeout: with a large max_tokens and the default timeout the
    # SDK refuses non-streaming calls outright ("Streaming is required for
    # operations that may take longer than 10 minutes"). One statement's
    # coding finishes in a few minutes; give it room and keep retries.
    return anthropic.Anthropic(timeout=900.0)


def _parse_date(raw) -> date | None:
    from .enrich import _parse_date as p
    return p(raw)


def _eligible(li) -> bool:
    note = (li.note or "")
    if getattr(li, "posted_ref", ""):
        return False                      # already in a journal entry
    if "inflight wifi purchase" in note or "United Club membership" in note:
        return False                      # hard rules own these
    return bool(li.person)                # unknown travelers stay "unknown"


def _evidence(doc, schedule_index, hotel_index, ramp_index, registry,
              active_projects, history) -> tuple[dict, list[int]]:
    """The JSON payload for the model, and which line indexes it covers."""
    names = project_resolver.project_display_names(registry or {})
    locations = _project_locations()
    projects = []
    for c, n in sorted(names.items()):
        if active_projects is not None and c not in active_projects:
            continue
        entry = {"code": c, "name": n}
        if locations.get(c):
            entry["location"] = locations[c]
        projects.append(entry)

    from . import config
    departments = config.accounts().get("departments", {})

    lines, covered = [], []
    for i, li in enumerate(doc.line_items):
        if not _eligible(li):
            continue
        dep = _parse_date(li.raw.get("Departure Date")) if li.raw else None
        routing = next((str(v) for k, v in (li.raw or {}).items()
                        if k.lower().startswith("routing")), "") or ""
        entry: dict = {
            "line": i,
            "traveler": li.person,
            "department_hint": li.department or "",
            "route": routing or li.description,
            "depart_date": dep.isoformat() if dep else "",
            "description": li.description,
            "amount": str(li.amount),
        }
        key = project_resolver.match_person_key(li.person, (schedule_index or {}).keys())
        if key and dep:
            rows = schedule_index[key]
            lo, hi = dep - timedelta(days=14), dep + timedelta(days=14)
            sched = {d: v for d, v in sorted(rows.items())
                     if v and lo.isoformat() <= d <= hi.isoformat()}
            if sched:
                entry["schedule"] = sched
        if hotel_index and dep:
            stays = [b for b in hotel_index
                     if any(project_resolver.same_person(g, li.person)
                            for g in (b.get("guests") or []))
                     and abs((_iso(b.get("start")) - dep).days) <= 10]
            if stays:
                entry["hotel_stays"] = [{k: b.get(k) for k in
                                         ("start", "end", "project", "city", "hotel")
                                         if b.get(k)} for b in stays[:4]]
        if ramp_index and dep:
            hits = [r for r in ramp_index
                    if project_resolver.same_person(r.get("person", ""), li.person or "")
                    and r.get("date") and abs((_iso(r["date"]) - dep).days) <= 21]
            if hits:
                entry["per_diems"] = [
                    {k: r[k] for k in ("date", "memo", "project",
                                       "gl_category", "department")
                     if r.get(k)} for r in hits[:4]]
        hist = (history or {}).get(li.person) or _history_from_note(li.note)
        if hist:
            entry["historical_projects"] = list(hist)[:8]
        lines.append(entry)
        covered.append(i)
    payload = {"projects": projects, "departments": departments, "lines": lines}
    notes = coding_notes()
    if notes:
        payload["coding_notes"] = notes
    confirmed, corrections = _feedback()
    if corrections:
        payload["past_corrections"] = corrections
    if confirmed:
        payload["confirmed_examples"] = confirmed
    return payload, covered


def _iso(raw) -> date:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return date(1970, 1, 1)


def _data_dir() -> str:
    return os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "data"))


def coding_notes() -> str:
    """Standing instructions the finance team typed on the Admin page —
    tribal knowledge no data file carries ("Cody is based in Sacramento, SMF
    is his home airport"). Injected into every coding call."""
    try:
        with open(os.path.join(_data_dir(), "coding_notes.txt"),
                  encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def save_coding_notes(text: str) -> None:
    path = os.path.join(_data_dir(), "coding_notes.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write((text or "").strip() + "\n")


def _corrections_path() -> str:
    return os.path.join(_data_dir(), "coding_corrections.json")


def _load_corrections() -> list[dict]:
    import json
    try:
        with open(_corrections_path(), encoding="utf-8") as fh:
            return json.load(fh) or []
    except (OSError, ValueError):
        return []


def record_outcomes(lines) -> int:
    """Called at post time with the lines that actually went into the JE.
    Diffs the human's final coding against what Claude proposed (stamped on
    the line at coding time) and appends the outcomes — this is the ground
    truth the next run learns from. Best-effort; returns records written."""
    import json
    from datetime import datetime

    records = []
    for li in lines:
        prop = (li.raw or {}).get("_claude")
        if not prop:
            continue
        final = {"project": li.project or "", "gl_account": li.gl_account or "",
                 "department": li.department or ""}
        proposed = {k: prop.get(k, "") for k in ("project", "gl_account", "department")}
        records.append({
            "when": datetime.now().isoformat(timespec="seconds"),
            "traveler": li.person or "",
            "route": prop.get("route", ""),
            "date": prop.get("date", ""),
            "description": li.description,
            "final": final,
            "proposed": proposed,
            "confidence": prop.get("confidence", ""),
            "agreed": final == proposed,
        })
    if not records:
        return 0
    history = _load_corrections()
    history.extend(records)
    history = history[-500:]
    try:
        path = _corrections_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        return 0
    return len(records)


def _feedback() -> tuple[list[dict], list[dict]]:
    """(confirmed examples, corrections) for the prompt — compact rows the
    model can pattern-match against, mistakes first."""
    def compact(e, with_proposed):
        row = {"traveler": e.get("traveler"), "route": e.get("route"),
               "date": e.get("date"), "correct": e.get("final")}
        if with_proposed:
            row["you_said"] = e.get("proposed")
        return row

    history = _load_corrections()
    corrections = [compact(e, True) for e in history if not e.get("agreed")][-30:]
    confirmed = [compact(e, False) for e in history if e.get("agreed")][-20:]
    return confirmed, corrections


def _project_locations() -> dict[str, str]:
    """{job code: "City, ST"} — the customer's address pulled with the Sage
    projects fetch. City-level ground truth for matching flight destinations,
    far sharper than the state embedded in a project name."""
    import re
    from .validate import load_projects
    out: dict[str, str] = {}
    for info in load_projects().values():
        loc = (info.get("location") or "").strip()
        if not loc:
            continue
        for code in re.findall(r"\b\d{3,5}\b", (info.get("name") or "")):
            out.setdefault(code, loc)
    return out


def _history_from_note(note) -> list[str]:
    import re
    m = re.search(r"past projects ([\d, ]+) — pick one", note or "")
    return [c.strip() for c in m.group(1).split(",")] if m else []


def apply(doc, schedule_index=None, hotel_index=None, ramp_index=None,
          registry=None, active_projects=None, history=None, client=None) -> int:
    """Code the eligible lines in place. Returns how many lines Claude
    decided (high/medium); low-confidence lines keep the rules-engine
    coding with Claude's reason and candidates appended. Raises on API
    failure — the caller keeps the rules-engine result."""
    import json

    payload, covered = _evidence(doc, schedule_index, hotel_index, ramp_index,
                                 registry, active_projects, history)
    if not payload["lines"]:
        return 0
    known = {p["code"] for p in payload["projects"]}
    client = client or _client()
    resp = client.messages.parse(
        model=model_name(),
        max_tokens=32000,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        output_config={"effort": os.environ.get("TRAVEL_CODER_EFFORT") or "medium"},
        messages=[{"role": "user", "content": json.dumps(payload)}],
        output_format=TravelCoding,
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("Claude declined to code this statement.")
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        raise RuntimeError("Claude returned no structured coding.")

    covered_set = set(covered)
    ev_by_line = {l["line"]: l for l in payload["lines"]}
    decided = 0
    for c in parsed.lines:
        if c.line not in covered_set:
            continue
        li = doc.line_items[c.line]
        # Stamp the raw proposal on the line (raw round-trips through the run
        # store): at post time record_outcomes() diffs it against the human's
        # final coding — the corrections the next run learns from.
        ev = ev_by_line.get(c.line, {})
        li.raw = dict(li.raw or {})
        li.raw["_claude"] = {
            "project": c.project.strip(), "gl_account": c.gl_account.strip(),
            "department": c.department.strip(), "confidence": c.confidence,
            "route": ev.get("route", ""), "date": ev.get("depart_date", ""),
        }
        project = c.project.strip()
        confidence = c.confidence
        reason = c.reason.strip()
        if project and project not in known:
            # Guardrail: never accept a code outside the active list.
            reason = (reason + f" (suggested {project}, not an active project)").strip()
            project, confidence = "", "low"
        li.needs_review = True
        if confidence in ("high", "medium"):
            if c.gl_account.strip():
                li.gl_account = c.gl_account.strip()
            if c.department.strip():
                li.department = c.department.strip()
            li.project = project or None
            li.note = f"Claude ({confidence}): {reason}"
            decided += 1
        else:
            li.note = (li.note or "") + f"; Claude (low): {reason}"
            cands = [x.strip() for x in c.candidates.split(",")
                     if x.strip() and x.strip() in known]
            if cands:
                li.note += ("; registry: candidate projects "
                            + ", ".join(cands) + " — pick one")
    return decided
