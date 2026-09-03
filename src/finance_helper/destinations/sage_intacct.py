"""Sage Intacct destination — builds and posts a General Ledger journal entry.

Docs:
  https://developer.sage.com/intacct/docs/1/sage-intacct-rest-api/get-started
  https://developer.intacct.com/api/general-ledger/journal-entries/

A journal entry must balance. We debit each categorized expense line to its GL
account and post a single offsetting credit to INTACCT_CLEARING_ACCOUNT (your AP
or a clearing account) for the total.

Auth: OAuth2 client-credentials grant (same pattern as
scripts/fetch_sage_projects.py — see that file's docstring for the credential
setup). This is a FIRST PASS at the live POST, not a confirmed-working
integration: developer.sage.com blocks automated doc fetches, so the exact
request field names below (_JE_URL's shape, the keys in _line_payload /
_to_rest_body) are a best-effort guess from what's independently verifiable,
not a verified spec. post_journal_entry() raises with the FULL response body
on any non-2xx so a live test tells you exactly what to fix — same "probe,
then correct one spot" approach used for the projects fetch. If it fails,
paste the error back rather than guessing a second time.
"""

from __future__ import annotations

import os

from ..models import SourceDocument

# The GL journal to post into (a.k.a. journal symbol). "GJ" = General Journal is
# a common default; change to match your Intacct setup.
_JOURNAL_SYMBOL = os.environ.get("INTACCT_JOURNAL_SYMBOL", "GJ")

_TOKEN_URL = "https://api.intacct.com/ia/api/v1/oauth2/token"
# Best-guess REST endpoint for creating a journal entry — override with
# INTACCT_JOURNAL_ENTRY_URL if this turns out to be wrong (see module docstring).
_JE_URL = os.environ.get(
    "INTACCT_JOURNAL_ENTRY_URL", "https://api.intacct.com/ia/api/v1/objects/general-ledger/journal-entry"
)


def build_journal_entry(doc: SourceDocument) -> dict:
    """Their real JE shape (from the AP team's working import file): NO
    clearing account. The entry reclasses within the SAME expense account —
    each statement line gets a dimensioned entry (project/department/
    location), mirrored by an undimensioned opposite entry to the same
    account, so the account nets to zero while cost lands in dimensions."""
    entry_date = (doc.document_date.isoformat() if doc.document_date else None)
    default_location = os.environ.get("INTACCT_DEFAULT_LOCATION", "")

    dimensioned, mirrors = [], []
    for li in doc.line_items:
        # Positive amounts are expenses (debit); negatives are refunds/credits
        # and post as a credit to the same account rather than a negative debit.
        debit = li.amount if li.amount >= 0 else 0
        credit = -li.amount if li.amount < 0 else 0
        memo = f"{doc.vendor}: {li.description}"[:200]
        line = {
            "account_no": li.gl_account,
            "debit": str(debit),
            "credit": str(credit),
            "memo": memo,
            "category": li.category,
        }
        # Intacct dimensions, when we learned them via enrichment.
        if li.department:
            line["department"] = li.department.split("--")[0].strip()
        if li.project:
            line["project"] = li.project
        location = getattr(li, "location", "") or default_location
        if location:
            line["location"] = str(location).split("--")[0].strip()
        dimensioned.append(line)
        # The mirror on the opposite side, same account. It carries NO
        # project (that's the whole point of the reclass) but keeps the
        # department/location — accounts configured to require a Department
        # (e.g. 71000 overhead) reject a bare line, and the net per
        # department is zero either way.
        mirror = {
            "account_no": li.gl_account,
            "debit": str(credit),
            "credit": str(debit),
            "memo": memo,
        }
        for dim in ("department", "location"):
            if line.get(dim):
                mirror[dim] = line[dim]
        mirrors.append(mirror)

    return {
        "journal": _JOURNAL_SYMBOL,
        "date": entry_date,
        "reference_no": doc.document_id,
        "description": f"{doc.vendor} — {doc.document_id}"[:80],
        "currency": doc.currency,
        "lines": dimensioned + mirrors,
    }


