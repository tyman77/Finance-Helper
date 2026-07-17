#!/usr/bin/env python3
"""Build a date->project index from Hotel Engine statement(s).

Hotel Engine billing carries no traveler name, but each booking has Start/End
dates, a Project Name, a Department, and Hotel City. A United flight for the
same trip lands on nearly the same dates, so this index lets the United
enrichment cross-reference a flight's departure date against hotel bookings to
narrow the likely project (see project_resolver.hotel_projects_in_window).

    data/hotel_project_index.json -> [ {start, end, project, department, city}, ... ]

Usage:
    python scripts/build_hotel_index.py <hotel_engine_statement.csv> [more.csv ...]

Merges into any existing index (de-duplicated), so each month can be added as
its statement comes in. Overhead stays (HQ visits, all-staff, etc.) carry no
project and are skipped.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime

from finance_helper import config as fh_config
from finance_helper import project_resolver
from finance_helper.enrich import _HE_DEPARTMENTS, _he_overhead_account

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")


def _parse_date(value):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _department(name):
    dn = (name or "").strip().lower()
    for key, dept_id in _HE_DEPARTMENTS.items():
        if key in dn:
            return dept_id
    return None


def _project_code(pname, hotel_name, registry, active_projects):
    m = re.search(r"\b(\d{3,5})\b", pname)
    if m:
        return m.group(1)
    if registry:
        matched = project_resolver.match_project(
            [{"summary": f"{pname} {hotel_name}", "domains": []}], registry, active_projects)
        if matched and matched.get("project"):
            return matched["project"]
    return None


def build(rows, registry=None, active_projects=None):
    """Hotel-booking records with a resolvable project code."""
    oh_map = fh_config.accounts().get("hotel_engine", {}).get("overhead", {})
    records = []
    for row in rows:
        start = _parse_date(row.get("Start Date"))
        if not start:
            continue
        end = _parse_date(row.get("End Date")) or start
        pname = (row.get("Project Name") or "").strip()
        if _he_overhead_account(pname, oh_map):
            continue  # overhead stay — not project work, nothing to cross-reference
        code = _project_code(pname, row.get("Hotel Name", ""), registry, active_projects)
        if not code:
            continue
        records.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "project": code,
            "department": _department(row.get("Department Name", "")),
            "city": (row.get("Hotel City") or "").strip(),
        })
    return records


def merge(existing, new):
    """Append new records, skipping exact duplicates (same dates/project/city)."""
    def key(r):
        return (r["start"], r["end"], r["project"], r.get("city", ""))

    seen = {key(r) for r in existing}
    out = list(existing)
    for r in new:
        if key(r) not in seen:
            seen.add(key(r))
            out.append(r)
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    registry = project_resolver_registry()
    active = project_resolver.load_active_projects()
    data_dir = os.environ.get("FINANCE_HELPER_DATA", "data")
    dest = os.path.join(data_dir, "hotel_project_index.json")
    existing = []
    if os.path.exists(dest):
        with open(dest, encoding="utf-8") as fh:
            existing = json.load(fh)
    added = 0
    for path in argv:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        recs = build(rows, registry, active)
        merged = merge(existing, recs)
        added += len(merged) - len(existing)
        existing = merged
    os.makedirs(data_dir, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2)
    print(f"Wrote {len(existing)} hotel bookings ({added} new) to {dest}")
    return 0


def project_resolver_registry():
    data_dir = os.environ.get("FINANCE_HELPER_DATA", "data")
    path = os.path.join(data_dir, "project_registry.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
