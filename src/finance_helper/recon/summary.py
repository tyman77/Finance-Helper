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


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def flow_chart(months: list[dict], width: int = 720, height: int = 230) -> dict:
    """Grouped monthly columns: cash in vs cash out (two fixed-hue series).

    Geometry only — the template renders dumb SVG, same pattern as
    insights.monthly_chart.
    """
    pad_l, pad_r, pad_t, pad_b = 52, 8, 8, 22
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    peak = max((max(float(m["in"]), float(-m["out"])) for m in months), default=0.0) or 1.0
    step = 10 ** max(len(str(int(peak))) - 1, 1)
    top = ((int(peak) // step) + 1) * step

    n = max(len(months), 1)
    slot = plot_w / n
    bar_w = min(max(slot * 0.28, 6), 34)

    columns = []
    for i, m in enumerate(months):
        cx = pad_l + slot * i + slot / 2
        bars = []
        for series, value, offset in (("in", float(m["in"]), -bar_w - 1),
                                      ("out", float(-m["out"]), 1)):
            h = value / top * plot_h
            bars.append({"series": series, "value": value,
                         "x": round(cx + offset, 1), "w": round(bar_w, 1),
                         "y": round(pad_t + plot_h - h, 1), "h": round(h, 1)})
        y_, mo_ = m["month"].split("-")
        columns.append({"label": f"{_MONTH_ABBR[int(mo_) - 1]} {y_}",
                        "cx": round(cx, 1), "bars": bars})

    ticks = [{"y": round(pad_t + plot_h * (1 - f), 1), "label": f"{top * f:,.0f}"}
             for f in (0, 0.5, 1)]
    return {"width": width, "height": height, "columns": columns, "ticks": ticks,
            "baseline_y": round(pad_t + plot_h, 1), "pad_l": pad_l, "pad_r": pad_r}


def tie_stats(bank: list[Txn]) -> dict:
    """Share of posted outflow dollars that tie (or are internal/pending).

    Intercompany outflow (external-entity carve-outs) is reported on its own:
    it can't tie because those books aren't in Sage, so counting it either
    way would distort the percentage that IS provable.
    """
    out_total = Decimal("0")
    out_tied = Decimal("0")
    out_intercompany = Decimal("0")
    for t in bank:
        if t.pending or t.amount >= 0:
            continue
        if t.status == "intercompany":
            out_intercompany += -t.amount
            continue
        out_total += -t.amount
        if t.status in ("tied", "internal"):
            out_tied += -t.amount
    pct = (out_tied / out_total * 100) if out_total else Decimal("0")
    return {"out_total": out_total, "out_tied": out_tied,
            "out_intercompany": out_intercompany,
            "tied_pct": pct.quantize(Decimal('0.1'))}


def book_vs_bank_drift(bank: list[Txn], ledger: list[Txn],
                       cash_accounts: set[str]) -> dict:
    """Net change per the books' cash account vs per the bank statement.

    If entries are missing from the cash GL (e.g. payroll pulls whose JE
    never credits cash), the book net change runs higher than the bank's by
    exactly the missing amount — a one-number tell no matching can fake.
    """
    bank_net = sum((t.amount for t in bank if not t.pending), Decimal("0"))
    ledger_net = sum((t.amount for t in ledger
                      if not cash_accounts or t.account_ref in cash_accounts),
                     Decimal("0"))
    return {"bank_net": bank_net, "ledger_net": ledger_net,
            "drift": ledger_net - bank_net}
