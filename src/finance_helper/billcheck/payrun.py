"""Pay Run: the weekly pre-payment review of the bills going out on pay day.

AP reviews on Thursday and Bill.com pays on Friday. Everything that will be
paid in that batch gets one verdict that combines:

  - the Bill Check result (does the invoice say what was entered?), and
  - payment-fraud signals for the bill and its vendor, run BEFORE the money
    leaves — bank details changed recently, banking details on the invoice
    that don't match the vendor record, one person setting up the vendor /
    entering the bill / approving it, a brand-new vendor, a first payment,
    an amount far outside the vendor's history, a resubmitted amount under
    a new invoice number, plus the vendor-master checks Cash Proof already
    runs (lookalike names, shared emails, employee names).

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

from .compare import SEVERITY_ORDER, normalize_vendor
from .extract import SCHEMA_VERSION

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

BANK_CHANGE_DAYS = 45      # bank details this recent on a vendor being paid -> critical
NEW_VENDOR_DAYS = 60       # vendor created this recently -> high
# Bill sizes swing widely for most vendors, so "N x the usual bill" fires
# constantly. Flag only a bill well beyond the vendor's largest ever.
OUTLIER_OVER_MAX = Decimal("1.5")   # amount > 1.5 x the largest earlier bill -> high
OUTLIER_MIN_HISTORY = 5             # ... once the vendor has this many earlier bills
OUTLIER_MIN_AMOUNT = Decimal("5000")
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
        "horizon_days": int(cfg.get("horizon_days") or 15),
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


def _zip(text) -> str:
    import re
    found = re.findall(r"\b(\d{5})(?:-\d{4})?\b", str(text or ""))
    return found[-1] if found else ""


def remit_signals(extracted: dict | None, vendor: dict, accounts: list[dict]) -> list[dict]:
    """Bank/remit details printed on the invoice vs the vendor in Bill.com.
    Fake and altered invoices redirect payment by printing new banking
    details; Bill.com pays whatever account is on the vendor record, so the
    danger is someone updating that record to match the invoice."""
    ex = extracted or {}
    out: list[dict] = []
    if ex.get("bank_change_notice"):
        out.append(_signal(
            "invoice_bank_notice", "critical",
            "Invoice announces new bank details",
            f"\u201c{ex.get('bank_change_text') or 'Bank/remittance details changed'}\u201d — "
            "this is how payment-redirection fraud arrives. Don't update Bill.com from "
            "the invoice or its email; call the vendor on a number you already had."))
    inv4 = "".join(ch for ch in str(ex.get("remit_account_last4") or "") if ch.isdigit())[-4:]
    inv_routing = "".join(ch for ch in str(ex.get("remit_routing_number") or "") if ch.isdigit())
    if len(inv4) == 4:
        on_file = [a for a in accounts if a.get("active", True)] or accounts
        known = [a for a in on_file if a.get("account_last4")]
        bank = f" at {ex['remit_bank_name']}" if ex.get("remit_bank_name") else ""
        if not on_file:
            out.append(_signal(
                "invoice_bank_no_account", "high",
                f"Invoice asks for ACH to account ending {inv4}",
                f"The invoice prints bank details (account ending {inv4}{bank}) but "
                "Bill.com has no bank account for this vendor, so it pays by check. "
                "If anyone asks to add this account, verify it by phone first."))
        elif not known:
            out.append(_signal(
                "invoice_bank_unchecked", "review",
                f"Invoice bank account ending {inv4} not compared",
                "Bill.com didn't return the vendor's account number, so the invoice's "
                "banking details couldn't be compared. Check them against the vendor "
                "record by hand."))
        else:
            match = [a for a in known if a["account_last4"] == inv4]
            if not match:
                ours = ", ".join(sorted({a["account_last4"] for a in known}))
                out.append(_signal(
                    "invoice_bank_mismatch", "critical",
                    f"Invoice bank account …{inv4} ≠ Bill.com …{ours}",
                    f"The invoice asks for payment to account ending {inv4}{bank}; Bill.com "
                    f"pays this vendor to account ending {ours}. Either the invoice was "
                    "altered or the vendor changed banks — call the vendor on a number "
                    "you already had before paying or changing anything."))
            elif inv_routing and len(inv_routing) == 9 and all(
                    a.get("routing") and a["routing"] != inv_routing for a in match):
                out.append(_signal(
                    "invoice_routing_mismatch", "critical",
                    f"Invoice routing number {inv_routing} differs from Bill.com",
                    "Same last four account digits but a different bank routing number — "
                    "a different account. Verify by phone before paying."))
    inv_zip, our_zip = _zip(ex.get("remit_address")), (vendor.get("zip") or "")[:5]
    if inv_zip and our_zip and inv_zip != our_zip:
        out.append(_signal(
            "remit_address_differs", "review",
            f"Remit-to ZIP {inv_zip} ≠ Bill.com {our_zip}",
            f"The invoice's remit-to address ({ex.get('remit_address')}) is not the "
            f"address on the vendor record ({vendor.get('address') or our_zip}). Often a "
            "lockbox; confirm before changing the vendor's address."))
    return out


def same_person_signals(bill: dict, vendor: dict, approvers: list[dict] | None,
                        users: dict) -> list[dict]:
    """Segregation of duties: one person able to create a payee, enter a bill
    to it and approve that bill can pay anyone they like."""
    def who(uid):
        u = users.get(str(uid)) or {}
        return u.get("name") or u.get("email") or f"user {uid}"

    entered = str(bill.get("created_by") or "")
    set_up = str(vendor.get("created_by") or "")
    approver_ids = {a.get("user_id") for a in approvers or [] if a.get("user_id")}
    if entered and set_up and entered == set_up and entered in approver_ids:
        return [_signal(
            "same_person_all", "critical",
            f"{who(entered)} set up the vendor, entered the bill and approves it",
            "One person controls every step of this payment. Have someone else "
            "confirm the vendor is real and the work was received before release.")]
    out = []
    if entered and entered in approver_ids:
        out.append(_signal(
            "same_person_enter_approve", "high",
            f"{who(entered)} entered and approves this bill",
            "The person who entered the bill is also on its approval chain, so the "
            "approval isn't independent. Have another approver review it."))
    if set_up and set_up in approver_ids and set_up != entered:
        out.append(_signal(
            "same_person_vendor_approve", "high",
            f"{who(set_up)} set up this vendor and approves its bill",
            "The approver created the payee they're approving payment to. Confirm the "
            "vendor independently."))
    if entered and set_up and entered == set_up and entered not in approver_ids:
        out.append(_signal(
            "same_person_vendor_enter", "review",
            f"{who(entered)} set up the vendor and entered the bill",
            "Common in a small AP team; the approval step is the independent check "
            "here, so make sure it was done by someone else."))
    return out


def fraud_signals(bill: dict, master: dict, vendor_findings: list[dict],
                  today: date, extracted: dict | None = None,
                  approvers: list[dict] | None = None) -> list[dict]:
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
        largest = max(amounts)
        if amount >= OUTLIER_MIN_AMOUNT and amount > largest * OUTLIER_OVER_MAX:
            out.append(_signal(
                "amount_outlier", "high",
                "Biggest bill ever from this vendor",
                f"${amount:,.2f}; the largest of its {len(amounts)} earlier bills was "
                f"${largest:,.2f}. Confirm the quantity/scope with whoever ordered it."))

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

    accounts = [a for a in master.get("bank_accounts") or []
                if str(a.get("vendor_id")) == str(vendor_id)]
    out.extend(remit_signals(extracted, vendor, accounts))
    users = {str(u.get("id")): u for u in master.get("users") or []}
    out.extend(same_person_signals(bill, vendor, approvers, users))

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


def _list_bill(line: dict) -> dict:
    """A finance-list line as a bill, for runs built from the list alone."""
    from .finance_list import line_key
    return {"id": "list-" + hashlib.sha1(line_key(line["vendor"], line["invoice"]).encode()
                                         ).hexdigest()[:10],
            "vendor": line["vendor"], "invoice": line["invoice"],
            "invoice_date": line["invoice_date"], "due_date": line["due_date"],
            "amount": line["balance"], "payment_status": "open", "source": "list",
            "note": line.get("note", "")}


def _match_lines(bills: list[dict], lines: list[dict]) -> dict:
    """bill id -> list line. Vendor + invoice number first; then invoice
    number alone when it's unique on both sides and the vendors agree."""
    from .compare import vendor_matches, normalize_invoice_number
    from .finance_list import line_key

    by_key: dict[str, list] = {}
    by_inv: dict[str, list] = {}
    for x in lines:
        by_key.setdefault(line_key(x["vendor"], x["invoice"]), []).append(x)
        by_inv.setdefault(normalize_invoice_number(x["invoice"].rstrip("*")), []).append(x)
    taken: set[int] = set()
    out = {}
    for b in bills:
        cands = [x for x in by_key.get(line_key(b.get("vendor"), b.get("invoice")), [])
                 if x["row"] not in taken]
        if not cands:
            inv = normalize_invoice_number(str(b.get("invoice") or "").rstrip("*"))
            cands = [x for x in by_inv.get(inv, []) if x["row"] not in taken
                     and vendor_matches(x["vendor"], b.get("vendor"))] if inv else []
            if len(cands) != 1:
                cands = []
        if cands:
            taken.add(cands[0]["row"])
            out[b["id"]] = cands[0]
    return out


