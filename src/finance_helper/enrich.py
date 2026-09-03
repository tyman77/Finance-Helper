"""Traveler enrichment for United, learned from historical coding.

For each United line we look up the traveler in the map produced by
scripts/build_traveler_map.py and assign:

  - person + department   -> high confidence (department is stable per traveler)
  - gl_account (hint)     -> the traveler's most-common account, but ALWAYS
                             flagged for review because the real account depends
                             on the trip's project/purpose, which isn't in the
                             UATP feed.

If the map is missing or the traveler is unknown, the line is flagged for review
with a note so nothing is silently mis-coded.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime

import yaml

from . import config, project_resolver
from .models import SourceDocument

def _data_dir() -> str:
    # Read the env at call time (not import time) so FINANCE_HELPER_DATA set
    # after import — e.g. per-test monkeypatching — is honored.
    return os.environ.get(
        "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data")
    )


_DEPT_CONF_MIN = 0.9   # department is trusted above this

# Distinguishes "caller didn't pass active_projects" (load from disk) from
# "caller explicitly passed None" (skip filtering) — plain None can't do both,
# and tests need the latter to stay isolated from whatever's on disk.
_UNSET = object()


def load_traveler_map(path: str) -> dict:
    """Not cached: the web UI is a long-running process, and re-reading this
    small file each time means a freshly (re)generated map is picked up
    immediately, without requiring a server restart."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_json(name: str):
    path = os.path.join(_data_dir(), name)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")


def _parse_date(value) -> date | None:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _surname(name: str) -> str:
    return name.split("/")[0].strip().upper()


def _lookup(name: str, tmap: dict, surname_index: dict):
    key = name.strip().upper()
    # Map keys are stored as-is (upper for names like CODY/JACOBLEE).
    for k, v in tmap.items():
        if k.upper() == key:
            return v, True  # exact
    entries = surname_index.get(_surname(name))
    if entries and len(entries) == 1:
        return entries[0], False  # unambiguous surname match
    if entries:
        # Multiple people share the surname — pick the busiest but flag it.
        return max(entries, key=lambda e: e.get("n", 0)), False
    return None, False


