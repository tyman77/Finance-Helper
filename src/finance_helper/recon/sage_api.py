"""Pull GL detail for the cash account(s) straight from Sage Intacct.

Lets Cash Proof run without a GL CSV export: upload the bank file, tick
"pull ledger from Sage", and the ledger side comes from the API for the bank
file's date range.

Auth is the OAuth2 client-credentials flow already confirmed live by the
posting module (destinations/sage_intacct.py, 2026-07-01): client_id/secret
identify the registered app, INTACCT_USER_ID/PASSWORD identify the Web
Services User inside it. The same five INTACCT_* env vars power all of it.

Endpoint reality check (same convention as scripts/fetch_sage_projects.py):
the query URL and field names below are a first pass from Sage's public REST
docs, overridable via env without code changes:
    INTACCT_GL_QUERY_URL   default https://api.intacct.com/ia/api/v1/services/core/query
    INTACCT_GL_OBJECT      default general-ledger/journal-entry-line
If the first live call fails or maps zero rows, the raised error carries the
response body / first raw record so the fix is a small config edit.

Controls note: use a READ-ONLY Web Services User for this if possible —
Cash Proof never needs write access, and a fraud-checking tool ideally can't
alter what it checks. A separate user from the Apex/Salesforce integration
also keeps the Intacct audit trail attributable.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation

from .bank import normalize_counterparty
from .models import Txn
from .settings import recon_config

_QUERY_URL = os.environ.get(
    "INTACCT_GL_QUERY_URL", "https://api.intacct.com/ia/api/v1/services/core/query"
)
_GL_OBJECT = os.environ.get("INTACCT_GL_OBJECT", "general-ledger/journal-entry-line")

_REQUIRED = ["INTACCT_CLIENT_ID", "INTACCT_CLIENT_SECRET", "INTACCT_COMPANY_ID",
             "INTACCT_USER_ID", "INTACCT_USER_PASSWORD"]

_DATE_KEYS = ("entryDate", "ENTRY_DATE", "postingDate", "GLENTRYDATE", "entry_date")
_MEMO_KEYS = ("memo", "description", "MEMO", "DESCRIPTION")
_DOC_KEYS = ("documentNumber", "docNumber", "DOCNUMBER", "referenceNumber", "RECORDID")
_ID_KEYS = ("key", "id", "recordNo", "RECORDNO")
_ACCT_KEYS = ("glAccount", "accountNo", "ACCOUNTNO", "account", "ACCOUNTKEY")
_JOURNAL_KEYS = ("journal", "journalSymbol", "SYMBOL", "BATCH_TITLE")


def credentials_present() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED)


def _get_token() -> str:
    import requests

    from ..intacct_auth import env, web_services_username
    data = {"grant_type": "client_credentials",
            "username": web_services_username(),
            "password": env("INTACCT_USER_PASSWORD")}
    try:
        resp = requests.post(
            "https://api.intacct.com/ia/api/v1/oauth2/token",
            auth=(env("INTACCT_CLIENT_ID"), env("INTACCT_CLIENT_SECRET")),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Sage token request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Sage token request failed: HTTP {resp.status_code}\n{resp.text[:800]}")
    return resp.json()["access_token"]


def fetch_gl_records(start: date, end: date) -> list[dict]:
    """Query journal-entry lines for the period. Filters are best-guess; rows
    are re-filtered client-side in to_txns(), so an over-broad server response
    still reconciles correctly."""
    import requests

    token = _get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "company-id": os.environ.get("INTACCT_COMPANY_ID", "").strip()}
    body = {
        "object": _GL_OBJECT,
        "filters": [
            {"$gte": {"entryDate": start.isoformat()}},
            {"$lte": {"entryDate": end.isoformat()}},
        ],
        "size": 1000,
    }
    records: list[dict] = []
    page_start = 0
    for _page in range(40):                      # hard cap: 40k rows
        resp = requests.post(_QUERY_URL, json={**body, "start": page_start},
                             headers=headers, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Sage GL query failed: HTTP {resp.status_code}\n{resp.text[:1000]}\n\n"
                "If the object or filter field is rejected, set INTACCT_GL_OBJECT / "
                "INTACCT_GL_QUERY_URL (see recon/sage_api.py).")
        data = resp.json()
        batch = data.get("ia::result") or data.get("data") or data.get("items") or []
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        if records and batch[0] == records[0]:
            break                                # server ignored paging; stop
        records.extend(batch)
        if len(batch) < body["size"]:
            break
        page_start += body["size"]
    return records


def _get(rec: dict, keys: tuple) -> str:
    for k in keys:
        v = rec.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, dict):                  # e.g. glAccount: {"id": "10000", ...}
            for sub in ("id", "number", "key", "accountNo"):
                if v.get(sub) not in (None, ""):
                    return str(v[sub])
            continue
        return str(v)
    return ""


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _signed_amount(rec: dict) -> Decimal | None:
    """Debit to a cash account = money in (positive)."""
    debit = _dec(rec.get("debitAmount") or rec.get("DEBITAMOUNT"))
    credit = _dec(rec.get("creditAmount") or rec.get("CREDITAMOUNT"))
    if debit is not None or credit is not None:
        return (debit or Decimal("0")) - (credit or Decimal("0"))
    amount = _dec(rec.get("txnAmount") or rec.get("amount") or rec.get("AMOUNT"))
    if amount is None:
        return None
    tr_type = str(rec.get("txnType") or rec.get("trType") or rec.get("TR_TYPE") or "").lower()
    if tr_type in ("credit", "cr", "-1"):
        return -abs(amount)
    if tr_type in ("debit", "dr", "1"):
        return abs(amount)
    return amount                                # already signed


def to_txns(records: list[dict], start: date, end: date) -> list[Txn]:
    cash_accounts = {str(a) for a in recon_config()["sage"].get("cash_accounts") or []}
    txns: list[Txn] = []
    for i, rec in enumerate(records):
        account = _get(rec, _ACCT_KEYS)
        if cash_accounts and account not in cash_accounts:
            continue
        raw_date = _get(rec, _DATE_KEYS)
        try:
            posted = date.fromisoformat(raw_date[:10])
        except (ValueError, IndexError):
            continue
        if not (start <= posted <= end):
            continue
        amount = _signed_amount(rec)
        if amount is None or amount == 0:
            continue
        desc = _get(rec, _MEMO_KEYS)
        txns.append(Txn(
            source="sage",
            source_id=f"sage-api:{_get(rec, _ID_KEYS) or i}",
            posted_date=posted,
            amount=amount,
            counterparty_raw=desc or _get(rec, _DOC_KEYS) or "(no memo)",
            counterparty_norm=normalize_counterparty(desc),
            memo=_get(rec, _JOURNAL_KEYS),
            account_ref=account,
        ))
    return txns


def fetch_ledger(start: date, end: date) -> list[Txn]:
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError("Sage credentials missing: " + ", ".join(missing)
                           + ". Add them to the environment (see .env.example).")
    records = fetch_gl_records(start, end)
    txns = to_txns(records, start, end)
    if records and not txns:
        import json
        raise RuntimeError(
            "Sage returned records but none mapped to the configured cash "
            f"account(s) {sorted(recon_config()['sage'].get('cash_accounts') or [])} "
            "in the period. First raw record for field-name inspection:\n"
            + json.dumps(records[0], default=str)[:1200])
    return txns
