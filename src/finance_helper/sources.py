"""Config-driven CSV ingestion.

Two real-world layouts are supported, chosen per-source via `layout` in
config/sources.yml:

  long  — one amount column; each CSV row is one line item. Optionally a whole
          file is a "statement" (e.g. a month of United tickets) that posts as a
          single entry. (United, UPS, National)

  wide  — each row is a document/booking whose total is already split across
          several charge columns (room, taxes, fees...). Each configured
          component column becomes its own categorized line item, and the room
          remainder is derived so the parts tie exactly to the row total.
          (Hotel Engine)

Add a new vendor by adding config, not code.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from . import config
from .models import LineItem, SourceDocument

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%m-%d-%Y")


def _norm(header: str) -> str:
    """Collapse whitespace so 'Customer Currency  (Charges/Credits)' matches."""
    return " ".join((header or "").split())


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: Optional[str]) -> Decimal:
    if value is None or value.strip() == "":
        return Decimal("0")
    cleaned = value.strip().replace("$", "").replace(",", "")
    # Parenthesized values are negatives/credits, e.g. "(92.27)".
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse amount: {value!r}") from exc


class _Reader:
    """Resolves config column names to real CSV headers (whitespace-tolerant)."""

    def __init__(self, source: str, fieldnames):
        self.source = source
        self._map = {_norm(h): h for h in (fieldnames or [])}
        self._fieldnames = list(fieldnames or [])

    def col(self, name: Optional[str], required: bool = True) -> Optional[str]:
        if not name:
            return None
        actual = self._map.get(_norm(name))
        if actual is None and required:
            raise ValueError(
                f"Source '{self.source}': column {name!r} not found. CSV has "
                f"{self._fieldnames}. Fix config/sources.yml."
            )
        return actual


def load(source: str, path: str) -> SourceDocument:
    cfg = config.source_config(source)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        csv_reader = csv.DictReader(fh)
        reader = _Reader(source, csv_reader.fieldnames)
        rows = list(csv_reader)
    if not rows:
        raise ValueError(f"No rows found in {path}")

    if cfg.get("layout", "long") == "wide":
        items, dates = _load_wide(cfg, reader, rows)
    else:
        items, dates = _load_long(cfg, reader, rows)

    document_id, document_date = _document_identity(cfg, reader, rows, dates)
    return SourceDocument(
        source=source,
        destination=cfg["destination"],
        vendor=cfg["vendor"],
        document_id=document_id,
        currency=cfg.get("currency", "USD"),
        line_items=items,
        document_date=document_date,
    )


def _load_long(cfg, reader, rows):
    cols = cfg["columns"]
    # Amount is a single column, or the sum of several (e.g. UPS billed + credit).
    amount_cols = cols.get("amount_columns")
    amount_hs = ([reader.col(c) for c in amount_cols] if amount_cols
                 else [reader.col(cols["amount"])])
    date_h = reader.col(cols.get("date"), required=False)
    desc_spec = cols["description"]
    desc_hs = [reader.col(c) for c in (desc_spec if isinstance(desc_spec, list) else [desc_spec])]

    items, dates = [], []
    for row in rows:
        d = _parse_date(row.get(date_h)) if date_h else None
        desc = " ".join(row.get(h, "").strip() for h in desc_hs if row.get(h, "").strip())
        amount = sum((_parse_amount(row.get(h)) for h in amount_hs), Decimal("0"))
        items.append(LineItem(description=desc, amount=amount, date=d, raw=dict(row)))
        if d:
            dates.append(d)
    return items, dates


def _load_wide(cfg, reader, rows):
    total_h = reader.col(cfg["total_column"])
    date_h = reader.col(cfg.get("date_column"), required=False)
    label_hs = [reader.col(c, required=False) for c in cfg.get("label_columns", [])]
    remainder_label = cfg.get("remainder_label", "Room charge")

    items, dates = [], []
    for row in rows:
        label = " / ".join(row.get(h, "").strip() for h in label_hs if h and row.get(h, "").strip())
        total = _parse_amount(row.get(total_h))
        component_sum = Decimal("0")
        row_items = []
        for comp in cfg["components"]:
            h = reader.col(comp["column"], required=False)
            if h is None:
                continue
            val = _parse_amount(row.get(h))
            if val == 0:
                continue
            component_sum += val
            row_items.append(
                LineItem(
                    description=f"{label} — {comp['column']}",
                    amount=val,
                    category=comp["category"],
                    raw=dict(row),
                )
            )
        # Derive the room/base charge so components tie exactly to the total.
        remainder = total - component_sum
        if remainder != 0:
            row_items.insert(
                0,
                LineItem(
                    description=f"{label} — {remainder_label}",
                    amount=remainder,
                    category=cfg["remainder_category"],
                    raw=dict(row),
                ),
            )
        items.extend(row_items)
        d = _parse_date(row.get(date_h)) if date_h else None
        if d:
            dates.append(d)
    return items, dates


def _document_identity(cfg, reader, rows, dates):
    """A statement collapses many rows into one entry; otherwise use an id column."""
    if cfg.get("statement"):
        sid_col = reader.col(cfg.get("statement_id_column"), required=False)
        sid = None
        if sid_col:
            sid = next((row.get(sid_col, "").strip() for row in rows if row.get(sid_col, "").strip()), None)
        if not sid:
            sid = f"{min(dates).isoformat()}..{max(dates).isoformat()}" if dates else "(statement)"
        return sid, (max(dates) if dates else None)

    # Document mode: first non-empty id and earliest date.
    id_h = reader.col(cfg["columns"].get("document_id"), required=False)
    doc_id = ""
    if id_h:
        doc_id = next((row.get(id_h, "").strip() for row in rows if row.get(id_h, "").strip()), "")
    return (doc_id or "(unknown)"), (min(dates) if dates else None)
