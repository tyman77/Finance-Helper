#!/usr/bin/env python3
"""Fetch the crew-schedule grid into data/schedule_index.json (installer path).

The planner sheet is a matrix: each row is a person, each column a calendar day,
each cell the project code that person works that day (or a marker like HQ / PTO
/ ✈️). This flattens it to:

    {"<Person Name>": {"YYYY-MM-DD": "<cell>", ...}, ...}

Reads via the Sheets *values* REST API with a service account (google-auth +
requests — proxy-friendly, unlike httplib2). Set:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
    SCHEDULE_SHEET_ID=1Pznz22qs2HuAq9iWFZTtcxqJGzzx2QksEPuwwldJOcE
    SCHEDULE_SHEET_RANGE="'2026'!A1:NZ1008"        # tab + range covering the year
    SCHEDULE_NAME_COL=<n>                            # optional: force the name column

Usage:
    python scripts/fetch_schedule_index.py 2026
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from datetime import date

_MD = re.compile(r"^\d{1,2}/\d{1,2}$")
_NAME_LIKE = re.compile(r"^[A-Za-z][A-Za-z.'-]+ [A-Za-z][A-Za-z.'-]+")  # "First Last"


def _day_row_index(values: list) -> int:
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
    best, best_col = -1, 0
    for col in range(first_day_col):
        score = sum(1 for r in rows if col < len(r) and _NAME_LIKE.match((r[col] or "").strip()))
        if score > best:
            best, best_col = score, col
    return best_col


def parse_grid(values: list, year: int, name_col: int | None = None) -> dict:
    """Flatten the crew grid to {person: {ISO date: cell}} (auto-detects layout)."""
    if not values:
        return {}
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
        days = {iso: row[col].strip()
                for col, iso in day_cols.items()
                if col < len(row) and (row[col] or "").strip()}
        if days:
            index[person] = days
    return index


def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    sess = AuthorizedSession(creds)
    ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca:
        sess.verify = ca
    return sess


def fetch(sheet_id: str, rng: str) -> list:
    sess = _session()
    q = urllib.parse.quote(rng)
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{q}"
           "?valueRenderOption=FORMATTED_VALUE")
    resp = sess.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json().get("values", [])


def main(argv):
    year = int(argv[0]) if argv else date(2026, 1, 1).year
    values = fetch(os.environ["SCHEDULE_SHEET_ID"],
                   os.environ.get("SCHEDULE_SHEET_RANGE", "'2026'!A1:NZ1008"))
    name_col = os.environ.get("SCHEDULE_NAME_COL")
    index = parse_grid(values, year, int(name_col) if name_col else None)
    os.makedirs("data", exist_ok=True)
    with open("data/schedule_index.json", "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    print(f"Wrote {len(index)} people to data/schedule_index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
