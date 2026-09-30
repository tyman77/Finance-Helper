"""Pull reimbursements (per-diem) from Ramp's developer API.

Per-diem is trip-shaped data: paid to a person, dated around travel, and the
memo the employee types often names the job ("Per diem - Northview 4499").
The index this builds (data/ramp_reimbursements.json) feeds flight coding:
a reimbursement whose memo carries a project number tags the flight; one
without still corroborates that the person traveled that week.

Auth: OAuth2 client_credentials against Ramp's token endpoint using
RAMP_CLIENT_ID / RAMP_CLIENT_SECRET (create the app in Ramp: Settings ->
Developer API; scope needed: reimbursements:read).

First-pass conventions as with Sage: endpoint paths and field names are a
best effort from Ramp's public docs, env-overridable (RAMP_API_URL), and
errors carry the raw response/record so a mismatch is a one-line fix.
"""

from __future__ import annotations

import os
import re
from datetime import date

_API = os.environ.get("RAMP_API_URL", "https://api.ramp.com/developer/v1")

_PROJECT_CODE = re.compile(r"\b(\d{3,5})\b")

_NAME_KEYS = ("user_full_name", "employee_name", "full_name")
_DATE_KEYS = ("transaction_date", "created_at", "user_transaction_time", "date")
_MEMO_KEYS = ("memo", "merchant", "description", "note")


def credentials_present() -> bool:
    return bool(os.environ.get("RAMP_CLIENT_ID") and os.environ.get("RAMP_CLIENT_SECRET"))


def _get_token() -> str:
    import requests

    try:
        resp = requests.post(
            f"{_API}/token",
            auth=(os.environ["RAMP_CLIENT_ID"].strip(), os.environ["RAMP_CLIENT_SECRET"].strip()),
            data={"grant_type": "client_credentials", "scope": "reimbursements:read"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Ramp token request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Ramp token request failed: HTTP {resp.status_code}\n{resp.text[:800]}")
    return resp.json()["access_token"]


def fetch_reimbursements(start: date, end: date) -> list[dict]:
    import requests

    token = _get_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_API}/reimbursements"
    # Confirmed live (2026-08-28): bare dates get HTTP 422 "Not a valid
    # datetime." — the endpoint wants full RFC3339 datetimes.
    params = {"from_date": f"{start.isoformat()}T00:00:00Z",
              "to_date": f"{end.isoformat()}T23:59:59Z",
              "page_size": 100}
    records: list[dict] = []
    for _page in range(200):
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ramp reimbursements request failed: HTTP {resp.status_code}\n{resp.text[:800]}")
        data = resp.json()
        batch = data.get("data") or []
        records.extend(batch)
        next_url = ((data.get("page") or {}).get("next")
                    or data.get("next") or (data.get("meta") or {}).get("next_page"))
        if not next_url:
            break
        url, params = next_url, None            # cursor URL carries everything
    return records


def _get(rec: dict, keys: tuple) -> str:
    for k in keys:
        v = rec.get(k)
        if isinstance(v, dict):
            v = " ".join(str(v.get(p) or "") for p in ("first_name", "last_name")).strip()
        if v not in (None, ""):
            return str(v)
    # Nested user object under common keys.
    user = rec.get("user")
    if keys is _NAME_KEYS and isinstance(user, dict):
        name = " ".join(str(user.get(p) or "") for p in ("first_name", "last_name")).strip()
        if name:
            return name
    return ""


def _selection(rec: dict, category: str) -> str:
    """One accounting-field selection's option name by category ("Project",
    "Category", "Department") — handles both the developer API's
    accounting_field_selections shape and the normalized
    tracking_category_selections shape."""
    sels = (rec.get("accounting_field_selections")
            or rec.get("tracking_category_selections") or [])
    for sel in sels:
        if not isinstance(sel, dict):
            continue
        cat = ((sel.get("category_info") or {}).get("name")
               or sel.get("category_name") or "")
        if str(cat).strip().lower() != category.lower():
            continue
        opt = sel.get("option_selection") or {}
        return str(sel.get("name") or opt.get("option_name")
                   or opt.get("name") or "")
    return ""


def _project_from_selections(rec: dict) -> str | None:
    """The Intacct Project accounting field on the reimbursement — where the
    coding actually lives (the memo is free text and usually has no code).
    The job number is the trailing numeric token of the option name
    ("P000288 - Life.Church, CO | PKR Campus Build | 3335" -> "3335")."""
    name = _selection(rec, "Project")
    if not name:
        return None
    codes = _PROJECT_CODE.findall(name)
    if codes:
        return codes[-1]
    return name.split(" - ")[0].strip() or None       # bare P-number option


def build_index(records: list[dict]) -> list[dict]:
    """Raw Ramp records -> the compact per-person index enrich consumes."""
    out = []
    for rec in records:
        person = _get(rec, _NAME_KEYS)
        raw_date = _get(rec, _DATE_KEYS)[:10]
        try:
            when = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if not person:
            continue
        memo = _get(rec, _MEMO_KEYS)
        project = _project_from_selections(rec)
        if not project:
            m = _PROJECT_CODE.search(memo)
            project = m.group(1) if m else None
        entry = {
            "person": person,
            "date": when.isoformat(),
            "amount": str(rec.get("amount") or ""),
            "memo": memo,
            "project": project,
        }
        # The Ramp GL coding is a signal in its own right: a per-diem coded
        # 71040 OH (no project) says the trip was overhead, not project work.
        gl = _selection(rec, "Category")
        if gl:
            entry["gl_category"] = gl
        dept = _selection(rec, "Department")
        if dept:
            entry["department"] = dept
        out.append(entry)
    if records and not out:
        import json
        raise RuntimeError(
            "Ramp returned records but none mapped (person/date fields not "
            "recognized). First raw record:\n" + json.dumps(records[0], default=str)[:1200])
    return out


def fetch_index(start: date, end: date) -> list[dict]:
    if not credentials_present():
        raise RuntimeError("Ramp credentials missing: set RAMP_CLIENT_ID and "
                           "RAMP_CLIENT_SECRET (Ramp -> Settings -> Developer API).")
    return build_index(fetch_reimbursements(start, end))
