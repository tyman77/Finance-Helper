"""Timecards -> {person: {date: project code}} — labor is the ground truth.

A person who logged hours to job 4499 on the days around a flight was flying
for 4499; that beats every inference Scout makes. The index this module
builds (data/timecards_index.json) has the exact shape of the crew-schedule
index, so flight coding consumes it through the same resolver — top rung.

Two intake paths, same index:
  - The Paychex Flex API (developer.paychex.com): OAuth2 client_credentials
    with PAYCHEX_CLIENT_ID / PAYCHEX_CLIENT_SECRET + PAYCHEX_COMPANY_ID.
    NOTE: unlike Ramp, Paychex API access is gated behind their developer
    program and company authorization — endpoint/field names below are a
    first pass with env overrides (PAYCHEX_API_URL, PAYCHEX_TIMECARDS_URL)
    and raw-record errors, per this repo's convention.
  - A timecard export CSV (from Paychex Flex Time reports) — no API access
    needed; columns are matched by candidates.

The index is vendor-neutral on purpose: when the Paylocity migration lands,
only the fetch changes.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime

_PROJECT_CODE = re.compile(r"\b(\d{3,5})\b")

_NAME_KEYS = ("workerName", "employeeName", "Employee Name", "Employee", "Worker",
              "name", "Name", "employee_full_name")
_DATE_KEYS = ("businessDate", "workDate", "apply_date", "Date", "Work Date",
              "date", "entryDate")
_JOB_KEYS = ("jobCode", "jobId", "job", "Job", "Job Code", "project", "Project",
             "laborAssignmentId", "costCenter", "Cost Center", "Department/Job")


def credentials_present() -> bool:
    return bool(os.environ.get("PAYCHEX_CLIENT_ID")
                and os.environ.get("PAYCHEX_CLIENT_SECRET")
                and os.environ.get("PAYCHEX_COMPANY_ID"))


def _api() -> str:
    return os.environ.get("PAYCHEX_API_URL", "https://api.paychex.com")


def _get_token() -> str:
    import requests

    try:
        resp = requests.post(
            f"{_api()}/auth/oauth/v2/token",
            data={"grant_type": "client_credentials"},
            auth=(os.environ["PAYCHEX_CLIENT_ID"].strip(),
                  os.environ["PAYCHEX_CLIENT_SECRET"].strip()),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Paychex token request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Paychex token request failed: HTTP {resp.status_code}\n{resp.text[:800]}")
    return resp.json()["access_token"]


def fetch_timecards(start: date, end: date) -> list[dict]:
    import requests

    token = _get_token()
    company = os.environ["PAYCHEX_COMPANY_ID"].strip()
    url = os.environ.get("PAYCHEX_TIMECARDS_URL",
                         f"{_api()}/companies/{company}/timecards")
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {token}"},
        params={"from": start.isoformat(), "to": end.isoformat()}, timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Paychex timecards request failed: HTTP {resp.status_code}\n{resp.text[:1000]}\n\n"
            "If the path is rejected, set PAYCHEX_TIMECARDS_URL (see paychex_api.py).")
    data = resp.json()
    rows = data.get("content") or data.get("data") or data.get("timecards") or []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def _get(row: dict, keys: tuple) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, dict):
            for sub in ("code", "id", "value", "name"):
                if v.get(sub) not in (None, ""):
                    return str(v[sub])
            continue
        if v not in (None, ""):
            return str(v)
    return ""


def _parse_any_date(value: str):
    text = (value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def build_index(rows: list[dict]) -> dict:
    """Rows (API entries or CSV lines) -> {person: {iso_date: project code}}.

    Only entries whose job field carries a 3-5 digit code land in the index —
    overhead/PTO time can't tag a flight. Raises with the first raw row when
    nothing maps, for field-name inspection.
    """
    index: dict[str, dict] = {}
    for row in rows:
        person = _get(row, _NAME_KEYS).strip()
        when = _parse_any_date(_get(row, _DATE_KEYS))
        m = _PROJECT_CODE.search(_get(row, _JOB_KEYS))
        if not person or when is None or not m:
            continue
        # "Last, First" from payroll exports -> "First Last" to match the map.
        if "," in person:
            last, _, first = person.partition(",")
            person = f"{first.strip()} {last.strip()}".strip()
        index.setdefault(person, {})[when.isoformat()] = m.group(1)
    if rows and not index:
        import json
        raise RuntimeError(
            "No timecard rows mapped (person/date/job fields not recognized). "
            "First raw row:\n" + json.dumps(rows[0], default=str)[:1200])
    return index


def fetch_index(start: date, end: date) -> dict:
    if not credentials_present():
        raise RuntimeError(
            "Paychex credentials missing: set PAYCHEX_CLIENT_ID, "
            "PAYCHEX_CLIENT_SECRET and PAYCHEX_COMPANY_ID — or upload a "
            "timecard export CSV instead (no API access needed).")
    return build_index(fetch_timecards(start, end))
