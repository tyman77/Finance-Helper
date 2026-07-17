#!/usr/bin/env python3
"""Build the United traveler map from a historical categorized export.

The historical "United Flights" export carries, for each ticket, the columns
your team actually coded: Person, Department, and Account. Department is stable
per traveler, so we learn it with confidence; Account varies by trip purpose, so
we record the traveler's most-common account only as a *hint* for review.

Usage:
    python scripts/build_traveler_map.py <historical_export.csv> [out.yml]

Default output: data/united_travelers.yml  (gitignored — contains employee names)

Name mismatches (e.g. a nickname on the crew schedule that doesn't match the
legal/booking name — "Diego Munguia" on the sheet vs. "Israel Munguia" in United
history) can be fixed without touching this script: add a line to
data/name_aliases.yml (gitignored), e.g.:

    "MUNGUIA/ISRAELDIEGO": "Diego Munguia"
    "YOCUM/CARSONJAMES": "Carson Yocum"

The key is the United "Passenger Name" as it appears in the CSV; the value is
the exact name to use when looking up the crew schedule / calendar roster.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from collections import Counter, defaultdict

# Placeholder values seen in the historical "Person" column that aren't real
# names (data-entry junk) — treated the same as blank.
_JUNK_PERSON = {"", "customer", "n/a", "na", "tbd", "unknown", "-"}

# Project codes embedded in the historical "Project" column, e.g.
# "Northview Church, IN [3428] Camera Upgrade" or "Little Country, 4173".
_CODE_BRACKET = re.compile(r"\[(\d{3,5})\]")
_CODE_TRAILING = re.compile(r",\s*(\d{3,5})\b")


def _project_code(project: str) -> str | None:
    m = _CODE_BRACKET.search(project) or _CODE_TRAILING.search(project)
    return m.group(1) if m else None


def _guess_person(passenger_name: str) -> str:
    """"JUDY/JOSHUA" -> "Joshua Judy" — best-effort fallback so a traveler with
    no usable historical Person value can still be looked up by name in the
    crew schedule / calendar roster."""
    parts = [p.strip() for p in passenger_name.split("/") if p.strip()]
    if len(parts) < 2:
        return ""
    surname, first = parts[0], parts[1]
    if not re.match(r"^[A-Za-z]+$", surname) or not re.match(r"^[A-Za-z]+$", first):
        return ""
    return f"{first.title()} {surname.title()}"


def _load_aliases() -> dict:
    """Optional manual overrides for United passenger -> schedule/calendar name."""
    data_dir = os.environ.get("FINANCE_HELPER_DATA", "data")
    path = os.path.join(data_dir, "name_aliases.yml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build(path: str) -> dict:
    aliases = _load_aliases()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    agg: dict[str, dict[str, Counter]] = defaultdict(
        lambda: {"person": Counter(), "department": Counter(), "account": Counter(),
                 "project": Counter()}
    )
    for row in rows:
        name = (row.get("Passenger Name") or "").strip()
        if not name:
            continue
        # Skip ancillary-fee rows ("SMITH /SECOND CHECKED BAG"): they inherit the
        # traveler's coding and would pollute the name key.
        if "/" in name and any(w in name.upper() for w in ("BAG", "ZONE", "SEAT")):
            continue
        a = agg[name]
        person_val = row.get("Person", "").strip()
        if person_val.lower() not in _JUNK_PERSON:
            a["person"][person_val] += 1
        if row.get("Department", "").strip() not in ("", "#N/A"):
            a["department"][row["Department"].strip()] += 1
        if row.get("Account", "").strip():
            a["account"][row["Account"].strip()] += 1
        code = _project_code((row.get("Project") or "").strip())
        if code:
            a["project"][code] += 1

    out = {}
    for name, a in sorted(agg.items()):
        n = sum(a["department"].values()) or sum(a["account"].values())
        dept = a["department"].most_common(1)
        acct = a["account"].most_common(1)
        person = a["person"].most_common(1)
        resolved_person = aliases.get(name) or (person[0][0] if person else _guess_person(name))
        out[name] = {
            "person": resolved_person,
            "department": dept[0][0] if dept else "",
            "department_confidence": round(dept[0][1] / sum(a["department"].values()), 3) if dept else 0,
            "account_hint": acct[0][0] if acct else "",
            "account_confidence": round(acct[0][1] / sum(a["account"].values()), 3) if acct else 0,
            # The traveler's own past project codes (most-frequent first) — a
            # fallback suggestion when we have no live schedule/calendar match.
            "projects": [c for c, _ in a["project"].most_common(4)],
            "n": n,
        }
    return out


def dump_yaml(data: dict) -> str:
    # Minimal, dependency-light YAML writer (values are simple scalars).
    lines = ["# Generated by scripts/build_traveler_map.py — do not edit by hand.",
             "# Contains employee names; gitignored by default.", ""]
    for name, v in data.items():
        lines.append(f'"{name}":')
        lines.append(f'  person: "{v["person"]}"')
        lines.append(f'  department: "{v["department"]}"')
        lines.append(f'  department_confidence: {v["department_confidence"]}')
        lines.append(f'  account_hint: "{v["account_hint"]}"')
        lines.append(f'  account_confidence: {v["account_confidence"]}')
        # Quote each code so YAML keeps them strings ("3428"), not ints.
        projects = ", ".join(f'"{c}"' for c in v.get("projects", []))
        lines.append(f'  projects: [{projects}]')
        lines.append(f'  n: {v["n"]}')
    return "\n".join(lines) + "\n"


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    src = argv[0]
    out = argv[1] if len(argv) > 1 else os.path.join(
        os.environ.get("FINANCE_HELPER_DATA", "data"), "united_travelers.yml"
    )
    data = build(src)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(dump_yaml(data))
    print(f"Wrote {len(data)} travelers to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
