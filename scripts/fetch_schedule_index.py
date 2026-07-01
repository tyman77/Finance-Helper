#!/usr/bin/env python3
"""Fetch the crew-schedule grid into data/schedule_index.json (installer path).

The planner sheet is a matrix: each row is a person, each column a calendar day,
and each cell holds the project code that person works that day (or a marker like
HQ / PTO / REST / ✈️). This flattens it to:

    {"<Person Name>": {"YYYY-MM-DD": "<cell>", ...}, ...}

which project_resolver.resolve_schedule() reads to pick the project worked during
a trip.

Requires a Google service account with read access to the sheet. Set:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
    SCHEDULE_SHEET_ID=1Pznz22qs2HuAq9iWFZTtcxqJGzzx2QksEPuwwldJOcE
    SCHEDULE_SHEET_RANGE='Installers!A1:NZ400'   # tab + range covering the year

Usage:
    python scripts/fetch_schedule_index.py 2026

Reading via the Sheets *values* API (not the Drive text export) is important:
the text export truncates and mangles the emoji markers. The values API returns
the raw grid so the day-header row and each person row parse cleanly.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date

_MD = re.compile(r"^\d{1,2}/\d{1,2}$")
_NAME_LIKE = re.compile(r"^[A-Za-z][A-Za-z.'-]+ [A-Za-z][A-Za-z.'-]+")  # "First Last"


def _day_row_index(values: list) -> int:
    """The header row is the one with the most 'M/D' cells."""
    best, best_i = -1, 0
    for i, row in enumerate(values):
        count = sum(1 for c in row if _MD.match((c or "").strip()))
        if count > best:
            best, best_i = count, i
    return best_i


def _day_headers(day_row, year: int) -> dict:
    cols = {}
    for i, cell in enumerate(day_row):
        cell = (cell or "").strip()
        if _MD.match(cell):
            m, d = cell.split("/")
            try:
                cols[i] = date(year, int(m), int(d)).isoformat()
            except ValueError:
                continue
    return cols


def _detect_name_col(rows, first_day_col: int) -> int:
    """Among the leading (pre-day) columns, pick the one that most looks like names."""
    best, best_col = -1, 0
    for col in range(first_day_col):
        score = sum(1 for r in rows if col < len(r) and _NAME_LIKE.match((r[col] or "").strip()))
        if score > best:
            best, best_col = score, col
    return best_col


def parse_grid(values: list, year: int, name_col: int | None = None) -> dict:
    """Flatten the crew grid to {person: {ISO date: cell}}.

    Auto-detects the day-header row and (unless overridden) the name column, so it
    tolerates tabs with different numbers of leading columns.
    """
    hdr = _day_row_index(values)
    day_cols = _day_headers(values[hdr], year)
    if not day_cols:
        return {}
    first_day_col = min(day_cols)
    data_rows = values[hdr + 1:]
    if name_col is None:
        name_col = _detect_name_col(data_rows, first_day_col)

    index: dict[str, dict] = {}
    for row in data_rows:
        if len(row) <= name_col:
            continue
        person = (row[name_col] or "").strip()
        if not person or _NAME_LIKE.match(person) is None:
            continue
        days = {col_iso: row[col].strip()
                for col, col_iso in day_cols.items()
                if col < len(row) and (row[col] or "").strip()}
        if days:
            index[person] = days
    return index


def fetch(sheet_id: str, rng: str) -> list:
    from google.oauth2 import service_account            # type: ignore
    from googleapiclient.discovery import build          # type: ignore

    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    svc = build("sheets", "v4", credentials=creds)
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    return resp.get("values", [])


def main(argv):
    year = int(argv[0]) if argv else date.today().year  # noqa: DTZ011 (cron passes year)
    values = fetch(os.environ["SCHEDULE_SHEET_ID"], os.environ.get("SCHEDULE_SHEET_RANGE", ""))
    name_col = os.environ.get("SCHEDULE_NAME_COL")
    index = parse_grid(values, year, int(name_col) if name_col else None)
    os.makedirs("data", exist_ok=True)
    with open("data/schedule_index.json", "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    print(f"Wrote {len(index)} people to data/schedule_index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
