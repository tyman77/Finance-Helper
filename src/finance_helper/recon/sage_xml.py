"""Pull GL detail via Sage Intacct's XML gateway (the pre-REST Web Services API).

Why this exists: the company's working Intacct integration (the Apex/Shopify
one) authenticates with a Sender ID + Sender Password — XML-gateway
credentials. The REST OAuth path (sage_api.py) needs a client id/secret from
a registered developer-portal app, which this company doesn't have yet. The
XML gateway is stable, documented, and already provisioned here, so Cash
Proof prefers it whenever sender credentials are present.

Env (reusing the slots .env.example already reserved):
    INTACCT_SENDER_ID, INTACCT_SENDER_PASSWORD   the gateway (app) credentials
    INTACCT_COMPANY_ID, INTACCT_USER_ID, INTACCT_USER_PASSWORD
    INTACCT_XML_URL   optional override, default the production gateway

Queries readByQuery on GLDETAIL filtered to the configured cash accounts and
the period, following readMore pagination. TR_TYPE is 1 for debits and -1 for
credits; debit to a cash account = money in, so signed amount is
TRX_AMOUNT * TR_TYPE and compares directly with bank amounts.
"""

from __future__ import annotations

import os
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..intacct_auth import env
from .bank import normalize_counterparty
from .models import Txn
from .settings import recon_config

_GATEWAY = os.environ.get("INTACCT_XML_URL", "https://api.intacct.com/ia/xml/xmlgw.phtml")

_REQUIRED = ["INTACCT_SENDER_ID", "INTACCT_SENDER_PASSWORD", "INTACCT_COMPANY_ID",
             "INTACCT_USER_ID", "INTACCT_USER_PASSWORD"]

_PAGE_SIZE = 1000
_MAX_PAGES = 40


def credentials_present() -> bool:
    return all(env(k) for k in _REQUIRED)


def _el(parent, tag, text=None):
    node = ET.SubElement(parent, tag)
    if text is not None:
        node.text = str(text)
    return node


def _request_xml(content_fn) -> bytes:
    """Wrap one <function> element (built by content_fn) in the full envelope."""
    root = ET.Element("request")
    control = _el(root, "control")
    _el(control, "senderid", env("INTACCT_SENDER_ID"))
    _el(control, "password", env("INTACCT_SENDER_PASSWORD"))
    _el(control, "controlid", uuid.uuid4().hex)
    _el(control, "uniqueid", "false")
    _el(control, "dtdversion", "3.0")
    _el(control, "includewhitespace", "false")
    operation = _el(root, "operation")
    auth = _el(operation, "authentication")
    login = _el(auth, "login")
    _el(login, "userid", env("INTACCT_USER_ID"))       # XML login wants the bare user id
    _el(login, "companyid", env("INTACCT_COMPANY_ID"))
    _el(login, "password", env("INTACCT_USER_PASSWORD"))
    content = _el(operation, "content")
    fn = _el(content, "function")
    fn.set("controlid", "q1")
    content_fn(fn)
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root)


