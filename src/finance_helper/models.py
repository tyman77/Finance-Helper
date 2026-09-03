"""Canonical data model shared by every source and destination.

Sources parse raw CSV rows into a `SourceDocument`. The categorizer fills in the
`gl_account` / `category` on each `LineItem`. Destinations turn the document into
their own payload (a Sage journal entry or a Bill.com bill).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass
class LineItem:
    description: str
    amount: Decimal
    date: Optional[date] = None
    # Filled in by the categorizer:
    category: Optional[str] = None
    gl_account: Optional[str] = None
    # Filled in by enrichment (dimensions):
    person: Optional[str] = None
    department: Optional[str] = None
    project: Optional[str] = None
    # Review workflow: set when a human should confirm before posting.
    needs_review: bool = False
    posted_ref: str = ""        # set once the line lands in a journal entry
    note: Optional[str] = None
    # Original CSV row, kept for audit/debugging.
    raw: dict = field(default_factory=dict)


@dataclass
class SourceDocument:
    source: str            # e.g. "ups"
    destination: str       # "sage" or "bill"
    vendor: str            # e.g. "UPS"
    document_id: str       # invoice / itinerary / agreement number
    currency: str
    line_items: list[LineItem]
    document_date: Optional[date] = None

    @property
    def total(self) -> Decimal:
        return sum((li.amount for li in self.line_items), Decimal("0"))
