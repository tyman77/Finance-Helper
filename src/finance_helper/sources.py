"""Config-driven CSV ingestion.

Because all four vendors export CSV, one generic reader handles them all. Each
source's `columns` mapping (in config/sources.yml) says which CSV header holds
the date/description/amount/document id. Add a new vendor by adding config, not
code.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from . import config
from .models import LineItem, SourceDocument

# Date formats we'll try, in order.
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%m-%d-%Y")


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: Optional[str]) -> Decimal:
    if value is None or value.strip() == "":
        return Decimal("0")
    cleaned = value.strip().replace("$", "").replace(",", "")
    # Handle parenthesized negatives, e.g. "(12.50)".
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse amount: {value!r}") from exc


def load(source: str, path: str) -> SourceDocument:
    """Read `path` as `source`'s CSV and return a normalized SourceDocument."""
    cfg = config.source_config(source)
    cols = cfg["columns"]

    line_items: list[LineItem] = []
    document_id = ""
    document_date: Optional[date] = None

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        _require_columns(reader.fieldnames, cols, source)
        for row in reader:
            li = LineItem(
                description=(row.get(cols["description"], "") or "").strip(),
                amount=_parse_amount(row.get(cols["amount"])),
                date=_parse_date(row.get(cols.get("date"))),
                raw=dict(row),
            )
            line_items.append(li)
            # Use the first non-empty document id / date we see.
            if not document_id and cols.get("document_id"):
                document_id = (row.get(cols["document_id"], "") or "").strip()
            if document_date is None:
                document_date = li.date

    if not line_items:
        raise ValueError(f"No rows found in {path}")

    return SourceDocument(
        source=source,
        destination=cfg["destination"],
        vendor=cfg["vendor"],
        document_id=document_id or "(unknown)",
        currency=cfg.get("currency", "USD"),
        line_items=line_items,
        document_date=document_date,
    )


def _require_columns(fieldnames, cols: dict, source: str) -> None:
    missing = [c for c in cols.values() if c and c not in (fieldnames or [])]
    if missing:
        raise ValueError(
            f"Source '{source}' expects columns {missing} but the CSV has "
            f"{list(fieldnames or [])}. Fix the `columns` mapping in "
            f"config/sources.yml to match your export."
        )
