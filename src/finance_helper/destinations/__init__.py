"""Destination adapters: turn a categorized document into a posted entry."""

from __future__ import annotations

from ..models import SourceDocument
from . import billdotcom, sage_intacct


def build_payload(doc: SourceDocument) -> dict:
    """Build the destination-specific payload without posting."""
    if doc.destination == "sage":
        return sage_intacct.build_journal_entry(doc)
    if doc.destination == "bill":
        return billdotcom.build_bill(doc)
    raise ValueError(f"Unknown destination '{doc.destination}'")


def post(doc: SourceDocument, payload: dict) -> dict:
    """Actually send the payload. Raises if credentials are missing."""
    if doc.destination == "sage":
        return sage_intacct.post_journal_entry(payload)
    if doc.destination == "bill":
        return billdotcom.post_bill(payload)
    raise ValueError(f"Unknown destination '{doc.destination}'")
