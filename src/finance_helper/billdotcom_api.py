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


def _v2_login() -> tuple[str, str]:
    """(devKey, sessionId) for the classic API."""
    dev_key = os.environ["BILLDOTCOM_DEV_KEY"].strip()
    session = _v2_call("Login.json", {
        "devKey": dev_key,
        "userName": os.environ["BILLDOTCOM_USERNAME"].strip(),
        "password": os.environ["BILLDOTCOM_PASSWORD"].strip(),
        "orgId": os.environ["BILLDOTCOM_ORG_ID"].strip(),
    })["sessionId"]
    return dev_key, session


def _v2_pages(dev_key: str, session: str, entity: str, filters=None) -> list:
    import json as _json

    out, start = [], 0
    for _ in range(100):
        data = {"start": start, "max": 999}
        if filters:
            data["filters"] = filters
        batch = _v2_call(f"List/{entity}.json", {
            "devKey": dev_key, "sessionId": session,
            "data": _json.dumps(data),
        }) or []
        out.extend(batch)
        if len(batch) < 999:
            break
        start += 999
    return out


def _v2_session():
    """pages(entity) — one login shared across entity listings."""
    dev_key, session = _v2_login()

    def pages(entity, filters=None):
        return _v2_pages(dev_key, session, entity, filters)

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


# --- Bill Check: open bills + their attachments ------------------------------
# The AP review pulls every unpaid bill (what the clerk entered) and the
# invoice attached to it (what the vendor actually sent), so the two can be
# compared field by field. Same first-pass conventions as above: v2 entity
# names/fields are best effort, the document-pages path is env-overridable
# (BILLDOTCOM_DOC_PAGES_PATH), and failures carry the raw response.

_APPROVAL_STATUS = {"0": "unassigned", "1": "assigned", "3": "approving",
                    "4": "approved", "5": "denied"}
_PAYMENT_STATUS = {"0": "paid", "1": "open", "2": "partially paid",
                   "4": "scheduled"}


def _iso_day(value) -> str:
    return str(value or "")[:10]


def _money_str(value) -> str:
    try:
        return f"{float(str(value).replace(',', '')):.2f}"
    except (TypeError, ValueError):
        return ""


def _normalize_bill(b: dict, vendor: dict, term: dict) -> dict:
    days = term.get("dueDays")
    try:
        terms_days = int(days) if days not in (None, "") else None
    except (TypeError, ValueError):
        terms_days = None
    return {
        "id": str(b.get("id") or ""),
        "vendor": str(vendor.get("name") or "").strip(),
        "vendor_id": str(b.get("vendorId") or ""),
        "invoice": str(b.get("invoiceNumber") or "").strip(),
        "invoice_date": _iso_day(b.get("invoiceDate")),
        "due_date": _iso_day(b.get("dueDate")),
        "amount": _money_str(b.get("amount")),
        "terms": str(term.get("name") or "").strip(),
        "terms_days": terms_days,
        "approval_status": _APPROVAL_STATUS.get(str(b.get("approvalStatus")), str(b.get("approvalStatus") or "")),
        "payment_status": _PAYMENT_STATUS.get(str(b.get("paymentStatus")), str(b.get("paymentStatus") or "")),
        "description": str(b.get("description") or "").strip(),
        "po": str(b.get("poNumber") or "").strip(),
        "created": _iso_day(b.get("createdTime")),
        "updated": _iso_day(b.get("updatedTime")),
    }


def fetch_open_bills() -> list[dict]:
    """Every active, not-yet-paid bill with the fields Bill Check compares.

    Payment terms come from the bill's term when it has one, else the
    vendor's default term — that is what Bill.com used to propose the due
    date the clerk confirmed.
    """
    if not credentials_present():
        raise RuntimeError("Bill.com credentials missing: set "
                           + ", ".join(_REQUIRED) + " (see .env.example).")
    dev_key, session = _v2_login()

    def pages(entity, filters=None):
        return _v2_pages(dev_key, session, entity, filters)

    vendors = {v.get("id"): v for v in pages("Vendor")}
    try:
        terms = {t.get("id"): t for t in pages("PaymentTerm")}
    except RuntimeError:
        terms = {}
    out = []
    for b in pages("Bill", [{"field": "isActive", "op": "=", "value": "1"}]):
        if str(b.get("isActive") or "1") != "1":
            continue
        if str(b.get("paymentStatus") or "") == "0":      # paid in full
            continue
        vendor = vendors.get(b.get("vendorId")) or {}
        term = (terms.get(b.get("paymentTermId"))
                or terms.get(vendor.get("paymentTermId")) or {})
        out.append(_normalize_bill(b, vendor, term))
    return out


def sniff_media_type(data: bytes, header: str = "") -> str:
    head = data[:12]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    mime = (header or "").split(";")[0].strip().lower()
    return mime or "application/octet-stream"


def fetch_bill_documents(bill_id: str) -> list[dict]:
    """The attachment(s) on a bill as [{name, media_type, data}].

    Uses the classic GetDocumentPages call: page 1 tells us how many pages
    there are and where the file is; a PDF comes back whole, an image-backed
    attachment one page at a time. Raises with the raw response when the org
    has no attachment on the bill (or the path is different for this org).
    """
    import json as _json
    import requests

    dev_key, session = _v2_login()
    path = os.environ.get("BILLDOTCOM_DOC_PAGES_PATH") or "GetDocumentPages.json"
    docs: list[dict] = []
    page = 1
    for _ in range(60):
        resp = _v2_call(path, {
            "devKey": dev_key, "sessionId": session,
            "data": _json.dumps({"id": bill_id, "pageNumber": page}),
        }) or {}
        info = resp.get("documentPages") if isinstance(resp, dict) else None
        info = info if isinstance(info, dict) else (resp if isinstance(resp, dict) else {})
        url = info.get("fileUrl") or info.get("url") or info.get("downloadUrl")
        if not url:
            raise RuntimeError("Bill.com returned no file URL for the attachment on "
                               f"bill {bill_id}:\n" + _json.dumps(resp, default=str)[:400])
        try:
            r = requests.get(url, headers={"devKey": dev_key, "sessionId": session},
                             timeout=60)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Attachment download failed: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"Attachment download failed: HTTP {r.status_code}\n{r.text[:300]}")
        media = sniff_media_type(r.content, r.headers.get("Content-Type", ""))
        docs.append({"name": str(info.get("name") or info.get("fileName") or f"page-{page}"),
                     "media_type": media, "data": r.content})
        try:
            num_pages = int(info.get("numPages") or 1)
        except (TypeError, ValueError):
            num_pages = 1
        if media == "application/pdf" or page >= num_pages:
            break
        page += 1
    return docs
