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

# Candidates cover both API records and the payments CSV export's headers
# (which vary by which report/export screen produced the file).
_VENDOR_KEYS = ("vendorName", "vendor_name", "name", "payee",
                "Vendor", "Vendor Name", "Payee", "Payee Name", "Pay To",
                "Paid To", "Recipient")
_DATE_KEYS = ("processDate", "paymentDate", "sentDate", "createdTime", "date",
              "Process Date", "Payment Date", "Sent Date", "Paid Date",
              "Date", "Processed Date")
_AMOUNT_KEYS = ("amount", "paymentAmount", "totalAmount",
                "Amount", "Payment Amount", "Paid Amount", "Total Amount")
_ID_KEYS = ("id", "paymentId", "Payment Confirmation Number",
            "Confirmation Number", "Payment #", "Payment Number",
            "Check #", "Check Number", "Ref #", "Reference")


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


def _parse_any_date(text: str):
    from datetime import datetime
    text = (text or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def build_index(records: list[dict]) -> list[dict]:
    out = []
    for i, rec in enumerate(records):
        when = _parse_any_date(_get(rec, _DATE_KEYS))
        if when is None:
            continue
        amount = _get(rec, _AMOUNT_KEYS).replace(",", "").replace("$", "")
        if amount.startswith("(") and amount.endswith(")"):
            amount = "-" + amount[1:-1]
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


# --- v2 API fallback --------------------------------------------------------
# Dev keys are often provisioned for the older v2 API (api.bill.com/api/v2)
# but rejected by the v3 Connect gateway (BDC_1102). Same data, older door:
# form-encoded requests, sessionId from Login.json, List/SentPay.json pages.

def _v2_base() -> str:
    return (os.environ.get("BILLDOTCOM_V2_URL") or "https://api.bill.com/api/v2").rstrip("/")


def _v2_call(path: str, data: dict) -> dict:
    import requests

    try:
        resp = requests.post(f"{_v2_base()}/{path}", data=data, timeout=60)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Bill.com v2 request failed: {type(exc).__name__}: {exc}") from exc
    try:
        js = resp.json()
    except ValueError:
        raise RuntimeError(f"Bill.com v2 returned non-JSON: HTTP {resp.status_code}\n{resp.text[:400]}")
    if js.get("response_status") != 0:
        detail = js.get("response_data") or {}
        raise RuntimeError("Bill.com v2 error: "
                           + str(detail.get("error_message") or detail)[:400])
    return js.get("response_data")


def _v2_session():
    """(pages, vendors_by_id) — one login shared across entity listings."""
    import json as _json

    dev_key = os.environ["BILLDOTCOM_DEV_KEY"].strip()
    session = _v2_call("Login.json", {
        "devKey": dev_key,
        "userName": os.environ["BILLDOTCOM_USERNAME"].strip(),
        "password": os.environ["BILLDOTCOM_PASSWORD"].strip(),
        "orgId": os.environ["BILLDOTCOM_ORG_ID"].strip(),
    })["sessionId"]

    def pages(entity):
        out, start = [], 0
        for _ in range(100):
            batch = _v2_call(f"List/{entity}.json", {
                "devKey": dev_key, "sessionId": session,
                "data": _json.dumps({"start": start, "max": 999}),
            }) or []
            out.extend(batch)
            if len(batch) < 999:
                break
            start += 999
        return out

    return pages


def fetch_payments_v2() -> list[dict]:
    pages = _v2_session()
    vendors = {v.get("id"): v.get("name", "") for v in pages("Vendor")}
    records = []
    for p in pages("SentPay"):
        records.append({
            "id": p.get("id"),
            "vendorName": vendors.get(p.get("vendorId"), ""),
            "amount": p.get("amount"),
            "processDate": p.get("processDate"),
            "status": str(p.get("status") or ""),
        })
    return records


def fetch_master_index() -> dict:
    """Vendor master + bills + vendor bank accounts, for the integrity checks.

    Each entity is fetched independently: an org that forbids one listing
    (e.g. VendorBankAccount) still yields the others, with the failure noted
    in "gaps" instead of sinking the whole pull.
    """
    if not credentials_present():
        raise RuntimeError("Bill.com credentials missing: set "
                           + ", ".join(_REQUIRED) + " (see .env.example).")
    pages = _v2_session()
    gaps: list[str] = []

    def safe(entity):
        try:
            return pages(entity)
        except RuntimeError as exc:
            gaps.append(f"{entity}: {str(exc)[:100]}")
            return []

    raw_vendors = safe("Vendor")
    raw_accounts = safe("VendorBankAccount")
    raw_bills = safe("Bill")

    vendors = []
    for v in raw_vendors:
        vendors.append({
            "id": v.get("id"),
            "name": str(v.get("name") or "").strip(),
            "active": str(v.get("isActive") or "") == "1",
            "created": str(v.get("createdTime") or "")[:10],
            "email": str(v.get("email") or "").strip().lower(),
            "payment_email": str(v.get("paymentEmail") or "").strip().lower(),
        })
    names = {v["id"]: v["name"] for v in vendors}

    bank_accounts = [{
        "vendor_id": a.get("vendorId"),
        "vendor": names.get(a.get("vendorId"), ""),
        "created": str(a.get("createdTime") or "")[:10],
        "active": str(a.get("isActive") or "") == "1",
    } for a in raw_accounts]

    bills = []
    for b in raw_bills:
        bills.append({
            "id": b.get("id"),
            "vendor": names.get(b.get("vendorId"), ""),
            "invoice": str(b.get("invoiceNumber") or "").strip(),
            "invoice_date": str(b.get("invoiceDate") or "")[:10],
            "created": str(b.get("createdTime") or "")[:10],
            "amount": str(b.get("amount") or ""),
            "po": str(b.get("poNumber") or "").strip(),
        })
    return {"vendors": vendors, "bank_accounts": bank_accounts,
            "bills": bills, "gaps": gaps}


def fetch_index() -> list[dict]:
    if not credentials_present():
        raise RuntimeError("Bill.com credentials missing: set "
                           + ", ".join(_REQUIRED) + " (see .env.example).")
    try:
        return build_index(fetch_payments())
    except RuntimeError as v3_err:
        if "login failed" not in str(v3_err):
            raise                       # v3 authed fine; the problem is elsewhere
        try:
            return build_index(fetch_payments_v2())
        except RuntimeError as v2_err:
            raise RuntimeError(
                "Both Bill.com APIs rejected the credentials.\n"
                f"v3 (Connect): {v3_err}\n"
                f"v2 (classic): {v2_err}\n\n"
                "If both say the developer key is invalid, the key is sandbox-only: "
                "request PRODUCTION API access for your org at developer.bill.com "
                "(or via Bill.com support), then update BILLDOTCOM_DEV_KEY.") from v2_err
