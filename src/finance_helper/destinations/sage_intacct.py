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
    clearing = os.environ.get("INTACCT_CLEARING_ACCOUNT", "<INTACCT_CLEARING_ACCOUNT>")
    entry_date = (doc.document_date.isoformat() if doc.document_date else None)

    lines = []
    for li in doc.line_items:
        # Positive amounts are expenses (debit); negatives are refunds/credits
        # and post as a credit to the same account rather than a negative debit.
        debit = li.amount if li.amount >= 0 else 0
        credit = -li.amount if li.amount < 0 else 0
        line = {
            "account_no": li.gl_account,
            "debit": str(debit),
            "credit": str(credit),
            "memo": f"{doc.vendor}: {li.description}"[:200],
            "category": li.category,
        }
        # Intacct dimensions, when we learned them via enrichment.
        if li.department:
            line["department"] = li.department.split("--")[0].strip()
        if li.project:
            line["project"] = li.project
        lines.append(line)
    # Offsetting line to the clearing/AP account for the net total. If the net is
    # a credit (a net refund), flip it to a debit so the entry still balances.
    net = doc.total
    lines.append(
        {
            "account_no": clearing,
            "debit": str(-net if net < 0 else 0),
            "credit": str(net if net >= 0 else 0),
            "memo": f"{doc.vendor} {doc.document_id}"[:200],
        }
    )

    return {
        "journal": _JOURNAL_SYMBOL,
        "date": entry_date,
        "reference_no": doc.document_id,
        "description": f"{doc.vendor} — {doc.document_id}",
        "currency": doc.currency,
        "lines": lines,
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
    data = {"grant_type": "client_credentials"}
    if os.environ.get("INTACCT_USER_ID"):
        data["username"] = os.environ["INTACCT_USER_ID"]
    if os.environ.get("INTACCT_USER_PASSWORD"):
        data["password"] = os.environ["INTACCT_USER_PASSWORD"]
    try:
        resp = requests.post(
            _TOKEN_URL,
            auth=(os.environ["INTACCT_CLIENT_ID"], os.environ["INTACCT_CLIENT_SECRET"]),
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


def post_journal_entry(payload: dict) -> dict:
    """POST the journal entry to Sage Intacct. Raises with the full response
    body on failure — see the module docstring if this is your first live test."""
    import requests

    required = [
        "INTACCT_CLIENT_ID", "INTACCT_CLIENT_SECRET", "INTACCT_COMPANY_ID",
        "INTACCT_USER_ID", "INTACCT_USER_PASSWORD",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Sage Intacct credentials missing: " + ", ".join(missing) + ". Add them to .env (see .env.example)."
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
                "company-id": os.environ.get("INTACCT_COMPANY_ID", ""),
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
