"""Pay Run: the weekly pre-payment review of the bills going out on pay day.

AP reviews on Thursday and Bill.com pays on Friday. Everything that will be
paid in that batch gets one verdict that combines:

  - the Bill Check result (does the invoice say what was entered?), and
  - payment-fraud signals for the bill and its vendor, run BEFORE the money
    leaves — bank details changed recently, a brand-new vendor, a first
    payment, an amount far outside the vendor's history, a resubmitted
    amount under a new invoice number, plus the vendor-master checks Cash
    Proof already runs (lookalike names, shared emails, employee names).

A bill that's flagged critical/high needs a decision before the run can be
signed off: *release* (with a note — for a bank change, who was called and
on what number) or *hold* (pull it from Friday's run in Bill.com). Nothing
is written to Bill.com. Decisions and the sign-off are stored per pay date
and appended to billcheck/audit.jsonl.

No Flask in here, so the logic is testable on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from .compare import SEVERITY_ORDER, normalize_vendor

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

BANK_CHANGE_DAYS = 45      # bank details this recent on a vendor being paid -> critical
NEW_VENDOR_DAYS = 60       # vendor created this recently -> high
OUTLIER_MULTIPLE = 3       # amount > N x the vendor's median bill -> high
OUTLIER_MIN_HISTORY = 3    # ... once the vendor has this many prior bills
OUTLIER_MIN_AMOUNT = Decimal("1000")
SAME_AMOUNT_DAYS = 30

DECISIONS = ("release", "hold")


def settings() -> dict:
    """Pay day and horizon from config/recon.yml (billcheck.payrun), with
    defaults matching the Thursday-review / Friday-pay schedule."""
    try:
        from ..recon.settings import recon_config
        cfg = (recon_config().get("billcheck") or {}).get("payrun") or {}
    except Exception:
        cfg = {}
    day = str(cfg.get("pay_day") or "friday").strip().lower()
    return {
        "pay_weekday": WEEKDAYS.index(day) if day in WEEKDAYS else 4,
        "horizon_days": int(cfg.get("horizon_days") or 7),
    }


def next_pay_date(today: date, pay_weekday: int = 4) -> date:
    """The pay day on or after today (Thursday -> tomorrow, Friday -> today)."""
    return today + timedelta(days=(pay_weekday - today.weekday()) % 7)


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _iso(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def in_run(bill: dict, pay_date: date, horizon_days: int = 7) -> bool:
    """A bill goes out on this pay date when Bill.com already has it
    scheduled, or when it falls due before the following pay date (waiting
    a week would make it late). A bill with no due date is included, so it
    gets looked at rather than silently skipped."""
    if bill.get("payment_status") == "paid":
        return False
    if bill.get("payment_status") == "scheduled":
        return True
    due = _iso(bill.get("due_date"))
    return due is None or due < pay_date + timedelta(days=horizon_days)


def _vkey(name) -> str:
    return " ".join(normalize_vendor(name))


def _signal(kind, severity, title, detail) -> dict:
    return {"kind": kind, "severity": severity, "title": title, "detail": detail}


def fraud_signals(bill: dict, master: dict, vendor_findings: list[dict],
                  today: date) -> list[dict]:
    """Payment-fraud signals for one bill about to be paid."""
    out: list[dict] = []
    vendor_id = bill.get("vendor_id") or ""
    vendors = {str(v.get("id")): v for v in master.get("vendors") or []}
    vendor = vendors.get(str(vendor_id)) or {}
    amount = _dec(bill.get("amount"))

    # Bank details added/changed recently on a vendor we're about to pay —
    # the payment-redirection scheme. Any vendor age counts here: unlike
    # Cash Proof's after-the-fact check, this one stops the payment.
    for acct in master.get("bank_accounts") or []:
        if str(acct.get("vendor_id")) != str(vendor_id):
            continue
        added = _iso(acct.get("created"))
        if added and 0 <= (today - added).days <= BANK_CHANGE_DAYS:
            out.append(_signal(
                "bank_change", "critical",
                f"Bank details added {added}",
                "This vendor's bank account was added or changed "
                f"{(today - added).days} days ago and a payment is about to go to it. "
                "Before releasing, call the vendor on a phone number you already had "
                "on file (not one from the email or invoice that asked for the change) "
                "and confirm the account."))
            break

    created = _iso(vendor.get("created"))
    if created and 0 <= (today - created).days <= NEW_VENDOR_DAYS:
        out.append(_signal(
            "new_vendor", "high",
            f"New vendor (set up {created})",
            "The vendor was created in Bill.com within the last "
            f"{NEW_VENDOR_DAYS} days. Confirm it's a real business someone here "
            "engaged — W-9 on file, a known contact, work actually received."))

    # Prior bills from the same vendor, this one excluded.
    key = _vkey(bill.get("vendor"))
    prior = [b for b in master.get("bills") or []
             if b.get("id") != bill.get("id") and key and _vkey(b.get("vendor")) == key]
    if not prior and master.get("bills"):
        out.append(_signal(
            "first_payment", "review",
            "First bill from this vendor",
            "No earlier bills from this vendor in Bill.com. First payments are where "
            "fake vendors and fake invoices get paid — confirm the work or goods."))

    amounts = [a for a in (_dec(b.get("amount")) for b in prior) if a and a > 0]
    if amount is not None and len(amounts) >= OUTLIER_MIN_HISTORY:
        typical = Decimal(str(median(amounts)))
        if amount >= OUTLIER_MIN_AMOUNT and typical > 0 and amount > typical * OUTLIER_MULTIPLE:
            out.append(_signal(
                "amount_outlier", "high",
                f"{amount / typical:.1f}x this vendor's usual bill",
                f"${amount:,.2f} against a typical ${typical:,.2f} over "
                f"{len(amounts)} earlier bills. Confirm the quantity/scope with "
                "whoever ordered it."))

    # Same amount under a different invoice number, close together —
    # resubmission beats a plain invoice-number duplicate check.
    inv_date = _iso(bill.get("invoice_date"))
    if amount is not None and amount != 0 and inv_date:
        for b in prior:
            other = _iso(b.get("invoice_date"))
            if (_dec(b.get("amount")) == amount and other
                    and (b.get("invoice") or "") != (bill.get("invoice") or "")
                    and abs((inv_date - other).days) <= SAME_AMOUNT_DAYS):
                out.append(_signal(
                    "same_amount", "high",
                    f"Same amount as invoice {b.get('invoice') or '?'}",
                    f"Invoice {b.get('invoice') or '?'} ({other}) billed the identical "
                    f"${amount:,.2f}. Make sure this isn't the same charge under a new "
                    "invoice number."))
                break

    for f in vendor_findings:
        if str(vendor_id) in {str(i) for i in f.get("vendor_ids") or []}:
            out.append(_signal(f["kind"], f["severity"], f["title"], f["detail"]))

    if bill.get("approval_status") and bill["approval_status"] not in ("approved", "unassigned"):
        out.append(_signal(
            "not_approved", "review",
            f"Approval: {bill['approval_status']}",
            "Not fully approved in Bill.com yet — it won't pay until it is."))

    out.sort(key=lambda s: SEVERITY_ORDER.get(s["severity"], 9))
    return out


def _check_status(result: dict) -> tuple[str, str]:
    """(severity, one-line summary) for the Bill Check side of a bill."""
    status = result.get("status")
    if result.get("disposition"):
        d = result["disposition"]
        return "clear", f"Bill Check {d.get('action')} by {d.get('who')}"
    if status in ("match", "skipped"):
        return "clear", "Invoice matches the entry" if status == "match" else "Excluded by vendor policy"
    if status in ("no_document", "error", "unreadable"):
        return "high", "Invoice not verified — " + ("no attachment" if status == "no_document"
                                                     else "attachment couldn't be read")
    findings = (result.get("comparison") or {}).get("findings") or []
    sev = result.get("severity") or "review"
    return sev, findings[0]["reason"] if findings else f"Bill Check: {status}"


def signals_key(bill: dict, signals: list[dict], check_sev: str) -> str:
    """A decision holds only while what it was made on is unchanged."""
    raw = json.dumps({"bill": {k: bill.get(k) for k in
                               ("vendor_id", "vendor", "invoice", "amount", "due_date")},
                      "signals": sorted(s["kind"] for s in signals),
                      "check": check_sev}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def build(results: list[dict], master: dict, people: list[str], today: date,
          decisions: dict | None = None, pay_weekday: int = 4,
          horizon_days: int = 7) -> dict:
    """The pay-run view: every bill going out on the next pay date, with its
    combined verdict, decision state, and the batch totals."""
    from ..recon.checks import vendor_master_checks

    decisions = decisions or {}
    pay_date = next_pay_date(today, pay_weekday)
    vendor_findings = [f for f in vendor_master_checks(master or {}, people, today)["findings"]
                       if f.get("kind") != "vendor_bank_change"]   # covered per bill, stricter

    rows = []
    for r in results:
        bill = r.get("bill") or {}
        if not bill.get("id") or not in_run(bill, pay_date, horizon_days):
            continue
        check_sev, check_line = _check_status(r)
        signals = fraud_signals(bill, master or {}, vendor_findings, today)
        severity = min([check_sev] + [s["severity"] for s in signals],
                       key=lambda s: SEVERITY_ORDER.get(s, 9))
        key = signals_key(bill, signals, check_sev)
        decision = decisions.get(bill["id"])
        if decision and decision.get("key") != key:
            decision = None                      # something changed since — decide again
        due = _iso(bill.get("due_date"))
        rows.append({
            "bill_id": bill["id"], "bill": bill, "severity": severity,
            "check_severity": check_sev, "check_line": check_line,
            "check_status": r.get("status"), "signals": signals, "key": key,
            "decision": decision,
            "needs_decision": severity in ("critical", "high"),
            "past_due": bool(due and due < pay_date),
        })
    rows.sort(key=lambda x: (0 if x["needs_decision"] and not x["decision"] else 1,
                             SEVERITY_ORDER.get(x["severity"], 9),
                             x["bill"].get("due_date") or "9999",
                             x["bill"].get("vendor") or ""))

    def total(items):
        return sum((_dec(x["bill"].get("amount")) or Decimal(0)) for x in items)

    held = [x for x in rows if (x["decision"] or {}).get("action") == "hold"]
    releasing = [x for x in rows if x not in held]
    undecided = [x for x in rows if x["needs_decision"] and not x["decision"]]
    counts = {s: sum(1 for x in rows if x["severity"] == s) for s in SEVERITY_ORDER}
    return {
        "pay_date": pay_date.isoformat(),
        "rows": rows,
        "counts": counts,
        "total": total(rows),
        "releasing_total": total(releasing),
        "releasing_count": len(releasing),
        "held_total": total(held),
        "held_count": len(held),
        "undecided": len(undecided),
        "ready_to_sign": not undecided,
    }


# --- persistence -------------------------------------------------------------
# <OUT>/billcheck/payruns/<pay_date>.json — decisions + sign-off for one run.

def _root() -> str:
    return os.path.join(os.environ.get("FINANCE_HELPER_OUT_DIR", "out"), "billcheck")


def _path(pay_date: str) -> str:
    safe = "".join(c for c in pay_date if c.isdigit() or c == "-")[:10]
    return os.path.join(_root(), "payruns", f"{safe}.json")


def load(pay_date: str) -> dict:
    try:
        with open(_path(pay_date), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"pay_date": pay_date, "decisions": {}, "signoff": None}


def _save(state: dict) -> None:
    path = _path(state["pay_date"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    os.replace(tmp, path)


def _audit(entry: dict) -> None:
    os.makedirs(_root(), exist_ok=True)
    with open(os.path.join(_root(), "audit.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "payrun", **entry}, default=str) + "\n")


def record_decision(pay_date: str, bill_id: str, key: str, action: str,
                    note: str, who: str) -> None:
    state = load(pay_date)
    entry = {"action": action, "note": note, "who": who, "key": key,
             "when": datetime.now().isoformat(timespec="seconds")}
    state.setdefault("decisions", {})[bill_id] = entry
    state["signoff"] = None                  # a changed decision voids the sign-off
    _save(state)
    _audit({"pay_date": pay_date, "bill_id": bill_id, **entry})


def sign_off(pay_date: str, view: dict, who: str, note: str = "") -> None:
    state = load(pay_date)
    state["signoff"] = {
        "who": who, "note": note, "when": datetime.now().isoformat(timespec="seconds"),
        "releasing_count": view["releasing_count"],
        "releasing_total": str(view["releasing_total"]),
        "held_count": view["held_count"], "held_total": str(view["held_total"]),
        "bill_keys": {x["bill_id"]: x["key"] for x in view["rows"]},
    }
    _save(state)
    _audit({"pay_date": pay_date, "action": "signoff",
            **{k: v for k, v in state["signoff"].items() if k != "bill_keys"}})


def signoff_current(state: dict, view: dict) -> bool:
    """A sign-off stands only for the batch it was given on: a bill added,
    removed or changed since means the run needs signing again."""
    s = state.get("signoff")
    return bool(s) and s.get("bill_keys") == {x["bill_id"]: x["key"] for x in view["rows"]}


def recent_signoffs(limit: int = 6) -> list[dict]:
    """Earlier signed-off runs, newest first — the 'typical week' baseline."""
    folder = os.path.join(_root(), "payruns")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            continue
        if state.get("signoff"):
            out.append({"pay_date": state.get("pay_date"), **state["signoff"]})
        if len(out) >= limit:
            break
    return out
