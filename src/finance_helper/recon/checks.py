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
    """"rmpr c spreng bank" -> "c spreng".

    The descriptor is "RMPR <F LAST> <BANK NAME> ACH DEBIT <ids>"; noise
    stripping removes the bank's name and the ACH plumbing but the literal
    word "bank" survives into the norm — and a trailing "bank" token made
    every surname look like "Bank" (0/622 tied on the first real run).
    """
    toks = txn.counterparty_norm.split()
    if toks and toks[0] == "rmpr":
        toks = toks[1:]
    while toks and toks[-1] in ("bank", "banks", "bk"):
        toks = toks[:-1]
    return " ".join(toks)


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


# ---------------------------------------------------------------------------
# 5. Vendor master integrity (Bill.com vendors / bank accounts / bills).

_PERSONAL_DOMAINS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "aol.", "icloud.")


def vendor_master_checks(master: dict, people: list[str],
                         today: date | None = None) -> dict:
    from ..project_resolver import same_person
    today = today or date.today()
    vendors = master.get("vendors") or []
    accounts = master.get("bank_accounts") or []
    findings: list[dict] = []

    active = [v for v in vendors if v.get("active", True) and v.get("name")]

    # Lookalike vendor names: a distinctive shared token between different
    # vendors — generic business words don't count as identity.
    generic = {"supply", "supplies", "service", "services", "group", "company",
               "holdings", "solutions", "systems", "enterprises", "tech",
               "north", "south", "east", "west", "audio", "video"}
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            ta, tb = _tokens(a["name"]), _tokens(b["name"])
            strong = {t for t in (ta & tb) if len(t) >= 4 and t not in generic}
            if strong and ta != tb:
                findings.append(_finding(
                    "vendor_lookalike", "high",
                    f"Lookalike vendors: {a['name']} / {b['name']}",
                    "Two active vendors share a distinctive name part "
                    f"({', '.join(sorted(strong))}). Fake-vendor schemes hide "
                    "behind near-duplicates of real ones — confirm both are real "
                    "and distinct.",
                    a["id"], b["id"]))

    # Same email behind different vendor names.
    by_email: dict[str, list] = defaultdict(list)
    for v in active:
        for e in (v.get("email"), v.get("payment_email")):
            if e:
                by_email[e].append(v["name"])
    for email, names in by_email.items():
        if len(set(names)) > 1:
            findings.append(_finding(
                "vendor_shared_email", "high",
                f"One email, several vendors: {email}",
                "Vendors " + ", ".join(sorted(set(names))) + " share the same "
                "email — one operator behind multiple payees is a ghost-vendor "
                "pattern.",
                email))

    # Personal-domain payment emails.
    for v in active:
        pe = v.get("payment_email") or ""
        if any(d in pe for d in _PERSONAL_DOMAINS):
            findings.append(_finding(
                "vendor_personal_email", "review",
                f"Personal payment email: {v['name']}",
                f"Payments to {v['name']} route via {pe} — a personal mailbox, "
                "not a company domain. Fine for sole proprietors; verify it's "
                "expected.",
                v["id"]))

    # Vendor named like an employee.
    for v in active:
        for person in people:
            if same_person(person, v["name"]):
                findings.append(_finding(
                    "vendor_employee_collision", "critical",
                    f"Vendor named like an employee: {v['name']}",
                    f"Active vendor '{v['name']}' matches employee '{person}'. "
                    "Employees paying themselves as vendors is the textbook "
                    "internal scheme — verify this vendor's ownership.",
                    v["id"], person))
                break

    # Bank details added recently on an established vendor.
    created_by_vendor = {v["id"]: v.get("created", "") for v in vendors}
    for a in accounts:
        acct_created = _parse_iso(a.get("created"))
        vend_created = _parse_iso(created_by_vendor.get(a.get("vendor_id"), ""))
        if acct_created is None:
            continue
        if (today - acct_created).days <= 45 and vend_created \
                and (acct_created - vend_created).days > 90:
            findings.append(_finding(
                "vendor_bank_change", "critical",
                f"Bank details changed on established vendor: {a.get('vendor') or a.get('vendor_id')}",
                f"A bank account was added on {acct_created} to a vendor created "
                f"{vend_created} — payment-redirection fraud starts exactly this "
                "way. Verify by calling the vendor on a number you already had.",
                a.get("vendor_id"), str(acct_created)))

    return {"findings": findings, "vendors": len(active), "gaps": master.get("gaps") or []}


def _parse_iso(text):
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 6. Bill-level checks: duplicate and fabricated invoices.

