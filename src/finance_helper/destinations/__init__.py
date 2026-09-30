"""Destination adapters: turn a categorized document into a posted entry."""

from __future__ import annotations

from ..models import SourceDocument
from . import billdotcom, sage_intacct


def build_payload(doc: SourceDocument) -> dict:
    """Build the destination-specific payload without posting."""
    if doc.destination == "sage":
        if sage_intacct.je_style(doc.source) == "bank_offset":
            # One JE per bill (preview/download shape; posting goes bill by bill).
            return {"style": "bank_offset",
                    "entries": [p for _, p in sage_intacct.build_bank_offset_entries(doc)]}
        return sage_intacct.build_journal_entry(doc)
    if doc.destination == "bill":
        return billdotcom.build_bill(doc)
    raise ValueError(f"Unknown destination '{doc.destination}'")


def build_batches(doc: SourceDocument) -> list[tuple[list, dict]]:
    """[(lines, payload), ...] to post — one per journal entry. Bank-offset
    sources post one entry per bill; everything else is a single entry."""
    if doc.destination == "sage" and sage_intacct.je_style(doc.source) == "bank_offset":
        return sage_intacct.build_bank_offset_entries(doc)
    return [(list(doc.line_items), build_payload(doc))]


def post(doc: SourceDocument, payload: dict) -> dict:
    """Actually send the payload. Raises if credentials are missing."""
    if doc.destination == "sage":
        return sage_intacct.post_journal_entry(payload)
    if doc.destination == "bill":
        return billdotcom.post_bill(payload)
    raise ValueError(f"Unknown destination '{doc.destination}'")