def enrich_united(
    doc: SourceDocument,
    tmap: dict | None = None,
    schedule_index: dict | None = None,
    calendar_index: dict | None = None,
    roster: dict | None = None,
    registry: dict | None = None,
    active_projects=_UNSET,
    hotel_index: list | None = None,
    ramp_index: list | None = None,
    timecard_index: dict | None = None,
) -> SourceDocument:
    if tmap is None:
        tmap = load_traveler_map(os.path.join(_data_dir(), "united_travelers.yml"))
    if schedule_index is None:
        schedule_index = _load_json("schedule_index.json") or {}
    if calendar_index is None:
        calendar_index = _load_json("calendar_index.json") or {}
    if roster is None:
        roster = _load_json("roster.json") or {}
    if registry is None:
        registry = _load_json("project_registry.json") or {}
    if active_projects is _UNSET:
        active_projects = project_resolver.load_active_projects()
    if hotel_index is None:
        hotel_index = _load_json("hotel_project_index.json") or []
    if ramp_index is None:
        ramp_index = _load_json("ramp_reimbursements.json") or []
    if timecard_index is None:
        timecard_index = _load_json("timecards_index.json") or {}

    surname_index: dict[str, list] = {}
    for k, v in tmap.items():
        surname_index.setdefault(_surname(k), []).append(v)

    for li in doc.line_items:
        # Prefer the original Passenger Name column; fall back to the description.
        passenger = (li.raw.get("Passenger Name") or li.description).strip()
        upper = passenger.upper()
        if "WI-FI" in upper or "WIFI" in upper:
            # Inflight wifi purchases carry no traveler by nature. Per policy
            # these are accepted as-is (default travel account, no review) —
            # they stay in the entry so the statement still ties out, but they
            # never demand human attention.
            li.needs_review = False
            li.note = "inflight wifi purchase — auto-accepted, no traveler"
            continue
        entry, exact = _lookup(passenger, tmap, surname_index)
        if not entry:
            li.needs_review = True
            li.note = "traveler not found in history — assign department & account"
            continue

        li.person = entry.get("person") or None
        dept = entry.get("department") or None
        # Store the bare department code ("60--Install Team" -> "60") so it
        # matches the code-keyed dropdown in the web UI and the department id
        # the Sage/Bill.com payloads expect.
        li.department = dept.split("--")[0].strip() if dept else None
        if not (dept and entry.get("department_confidence", 0) >= _DEPT_CONF_MIN):
            li.needs_review = True
            li.note = "low-confidence department"

        # 1) Installers: pin project + 52200 COGS from the crew schedule.
        resolved = _resolve_project(li, entry, passenger, schedule_index, calendar_index, roster,
                                    registry, active_projects, timecard_index)
        if resolved and resolved.get("account"):
            li.gl_account = resolved["account"]
            if resolved.get("project"):
                li.project = resolved["project"]
            li.needs_review = True  # reviewed to start
            li.note = (li.note + "; " if li.note else "") + resolved["note"]
            if not li.project:
                _destination_narrow(li, registry, active_projects)
            continue

        # 2) Everyone else falls back to the traveler's usual account, with any
        #    calendar trip context attached for the reviewer.
        context = resolved.get("note") if resolved else None
        hint = entry.get("account_hint") or ""
        if hint:
            li.gl_account = hint.split("--")[0].strip()  # "52200--COGS..." -> "52200"
        li.needs_review = True
        conf = entry.get("account_confidence", 0)
        li.note = (li.note + "; " if li.note else "") + (
            f"account hint {hint!r} (used {int(conf * 100)}% of trips) — confirm project/COGS"
        )
        if not exact:
            li.note += "; matched by surname only"
        if context:
            li.note += "; " + context
        # No live schedule/calendar project match: fall back to the traveler's
        # own past project codes, and — the strongest no-Google signal we have —
        # cross-reference Hotel Engine bookings on the same dates to narrow them.
        if not li.project:
            _fallback_project(li, entry, hotel_index, ramp_index, active_projects)
        # Last cross-check: the flight's destination state vs. the state in
        # project names — narrows candidate lists, and surfaces the projects
        # in that state when history offers nothing at all.
        if not li.project:
            _destination_narrow(li, registry, active_projects)

    return doc


_PICK_RE = re.compile(
    r"(?:registry: candidate projects|calendar title codes|past projects"
    r"|hotel-week projects|hotel-stay projects|ramp-memo projects)"
    r" ([\d, ]+) — pick one")


def _destination_narrow(li, registry, active_projects) -> None:
    """Match the flight's destination state against the ", XX" in project
    names. A unique hit among the already-suggested candidates auto-fills the
    project; several hits reorder the pick; with no candidates at all, the
    active projects in that state become the suggestion."""
    routing = next((str(v) for k, v in (li.raw or {}).items()
                    if k.lower().startswith("routing")), "") or li.description or ""
    states = project_resolver.route_states(routing)
    if not states:
        return
    matches = project_resolver.projects_in_states(registry or {}, states, active_projects)
    if not matches:
        return
    label = "/".join(states)
    m = _PICK_RE.search(li.note or "")
    if m:
        codes = [c.strip() for c in m.group(1).split(",") if c.strip()]
        sp = [c for c in codes if c in matches]
        if len(sp) == 1:
            li.project = sp[0]
            li.gl_account = "52200"
            li.note += (f"; flight lands in {label} -> project {sp[0]} "
                        f"({matches[sp[0]]}) (confirm)")
        elif sp:
            li.note += ("; destination matches projects "
                        + ", ".join(sp) + " — pick one")
        return
    if len(matches) == 1:
        code, client = next(iter(matches.items()))
        li.project = code
        li.gl_account = "52200"
        li.note += f"; flight lands in {label} -> project {code} ({client}) (confirm)"
    elif len(matches) <= 4:
        li.note += (f"; flight lands in {label} — projects there: "
                    + ", ".join(sorted(matches)) + " — pick one")


