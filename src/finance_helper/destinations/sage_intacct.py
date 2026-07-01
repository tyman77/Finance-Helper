"""Sage Intacct destination — builds and posts a General Ledger journal entry.

Docs:
  https://developer.sage.com/intacct/docs/1/sage-intacct-rest-api/get-started
  https://developer.intacct.com/api/general-ledger/journal-entries/

A journal entry must balance. We debit each categorized expense line to its GL
account and post a single offsetting credit to INTACCT_CLEARING_ACCOUNT (your AP
or a clearing account) for the total. Confirm the exact field names / dimensions
(location, department, journal symbol) against your Intacct company setup before
going live.
"""

from __future__ import annotations

import os

from ..models import SourceDocument

# The GL journal to post into (a.k.a. journal symbol). "GJ" = General Journal is
# a common default; change to match your Intacct setup.
_JOURNAL_SYMBOL = os.environ.get("INTACCT_JOURNAL_SYMBOL", "GJ")


def build_journal_entry(doc: SourceDocument) -> dict:
    clearing = os.environ.get("INTACCT_CLEARING_ACCOUNT", "<INTACCT_CLEARING_ACCOUNT>")
    entry_date = (doc.document_date.isoformat() if doc.document_date else None)

    lines = []
    for li in doc.line_items:
        lines.append(
            {
                "account_no": li.gl_account,
                "debit": str(li.amount),
                "credit": "0",
                "memo": f"{doc.vendor}: {li.description}",
                "category": li.category,
            }
        )
    # Balancing credit for the full total.
    lines.append(
        {
            "account_no": clearing,
            "debit": "0",
            "credit": str(doc.total),
            "memo": f"{doc.vendor} {doc.document_id}",
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


def post_journal_entry(payload: dict) -> dict:
    """POST the journal entry to Sage Intacct.

    Not yet wired to live HTTP — fill in once you have sandbox credentials. Raises
    a clear error so nothing silently no-ops.
    """
    required = [
        "INTACCT_SENDER_ID",
        "INTACCT_COMPANY_ID",
        "INTACCT_USER_ID",
        "INTACCT_USER_PASSWORD",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Sage Intacct credentials missing: "
            + ", ".join(missing)
            + ". Add them to .env (see .env.example)."
        )
    raise NotImplementedError(
        "Live Sage Intacct posting is not implemented yet. Credentials are "
        "present; the next step is wiring the REST call + session auth against "
        "your sandbox. Until then, run without --approve to review the payload."
    )
