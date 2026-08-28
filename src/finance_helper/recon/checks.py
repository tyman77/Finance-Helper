"""Fraud checks beyond the tie-out: reimbursements, per-diem, vendor patterns.

Pure functions over data Scout already holds — bank txns from the run, the
Ramp reimbursement index, hotel/timecard indexes, and flight (person, date)
pairs from saved statements. Each finding carries a stable id (so
dispositions attach to it across re-renders), a severity, and a
human-complete explanation. Same design rule as the tie-out: every
conclusion says exactly what was compared.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from ..project_resolver import same_person
from .models import Txn
from .settings import recon_config


def _fid(*parts) -> str:
    return "check:" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _finding(kind, severity, title, detail, *key) -> dict:
    return {"id": _fid(kind, *key), "kind": kind, "severity": severity,
            "title": title, "detail": detail}


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rmpr_person(txn: Txn) -> str:
    """"rmpr j cody" -> "j cody" (the bank abbreviates to initial+surname)."""
    norm = txn.counterparty_norm
    return norm[4:].strip() if norm.startswith("rmpr") else norm


# ---------------------------------------------------------------------------
# 1. Reimbursement tie-out: every bank RMPR debit must exist in Ramp.

def reimbursement_tieout(bank: list[Txn], ramp_index: list) -> dict:
    cfg = recon_config()["checks"]
    window = cfg["reimb_window_days"]
    debits = [t for t in bank
              if t.kind == "ramp_reimbursement" and not t.pending and t.amount < 0]
    findings: list[dict] = []
    if not debits:
        return {"findings": [], "checked": 0, "matched": 0, "coverage": True}
    if not ramp_index:
        return {"findings": [], "checked": len(debits), "matched": 0, "coverage": False}

    used: set[int] = set()
    matched = 0
    for t in debits:
        person = _rmpr_person(t)
        amount = -t.amount
        hit = None
        for i, r in enumerate(ramp_index):
            if i in used:
                continue
            if _dec(r.get("amount")) != amount:
                continue
            if not same_person(person, r.get("person", "")):
                continue
            try:
                when = date.fromisoformat(r["date"])
            except (KeyError, ValueError):
                continue
            if abs((when - t.posted_date).days) <= window:
                hit = i
                break
        if hit is not None:
            used.add(hit)
            matched += 1
        else:
            findings.append(_finding(
                "reimb_unmatched", "critical",
                f"Bank paid a reimbursement Ramp doesn't show: {t.counterparty_raw[:40]}",
                f"${amount:,.2f} left the bank on {t.posted_date} as a Ramp-style "
                f"reimbursement to '{person}', but no Ramp reimbursement record matches "
                f"that person + amount within {window} days. Redirected or fabricated "
                "payouts look exactly like this — verify in Ramp.",
                t.source_id))

    # Duplicates: two bank payouts, same person+amount, close together, but
    # fewer Ramp records than payouts.
    by_key: dict[tuple, list[Txn]] = defaultdict(list)
    for t in debits:
        by_key[(_rmpr_person(t), t.amount)].append(t)
    for (person, amount), txns in by_key.items():
        txns.sort(key=lambda t: t.posted_date)
        for a, b in zip(txns, txns[1:]):
            if (b.posted_date - a.posted_date).days <= 7:
                ramp_count = sum(
                    1 for r in ramp_index
                    if _dec(r.get("amount")) == -amount and same_person(person, r.get("person", "")))
                if ramp_count < 2:
                    findings.append(_finding(
                        "reimb_duplicate", "high",
                        f"Possible duplicate reimbursement to {person}",
                        f"Two bank payouts of ${-amount:,.2f} on {a.posted_date} and "
                        f"{b.posted_date}, but Ramp shows {ramp_count} matching "
                        "reimbursement record(s). Verify one isn't a re-send.",
                        person, str(amount), str(b.posted_date)))
    return {"findings": findings, "checked": len(debits), "matched": matched, "coverage": True}


# ---------------------------------------------------------------------------
# 2. Per-diem with no trip evidence.

def _is_perdiem(memo: str) -> bool:
    low = (memo or "").lower()
    return "per diem" in low or "perdiem" in low or "per-diem" in low


def perdiem_no_trip(ramp_index: list, hotel_index: list, timecard_index: dict,
                    flight_pairs: list[tuple]) -> dict:
    """flight_pairs: (person, date) tuples from processed United statements."""
    cfg = recon_config()["checks"]
    window = timedelta(days=cfg["perdiem_evidence_days"])
    perdiems = [r for r in ramp_index if _is_perdiem(r.get("memo", ""))]
    findings: list[dict] = []

    for r in perdiems:
        person = r.get("person", "")
        try:
            when = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        lo, hi = when - window, when + window

        flew = any(same_person(person, p) and d and lo <= d <= hi
                   for p, d in flight_pairs)
        stayed = False
        for b in hotel_index:
            if not any(same_person(person, g) for g in (b.get("guests") or [])):
                continue
            try:
                s = date.fromisoformat(b["start"])
                e = date.fromisoformat(b.get("end") or b["start"])
            except (KeyError, ValueError):
                continue
            if s <= hi and e >= lo:
                stayed = True
                break
        worked = False
        for name, days in (timecard_index or {}).items():
            if not same_person(person, name):
                continue
            if any(lo <= date.fromisoformat(d) <= hi for d in days):
                worked = True
                break

        if not (flew or stayed or worked):
            amount = _dec(r.get("amount"))
            findings.append(_finding(
                "perdiem_no_trip", "review",
                f"Per diem with no trip evidence: {person}",
                f"${amount or 0:,.2f} per diem on {when} "
                f"(memo: {r.get('memo', '')[:60]!r}), but no flight, hotel stay, or "
                f"timecard entry for {person} within {cfg['perdiem_evidence_days']} days. "
                "Phantom travel looks like this — could also be a car trip; confirm.",
                person, str(when), str(r.get("amount"))))
    return {"findings": findings, "checked": len(perdiems)}


# ---------------------------------------------------------------------------
# 3. Vendor integrity: new vendors, threshold skirting, round dollars, velocity.

_VENDOR_KINDS = {"ach_debit", "wire_out", "check", "other"}


def vendor_integrity(bank: list[Txn]) -> dict:
    cfg = recon_config()["checks"]
    pays = [t for t in bank
            if t.kind in _VENDOR_KINDS and not t.pending and t.amount < 0
            and t.counterparty_norm]
    by_vendor: dict[str, list[Txn]] = defaultdict(list)
    for t in pays:
        by_vendor[t.counterparty_norm].append(t)
    period_end = max((t.posted_date for t in bank if not t.pending), default=None)
    findings: list[dict] = []
    if period_end is None:
        return {"findings": [], "vendors": 0}

    for vendor, txns in by_vendor.items():
        txns.sort(key=lambda t: t.posted_date)
        label = txns[0].counterparty_raw[:40]
        first = txns[0].posted_date

        if (period_end - first).days <= cfg["new_vendor_days"]:
            total = sum(-t.amount for t in txns)
            findings.append(_finding(
                "new_vendor", "review",
                f"New payee: {label}",
                f"First-ever payment on {first}; {len(txns)} payment(s) totaling "
                f"${total:,.2f} since. Ghost vendors always start as a new payee — "
                "confirm this one is real and was approved.",
                vendor))

        for threshold in cfg["approval_thresholds"]:
            band = [t for t in txns
                    if threshold * cfg["threshold_band"] <= -t.amount < threshold]
            close = [(a, b) for a, b in zip(band, band[1:])
                     if (b.posted_date - a.posted_date).days <= 14]
            if close:
                a, b = close[0]
                findings.append(_finding(
                    "threshold_split", "high",
                    f"Payments hugging the ${threshold:,} line: {label}",
                    f"{len(band)} payments between ${threshold * cfg['threshold_band']:,.0f} "
                    f"and ${threshold:,} within days of each other (e.g. "
                    f"${-a.amount:,.2f} on {a.posted_date}, ${-b.amount:,.2f} on "
                    f"{b.posted_date}). Splitting to stay under an approval limit "
                    "looks like this.",
                    vendor, threshold))

        rounds = [t for t in txns if -t.amount >= 500 and (-t.amount) % 100 == 0]
        if len(rounds) >= 3:
            findings.append(_finding(
                "round_dollar", "info",
                f"Round-dollar pattern: {label}",
                f"{len(rounds)} payments of exact hundreds (invoices rarely land on "
                "round numbers repeatedly). Worth a glance at the backing invoices.",
                vendor))

        for i in range(len(txns)):
            burst = [t for t in txns[i:]
                     if (t.posted_date - txns[i].posted_date).days <= cfg["velocity_days"]]
            if len(burst) >= cfg["velocity_count"]:
                total = sum(-t.amount for t in burst)
                findings.append(_finding(
                    "velocity", "info",
                    f"Payment burst: {label}",
                    f"{len(burst)} payments in {cfg['velocity_days']} days starting "
                    f"{txns[i].posted_date}, totaling ${total:,.2f}.",
                    vendor, str(txns[i].posted_date)))
                break
    return {"findings": findings, "vendors": len(by_vendor)}


# ---------------------------------------------------------------------------
# 4. Bill.com: expand funding debits into payments; cross-system duplicates.

def _tokens(text: str) -> set[str]:
    return {t for t in (text or "").lower().split() if len(t) >= 3
            and t not in ("inc", "llc", "corp", "the", "and")}


def _vendors_alike(bank_norm: str, vendor: str) -> bool:
    a, b = _tokens(bank_norm), _tokens(vendor)
    strong = {t for t in (a & b) if len(t) >= 4}
    return bool(strong) or len(a & b) >= 2


def billcom_tieout(bank: list[Txn], bill_index: list) -> dict:
    """Every BILL.COM funding debit must equal one payment (±3d) or the sum
    of that day's payments; every disbursed payment in the period should be
    funded from this account."""
    debits = [t for t in bank if t.kind == "billcom" and not t.pending and t.amount < 0]
    findings: list[dict] = []
    if not debits:
        return {"findings": [], "checked": 0, "matched": 0, "coverage": True}
    if not bill_index:
        return {"findings": [], "checked": len(debits), "matched": 0, "coverage": False}

    pays = []
    for p in bill_index:
        try:
            pays.append((date.fromisoformat(p["date"]), Decimal(p["amount"]), p))
        except (KeyError, ValueError, InvalidOperation):
            continue
    used: set[int] = set()
    matched = 0
    for t in debits:
        amount = -t.amount
        hit = next((i for i, (d, a, _p) in enumerate(pays)
                    if i not in used and a == amount
                    and abs((d - t.posted_date).days) <= 3), None)
        if hit is None:
            # A funding debit can cover several same-day payments.
            for offset in (0, -1, 1, -2, 2, -3, 3):
                day = t.posted_date + timedelta(days=offset)
                day_ix = [i for i, (d, _a, _p) in enumerate(pays)
                          if i not in used and d == day]
                if day_ix and sum(pays[i][1] for i in day_ix) == amount:
                    used.update(day_ix)
                    matched += 1
                    break
            else:
                findings.append(_finding(
                    "billcom_unmatched", "critical",
                    f"Bill.com funding debit with no matching payments",
                    f"${amount:,.2f} left the bank on {t.posted_date} as a Bill.com "
                    "debit, but no Bill.com payment (or same-day payment batch) "
                    "matches it. Verify in Bill.com what this funded.",
                    t.source_id))
            continue
        used.add(hit)
        matched += 1
    return {"findings": findings, "checked": len(debits), "matched": matched, "coverage": True}


def cross_system_duplicates(bank: list[Txn], bill_index: list, window_days: int = 60) -> dict:
    """The same vendor + amount paid via Bill.com AND again by check/ACH/wire
    from the bank — the classic double payment."""
    pays = [t for t in bank
            if t.kind in _VENDOR_KINDS and not t.pending and t.amount < 0]
    findings: list[dict] = []
    for p in bill_index:
        try:
            when = date.fromisoformat(p["date"])
            amount = Decimal(p["amount"])
        except (KeyError, ValueError, InvalidOperation):
            continue
        if amount <= 0 or not p.get("vendor"):
            continue
        for t in pays:
            if -t.amount != amount:
                continue
            if abs((t.posted_date - when).days) > window_days:
                continue
            if not _vendors_alike(t.counterparty_norm, p["vendor"]):
                continue
            findings.append(_finding(
                "cross_duplicate", "high",
                f"Paid twice? {p['vendor']} — ${amount:,.2f}",
                f"Bill.com paid {p['vendor']} ${amount:,.2f} on {when}, and the bank "
                f"also shows a direct payment of the same amount to "
                f"'{t.counterparty_raw[:40]}' on {t.posted_date}. If these settle the "
                "same invoice, one is a double payment — pull both backups.",
                p.get("id"), t.source_id))
    return {"findings": findings, "checked": len(bill_index)}


SEVERITY_ORDER = {"critical": 0, "high": 1, "review": 2, "info": 3}


def run_all(bank: list[Txn], ramp_index: list, hotel_index: list,
            timecard_index: dict, flight_pairs: list[tuple],
            bill_index: list | None = None) -> dict:
    bill_index = bill_index or []
    reimb = reimbursement_tieout(bank, ramp_index)
    perdiem = perdiem_no_trip(ramp_index, hotel_index, timecard_index, flight_pairs)
    vendors = vendor_integrity(bank)
    billcom = billcom_tieout(bank, bill_index)
    dupes = cross_system_duplicates(bank, bill_index)
    findings = sorted(reimb["findings"] + perdiem["findings"] + vendors["findings"]
                      + billcom["findings"] + dupes["findings"],
                      key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return {"findings": findings, "reimb": reimb, "perdiem": perdiem,
            "vendors": vendors, "billcom": billcom, "dupes": dupes}