def _fallback_project(li, entry, hotel_index, ramp_index, active_projects) -> None:
    """Suggest a project without live schedule/calendar data.

    Strongest first: a hotel stay that NAMES this traveler (statement guest
    columns) and overlaps the flight auto-fills its project. Then the
    date+department cross-reference narrows the traveler's historical codes;
    then history alone.
    """
    past = _active_history_projects(entry, active_projects)
    dep = _parse_date(li.raw.get("Departure Date"))

    if hotel_index and dep and li.person:
        mine = project_resolver.hotel_projects_for_person(
            hotel_index, li.person, dep, active_projects=active_projects)
        if len(mine) == 1:
            li.project = mine[0]
            li.gl_account = "52200"     # project work -> COGS Travel
            li.note += (f"; hotel stay names {li.person} that week -> "
                        f"project {mine[0]} + 52200 COGS (confirm)")
            return
        if mine:
            li.note += "; hotel-stay projects " + ", ".join(mine) + " — pick one"
            return
        # Nothing matched — say which link of the chain broke, so a quiet
        # result is diagnosable from the line itself instead of a mystery.
        overlapping = project_resolver.person_stays_in_window(hotel_index, li.person, dep)
        if overlapping:
            coded = [b["project"] for b in overlapping if b.get("project")]
            where = overlapping[0].get("hotel") or overlapping[0].get("city") or "a hotel"
            if coded:
                li.note += (f"; hotel stay that week is project {coded[0]}, "
                            "which is archived in Sage — confirm by hand")
            else:
                li.note += (f"; {li.person} stayed at {where} that week but the booking "
                            "carries no project number — add it in Hotel Engine")
        elif project_resolver.person_has_any_stay(hotel_index, li.person):
            li.note += (f"; {li.person} has hotel stays on other dates — "
                        "none overlap this flight")
        elif not any(b.get("guests") for b in hotel_index):
            li.note += "; hotel index has no guest names — check the HE statement's guest column"
    elif not hotel_index and dep and li.person:
        li.note += "; no hotel stays indexed yet (upload Hotel Engine statements)"

    # Next rung: Ramp per-diem reimbursements. A memo with a project number
    # tags the flight; a code-less payout still corroborates the trip.
    if ramp_index and dep and li.person and not li.project:
        codes, hits = project_resolver.ramp_matches_for_person(
            ramp_index, li.person, dep, active_projects=active_projects)
        if len(codes) == 1:
            li.project = codes[0]
            li.gl_account = "52200"
            li.note += (f"; ramp per-diem memo -> project {codes[0]} "
                        "+ 52200 COGS (confirm)")
            return
        if codes:
            li.note += "; ramp-memo projects " + ", ".join(codes) + " — pick one"
            return
        if hits:
            li.note += (f"; per diem paid to {li.person} on {hits[0]['date']}"
                        " — trip corroborated, but no project in the memo")
    hotel = (
        project_resolver.hotel_projects_in_window(
            hotel_index, dep, department=li.department, active_projects=active_projects
        )
        if (hotel_index and dep)
        else []
    )
    narrowed = [c for c in past if c in hotel]
    if len(narrowed) == 1:
        li.project = narrowed[0]
        li.note += f"; hotel booking that week -> project {narrowed[0]} (confirm)"
    elif narrowed:
        li.note += "; past projects " + ", ".join(narrowed) + " — pick one"
    elif past:
        li.note += "; past projects " + ", ".join(past) + " — pick one"
    elif hotel:
        # No history for this traveler, but hotels booked that week give options.
        li.note += "; hotel-week projects " + ", ".join(hotel) + " — pick one"


