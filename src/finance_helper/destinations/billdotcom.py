"""Bill.com destination — builds and posts an Accounts Payable bill (v3 API).

Docs:
  https://developer.bill.com/docs/ap-bills
  https://developer.bill.com/reference/api-reference-overview

Each categorized line becomes a bill line item. `chartOfAccountId` maps to your
Bill.com chart of accounts; here we pass the GL account we categorized to and let
the caller reconcile the id mapping. Vendor lookup/creation is left as a follow-up
(see docs/NEXT_STEPS.md) — for now we pass the vendor name for review.
"""

from __future__ import annotations

import os

from ..models import SourceDocument


def build_bill(doc: SourceDocument) -> dict:
    bill_line_items = [
        {
            "amount": str(li.amount),
            "description": li.description,
            # In Bill.com this is chartOfAccountId; we surface the GL account we
            # categorized to so you can map it to the Bill.com CoA id.
            "gl_account": li.gl_account,
            "category": li.category,
        }
        for li in doc.line_items
    ]
    return {
        "vendorName": doc.vendor,
        "invoice": {
            "invoiceNumber": doc.document_id,
            "invoiceDate": doc.document_date.isoformat() if doc.document_date else None,
        },
        "billLineItems": bill_line_items,
        "total": str(doc.total),
        "currency": doc.currency,
    }


def post_bill(payload: dict) -> dict:
    """POST the bill to Bill.com.

    Not yet wired to live HTTP — fill in once you have sandbox credentials. Raises
    a clear error so nothing silently no-ops.
    """
    required = ["BILLDOTCOM_DEV_KEY", "BILLDOTCOM_USERNAME", "BILLDOTCOM_PASSWORD", "BILLDOTCOM_ORG_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Bill.com credentials missing: "
            + ", ".join(missing)
            + ". Add them to .env (see .env.example)."
        )
    raise NotImplementedError(
        "Live Bill.com posting is not implemented yet. Credentials are present; "
        "the next step is wiring the v3 login + POST /bills call against the "
        "sandbox gateway. Until then, run without --approve to review the payload."
    )
