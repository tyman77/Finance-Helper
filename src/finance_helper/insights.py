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


# Cost-component labels for the Hotels report, in display order.
_HOTEL_COMPONENTS = [
    ("travel_lodging", "Room"),
    ("travel_lodging_taxes", "Taxes & fees"),
    ("travel_lodging_incidentals", "Incidentals"),
    ("travel_lodging_flex", "Flex"),
    ("travel_booking_fee", "Booking fees"),
    ("travel_credits", "Credits redeemed"),
    ("travel_airfare", "Flights (via HE)"),
    ("travel_airfare_fees", "Flight fees (via HE)"),
    ("travel_car_rental", "Cars (via HE)"),
]


def _num(value) -> Decimal:
    text = str(value or "").replace(",", "").replace("$", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return Decimal(text or "0")
    except Exception:
        return Decimal("0")


# Guest-list export column candidates (Hotel Engine's trips/guests report —
# separate from the billing statement, which carries no guest names).
_GUEST_ID_COLS = ("Invoice Number", "Confirmation Number", "Confirmation", "Booking ID",
                  "Itinerary Number", "Reservation ID", "Trip ID")
_GUEST_NAME_COLS = ("Guest", "Guest Name", "Traveler", "Traveler Name", "Primary Guest",
                    "Guests", "Guest(s)", "Name")
_GUEST_COUNT_COLS = ("Guests", "Guest Count", "Number of Guests", "Occupancy", "Adults")
_ROOM_COUNT_COLS = ("Rooms", "Room Count", "Number of Rooms")


def _first_col(row: dict, candidates: tuple) -> str:
    for c in candidates:
        v = str(row.get(c) or "").strip()
        if v:
            return v
    return ""


def _split_names(cell: str) -> list[str]:
    """Split a multi-guest cell without breaking "Last, First" single names.

    ";", "&", and " and " always separate. A comma separates only when every
    comma-part looks like a full name (contains a space) — "Jake Cody, Natalie
    Brady" splits, "Cody, Jake" stays one person.
    """
    text = cell.replace("&", ";").replace(" and ", ";")
    parts = [p.strip() for p in text.split(";") if p.strip()]
    out: list[str] = []
    for part in parts:
        commas = [c.strip() for c in part.split(",") if c.strip()]
        if len(commas) > 1 and all(" " in c for c in commas):
            out.extend(commas)
        else:
            out.append(part)
    return out


def statement_occupants(raw: dict) -> tuple[list[str], int, int]:
    """(guest names, guest count, room count) straight from a statement row.

    Real Hotel Engine exports vary; rather than pin one header, any column
    whose name says guest/traveler-ish is read — text cells as names, numeric
    cells as counts, Single/Double words as counts of 1/2.
    """
    names: list[str] = []
    count = rooms = 0
    for key, value in raw.items():
        kl = (key or "").lower().strip()
        val = str(value or "").strip()
        if not val or "hotel" in kl:
            continue
        if "room" in kl and val.isdigit():
            rooms = max(rooms, int(val))
            continue
        guestish = ("guest" in kl or "traveler" in kl
                    or kl in ("booked for", "booked by", "employee", "attendee", "occupancy"))
        if not guestish:
            continue
        low = val.lower()
        if low in ("single", "double"):
            count = max(count, 1 if low == "single" else 2)
        elif val.replace(".", "", 1).isdigit():
            count = max(count, int(float(val)))
        else:
            for n in _split_names(val):
                if n not in names:
                    names.append(n)
    return names, count, rooms


def build_guest_index(rows: list[dict]) -> dict:
    """Hotel Engine guest/trips CSV -> {booking id: {guests: [...], rooms, count}}.

    Multiple rows for one booking accumulate guests. Raises with the columns
    found when nothing maps, so a mismatched export is a one-line fix here.
    """
    index: dict[str, dict] = {}
    for row in rows:
        bid = _first_col(row, _GUEST_ID_COLS)
        if not bid:
            continue
        entry = index.setdefault(bid, {"guests": [], "rooms": 0, "count": 0})
        name = _first_col(row, _GUEST_NAME_COLS)
        for part in _split_names(name):
            if part not in entry["guests"]:
                entry["guests"].append(part)
        entry["rooms"] = max(entry["rooms"], int(_num(_first_col(row, _ROOM_COUNT_COLS) or 0)))
        entry["count"] = max(entry["count"], int(_num(_first_col(row, _GUEST_COUNT_COLS) or 0)))
    if rows and not index:
        raise ValueError(
            "No booking ids recognized in that file. Columns present: "
            + ", ".join(sorted((rows[0] or {}).keys()))
            + f" — expected one of {_GUEST_ID_COLS}.")
    return index


def occupancy_label(guests: int, rooms: int) -> str:
    if rooms > 1:
        return f"{rooms} rooms"
    if guests <= 0:
        return ""
    return {1: "Single", 2: "Double"}.get(guests, f"{guests} guests")


def _parse_mdy(value: str):
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _infer_travelers(booking: dict, flight_docs: list) -> list[str]:
    """Likely guests for a stay, from United flights that overlap it.

    Conservative: a flight only counts when its departure falls inside the
    stay window (±2 days) AND its project or department agrees with the
    booking's — dates alone are too weak to name a person.
    """
    from datetime import timedelta

    from .enrich import _HE_DEPARTMENTS

    start = _parse_mdy(booking["start"])
    end = _parse_mdy(booking["end"]) or start
    if start is None:
        return []
    lo, hi = start - timedelta(days=2), end + timedelta(days=2)
    dept_id = None
    dn = booking["department"].lower()
    for key, did in _HE_DEPARTMENTS.items():
        if key in dn:
            dept_id = did
            break

    names: list[str] = []
    for doc in flight_docs:
        for li in doc.line_items:
            if not li.person or li.date is None or not (lo <= li.date <= hi):
                continue
            same_project = booking["project"] and li.project and booking["project"] == li.project
            same_dept = dept_id and li.department and li.department.split("--")[0] == dept_id
            if (same_project or same_dept) and li.person not in names:
                names.append(li.person)
    return names


def hotels_detail(docs: list, flight_docs: list | None = None,
                  guest_index: dict | None = None) -> dict:
    """The booking-level Hotels report: nights, rates, hotels, cities,
    departments, cost composition, and one row per booking.

    Hotel Engine's wide layout splits each booking (CSV row) into several
    categorized line items that share the raw row; Invoice Number stitches
    them back into bookings.
    """
    by_component: dict[str, Decimal] = defaultdict(Decimal)
    bookings: dict[str, dict] = {}
    by_dept: dict[str, Decimal] = defaultdict(Decimal)
    by_hotel: dict[str, dict] = defaultdict(lambda: {"spend": Decimal("0"), "stays": set()})
    by_city: dict[str, dict] = defaultdict(lambda: {"spend": Decimal("0"), "nights": 0,
                                                    "stays": set()})

    for doc in docs:
        for li in doc.line_items:
            raw = li.raw
            inv = str(raw.get("Invoice Number") or "").strip()
            if not inv:
                continue
            by_component[li.category or "other"] += li.amount
            dept = (raw.get("Department Name") or "").strip() or "(none)"
            by_dept[dept] += li.amount

            b = bookings.get(inv)
            if b is None:
                nights = int(_num(raw.get("Nights")))
                st_guests, st_count, st_rooms = statement_occupants(raw)
                b = bookings[inv] = {
                    "guests": st_guests, "_count": st_count, "_rooms": st_rooms,
                    "invoice": inv,
                    "type": (raw.get("Invoice Type") or "").strip() or "Hotel",
                    "hotel": (raw.get("Hotel Name") or "").strip(),
                    "city": (raw.get("Hotel City") or "").strip(),
                    "start": (raw.get("Start Date") or "").strip(),
                    "end": (raw.get("End Date") or "").strip(),
                    "nights": nights,
                    "rate": _num(raw.get("Average Nightly Rate w/out Taxes and Fees")),
                    "department": dept,
                    "project": li.project or (raw.get("Project Name") or "").strip(),
                    "total": Decimal("0"),
                    "flagged": False,
                }
            b["total"] += li.amount
            b["flagged"] = b["flagged"] or li.needs_review
            if li.project and not b["project"]:
                b["project"] = li.project

    for b in bookings.values():
        if b["hotel"]:
            h = by_hotel[b["hotel"]]
            h["spend"] += b["total"]
            h["stays"].add(b["invoice"])
        if b["city"]:
            c = by_city[b["city"]]
            c["spend"] += b["total"]
            c["nights"] += b["nights"]
            c["stays"].add(b["invoice"])

    hotel_stays = [b for b in bookings.values() if b["type"].lower() == "hotel"]
    total_nights = sum(b["nights"] for b in hotel_stays)
    room_spend = by_component.get("travel_lodging", Decimal("0"))
    ordered = sorted(bookings.values(), key=lambda b: b["start"], reverse=True)

    guest_index = guest_index or {}
    by_traveler: dict[str, dict] = defaultdict(lambda: {"spend": Decimal("0"), "stays": 0})
    for b in ordered:
        entry = guest_index.get(b["invoice"], {})
        for g in entry.get("guests", []):
            if g not in b["guests"]:
                b["guests"].append(g)
        guests_n = max(len(b["guests"]), b.pop("_count", 0), entry.get("count") or 0)
        rooms_n = max(b.pop("_rooms", 0), entry.get("rooms") or 0)
        b["occupancy"] = occupancy_label(guests_n, rooms_n)
        b["inferred"] = ([] if b["guests"] else
                         _infer_travelers(b, flight_docs or []))
        if b["guests"]:
            share = b["total"] / len(b["guests"])   # co-stays split evenly
            for g in b["guests"]:
                t = by_traveler[g]
                t["spend"] += share
                t["stays"] += 1

    return {
        "bookings": ordered,
        "booking_count": len(bookings),
        "total_nights": total_nights,
        "avg_rate": (room_spend / total_nights) if total_nights else Decimal("0"),
        "components": [(label, by_component[cat]) for cat, label in _HOTEL_COMPONENTS
                       if by_component.get(cat)],
        "departments": sorted(by_dept.items(), key=lambda kv: -abs(kv[1])),
        "hotels": sorted(((name, v["spend"], len(v["stays"])) for name, v in by_hotel.items()),
                         key=lambda t: -abs(t[1])),
        "cities": sorted(((name, v["spend"], v["nights"]) for name, v in by_city.items()),
                         key=lambda t: -abs(t[1])),
        "guest_bookings": sum(1 for b in ordered if b["guests"]),
        "travelers": sorted(((n, v["spend"], v["stays"]) for n, v in by_traveler.items()),
                            key=lambda t: -abs(t[1])),
    }


# Rows that aren't a flight ticket (they inherit the traveler's coding but
# would poison fare and lead-time math).
_ANCILLARY_WORDS = ("BAG", "SEAT", "ZONE", "WI-FI", "WIFI", "UPGRADE")
_ROUTING_COLS = ("Routing (Origin To To To To )", "Routing")
_AIRPORT = __import__("re").compile(r"\b[A-Z]{3}\b")


def trip_type(routing: str) -> str:
    """"DEN AUS DEN" -> round trip; "DEN PHX" -> one-way; open-jaw -> multi."""
    airports = _AIRPORT.findall(routing or "")
    if len(airports) >= 3 and airports[0] == airports[-1]:
        return "round"
    if len(airports) == 2:
        return "oneway"
    if len(airports) >= 3:
        return "multi"
    return "unknown"


def flights_detail(docs: list) -> dict:
    """Ticket-level Flights report: fares split by round-trip vs one-way,
    booking lead time (issue -> departure) overall and per traveler.

    Fee rows (bags/seats/wifi) and refunds count toward spend but are excluded
    from fare averages and lead times — averaging a $60 bag fee or a credit
    into "average flight price" would be nonsense.
    """
    fares: dict[str, list] = {"round": [], "oneway": [], "multi": []}
    leads: list[int] = []
    by_person: dict[str, dict] = defaultdict(lambda: {
        "spend": Decimal("0"), "tickets": 0, "fares": [], "leads": [], "round": 0})
    fees_total = Decimal("0")
    refunds_total = Decimal("0")
    short_notice = 0
    tickets = 0

    for doc in docs:
        for li in doc.line_items:
            raw = li.raw
            passenger = (raw.get("Passenger Name") or "").upper()
            person = li.person
            if person:
                by_person[person]["spend"] += li.amount
            if any(w in passenger for w in _ANCILLARY_WORDS):
                fees_total += li.amount
                continue
            if li.amount < 0:
                refunds_total += li.amount
                continue
            kind = trip_type(_first_col(raw, _ROUTING_COLS))
            issue = _parse_mdy(raw.get("Issue Date", ""))
            depart = _parse_mdy(raw.get("Departure Date", ""))
            lead = (depart - issue).days if issue and depart and depart >= issue else None

            tickets += 1
            if kind in fares:
                fares[kind].append(li.amount)
            if lead is not None:
                leads.append(lead)
                if lead <= 7:
                    short_notice += 1
            if person:
                p = by_person[person]
                p["tickets"] += 1
                p["fares"].append(li.amount)
                if lead is not None:
                    p["leads"].append(lead)
                if kind == "round":
                    p["round"] += 1

    def _avg(values):
        return (sum(values) / len(values)) if values else None

    people = []
    for name, p in by_person.items():
        people.append({
            "person": name,
            "tickets": p["tickets"],
            "spend": p["spend"],
            "avg_fare": _avg(p["fares"]),
            "avg_lead": _avg([Decimal(l) for l in p["leads"]]),
            "round_pct": (100 * p["round"] // p["tickets"]) if p["tickets"] else 0,
        })
    people.sort(key=lambda r: -abs(r["spend"]))

    return {
        "kind": "flights",
        "tickets": tickets,
        "avg_round": _avg(fares["round"]),
        "avg_oneway": _avg(fares["oneway"]),
        "avg_multi": _avg(fares["multi"]),
        "counts": {k: len(v) for k, v in fares.items()},
        "avg_lead": _avg([Decimal(l) for l in leads]),
        "short_notice": short_notice,
        "short_notice_pct": (100 * short_notice // len(leads)) if leads else 0,
        "fees_total": fees_total,
        "refunds_total": refunds_total,
        "people": people,
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
    dom_docs = [run["doc"] for _, run in run_items
                if id(run) in kept and run["doc"].source in sources]
    detail = None
    if domain_key == "hotels" and dom_docs:
        flight_docs = [run["doc"] for _, run in run_items
                       if id(run) in kept and run["doc"].source in DOMAINS["flights"]["sources"]]
        detail = hotels_detail(dom_docs, flight_docs, load_guest_index())
        detail["kind"] = "hotels"
    elif domain_key == "flights" and dom_docs:
        detail = flights_detail(dom_docs)
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
        "detail": detail,
    }


def _guest_index_path() -> str:
    import os
    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
    return os.path.join(data_dir, "hotel_guests.json")


def load_guest_index() -> dict:
    import json
    import os
    path = _guest_index_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_guest_index(index: dict) -> int:
    """Merge into the stored guest index; returns the total booking count."""
    import json
    import os
    merged = load_guest_index()
    merged.update(index)
    path = _guest_index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    return len(merged)


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