def _active_history_projects(entry: dict, active_projects) -> list[str]:
    """The traveler's historical project codes, most-frequent first, dropping
    any archived in Sage. Order is preserved (unlike a set intersection)."""
    codes = entry.get("projects") or []
    if active_projects is None:
        return list(codes)
    return [c for c in codes if c in active_projects]


_HE_DEPARTMENTS = {
    "sales": "10", "marketing": "20", "architect": "30",
    "project manager": "35", "engineering": "40", "assembly": "50",
    "install": "60", "finance": "70", "lead": "80",
}


def _he_overhead_account(project_name: str, oh: dict) -> str | None:
    """Overhead account (by Project Name) when the stay isn't project work."""
    t = project_name.lower()
    if "hire" in t or "onboard" in t:
        return oh.get("hiring")                                  # OH - Hiring/Recruiting
    if "hq" in t or any(w in t for w in ("all staff", "syncup", "sync up", "bbq")):
        return oh.get("hq")                                      # OH - Travel
    if "not project" in t or "tour" in t or "intro" in t:
        return oh.get("oh_sales_not_project")
    if "oh sales" in t or "discovery" in t:
        return oh.get("oh_sales_new")
    if any(w in t for w in ("event", "conference", "training", "thrive", "storybrand")):
        return oh.get("events")
    return None


def enrich_hotel_engine(
    doc: SourceDocument, registry: dict | None = None, active_projects=_UNSET
) -> SourceDocument:
    """Code each Hotel Engine booking to ONE trip account + set dimensions.

    Their chart has no hotel tax/incidental sub-accounts, so the whole booking
    (every component line) codes to a single account: COGS Travel: Hotel for a
    client project, otherwise an overhead account chosen from the Project Name.
    Department and Project are set as Intacct dimensions. Every line is
    review-flagged to start.
    """
    if registry is None:
        registry = _load_json("project_registry.json") or {}
    if active_projects is _UNSET:
        active_projects = project_resolver.load_active_projects()
    he_cfg = config.accounts().get("hotel_engine", {})
    cogs_hotel = he_cfg.get("cogs_travel_hotel", "COGS-TRAVEL-HOTEL")
    oh_map = he_cfg.get("overhead", {})

    for li in doc.line_items:
        raw = li.raw
        dn = (raw.get("Department Name") or "").strip().lower()
        for key, dept_id in _HE_DEPARTMENTS.items():
            if key in dn:
                li.department = dept_id
                break

        pname = (raw.get("Project Name") or "").strip()
        oh = _he_overhead_account(pname, oh_map)
        code = None
        if oh:
            # Overhead trips (HQ visit, discovery, all-staff...) aren't project work.
            li.gl_account = oh
        else:
            m = re.search(r"\b(\d{3,5})\b", pname)
            if m:
                code = m.group(1)
            elif registry:
                matched = project_resolver.match_project(
                    [{"summary": f"{pname} {raw.get('Hotel Name', '')}", "domains": []}],
                    registry, active_projects)
                if matched and matched.get("project"):
                    code = matched["project"]
            if code:
                li.project = code
                li.gl_account = cogs_hotel     # whole project stay -> COGS Travel: Hotel
            # else: no project/overhead signal -> leave for the reviewer.

        li.needs_review = True
        li.note = (f"HE {pname!r}"
                   + (f" -> project {code} (COGS Travel: Hotel)" if code else "")
                   + (f"; overhead {oh}" if oh else ""))

    return doc