def _to_rest_body(payload: dict) -> dict:
    """Map our internal build_journal_entry() shape to the REST request body.

    THE PART MOST LIKELY TO NEED A FIX: the field names here (glAccountNumber,
    debitAmount, postingDate, ...) are a best-effort guess, not a verified
    spec — see the module docstring. If a live POST comes back with a
    "required field missing" / "unrecognized field" style error, this is the
    one function to edit; nothing else in this file should need to change.
    """
    lines = []
    for line in payload["lines"]:
        entry = {
            "glAccountNumber": line["account_no"],
            "debitAmount": line["debit"],
            "creditAmount": line["credit"],
            "memo": line.get("memo", ""),
        }
        if line.get("department"):
            entry["departmentId"] = line["department"]
        if line.get("project"):
            entry["projectId"] = line["project"]
        lines.append(entry)

    return {
        "journalSymbol": payload["journal"],
        "postingDate": payload["date"],
        "referenceNumber": payload["reference_no"],
        "description": payload["description"],
        "currency": payload["currency"],
        "lines": lines,
    }


def _get_token() -> str:
    import requests

    # Confirmed live (2026-07-01): despite being a "client_credentials" grant,
    # Sage's token endpoint also requires the Web Services User identifying
    # itself in the request body — a 400 "Either username or session_id is
    # required" comes back without it. client_id/secret identify the app;
    # username/password identify the authorized Web Services User within it.
    from ..intacct_auth import env as _ienv
    from ..intacct_auth import web_services_username
    data = {"grant_type": "client_credentials"}
    if os.environ.get("INTACCT_USER_ID"):
        data["username"] = web_services_username()  # "user@company" format required
    if os.environ.get("INTACCT_USER_PASSWORD"):
        data["password"] = _ienv("INTACCT_USER_PASSWORD")
    try:
        resp = requests.post(
            _TOKEN_URL,
            auth=(_ienv("INTACCT_CLIENT_ID"), _ienv("INTACCT_CLIENT_SECRET")),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        # Network-level failure (DNS, proxy, timeout, ...) — wrap so this
        # surfaces through the CLI/web UI's existing RuntimeError handling
        # instead of an uncaught traceback.
        raise RuntimeError(f"Sage Intacct token request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Sage Intacct token request failed: HTTP {resp.status_code}\n{resp.text[:1000]}")
    return resp.json()["access_token"]


def _mdy(iso_date: str | None) -> str:
    from datetime import date as _date
    from datetime import datetime as _dt
    if iso_date:
        try:
            return _dt.strptime(iso_date[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            pass
    return _date.today().strftime("%m/%d/%Y")


def _load_projects_index() -> dict:
    import json

    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
    try:
        with open(os.path.join(data_dir, "sage_projects.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _intacct_project_id(code: str) -> str:
    """Scout codes projects by job number (5368); Intacct's PROJECTID is a
    P-number (P000635) whose project NAME carries the job number. Translate
    via the fetched projects index; unmapped codes post without a project
    (their own import files do the same) with the job number kept in the memo."""
    import re

    code = str(code or "").strip()
    if not code:
        return ""
    if re.match(r"^P\d+$", code, re.I):
        return code
    hits = [pid for pid, meta in _load_projects_index().items()
            if code in str((meta or {}).get("name") or "")]
    return hits[0] if len(hits) == 1 else ""


def _to_xml_batch(payload: dict, fn) -> list[dict]:
    """Fill the XML gateway <create><GLBATCH> for this journal entry —
    the documented legacy shape, the same gateway every working Sage read
    in this app already uses."""
    from decimal import Decimal

    from ..recon.sage_xml import _el

    create = _el(fn, "create")
    batch = _el(create, "GLBATCH")
    _el(batch, "JOURNAL", payload["journal"])
    _el(batch, "BATCH_DATE", _mdy(payload.get("date")))
    _el(batch, "BATCH_TITLE", (payload.get("description") or "")[:80])
    if payload.get("reference_no"):
        _el(batch, "REFERENCENO", str(payload["reference_no"])[:20])
    entries = _el(batch, "ENTRIES")
    metas: list[dict] = []
    for line in payload["lines"]:
        debit = Decimal(str(line.get("debit") or "0"))
        credit = Decimal(str(line.get("credit") or "0"))
        amount, tr_type = (debit, 1) if debit > 0 else (credit, -1)
        if amount == 0:
            continue
        metas.append({"account": str(line["account_no"]).split("--")[0].strip(),
                      "side": "debit" if tr_type == 1 else "credit",
                      "amount": str(amount),
                      "memo": line.get("memo") or "",
                      "department": line.get("department") or ""})
        entry = _el(entries, "GLENTRY")
        _el(entry, "ACCOUNTNO", str(line["account_no"]).split("--")[0].strip())
        _el(entry, "TR_TYPE", tr_type)
        _el(entry, "TRX_AMOUNT", str(amount))
        memo = (line.get("memo") or "")[:1000]
        if line.get("department"):
            _el(entry, "DEPARTMENT", line["department"])
        if line.get("location"):
            _el(entry, "LOCATION", line["location"])
        if line.get("project"):
            pid = _intacct_project_id(line["project"])
            if pid:
                _el(entry, "PROJECTID", pid)
            else:
                memo = (memo + f" | job {line['project']}")[:1000]
        _el(entry, "DESCRIPTION", memo)
    return metas


def _post_via_xml(payload: dict) -> dict:
    import re as _re

    from ..recon import sage_xml

    metas: list[dict] = []

    def build(fn):
        metas.extend(_to_xml_batch(payload, fn))

    try:
        root = sage_xml._post(sage_xml._request_xml(build))
    except RuntimeError as exc:
        # Intacct names offending lines by number ("line no. 127") — say
        # which actual statement line that is so it can be fixed in the
        # review instead of counted by hand.
        m = _re.search(r"line no\.?\s*(\d+)", str(exc), _re.I)
        if m and metas:
            n = int(m.group(1))
            hints = []
            for idx in dict.fromkeys(i for i in (n - 1, n) if 0 <= i < len(metas)):
                c = metas[idx]
                hints.append(f"entry {idx + 1}: {c['side']} {c['amount']} to "
                             f"{c['account']}"
                             + (f" (dept {c['department']})" if c["department"]
                                else " (NO department)")
                             + f" — {c['memo'][:70]}")
            raise RuntimeError(
                f"{exc}\nThat line is one of these:\n  " + "\n  ".join(hints)
                + "\nFix that line's coding in the review and re-post.") from exc
        raise
    record_no = ""
    data = root.find(".//result/data")
    if data is not None:
        for row in data:
            record_no = row.findtext("RECORDNO", "") or record_no
    return {"posted_via": "xml_gateway", "journal": payload["journal"],
            "record_no": record_no}


def post_journal_entry(payload: dict) -> dict:
    """POST the journal entry to Sage Intacct. Prefers the XML gateway (the
    credential style this company actually has — same as every working Sage
    read); the REST path below remains for orgs with a registered app.
    Raises with the full response body on failure."""
    import requests

    from ..recon import sage_xml
    if sage_xml.credentials_present():
        return _post_via_xml(payload)

    required = [
        "INTACCT_CLIENT_ID", "INTACCT_CLIENT_SECRET", "INTACCT_COMPANY_ID",
        "INTACCT_USER_ID", "INTACCT_USER_PASSWORD",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Sage Intacct credentials missing: set the INTACCT_SENDER_* (XML "
            "gateway) variables, or for REST: " + ", ".join(missing)
            + ". Add them to .env (see .env.example)."
        )

    token = _get_token()
    body = _to_rest_body(payload)
    try:
        resp = requests.post(
            _JE_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "company-id": os.environ.get("INTACCT_COMPANY_ID", "").strip(),
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Sage Intacct journal entry POST failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Sage Intacct journal entry POST failed: HTTP {resp.status_code}\n{resp.text[:2000]}\n\n"
            "See sage_intacct.py's module docstring — this is a first-pass field "
            "mapping; paste this error back to get _to_rest_body() corrected."
        )
    return resp.json()