def build(results: list[dict], master: dict, people: list[str], today: date,
          decisions: dict | None = None, pay_weekday: int = 4,
          horizon_days: int = 7, approvers: dict | None = None,
          finance_list: dict | None = None) -> dict:
    """The pay-run view: every bill going out on the next pay date, with its
    combined verdict, decision state, and the batch totals.

    With finance's list uploaded, the list defines the run: Bill.com bills
    that aren't on it drop out (unless Bill.com has them scheduled — money
    leaving that finance didn't list), list lines are checked on their own,
    and matched bills are compared with the list."""
    from ..recon.checks import vendor_master_checks
    from . import finance_list as fl

    decisions = decisions or {}
    master = master or {}
    pay_date = next_pay_date(today, pay_weekday)
    vendor_findings = [f for f in vendor_master_checks(master, people, today)["findings"]
                       if f.get("kind") != "vendor_bank_change"]   # covered per bill, stricter

    items = []          # (bill, check_sev, check_line, check_status, signals, extracted)
    for r in results:
        bill = r.get("bill") or {}
        if not bill.get("id") or not in_run(bill, pay_date, horizon_days):
            continue
        check_sev, check_line = _check_status(r)
        signals = fraud_signals(bill, master, vendor_findings, today,
                                extracted=r.get("extracted"),
                                approvers=(approvers or {}).get(bill["id"]))
        items.append([bill, check_sev, check_line, r.get("status"), signals, r.get("extracted")])

    lines = (finance_list or {}).get("lines") or []
    list_view = None
    if lines:
        lsig = fl.line_signals(lines, pay_date, people, history=master.get("bills"))
        list_only, dropped = [], []
        if items:
            matched = _match_lines([it[0] for it in items], lines)
            kept = []
            for it in items:
                bill, signals = it[0], it[4]
                line = matched.get(bill["id"])
                if line is None:
                    if bill.get("payment_status") == "scheduled":
                        signals.insert(0, _signal(
                            "not_on_list", "critical", "Scheduled in Bill.com, not on finance's list",
                            "Bill.com is set to pay this, but it isn't on the list finance "
                            "approved for this run. Find out who added it before Friday."))
                        kept.append(it)
                    else:
                        dropped.append(bill)
                    continue
                signals.extend(lsig.get(line["row"], []))
                listed, entered = _dec(line["balance"]), _dec(bill.get("amount"))
                if listed is not None and entered is not None and listed != entered:
                    signals.insert(0, _signal(
                        "list_amount_changed", "high",
                        f"Amount changed since finance's list (${listed:,.2f} → ${entered:,.2f})",
                        "Bill.com's amount for this bill isn't what finance approved. "
                        "Find out who changed it and why."))
                if line["due_date"] and bill.get("due_date") and line["due_date"] != bill["due_date"]:
                    signals.append(_signal(
                        "list_due_changed", "review",
                        f"Due date changed ({line['due_date']} → {bill['due_date']})",
                        "Bill.com's due date differs from finance's list."))
                it.append(line)
                kept.append(it)
            items = kept
            seen_rows = {id(m) for m in matched.values()}
            list_only = [{**x, "signals": lsig.get(x["row"], [])}
                         for x in lines if id(x) not in seen_rows]
        else:
            for x in lines:
                items.append([_list_bill(x), "clear", "Not checked against the invoice",
                              "list", list(lsig.get(x["row"], [])), None, x])
        summ = fl.summary(finance_list)
        list_view = {**{k: finance_list.get(k) for k in ("filename", "uploaded_by", "when")},
                     **summ, "mode": "matched" if results else "list",
                     "list_only": list_only, "dropped": dropped,
                     "list_only_total": sum((_dec(x["balance"]) or Decimal(0)) for x in list_only)}

    rows = []
    for it in items:
        bill, check_sev, check_line, check_status, signals, extracted = it[:6]
        signals.sort(key=lambda s: SEVERITY_ORDER.get(s["severity"], 9))
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
            "check_status": check_status, "signals": signals, "key": key,
            "extracted_schema": ((extracted or {}).get("schema") or 0) >= SCHEMA_VERSION,
            "list_line": it[6] if len(it) > 6 else None,
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
        "due_through": (pay_date + timedelta(days=horizon_days - 1)).isoformat(),
        "rows": rows,
        "list": list_view,
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


