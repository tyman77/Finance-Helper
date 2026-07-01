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
from functools import lru_cache

import yaml

from . import project_resolver
from .models import SourceDocument

_DATA_DIR = os.environ.get(
    "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data")
)
_DEPT_CONF_MIN = 0.9   # department is trusted above this


@lru_cache(maxsize=4)
def load_traveler_map(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_json(name: str):
    path = os.path.join(_DATA_DIR, name)
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
) -> SourceDocument:
    if tmap is None:
        tmap = load_traveler_map(os.path.join(_DATA_DIR, "united_travelers.yml"))
    if schedule_index is None:
        schedule_index = _load_json("schedule_index.json") or {}
    if calendar_index is None:
        calendar_index = _load_json("calendar_index.json") or {}
    if roster is None:
        roster = _load_json("roster.json") or {}
    if registry is None:
        registry = _load_json("project_registry.json") or {}

    surname_index: dict[str, list] = {}
    for k, v in tmap.items():
        surname_index.setdefault(_surname(k), []).append(v)

    for li in doc.line_items:
        # Prefer the original Passenger Name column; fall back to the description.
        passenger = (li.raw.get("Passenger Name") or li.description).strip()
        entry, exact = _lookup(passenger, tmap, surname_index)
        if not entry:
            li.needs_review = True
            li.note = "traveler not found in history — assign department & account"
            continue

        li.person = entry.get("person") or None
        dept = entry.get("department") or None
        li.department = dept
        if not (dept and entry.get("department_confidence", 0) >= _DEPT_CONF_MIN):
            li.needs_review = True
            li.note = "low-confidence department"

        # 1) Installers: pin project + 52200 COGS from the crew schedule.
        resolved = _resolve_project(li, entry, passenger, schedule_index, calendar_index, roster, registry)
        if resolved and resolved.get("account"):
            li.gl_account = resolved["account"]
            if resolved.get("project"):
                li.project = resolved["project"]
            li.needs_review = True  # reviewed to start
            li.note = (li.note + "; " if li.note else "") + resolved["note"]
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

    return doc


_HE_DEPARTMENTS = {
    "sales": "10", "marketing": "20", "architect": "30",
    "project manager": "35", "engineering": "40", "assembly": "50",
    "install": "60", "finance": "70", "lead": "80",
}


def _he_overhead_account(project_name: str) -> str | None:
    """Overhead account when the Project Name isn't a client project."""
    t = project_name.lower()
    if "hq" in t:
        return "71000"                       # OH - Travel
    if "oh sales" in t or "discovery" in t:
        return "64302"                       # OH Sales: New Client
    if "hire" in t or "onboard" in t:
        return "63100"                       # OH - Hiring/Recruiting
    if any(w in t for w in ("all staff", "syncup", "sync up", "bbq", "event")):
        return "64000"                       # OH - Personal Development
    return None


def enrich_hotel_engine(doc: SourceDocument, registry: dict | None = None) -> SourceDocument:
    """Set Department + Project dimensions from the booking's own columns.

    Hotel Engine already carries Department Name and a Project Name that usually
    holds the code; uncoded-but-named rows fall back to the client->project
    registry. Overhead Project Names (HQ Visit, OH Sales, All Staff...) set an
    overhead account. Every line is review-flagged to start.
    """
    if registry is None:
        registry = _load_json("project_registry.json") or {}

    for li in doc.line_items:
        raw = li.raw
        dn = (raw.get("Department Name") or "").strip().lower()
        for key, dept_id in _HE_DEPARTMENTS.items():
            if key in dn:
                li.department = dept_id
                break

        pname = (raw.get("Project Name") or "").strip()
        oh = _he_overhead_account(pname)
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
                    [{"summary": f"{pname} {raw.get('Hotel Name', '')}", "domains": []}], registry)
                if matched and matched.get("project"):
                    code = matched["project"]
            if code:
                li.project = code

        li.needs_review = True
        li.note = (f"HE {pname!r}" + (f" -> project {code}" if code else "")
                   + (f"; overhead {oh}" if oh else ""))

    return doc


def _resolve_project(li, entry, passenger, schedule_index, calendar_index, roster, registry):
    dep = _parse_date(li.raw.get("Departure Date"))
    if dep is None:
        return None
    dept = (entry.get("department") or "")
    # Installers are on the crew schedule; everyone else on their own calendar.
    if dept.startswith("60") and schedule_index:
        return project_resolver.resolve_schedule(entry.get("person", ""), dep, schedule_index)
    if calendar_index:
        owner = roster.get(entry.get("person", "")) or project_resolver.email_for(passenger)
        if owner:
            return project_resolver.resolve_calendar(owner, dep, calendar_index, registry)
    return None
