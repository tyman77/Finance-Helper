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
import sys
from datetime import date


def _day_headers(month_row, day_row, year: int) -> dict:
    """Map each column index -> ISO date using the 'month' and 'M/D' header rows."""
    cols = {}
    for i, cell in enumerate(day_row):
        cell = (cell or "").strip()
        if "/" in cell:
            try:
                m, d = cell.split("/")
                cols[i] = date(year, int(m), int(d)).isoformat()
            except (ValueError, TypeError):
                continue
    return cols


def parse_grid(values: list, year: int, name_col: int = 1, day_header_row: int = 2) -> dict:
    """values = raw sheet rows. name_col holds the person; cells hold project codes."""
    day_cols = _day_headers(values[day_header_row - 1], values[day_header_row], year)
    index: dict[str, dict] = {}
    for row in values[day_header_row + 1:]:
        if len(row) <= name_col:
            continue
        person = (row[name_col] or "").strip()
        if not person:
            continue
        days = {}
        for col, iso in day_cols.items():
            if col < len(row):
                val = (row[col] or "").strip()
                if val:
                    days[iso] = val
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
    index = parse_grid(values, year)
    os.makedirs("data", exist_ok=True)
    with open("data/schedule_index.json", "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    print(f"Wrote {len(index)} people to data/schedule_index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
