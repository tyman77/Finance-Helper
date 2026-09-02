"""Read an invoice attachment with Claude and return the fields Bill Check compares.

The read is deliberately blind: the values the clerk entered in Bill.com are
never shown to the model, so it cannot anchor on them — it reports what the
document says and the comparison happens in compare.py. Structured output
pins the shape; dates come back ISO, the total as a decimal string.

Model is env-overridable (BILLCHECK_MODEL, default claude-opus-5); effort
too (BILLCHECK_EFFORT, default medium — reading a one-page invoice does
not need deep reasoning, and volume is the whole point here).
"""

from __future__ import annotations

import base64
import os
from typing import Literal, Optional

from pydantic import BaseModel

DEFAULT_MODEL = "claude-opus-5"
MAX_BYTES = 30 * 1024 * 1024          # API request ceiling is 32 MB
# Bump when InvoiceFields gains something the comparison depends on; reads
# stored under an older number are refreshed on the next run.
SCHEMA_VERSION = 4

SYSTEM_PROMPT = """You read vendor invoices for an accounts-payable team. Report only what the document actually states; use null for anything not printed on it. Do not guess.

Field rules:
- invoice_date: the date the invoice was issued. Not the order date, ship date, service date, or statement date.
- due_date: ONLY a date the document explicitly states as due / pay by / payment due. Never compute one from terms.
- terms: the payment terms exactly as printed (e.g. "Net 30", "Due upon receipt", "2% 10 Net 30"). terms_days: the net days as an integer (0 for due on receipt); null if no terms are printed.
- total: the full amount due on this invoice, after credits already applied on the document, BEFORE any early-payment discount. Digits with two decimals, no currency symbol or thousands separators, negative for a credit.
- Early-payment discount — look everywhere on the document: terms lines ("2% 10 Net 30", "3/30, NET 31" meaning 3% off within 30 days, "5% 25 Days"), remittance stubs ("Amount due w/discount", "Quick Pay total … pay by …"), and free-text notes ("Please deduct $18.36 if payment received by 09/18/2026"). discount_total is the reduced amount payable if paid early — when only a deduction amount or a percentage is printed, compute it (total minus the deduction, or total less the percentage) and say so in notes. discount_date is the last day the reduced amount is accepted (YYYY-MM-DD) if printed; discount_days the days from the invoice date the offer allows (10 for "2% 10 Net 30", 30 for "3/30, NET 31"); discount_terms the offer as printed. All null when no such offer appears anywhere. Never put the discounted amount in total.
- currency: the ISO code if stated or clearly implied, else null.
- vendor: the business issuing the invoice (the "from"/remit-to party), not the customer being billed.
- invoice_number: exactly as printed.
- po_number: the customer PO / reference number if printed.
- order_number: the vendor's own order / sales-order reference if printed ("S.O. #", "Order No", "Sales Order"); null if absent. Distinct from the customer PO.
- ship_date: the date goods shipped, if printed (YYYY-MM-DD); some vendors' payment terms run from it. Null if absent.
- is_invoice: false for statements, purchase orders, quotes, receipts, packing slips, credit memos, or anything that is not a bill for payment.
- confidence: low when the scan is unreadable, fields are hand-written, or several invoices/amounts compete.
- notes: anything a reviewer should know — multiple invoices in one file, a past-due balance included in the total, hand-written changes, missing pages."""


class InvoiceFields(BaseModel):
    is_invoice: bool
    vendor: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str]
    due_date: Optional[str]
    terms: Optional[str]
    terms_days: Optional[int]
    total: Optional[str]
    discount_total: Optional[str]
    discount_date: Optional[str]
    discount_days: Optional[int]
    discount_terms: Optional[str]
    currency: Optional[str]
    po_number: Optional[str]
    ship_date: Optional[str] = None
    order_number: Optional[str] = None
    confidence: Literal["high", "medium", "low"]
    notes: str


def credentials_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.environ.get("BILLCHECK_MODEL") or DEFAULT_MODEL


def _client():
    import anthropic
    return anthropic.Anthropic()


def content_blocks(documents: list[dict]) -> list[dict]:
    blocks = []
    total = 0
    for d in documents:
        data = d["data"]
        total += len(data)
        if total > MAX_BYTES:
            raise RuntimeError("Attachment is too large to read "
                               f"({total / 1024 / 1024:.0f} MB; limit is 30 MB).")
        b64 = base64.standard_b64encode(data).decode("ascii")
        media = d.get("media_type") or ""
        if media == "application/pdf":
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": b64}})
        elif media in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": media, "data": b64}})
        else:
            raise RuntimeError(f"Unsupported attachment type {media or 'unknown'}"
                               f" ({d.get('name', 'attachment')}) — PDF or image only.")
    blocks.append({"type": "text", "text":
                   "Extract the invoice fields from the attached document."})
    return blocks


def extract_invoice(documents: list[dict], client=None) -> dict:
    """documents: [{name, media_type, data(bytes)}] → dict of InvoiceFields
    plus model/usage. Raises RuntimeError with a plain reason on any failure."""
    if not documents:
        raise RuntimeError("No attachment to read.")
    if not credentials_present():
        raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example).")
    blocks = content_blocks(documents)
    client = client or _client()
    resp = None
    for attempt in range(3):
        try:
            resp = client.messages.parse(
                model=model_name(),
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                output_config={"effort": os.environ.get("BILLCHECK_EFFORT") or "medium"},
                messages=[{"role": "user", "content": blocks}],
                output_format=InvoiceFields,
            )
            break
        except Exception as exc:                   # SDK errors carry the reason
            # The structured-output grammar is compiled server-side and can
            # time out transiently; that is worth a couple of retries. The
            # SDK already retries 429/5xx itself.
            if "Grammar compilation timed out" in str(exc) and attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Claude read failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
    if getattr(resp, "stop_reason", None) == "refusal":
        details = getattr(resp, "stop_details", None)
        why = getattr(details, "explanation", None) or "no reason given"
        raise RuntimeError(f"Claude declined to read this attachment ({why}).")
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        raise RuntimeError("Claude returned no structured fields for this attachment.")
    out = parsed.model_dump()
    usage = getattr(resp, "usage", None)
    out["model"] = getattr(resp, "model", None) or model_name()
    out["usage"] = {"input": getattr(usage, "input_tokens", None),
                    "output": getattr(usage, "output_tokens", None)}
    return out