def save_list(pay_date: str, parsed: dict, filename: str, who: str, data: bytes) -> None:
    """Finance's list for this pay date. Replacing it voids the sign-off;
    the uploaded file is kept next to the run as the record of what was
    approved."""
    state = load(pay_date)
    when = datetime.now().isoformat(timespec="seconds")
    state["finance_list"] = {**parsed, "filename": filename, "uploaded_by": who, "when": when}
    state["signoff"] = None
    _save(state)
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".xlsx", ".xlsm", ".csv"):
        with open(_path(pay_date)[:-5] + "-list" + ext, "wb") as fh:
            fh.write(data)
    _audit({"pay_date": pay_date, "action": "list_upload", "who": who, "when": when,
            "filename": filename, "lines": len(parsed.get("lines") or []),
            "header_total": parsed.get("header_total")})


def sign_off(pay_date: str, view: dict, who: str, note: str = "") -> None:
    state = load(pay_date)
    state["signoff"] = {
        "who": who, "note": note, "when": datetime.now().isoformat(timespec="seconds"),
        "releasing_count": view["releasing_count"],
        "releasing_total": str(view["releasing_total"]),
        "held_count": view["held_count"], "held_total": str(view["held_total"]),
        "bill_keys": {x["bill_id"]: x["key"] for x in view["rows"]},
        "finance_list": ({k: str(v) if v is not None else None for k, v in view["list"].items()
                          if k in ("filename", "uploaded_by", "count", "total", "header_total")}
                         if view.get("list") else None),
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