def _post(body: bytes) -> ET.Element:
    import requests

    try:
        resp = requests.post(
            _GATEWAY, data=body,
            headers={"Content-Type": "x-intacct-xml-request"}, timeout=90,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Sage XML gateway request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Sage XML gateway request failed: HTTP {resp.status_code}\n{resp.text[:800]}")
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise RuntimeError(f"Sage XML gateway returned unparseable XML: {exc}\n{resp.text[:500]}") from exc

    auth_status = root.findtext(".//authentication/status")
    if auth_status and auth_status != "success":
        detail = root.findtext(".//errormessage/error/description2") or \
                 root.findtext(".//errormessage/error/description") or "(no detail)"
        raise RuntimeError(f"Sage XML login failed: {detail}")
    result_status = root.findtext(".//result/status")
    if result_status and result_status != "success":
        detail = root.findtext(".//result//error/description2") or \
                 root.findtext(".//result//error/description") or "(no detail)"
        raise RuntimeError(f"Sage XML query failed: {detail}")
    return root


def _pull_accounts() -> list[str]:
    """Cash accounts plus clearing/limbo accounts (aux_accounts) whose
    entries also represent this bank account's movements."""
    sage_cfg = recon_config()["sage"]
    return [str(a) for a in (sage_cfg.get("cash_accounts") or [])] + \
           [str(a) for a in (sage_cfg.get("aux_accounts") or [])]


def _query_string(start: date, end: date) -> str:
    pull_accounts = _pull_accounts()
    dates = (f"ENTRY_DATE >= '{start.strftime('%m/%d/%Y')}' AND "
             f"ENTRY_DATE <= '{end.strftime('%m/%d/%Y')}'")
    if not pull_accounts:
        return dates
    accounts = " OR ".join(f"ACCOUNTNO = '{a}'" for a in pull_accounts)
    return f"({accounts}) AND {dates}"


def fetch_gl_records(start: date, end: date) -> list[dict]:
    def first_page(fn):
        rbq = _el(fn, "readByQuery")
        _el(rbq, "object", "GLDETAIL")
        _el(rbq, "fields", "*")
        _el(rbq, "query", _query_string(start, end))
        _el(rbq, "pagesize", _PAGE_SIZE)

    records: list[dict] = []
    root = _post(_request_xml(first_page))
    for _page in range(_MAX_PAGES):
        data = root.find(".//result/data")
        if data is None:
            break
        for row in data:
            records.append({child.tag: (child.text or "") for child in row})
        remaining = int(data.get("numremaining") or 0)
        result_id = data.get("resultId")
        if remaining <= 0 or not result_id:
            break

        def more(fn, rid=result_id):
            rm = _el(fn, "readMore")
            _el(rm, "resultId", rid)

        root = _post(_request_xml(more))
    return records


def _dec(value: str) -> Decimal | None:
    try:
        return Decimal((value or "").replace(",", ""))
    except InvalidOperation:
        return None


def _parse_mdy(value: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def to_txns(records: list[dict], start: date, end: date) -> list[Txn]:
    allowed = set(_pull_accounts())
    txns: list[Txn] = []
    for i, rec in enumerate(records):
        account = (rec.get("ACCOUNTNO") or "").strip()
        if allowed and account not in allowed:
            continue
        posted = _parse_mdy(rec.get("ENTRY_DATE", ""))
        if posted is None or not (start <= posted <= end):
            continue
        amount = _dec(rec.get("TRX_AMOUNT") or rec.get("AMOUNT") or "")
        if amount is None or amount == 0:
            continue
        tr_type = (rec.get("TR_TYPE") or "").strip()
        if tr_type == "-1":
            amount = -abs(amount)               # credit to cash = money out
        elif tr_type == "1":
            amount = abs(amount)                # debit to cash = money in
        desc = (rec.get("DESCRIPTION") or rec.get("MEMO") or rec.get("BATCH_TITLE") or "").strip()
        txns.append(Txn(
            source="sage",
            source_id=f"sage-xml:{rec.get('RECORDNO') or i}",
            posted_date=posted,
            amount=amount,
            counterparty_raw=desc or (rec.get("DOCUMENT") or "(no memo)"),
            counterparty_norm=normalize_counterparty(desc),
            memo=(rec.get("JOURNAL") or rec.get("BATCH_TITLE") or "").strip(),
            account_ref=account,
            doc_ref=(rec.get("DOCUMENT") or rec.get("BATCH_NO") or "").strip(),
        ))
    return txns


_PO_NO_KEYS = ("DOCNO", "DOCID", "PONUMBER", "RECORDNO")
_PO_VENDOR_KEYS = ("VENDORNAME", "CUSTVENDNAME", "VENDORID", "CUSTVENDID")
_PO_TOTAL_KEYS = ("TOTAL", "TRX_TOTALENTERED", "TOTALENTERED", "SUBTOTAL")
_PO_DATE_KEYS = ("WHENCREATED", "WHENPOSTED", "WHENDUE")


def fetch_pos(start: date, end: date) -> list[dict]:
    """Purchase orders (PODOCUMENT) for the period, via the same gateway.

    Field names follow the candidate convention; a query that returns rows
    but maps nothing raises with the first raw record for inspection.
    """
    def first_page(fn):
        rbq = _el(fn, "readByQuery")
        _el(rbq, "object", "PODOCUMENT")
        _el(rbq, "fields", "*")
        _el(rbq, "query",
            f"WHENCREATED >= '{start.strftime('%m/%d/%Y')}' AND "
            f"WHENCREATED <= '{end.strftime('%m/%d/%Y')}'")
        _el(rbq, "pagesize", _PAGE_SIZE)

    records: list[dict] = []
    root = _post(_request_xml(first_page))
    for _page in range(_MAX_PAGES):
        data = root.find(".//result/data")
        if data is None:
            break
        for row in data:
            records.append({child.tag: (child.text or "") for child in row})
        remaining = int(data.get("numremaining") or 0)
        result_id = data.get("resultId")
        if remaining <= 0 or not result_id:
            break

        def more(fn, rid=result_id):
            rm = _el(fn, "readMore")
            _el(rm, "resultId", rid)

        root = _post(_request_xml(more))

    def pick(rec, keys):
        for k in keys:
            if rec.get(k):
                return rec[k].strip()
        return ""

    out = []
    for rec in records:
        when = _parse_mdy(pick(rec, _PO_DATE_KEYS))
        total = _dec(pick(rec, _PO_TOTAL_KEYS))
        out.append({
            "po": pick(rec, _PO_NO_KEYS),
            "vendor": pick(rec, _PO_VENDOR_KEYS),
            "total": str(total) if total is not None else "",
            "date": when.isoformat() if when else "",
        })
    out = [r for r in out if r["po"]]
    if records and not out:
        import json
        raise RuntimeError(
            "Sage returned PODOCUMENT records but none mapped. First raw record:\n"
            + json.dumps(records[0], default=str)[:1200])
    return out


def list_cash_candidate_accounts() -> list[tuple]:
    """(ACCOUNTNO, TITLE) for chart accounts that look like cash/checking —
    so a wrong cash_accounts config produces a self-service error."""
    def q(fn):
        rbq = _el(fn, "readByQuery")
        _el(rbq, "object", "GLACCOUNT")
        _el(rbq, "fields", "ACCOUNTNO,TITLE")
        _el(rbq, "query", "TITLE like '%ash%' OR TITLE like '%hecking%' OR TITLE like '%ank%'")
        _el(rbq, "pagesize", 100)
    try:
        root = _post(_request_xml(q))
        data = root.find(".//result/data")
        return [(row.findtext("ACCOUNTNO", ""), row.findtext("TITLE", ""))
                for row in (data if data is not None else [])]
    except Exception:
        return []


def _account_titles() -> dict[str, str]:
    """ACCOUNTNO -> TITLE for the whole chart (one page covers real charts)."""
    def q(fn):
        rbq = _el(fn, "readByQuery")
        _el(rbq, "object", "GLACCOUNT")
        _el(rbq, "fields", "ACCOUNTNO,TITLE")
        _el(rbq, "query", "ACCOUNTNO > '0'")
        _el(rbq, "pagesize", 1000)
    try:
        root = _post(_request_xml(q))
        data = root.find(".//result/data")
        return {row.findtext("ACCOUNTNO", ""): row.findtext("TITLE", "")
                for row in (data if data is not None else [])}
    except Exception:
        return {}


def _amount_probe(amount: Decimal, around: date, window_days: int) -> list[dict]:
    """GLDETAIL rows anywhere in the chart with this exact absolute amount
    near the date — answers 'where DID Sage record this bank movement?'."""
    from datetime import timedelta
    lo = (around - timedelta(days=window_days)).strftime("%m/%d/%Y")
    hi = (around + timedelta(days=window_days)).strftime("%m/%d/%Y")

    def q(fn):
        rbq = _el(fn, "readByQuery")
        _el(rbq, "object", "GLDETAIL")
        _el(rbq, "fields", "ACCOUNTNO,ENTRY_DATE,DESCRIPTION,JOURNAL,TR_TYPE")
        _el(rbq, "query", f"TRX_AMOUNT = '{abs(amount):.2f}' AND "
                          f"ENTRY_DATE >= '{lo}' AND ENTRY_DATE <= '{hi}'")
        _el(rbq, "pagesize", 20)
    root = _post(_request_xml(q))
    data = root.find(".//result/data")
    return [{child.tag: (child.text or "") for child in row}
            for row in (data if data is not None else [])]


def annotate_unmatched(bank_txns: list[Txn], limit: int = 20,
                       window_days: int = 10) -> int:
    """For the biggest untied bank debits, ask Sage where (if anywhere) each
    amount was recorded, and append the answer to the exception's reason.
    Turns 'no ledger tie found' into either 'posted to account X' (a
    misposting to chase) or 'nowhere in Sage' (an unrecorded movement —
    the finding that matters most)."""
    targets = sorted((t for t in bank_txns
                      if t.status == "exception" and t.amount < 0),
                     key=lambda t: t.amount)[:limit]
    if not targets:
        return 0
    cash_accounts = {str(a) for a in recon_config()["sage"].get("cash_accounts") or []}
    titles = _account_titles()
    for t in targets:
        rows = _amount_probe(t.amount, t.posted_date, window_days)
        if rows:
            seen: dict[str, dict] = {}
            for r in rows:
                seen.setdefault((r.get("ACCOUNTNO") or "?").strip(), r)
            frag = "; ".join(
                f"{no} ({titles.get(no) or '?'}) on {r.get('ENTRY_DATE', '?')}"
                + (" — the cash account itself, outside the match window"
                   if no in cash_accounts else "")
                for no, r in list(seen.items())[:3])
            t.reason += f" · Sage HAS this amount: {frag}"
        else:
            t.reason += (f" · amount appears NOWHERE in Sage GL ±{window_days}d "
                         "— unrecorded movement")
    return len(targets)


def fetch_ledger(start: date, end: date) -> list[Txn]:
    missing = [k for k in _REQUIRED if not env(k)]
    if missing:
        raise RuntimeError("Sage XML credentials missing: " + ", ".join(missing)
                           + ". Add them to the environment (see .env.example).")
    records = fetch_gl_records(start, end)
    if not records:
        configured = sorted(recon_config()["sage"].get("cash_accounts") or [])
        candidates = list_cash_candidate_accounts()
        hint = ("; cash-like accounts in your chart: "
                + "; ".join(f"{no} ({title})" for no, title in candidates[:12])
                ) if candidates else ""
        raise RuntimeError(
            f"Sage returned NO GL rows for cash account(s) {configured} between "
            f"{start} and {end} — the configured account number is almost "
            f"certainly wrong{hint}. Set SAGE_CASH_ACCOUNTS=<your checking "
            "account's GL number> in Railway (comma-separate several) and re-run.")
    txns = to_txns(records, start, end)
    if records and not txns:
        import json
        raise RuntimeError(
            "Sage returned GLDETAIL records but none mapped to the configured "
            f"cash account(s) {sorted(recon_config()['sage'].get('cash_accounts') or [])} "
            "in the period. First raw record for inspection:\n"
            + json.dumps(records[0], default=str)[:1200])
    return txns
