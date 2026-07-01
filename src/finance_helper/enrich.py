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

import os
from functools import lru_cache

import yaml

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


def enrich_united(doc: SourceDocument, tmap: dict | None = None) -> SourceDocument:
    if tmap is None:
        tmap = load_traveler_map(os.path.join(_DATA_DIR, "united_travelers.yml"))

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
        if dept and entry.get("department_confidence", 0) >= _DEPT_CONF_MIN:
            li.department = dept
        else:
            li.department = dept
            li.needs_review = True
            li.note = "low-confidence department"

        # Account is a hint only — the reviewer confirms COGS-vs-overhead / project.
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

    return doc
