"""Bill Check learns from the reviewer's decisions.

Three feedback loops, all driven by what the AP reviewer records on the
bill page and on the Learning page:

1. Prior decisions as context. Every Accept / Not-an-issue disposition is a
   lesson: "this vendor, this field, this reason — and it was fine". The next
   time the same vendor trips the same field, the finding carries the earlier
   decision ("accepted on #1234 by sarah@…: 'they bill from ship date'").
   After PROPOSE_AFTER such decisions the finding drops to *review* and the
   Learning page proposes a vendor policy; confirming it stops the flag for
   good, dismissing it keeps things as they are.

2. Standing notes. A free-text box of house rules ("Yamaha: no QP on warranty
   work") that, together with the prior decisions for the vendor, a short
   Claude pass reads against each remaining critical/high finding. It can
   only lower a finding — clear it when a note or prior decision plainly
   covers the situation, downgrade to review when it probably does — never
   raise one, never invent policy.

3. The rule-based vendor policies in config/recon.yml stay the backbone;
   policies confirmed here are merged over them (stored on the volume, so a
   redeploy doesn't lose them).

Lessons are read from the append-only audit log, which survives a bill being
paid and dropping out of the queue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime

from . import store
from .compare import SEVERITY_ORDER, normalize_vendor, vendor_matches

LESSON_ACTIONS = ("accept", "not_an_issue")      # the entry was right as entered
CONFIRM_ACTIONS = ("fixed",)                      # the finding was right
PROPOSE_AFTER = 2
FIELD_LABELS = {
    "vendor": "vendor name", "invoice": "invoice number", "invoice_date": "invoice date",
    "due_date": "due date", "amount": "total", "discount": "early-pay discount",
    "po": "PO number", "currency": "currency", "document": "document type / read quality",
    "duplicate": "duplicate invoice",
}


def _path(name: str) -> str:
    return os.path.join(store._root(), name)


# --- standing notes ---------------------------------------------------------

def standing_notes() -> str:
    try:
        with open(_path("notes.txt"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def save_standing_notes(text: str) -> None:
    os.makedirs(store._root(), exist_ok=True)
    with open(_path("notes.txt"), "w", encoding="utf-8") as fh:
        fh.write((text or "").strip() + "\n")


# --- lessons from the audit log ---------------------------------------------

def lessons() -> list[dict]:
    """Every disposition ever recorded, oldest first, with the bill context
    it was made against. Rows written before the context was logged are
    filled in from the bill's stored result when it still exists."""
    out: list[dict] = []
    try:
        with open(_path("audit.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return out
    for row in rows:
        if not row.get("vendor"):
            result = store.load_result(row.get("bill_id", "")) or {}
            if not result:
                continue
            row = {**row, **_context_from_result(result)}
        if row.get("action") not in LESSON_ACTIONS + CONFIRM_ACTIONS + ("investigate",):
            continue
        out.append(row)
    return out


def _context_from_result(result: dict) -> dict:
    bill = result.get("bill") or {}
    comp = result.get("comparison") or {}
    findings = list(comp.get("findings") or [])
    return {
        "vendor": bill.get("vendor", ""),
        "invoice": bill.get("invoice", ""),
        "amount": bill.get("amount", ""),
        "due_date": bill.get("due_date", ""),
        "status": result.get("status", ""),
        "severity": result.get("severity", ""),
        "fields": [f.get("field") for f in findings],
        "reasons": [f.get("reason", "")[:200] for f in findings],
    }


def lessons_for(vendor: str, all_lessons: list[dict] | None = None) -> list[dict]:
    if not vendor:
        return []
    return [l for l in (all_lessons if all_lessons is not None else lessons())
            if vendor_matches(vendor, l.get("vendor", "")) and l.get("action") in LESSON_ACTIONS]


def _vendor_key(vendor: str) -> str:
    return " ".join(normalize_vendor(vendor)) or str(vendor or "").strip().lower()


# --- learned policies (confirmed on the Learning page) ----------------------

def _learned() -> dict:
    data = store._read_json(_path("learned_policies.json")) or {}
    data.setdefault("policies", {})
    data.setdefault("dismissed", [])
    return data


def _save_learned(data: dict) -> None:
    store._write_json(_path("learned_policies.json"), data)


def learned_policies() -> dict:
    return _learned()["policies"]


def confirm_proposal(vendor: str, field: str, who: str, note: str = "") -> dict:
    """Stop flagging `field` for `vendor`. Returns the vendor's learned policy."""
    data = _learned()
    key = next((k for k in data["policies"] if vendor_matches(k, vendor)), vendor)
    pol = data["policies"].setdefault(key, {})
    ignored = list(pol.get("ignore_findings") or [])
    if field not in ignored:
        ignored.append(field)
    pol["ignore_findings"] = ignored
    if note:
        pol["notes"] = (pol.get("notes") + "; " if pol.get("notes") else "") + note.strip()
    pol.setdefault("learned", []).append({
        "field": field, "who": who, "when": datetime.now().isoformat(timespec="seconds"),
        "note": note.strip()})
    data["dismissed"] = [d for d in data["dismissed"]
                         if not (vendor_matches(d.get("vendor", ""), vendor) and d.get("field") == field)]
    _save_learned(data)
    return pol


def dismiss_proposal(vendor: str, field: str, who: str) -> None:
    data = _learned()
    data["dismissed"].append({"vendor": vendor, "field": field, "who": who,
                              "when": datetime.now().isoformat(timespec="seconds")})
    _save_learned(data)


def remove_learned(vendor: str, field: str) -> bool:
    data = _learned()
    key = next((k for k in data["policies"] if vendor_matches(k, vendor)), None)
    if key is None:
        return False
    pol = data["policies"][key]
    pol["ignore_findings"] = [f for f in (pol.get("ignore_findings") or []) if f != field]
    pol["learned"] = [l for l in (pol.get("learned") or []) if l.get("field") != field]
    if not pol["ignore_findings"]:
        del data["policies"][key]
    _save_learned(data)
    return True


def merged_policies(config_policies: dict | None) -> dict:
    """config/recon.yml policies with the learned ones laid over them: a
    learned vendor that matches a configured one adds its ignore_findings
    and notes to that entry; a new vendor is added as its own entry."""
    merged = {k: dict(v) for k, v in (config_policies or {}).items() if isinstance(v, dict)}
    for vendor, pol in learned_policies().items():
        key = next((k for k in merged if vendor_matches(k, vendor)), None)
        if key is None:
            merged[vendor] = {"ignore_findings": list(pol.get("ignore_findings") or []),
                              **({"notes": pol["notes"]} if pol.get("notes") else {})}
            continue
        base = merged[key]
        base["ignore_findings"] = sorted(set(base.get("ignore_findings") or [])
                                         | set(pol.get("ignore_findings") or []))
        if pol.get("notes"):
            base["notes"] = (base.get("notes") + "; " if base.get("notes") else "") + pol["notes"]
    return merged


def signature(vendor: str, all_lessons: list[dict] | None = None) -> str:
    """Changes whenever anything this vendor's comparison learns from
    changes — standing notes, its prior decisions, its learned policy — so
    the engine re-compares (from the cached read) instead of trusting a
    stale verdict."""
    prior = lessons_for(vendor, all_lessons)
    key = next((k for k in learned_policies() if vendor_matches(k, vendor)), None)
    pol = learned_policies().get(key) if key else None
    blob = json.dumps({"notes": standing_notes(), "prior": len(prior),
                       "last": (prior[-1].get("when") if prior else ""),
                       "policy": {k: v for k, v in (pol or {}).items() if k != "learned"}},
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# --- applying lessons to a comparison ---------------------------------------

def _recompute(comparison: dict) -> None:
    findings = comparison.get("findings") or []
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    if any(f["severity"] in ("critical", "high") for f in findings):
        comparison["status"] = "mismatch"
    elif findings:
        comparison["status"] = "review"
    else:
        comparison["status"] = "match"
    comparison["severity"] = findings[0]["severity"] if findings else "clear"


def apply_lessons(bill: dict, comparison: dict | None, vendor_lessons: list[dict],
                  policy: dict | None) -> dict | None:
    """Layer prior decisions and the (config + learned) policy over the
    rule-based findings. Mutates and returns `comparison`."""
    if not comparison:
        return comparison
    policy = policy or {}
    ignored = set(policy.get("ignore_findings") or [])
    kept, suppressed = [], list(comparison.get("suppressed") or [])
    context = []
    for f in comparison.get("findings") or []:
        field = f.get("field")
        if field in ignored and field != "duplicate":
            suppressed.append({**f, "why": "vendor policy: this is how this vendor is entered"
                               + (f" — {policy['notes']}" if policy.get("notes") else "")})
            continue
        prior = [l for l in vendor_lessons if field in (l.get("fields") or [])]
        if prior and field != "duplicate":
            notes = []
            for l in reversed(prior):
                n = (l.get("note") or "").strip()
                if n and n not in notes:
                    notes.append(n)
            last = prior[-1]
            who = (last.get("who") or "").split("@")[0]
            f = dict(f)
            f["prior_accepts"] = len(prior)
            f["prior_notes"] = notes[:3]
            tail = (f" Accepted before on #{last.get('invoice', '?')} by {who}"
                    + (f": \"{notes[0]}\"" if notes else "") + ".")
            if len(prior) >= PROPOSE_AFTER and f["severity"] in ("critical", "high"):
                f["severity"] = "review"
                f["learned"] = True
                tail += (f" Flagged {len(prior)} times for this vendor and accepted each "
                         "time — confirm on the Learning page to stop flagging it.")
            f["reason"] = f["reason"].rstrip() + tail
            context.append({"field": field, "count": len(prior), "notes": notes[:3],
                            "last_invoice": last.get("invoice", ""), "who": who})
        kept.append(f)
    comparison["findings"] = kept
    comparison["suppressed"] = suppressed
    comparison["learned_context"] = context
    _recompute(comparison)
    return comparison


# --- proposals & stats for the Learning page --------------------------------

def proposals(all_lessons: list[dict] | None = None, config_policies: dict | None = None) -> list[dict]:
    all_lessons = all_lessons if all_lessons is not None else lessons()
    data = _learned()
    merged = merged_policies(config_policies)
    groups: dict[tuple[str, str], list[dict]] = {}
    for l in all_lessons:
        if l.get("action") not in LESSON_ACTIONS:
            continue
        for field in l.get("fields") or []:
            if field == "duplicate":
                continue
            groups.setdefault((_vendor_key(l.get("vendor", "")), field), []).append(l)
    out = []
    for (vkey, field), rows in groups.items():
        if len(rows) < PROPOSE_AFTER:
            continue
        vendor = Counter(r.get("vendor", "") for r in rows).most_common(1)[0][0]
        pol = next((v for k, v in merged.items() if vendor_matches(k, vendor)), {})
        if field in (pol.get("ignore_findings") or []):
            continue
        if any(vendor_matches(d.get("vendor", ""), vendor) and d.get("field") == field
               for d in data["dismissed"]):
            continue
        notes = []
        for r in reversed(rows):
            n = (r.get("note") or "").strip()
            if n and n not in notes:
                notes.append(n)
        out.append({
            "vendor": vendor, "field": field, "label": FIELD_LABELS.get(field, field),
            "count": len(rows), "notes": notes[:4],
            "who": sorted({(r.get("who") or "").split("@")[0] for r in rows}),
            "last": rows[-1].get("when", ""),
            "invoices": [r.get("invoice", "") for r in rows[-4:]],
        })
    out.sort(key=lambda p: (-p["count"], p["vendor"]))
    return out


def stats(all_lessons: list[dict] | None = None) -> dict:
    all_lessons = all_lessons if all_lessons is not None else lessons()
    by_action = Counter(l.get("action") for l in all_lessons)
    by_who = Counter((l.get("who") or "").split("@")[0] for l in all_lessons)
    by_field = Counter(f for l in all_lessons if l.get("action") in LESSON_ACTIONS
                       for f in (l.get("fields") or []))
    right = sum(by_action[a] for a in CONFIRM_ACTIONS)
    wrong = sum(by_action[a] for a in LESSON_ACTIONS)
    return {
        "total": len(all_lessons),
        "by_action": dict(by_action),
        "by_who": by_who.most_common(),
        "accepted_by_field": [(FIELD_LABELS.get(f, f), n) for f, n in by_field.most_common()],
        "right": right, "wrong": wrong,
        "precision": (round(100 * right / (right + wrong)) if (right + wrong) else None),
        "recent": list(reversed(all_lessons[-25:])),
    }


# --- the Claude pass: notes + prior decisions vs the remaining findings -----

ADJUDICATE_SYSTEM = """You review accounts-payable exception findings for a finance team. A rule engine compared what a clerk entered in Bill.com against the vendor's invoice and produced findings. You are given the team's standing notes and the decisions the reviewer made on this vendor's earlier bills. Decide, per finding, whether those notes and decisions already cover it.

Verdicts:
- "clear": a standing note or a prior decision plainly says this exact situation is correct as entered (same vendor, same kind of difference). Quote it in why.
- "downgrade": the notes or prior decisions probably cover it but not with certainty, or cover a closely related situation. The finding stays visible at review severity.
- "keep": nothing in the notes or decisions speaks to it.

Rules: you may only lower a finding, never raise one. Do not invent policy, infer intent beyond what is written, or treat a single prior decision about a different field as covering this one. A duplicate-invoice finding is never cleared. When in doubt, keep. Keep `why` to one sentence."""


def _adjudicate_enabled() -> bool:
    from .extract import credentials_present
    return credentials_present() and os.environ.get("BILLCHECK_ADJUDICATE", "1") != "0"


def adjudicate(bill: dict, comparison: dict | None, extracted: dict | None,
               notes: str, vendor_lessons: list[dict], client=None) -> dict | None:
    """Run the notes + prior-decisions pass over the critical/high findings.
    Mutates `comparison` (clears/downgrades) and records the verdicts on it.
    Returns the adjudication record, or None when there was nothing to do."""
    if not comparison:
        return None
    targets = [f for f in comparison.get("findings") or []
               if f.get("severity") in ("critical", "high") and f.get("field") != "duplicate"]
    if not targets or not (notes or vendor_lessons):
        return None
    from pydantic import BaseModel
    from typing import List, Literal

    class Verdict(BaseModel):
        field: str
        verdict: Literal["keep", "downgrade", "clear"]
        why: str

    class Verdicts(BaseModel):
        verdicts: List[Verdict]

    ex = extracted or {}
    payload = {
        "vendor": bill.get("vendor"),
        "entered": {k: bill.get(k) for k in ("invoice", "invoice_date", "due_date", "amount", "terms", "po")},
        "invoice_says": {k: ex.get(k) for k in (
            "vendor", "invoice_number", "invoice_date", "due_date", "terms", "total",
            "discount_total", "discount_date", "discount_terms", "ship_date", "notes") if ex.get(k)},
        "findings": [{"field": f["field"], "severity": f["severity"], "reason": f["reason"],
                      "entered": f.get("entered"), "invoice": f.get("pdf")} for f in targets],
        "standing_notes": notes or "(none)",
        "prior_decisions_this_vendor": [
            {"invoice": l.get("invoice"), "when": (l.get("when") or "")[:10],
             "fields_flagged": l.get("fields"), "reasons": l.get("reasons"),
             "decision": l.get("action"), "reviewer_note": l.get("note"),
             "who": (l.get("who") or "").split("@")[0]}
            for l in vendor_lessons[-8:]],
    }
    if client is None:
        from .extract import _client
        client = _client()
    from .extract import model_name
    try:
        resp = client.messages.parse(
            model=os.environ.get("BILLCHECK_ADJUDICATE_MODEL") or model_name(),
            max_tokens=2000,
            system=[{"type": "text", "text": ADJUDICATE_SYSTEM,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            output_format=Verdicts,
        )
    except Exception as exc:
        comparison["adjudication"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return comparison["adjudication"]
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None or getattr(resp, "stop_reason", None) == "refusal":
        comparison["adjudication"] = {"error": "no verdicts returned"}
        return comparison["adjudication"]
    verdicts = {v.field: v for v in parsed.verdicts}
    kept, suppressed = [], list(comparison.get("suppressed") or [])
    applied = []
    for f in comparison.get("findings") or []:
        v = verdicts.get(f.get("field"))
        if v is None or f.get("field") == "duplicate" or f.get("severity") not in ("critical", "high"):
            kept.append(f)
            continue
        if v.verdict == "clear":
            suppressed.append({**f, "why": f"per the reviewer's notes: {v.why}"})
            applied.append({"field": f["field"], "verdict": "clear", "why": v.why})
        elif v.verdict == "downgrade":
            f = {**f, "severity": "review", "adjudicated": True,
                 "reason": f["reason"].rstrip() + f" Likely covered by the reviewer's notes: {v.why}"}
            kept.append(f)
            applied.append({"field": f["field"], "verdict": "downgrade", "why": v.why})
        else:
            kept.append(f)
            applied.append({"field": f["field"], "verdict": "keep", "why": v.why})
    comparison["findings"] = kept
    comparison["suppressed"] = suppressed
    _recompute(comparison)
    usage = getattr(resp, "usage", None)
    record = {"model": getattr(resp, "model", None), "verdicts": applied,
              "usage": {"input": getattr(usage, "input_tokens", None),
                        "output": getattr(usage, "output_tokens", None)}}
    comparison["adjudication"] = record
    return record
