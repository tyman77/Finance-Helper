"""Pull disbursed payments from Bill.com (v3 API) for the fraud checks.

Two things the payment list unlocks:
  - expanding each BILL.COM funding debit on the bank statement into the
    vendor payments behind it (completing the cash proof for AP), and
  - the cross-system duplicate scan: the same vendor+amount paid via
    Bill.com AND again by check/ACH — the classic double-payment fraud.

Auth (v3): POST {base}/login with devKey/username/password/organizationId
-> sessionId; subsequent requests carry devKey + sessionId headers.
First-pass conventions as with Sage/Ramp/Paychex: paths and field names are
best effort, env-overridable (BILLDOTCOM_BASE_URL, BILLDOTCOM_PAYMENTS_URL),
and failures carry the raw response/record.
"""

from __future__ import annotations

import os
import re
from datetime import date

_DEFAULT_BASE = "https://gateway.prod.bill.com/connect/v3"

_REQUIRED = ["BILLDOTCOM_DEV_KEY", "BILLDOTCOM_USERNAME",
             "BILLDOTCOM_PASSWORD", "BILLDOTCOM_ORG_ID"]

_VENDOR_KEYS = ("vendorName", "vendor_name", "name", "payee")
_DATE_KEYS = ("processDate", "paymentDate", "sentDate", "createdTime", "date")
_AMOUNT_KEYS = ("amount", "paymentAmount", "totalAmount")
_ID_KEYS = ("id", "paymentId")


def credentials_present() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED)


def _base() -> str:
    return (os.environ.get("BILLDOTCOM_BASE_URL") or _DEFAULT_BASE).rstrip("/")


def _login() -> dict:
    import requests

    try:
        resp = requests.post(
            f"{_base()}/login",
            json={"devKey": os.environ["BILLDOTCOM_DEV_KEY"].strip(),
                  "username": os.environ["BILLDOTCOM_USERNAME"].strip(),
                  "password": os.environ["BILLDOTCOM_PASSWORD"].strip(),
                  "organizationId": os.environ["BILLDOTCOM_ORG_ID"].strip()},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Bill.com login failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Bill.com login failed: HTTP {resp.status_code}\n{resp.text[:800]}")
    data = resp.json()
    session = data.get("sessionId") or (data.get("response") or {}).get("sessionId")
    if not session:
        raise RuntimeError("Bill.com login returned no sessionId:\n" + resp.text[:500])
    return {"sessionId": session, "devKey": os.environ["BILLDOTCOM_DEV_KEY"].strip()}


def fetch_payments() -> list[dict]:
    import requests

    headers = _login()
    url = os.environ.get("BILLDOTCOM_PAYMENTS_URL", f"{_base()}/payments")
    records: list[dict] = []
    page = 1
    for _ in range(100):
        resp = requests.get(url, headers=headers,
                            params={"max": 100, "page": page}, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Bill.com payments request failed: HTTP {resp.status_code}\n{resp.text[:800]}\n\n"
                "If the path is rejected, set BILLDOTCOM_PAYMENTS_URL.")
        data = resp.json()
        batch = (data if isinstance(data, list)
                 else data.get("results") or data.get("payments") or data.get("response") or [])
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        records.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return records


def _get(rec: dict, keys: tuple) -> str:
    for k in keys:
        v = rec.get(k)
        if isinstance(v, dict):
            for sub in ("name", "id"):
                if v.get(sub) not in (None, ""):
                    return str(v[sub])
            continue
        if v not in (None, ""):
            return str(v)
    if keys is _VENDOR_KEYS and isinstance(rec.get("vendor"), dict):
        return str(rec["vendor"].get("name") or "")
    return ""


def build_index(records: list[dict]) -> list[dict]:
    out = []
    for i, rec in enumerate(records):
        raw_date = _get(rec, _DATE_KEYS)[:10]
        try:
            when = date.fromisoformat(raw_date)
        except ValueError:
            continue
        amount = _get(rec, _AMOUNT_KEYS)
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            continue
        out.append({
            "id": _get(rec, _ID_KEYS) or str(i),
            "vendor": _get(rec, _VENDOR_KEYS),
            "amount": f"{amt:.2f}",
            "date": when.isoformat(),
            "status": str(rec.get("status") or ""),
        })
    if records and not out:
        import json
        raise RuntimeError(
            "No Bill.com payments mapped (date/amount fields not recognized). "
            "First raw record:\n" + json.dumps(records[0], default=str)[:1200])
    return out


def fetch_index() -> list[dict]:
    if not credentials_present():
        raise RuntimeError("Bill.com credentials missing: set "
                           + ", ".join(_REQUIRED) + " (see .env.example).")
    return build_index(fetch_payments())