def bill_checks(bills: list[dict]) -> dict:
    findings: list[dict] = []
    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for b in bills or []:
        if b.get("vendor"):
            by_vendor[b["vendor"]].append(b)

    for vendor, vbills in by_vendor.items():
        by_inv: dict[str, list[dict]] = defaultdict(list)
        for b in vbills:
            if b.get("invoice"):
                by_inv[b["invoice"]].append(b)
        for inv, dupes in by_inv.items():
            if len(dupes) > 1:
                amounts = ", ".join(f"${_dec(d['amount']) or 0:,.2f}" for d in dupes)
                findings.append(_finding(
                    "bill_dup_invoice", "high",
                    f"Invoice {inv} entered {len(dupes)}x for {vendor}",
                    f"The same invoice number appears on {len(dupes)} bills "
                    f"({amounts}). If both were paid, that's a double payment.",
                    vendor, inv))

        # Same amount, different invoice numbers, close together.
        seen_amt: dict[str, dict] = {}
        for b in sorted(vbills, key=lambda x: x.get("invoice_date") or ""):
            amt, inv = b.get("amount"), b.get("invoice")
            prior = seen_amt.get(amt)
            if prior and prior["invoice"] != inv:
                d1, d2 = _parse_iso(prior.get("invoice_date")), _parse_iso(b.get("invoice_date"))
                if d1 and d2 and abs((d2 - d1).days) <= 30:
                    findings.append(_finding(
                        "bill_same_amount", "high",
                        f"Same amount, two invoices: {vendor} ${_dec(amt) or 0:,.2f}",
                        f"Invoices {prior['invoice']} ({d1}) and {inv} ({d2}) bill "
                        "the identical amount within 30 days — resubmission under a "
                        "new number is how duplicate-payment fraud beats naive "
                        "invoice-number checks.",
                        vendor, amt, inv))
            seen_amt[amt] = b

        # Perfectly sequential invoice numbers -> we may be their only customer.
        nums = sorted(int(b["invoice"]) for b in vbills
                      if (b.get("invoice") or "").isdigit())
        run = max_run = 1
        for x, y in zip(nums, nums[1:]):
            run = run + 1 if y == x + 1 else 1
            max_run = max(max_run, run)
        if max_run >= 4:
            findings.append(_finding(
                "bill_sequential", "review",
                f"Sequential invoice numbers from {vendor}",
                f"{max_run} perfectly consecutive invoice numbers — meaning no "
                "other customer receives invoices between ours. Real vendors "
                "rarely bill one client exclusively; shells always do.",
                vendor))
    return {"findings": findings, "checked": len(bills or [])}


# ---------------------------------------------------------------------------
# 7. PO match (Sage Purchasing <-> Bill.com bills).

def po_match(bills: list[dict], pos: list[dict], tolerance: float = 0.05) -> dict:
    findings: list[dict] = []
    by_no = {p["po"]: p for p in pos or [] if p.get("po")}
    referenced = 0
    for b in bills or []:
        po_no = b.get("po")
        if not po_no:
            continue
        referenced += 1
        po = by_no.get(po_no)
        amt = _dec(b.get("amount"))
        if po is None:
            findings.append(_finding(
                "po_missing", "high",
                f"Bill cites PO {po_no} — no such PO in Sage",
                f"{b.get('vendor')} billed ${amt or 0:,.2f} against PO {po_no}, "
                "but Sage Purchasing has no such document in the period. A cited "
                "PO that doesn't exist is fabricated paperwork.",
                b.get("id"), po_no))
            continue
        total = _dec(po.get("total"))
        if amt and total and float(amt) > float(total) * (1 + tolerance):
            findings.append(_finding(
                "po_overrun", "high",
                f"Bill exceeds PO {po_no}: {b.get('vendor')}",
                f"Billed ${amt:,.2f} against a PO for ${total:,.2f} "
                f"(+{(float(amt) / float(total) - 1) * 100:.0f}%). Overruns beyond "
                "tolerance need an approved change order.",
                b.get("id"), po_no))
        d_po, d_inv = _parse_iso(po.get("date")), _parse_iso(b.get("invoice_date"))
        if d_po and d_inv and d_po > d_inv:
            findings.append(_finding(
                "po_retrofit", "review",
                f"PO {po_no} created after its invoice",
                f"The PO is dated {d_po}, but {b.get('vendor')}'s invoice is dated "
                f"{d_inv} — paperwork created after the fact to justify a "
                "purchase already made.",
                b.get("id"), po_no))
    return {"findings": findings, "pos": len(pos or []), "bills_with_po": referenced,
            "bills_without_po": sum(1 for b in bills or [] if not b.get("po"))}


SEVERITY_ORDER = {"critical": 0, "high": 1, "review": 2, "info": 3}


def run_all(bank: list[Txn], ramp_index: list, hotel_index: list,
            timecard_index: dict, flight_pairs: list[tuple],
            bill_index: list | None = None,
            bill_master: dict | None = None,
            po_index: list | None = None) -> dict:
    bill_index = bill_index or []
    bill_master = bill_master or {}
    reimb = reimbursement_tieout(bank, ramp_index)
    perdiem = perdiem_no_trip(ramp_index, hotel_index, timecard_index, flight_pairs)
    vendors = vendor_integrity(bank)
    billcom = billcom_tieout(bank, bill_index)
    dupes = cross_system_duplicates(bank, bill_index)

    people = sorted({p for p, _d in flight_pairs}
                    | set((timecard_index or {}).keys())
                    | {r.get("person", "") for r in ramp_index} - {""})
    master = vendor_master_checks(bill_master, people)
    bills = bill_checks(bill_master.get("bills") or [])
    pos = po_match(bill_master.get("bills") or [], po_index or [])

    findings = sorted(reimb["findings"] + perdiem["findings"] + vendors["findings"]
                      + billcom["findings"] + dupes["findings"]
                      + master["findings"] + bills["findings"] + pos["findings"],
                      key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return {"findings": findings, "reimb": reimb, "perdiem": perdiem,
            "vendors": vendors, "billcom": billcom, "dupes": dupes,
            "master": master, "bills": bills, "po": pos}
