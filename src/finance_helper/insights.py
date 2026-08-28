"""Aggregate saved runs into the numbers the Insights page shows.

Pure computation — no Flask. The web layer feeds it the persisted runs and
renders the result; keeping the math here makes it unit-testable.

Double-counting guard: the same vendor statement is often uploaded more than
once (re-processed after rebuilding the traveler map, etc.), so aggregation
first dedupes to ONE run per (source, document_id) — the most recently created.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

# Spend groups, in fixed display order. Slot order is also the chart's
# categorical color order (validated for the app's dark surface).
GROUPS = ["Flights", "Hotels", "Cars", "Shipping"]

# Which upload sources feed each domain page, and the fixed palette slot each
# domain owns everywhere in the UI (chart hue, KPI swatch, nav accent).
DOMAINS = {
    "flights": {"group": "Flights", "sources": ["united"], "slot": 0,
                "title": "Flights", "vendor": "United Airlines"},
    "hotels": {"group": "Hotels", "sources": ["hotel_engine"], "slot": 1,
               "title": "Hotels", "vendor": "Hotel Engine"},
    "cars": {"group": "Cars", "sources": ["national"], "slot": 2,
             "title": "Rental Cars", "vendor": "National Car Rental"},
}

_CATEGORY_GROUP = {
    "travel_airfare": "Flights",
    "travel_airfare_fees": "Flights",
    "travel_lodging": "Hotels",
    "travel_lodging_taxes": "Hotels",
    "travel_lodging_incidentals": "Hotels",
    "travel_lodging_flex": "Hotels",
    "travel_booking_fee": "Hotels",
    "travel_credits": "Hotels",  # HE credits net against hotel spend
    "travel_car_rental": "Cars",
    "shipping_freight": "Shipping",
}


def group_for(category: str | None) -> str:
    return _CATEGORY_GROUP.get(category or "", "Other")


def dedupe_runs(runs: Iterable[dict]) -> list[dict]:
    """Latest run per (source, document_id); ties broken by created."""
    best: dict[tuple, dict] = {}
    for run in runs:
        doc = run["doc"]
        key = (doc.source, doc.document_id)
        if key not in best or run["created"] > best[key]["created"]:
            best[key] = run
    return list(best.values())


def _month(d) -> str | None:
    return d.strftime("%Y-%m") if d else None


def build(runs: Iterable[dict]) -> dict:
    """All aggregates for the Insights page, from persisted runs."""
    deduped = dedupe_runs(runs)

    total = Decimal("0")
    by_group: dict[str, Decimal] = defaultdict(Decimal)
    by_month_group: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    by_project: dict[str, Decimal] = defaultdict(Decimal)
    by_person: dict[str, dict] = defaultdict(lambda: {"amount": Decimal("0"), "lines": 0})
    coded = Decimal("0")       # spend with a project assigned
    codable = Decimal("0")     # spend where a project is expected (COGS-ish travel)
    review_lines = 0
    line_count = 0

    for run in deduped:
        for li in run["doc"].line_items:
            amt = li.amount
            grp = group_for(li.category)
            total += amt
            by_group[grp] += amt
            m = _month(li.date) or _month(run["doc"].document_date)
            if m:
                by_month_group[m][grp] += amt
            if li.project:
                by_project[li.project] += amt
                coded += amt
            codable += amt
            if li.person:
                p = by_person[li.person]
                p["amount"] += amt
                p["lines"] += 1
            if li.needs_review:
                review_lines += 1
            line_count += 1

    months = sorted(by_month_group)
    projects = sorted(by_project.items(), key=lambda kv: -abs(kv[1]))
    people = sorted(by_person.items(), key=lambda kv: -abs(kv[1]["amount"]))

    return {
        "total": total,
        "by_group": {g: by_group.get(g, Decimal("0")) for g in GROUPS},
        "other": by_group.get("Other", Decimal("0")),
        "months": months,
        "by_month_group": {m: dict(by_month_group[m]) for m in months},
        "projects": projects,
        "people": [(name, v["amount"], v["lines"]) for name, v in people],
        "coded_pct": float(coded / codable * 100) if codable else 0.0,
        "review_lines": review_lines,
        "line_count": line_count,
        "run_count": len(deduped),
    }


def build_domain(run_items: list[tuple], domain_key: str) -> dict:
    """Everything one domain page (Flights / Hotels / Rental Cars) shows.

    run_items is (run_id, run) pairs so the page can link back to each
    statement's review screen. Lines are included when their category maps to
    the domain's group OR the whole statement came from one of the domain's
    sources (so uncategorized lines of a United upload still count as Flights).
    """
    dom = DOMAINS[domain_key]
    group, sources = dom["group"], set(dom["sources"])

    deduped = dedupe_runs([r for _, r in run_items])
    id_by_run = {id(run): rid for rid, run in run_items}
    kept = {id(r) for r in deduped}

    total = Decimal("0")
    by_month: dict[str, Decimal] = defaultdict(Decimal)
    by_project: dict[str, Decimal] = defaultdict(Decimal)
    by_person: dict[str, dict] = defaultdict(lambda: {"amount": Decimal("0"), "lines": 0})
    flagged = 0
    line_count = 0
    statements = []

    for rid, run in run_items:
        if id(run) not in kept:
            continue
        doc = run["doc"]
        from_source = doc.source in sources
        stmt_amount = Decimal("0")
        touched = False
        for li in doc.line_items:
            if not (from_source or group_for(li.category) == group):
                continue
            touched = True
            amt = li.amount
            total += amt
            stmt_amount += amt
            line_count += 1
            m = _month(li.date) or _month(doc.document_date)
            if m:
                by_month[m] += amt
            if li.project:
                by_project[li.project] += amt
            if li.person:
                p = by_person[li.person]
                p["amount"] += amt
                p["lines"] += 1
            if li.needs_review:
                flagged += 1
        if touched and from_source:
            statements.append({
                "run_id": id_by_run.get(id(run)), "filename": run.get("filename", ""),
                "document_id": doc.document_id, "total": stmt_amount,
                "created": run.get("created"),
                "posted": bool((run.get("posted") or {}).get("ok")),
            })

    from datetime import datetime as _dt
    statements.sort(key=lambda s: s["created"] or _dt.min, reverse=True)
    months = sorted(by_month)
    return {
        "title": dom["title"], "vendor": dom["vendor"], "slot": dom["slot"],
        "group": group, "sources": dom["sources"],
        "total": total, "line_count": line_count, "flagged": flagged,
        "months": months,
        "by_month_group": {m: {group: by_month[m]} for m in months},
        "projects": sorted(by_project.items(), key=lambda kv: -abs(kv[1])),
        "people": sorted(((n, v["amount"], v["lines"]) for n, v in by_person.items()),
                         key=lambda t: -abs(t[1])),
        "statements": statements,
    }


# ---------------------------------------------------------------------------
# Chart geometry (computed here so the template stays dumb SVG)

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def monthly_chart(months: list[str], by_month_group: dict, groups: list[str],
                  width: int = 720, height: int = 240) -> dict:
    """Stacked columns: x/y/width/height per segment, plus y-axis ticks.

    Negative segments (credits) are netted within their group per month before
    stacking, so a month's column height is its net spend.
    """
    pad_l, pad_r, pad_t, pad_b = 46, 8, 8, 22
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    net = {
        m: {g: max(float(by_month_group.get(m, {}).get(g, 0)), 0.0) for g in groups}
        for m in months
    }
    peak = max((sum(v.values()) for v in net.values()), default=0.0) or 1.0
    # Round the axis top up to a clean step.
    step = 10 ** max(len(str(int(peak))) - 1, 1)
    top = ((int(peak) // step) + 1) * step

    n = max(len(months), 1)
    slot = plot_w / n
    bar_w = min(max(slot * 0.55, 8), 64)

    columns = []
    for i, m in enumerate(months):
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = pad_t + plot_h
        segs = []
        for gi, g in enumerate(groups):
            v = net[m][g]
            if v <= 0:
                continue
            h = v / top * plot_h
            y -= h
            segs.append({"x": round(x, 1), "y": round(y, 1), "w": round(bar_w, 1),
                         "h": round(h, 1), "group": g, "slot": gi, "value": v})
        y_, mo_ = m.split("-")
        label = f"{_MONTH_ABBR[int(mo_) - 1]} {y_}"
        columns.append({"month": m, "label": label, "segments": segs,
                        "total": sum(net[m].values()),
                        "cx": round(x + bar_w / 2, 1)})

    ticks = []
    for frac in (0, 0.5, 1):
        ticks.append({"y": round(pad_t + plot_h * (1 - frac), 1),
                      "label": f"{top * frac:,.0f}"})
    return {"width": width, "height": height, "columns": columns, "ticks": ticks,
            "baseline_y": round(pad_t + plot_h, 1), "pad_l": pad_l,
            "pad_r": pad_r}


def hbar_chart(items: list[tuple], width: int = 720, row_h: int = 30,
               label_w: int = 170) -> dict:
    """Horizontal magnitude bars (single hue): one row per (label, value)."""
    vals = [abs(float(v)) for _, v in items] or [1.0]
    peak = max(vals) or 1.0
    plot_w = width - label_w - 90  # room for the value label at the right
    rows = []
    for i, (label, v) in enumerate(items):
        w = abs(float(v)) / peak * plot_w
        rows.append({"y": i * row_h, "label": label, "value": float(v),
                     "x": label_w, "w": round(max(w, 2), 1), "h": row_h - 10})
    return {"width": width, "height": max(len(items) * row_h, row_h), "rows": rows}
