"""Aggregate bank txns into the friendly numbers the Cash Proof page shows.

Pure computation, no Flask — same pattern as insights.py.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import Txn

KIND_LABELS = {
    "ramp_reimbursement": "Ramp reimbursements",
    "ramp_settlement": "Ramp card settlements",
    "billcom": "Bill.com",
    "payroll": "Payroll",
    "sweep": "Sweep transfers (internal)",
    "wire_out": "Wires out",
    "wire_in": "Wires in",
    "check": "Checks",
    "deposit": "Deposits",
    "interest": "Interest",
    "fee": "Fees & taxes",
    "ach_debit": "Other ACH out",
    "ach_credit": "Other ACH in",
    "other": "Other",
}


def build(bank: list[Txn]) -> dict:
    posted = [t for t in bank if not t.pending]
    months: dict[str, dict] = defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})
    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": Decimal("0")})
    outflow: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": Decimal("0")})

    for t in posted:
        m = t.posted_date.strftime("%Y-%m")
        months[m]["out" if t.amount < 0 else "in"] += t.amount
        b = buckets[t.kind]
        b["count"] += 1
        b["total"] += t.amount
        if t.amount < 0 and t.kind != "sweep":
            key = t.counterparty_norm or t.counterparty_raw.lower()
            o = outflow[key]
            o["count"] += 1
            o["total"] += t.amount
            o["label"] = t.counterparty_raw[:48]

    total_in = sum((m["in"] for m in months.values()), Decimal("0"))
    total_out = sum((m["out"] for m in months.values()), Decimal("0"))
    return {
        "months": [{"month": m, **v} for m, v in sorted(months.items())],
        "buckets": sorted(
            ({"kind": k, "label": KIND_LABELS.get(k, k), **v} for k, v in buckets.items()),
            key=lambda b: b["total"]),
        "top_outflows": sorted(outflow.values(), key=lambda o: o["total"])[:10],
        "total_in": total_in,
        "total_out": total_out,
        "txn_count": len(posted),
    }


def tie_stats(bank: list[Txn]) -> dict:
    """Share of posted outflow dollars that tie (or are internal/pending)."""
    out_total = Decimal("0")
    out_tied = Decimal("0")
    for t in bank:
        if t.pending or t.amount >= 0:
            continue
        out_total += -t.amount
        if t.status in ("tied", "internal"):
            out_tied += -t.amount
    pct = (out_tied / out_total * 100) if out_total else Decimal("0")
    return {"out_total": out_total, "out_tied": out_tied, "tied_pct": pct.quantize(Decimal('0.1'))}
