#!/usr/bin/env python3
"""Fetch the crew-schedule grid into data/schedule_index.json (installer path).

The planner sheet is a matrix: each row is a person, each column a calendar day,
each cell the project code that person works that day (or a marker like HQ / PTO
/ ✈️). This flattens it to:

    {"<Person Name>": {"YYYY-MM-DD": "<cell>", ...}, ...}

Reads via the Sheets *values* REST API with a service account (google-auth +
requests — proxy-friendly, unlike httplib2), via finance_helper.google_auth
(GOOGLE_APPLICATION_CREDENTIALS=path to a key file, or GOOGLE_SERVICE_ACCOUNT_JSON
= the key content, for hosts with no local file to point at). Also set:
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


def fetch(sheet_id: str, rng: str) -> list:
    from finance_helper import google_auth

    sess = google_auth.session(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    q = urllib.parse.quote(rng)
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{q}"
           "?valueRenderOption=FORMATTED_VALUE")
    resp = sess.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json().get("values", [])


def fetch_public_csv(sheet_id: str) -> list:
    """No-credentials path: a sheet shared as 'Anyone with the link — Viewer'
    serves a CSV export without any Google Cloud setup. SCHEDULE_SHEET_GID
    picks the tab — the number after '#gid=' in the tab's URL (0 = first)."""
    import csv
    import io

    import requests

    gid = os.environ.get("SCHEDULE_SHEET_GID", "0")
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
           f"?format=csv&gid={gid}")
    resp = requests.get(url, timeout=60)
    if resp.status_code == 404:
        raise RuntimeError(
            "Google says no such spreadsheet (HTTP 404). Usually this means "
            f"SCHEDULE_SHEET_ID doesn't match the sheet (using '{sheet_id}' — "
            "compare it to the long code between /d/ and /edit in the sheet's "
            "URL), or the file is an uploaded Excel file rather than a native "
            "Google Sheet (an .XLSX badge shows next to its name — use "
            "File > Save as Google Sheets and point SCHEDULE_SHEET_ID at the "
            "new file).")
    if resp.status_code != 200 or \
            "text/html" in (resp.headers.get("Content-Type") or ""):
        raise RuntimeError(
            f"The sheet isn't readable without credentials (HTTP {resp.status_code}). "
            "Two ways to fix: share the sheet as 'Anyone with the link — Viewer' "
            "(no key needed), or set GOOGLE_SERVICE_ACCOUNT_JSON in Railway for "
            "private access. If the schedule isn't on the sheet's FIRST tab, also "
            "set SCHEDULE_SHEET_GID to the number after #gid= in the tab's URL.")
    text = resp.content.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def normalize_sheet_id(raw: str) -> str:
    """Accept either the bare spreadsheet ID or a full docs.google.com URL
    (SCHEDULE_SHEET_ID is often pasted straight from the address bar)."""
    raw = (raw or "").strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", raw)
    return m.group(1) if m else raw


def fetch_values(sheet_id: str, rng: str) -> list:
    """Service-account API when a key is configured, but never dead-end on
    it: if the API path fails (sheet not shared with the service account,
    Sheets API not enabled), the public CSV export is tried before giving
    up — whichever path works, works."""
    sheet_id = normalize_sheet_id(sheet_id)
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or \
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            return fetch(sheet_id, rng)
        except Exception as api_err:
            try:
                return fetch_public_csv(sheet_id)
            except Exception as csv_err:
                raise RuntimeError(
                    f"Sheets API failed ({api_err}) — likely the sheet isn't "
                    "shared with the service account's email, or the Google "
                    "Sheets API isn't enabled in its project. The public CSV "
                    f"fallback failed too ({csv_err}).") from api_err
    return fetch_public_csv(sheet_id)


def main(argv):
    year = int(argv[0]) if argv else date(2026, 1, 1).year
    values = fetch_values(os.environ["SCHEDULE_SHEET_ID"],
                          os.environ.get("SCHEDULE_SHEET_RANGE", "'2026'!A1:NZ1008"))
    name_col = os.environ.get("SCHEDULE_NAME_COL")
    index = parse_grid(values, year, int(name_col) if name_col else None)
    data_dir = os.environ.get("FINANCE_HELPER_DATA", "data")
    os.makedirs(data_dir, exist_ok=True)
    out = os.path.join(data_dir, "schedule_index.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    print(f"Wrote {len(index)} people to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