def enrich_ups(doc: SourceDocument, registry: dict | None = None, active_projects=_UNSET) -> SourceDocument:
    """Code each UPS shipment: project -> COGS Shipping, else overhead postage.

    Project code comes from Reference No.1, else inherited from another line with
    the same Tracking Number (correction rows), else a registry match on the
    receiver (client) name. Non-project shipments go to overhead postage, with a
    department inferred from the reference (marketing/sales) where possible.
    """
    if registry is None:
        registry = _load_json("project_registry.json") or {}
    if active_projects is _UNSET:
        active_projects = project_resolver.load_active_projects()
    cfg = config.accounts().get("ups", {})
    cogs = cfg.get("cogs_shipping", "51700")
    oh = cfg.get("overhead_shipping", "65565")
    dept_kw = cfg.get("overhead_departments", {})

    # Pass 1: learn, from this invoice's coded rows, which project each tracking
    # number and each receiver (church/campus) maps to.
    def _norm(text):
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    tracking_code = {}
    receiver_codes: dict[str, set] = {}
    for li in doc.line_items:
        ref = (li.raw.get("Reference No.1") or "").strip()
        if not re.fullmatch(r"\d{3,5}", ref):
            continue
        trk = (li.raw.get("Tracking Number") or "").strip()
        if trk:
            tracking_code.setdefault(trk, ref)
        rk = _norm(li.raw.get("Receiver Company Name", ""))
        if len(rk) >= 4:
            receiver_codes.setdefault(rk, set()).add(ref)

    for li in doc.line_items:
        raw = li.raw
        ref = (raw.get("Reference No.1") or "").strip()
        trk = (raw.get("Tracking Number") or "").strip()
        rk = _norm(raw.get("Receiver Company Name", ""))
        code = src = None
        if re.fullmatch(r"\d{3,5}", ref):
            code, src = ref, "reference"
        elif trk and trk in tracking_code:
            code, src = tracking_code[trk], "same tracking #"
        elif rk in receiver_codes and len(receiver_codes[rk]) == 1:
            code, src = next(iter(receiver_codes[rk])), "receiver in this invoice"
        elif registry:
            matched = project_resolver.match_project(
                [{"summary": f"{raw.get('Receiver Company Name', '')} {ref}", "domains": []}],
                registry, active_projects)
            if matched and matched.get("project"):
                code, src = matched["project"], "registry guess — verify"

        if code:
            li.project = code
            li.gl_account = cogs
            li.note = f"UPS -> project {code} via {src} (COGS Shipping)"
        else:
            li.gl_account = oh
            for kw, dept_id in dept_kw.items():
                if kw in ref.lower():
                    li.department = dept_id
                    break
            li.note = (f"UPS overhead ref {ref!r} -> {oh}"
                       + ("" if li.department else " (needs department)"))
        li.needs_review = True

    return doc


def _resolve_project(li, entry, passenger, schedule_index, calendar_index, roster,
                     registry, active_projects, timecard_index=None):
    dep = _parse_date(li.raw.get("Departure Date"))
    if dep is None:
        return None
    person = entry.get("person", "")
    # Timecards first — logged labor is the ground truth for who worked which
    # project on which days, for every department, not just installers.
    if timecard_index:
        rows = timecard_index.get(person)
        if rows is None:
            rows = next((v for k, v in timecard_index.items()
                         if project_resolver.same_person(person, k)), None)
        if rows:
            result = project_resolver.resolve_schedule(
                "_", dep, {"_": rows}, active_projects)
            if result:
                code = result["project"]
                return {"project": code, "account": "52200", "source": "timecards",
                        "note": f"timecards: {person} logged hours to project {code} "
                                "during the stay -> 52200 COGS"}
    dept = (entry.get("department") or "")
    # Installers are on the crew schedule; try it first, but fall through to
    # their calendar if they're not on the sheet (e.g. not current crew) or the
    # sheet has no code for that stay — don't give up just because dept == 60.
    if dept.startswith("60") and schedule_index:
        result = project_resolver.resolve_schedule(entry.get("person", ""), dep, schedule_index, active_projects)
        if result:
            return result
    if calendar_index:
        owner = roster.get(entry.get("person", "")) or project_resolver.email_for(passenger)
        if owner:
            return project_resolver.resolve_calendar(owner, dep, calendar_index, registry, active_projects)
    return None
