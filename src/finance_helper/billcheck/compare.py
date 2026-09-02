"""Compare what the AP clerk entered in Bill.com against what the invoice says.

Pure functions, no I/O. `compare_bill` takes the normalized bill record
(billdotcom_api.fetch_open_bills) and the fields Claude read off the
attachment (extract.extract_invoice) and returns findings ranked

    critical — money out wrong: total differs, or the entered due date
               is LATER than the invoice's (a late payment waiting to happen)
    high     — a field that drives payment is wrong: invoice date, an early
               due date, invoice number, vendor
    review   — something a person should glance at: field not on the PDF,
               non-invoice attachment, PO differs, foreign currency,
               low-confidence read

The due date is the one Bill.com gets wrong most, so it is derived three
ways in order of trust: printed on the invoice; invoice date + printed
terms; invoice date + the vendor's terms in Bill.com (review-level only,
since that basis is Bill.com's own data).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

SEVERITY_ORDER = {"critical": 0, "high": 1, "review": 2, "clear": 3}
AMOUNT_TOLERANCE = Decimal("0.01")

FIELD_LABELS = [
    ("vendor", "Vendor"),
    ("invoice", "Invoice #"),
    ("invoice_date", "Invoice date"),
    ("due_date", "Due date"),
    ("amount", "Total"),
    ("discount", "Early-pay discount"),
    ("terms", "Terms"),
    ("po", "PO #"),
]

_VENDOR_NOISE = {"inc", "llc", "corp", "corporation", "co", "company", "ltd",
                 "limited", "lp", "llp", "plc", "the", "and", "of", "dba",
                 "incorporated", "group", "services", "service"}
_INVOICE_PREFIXES = ("INVOICE", "INVNO", "INV", "NO", "NUM", "REF")


# --- field normalizers ------------------------------------------------------

def parse_date(text) -> date | None:
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    s = str(text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
                "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                "%b %d %Y", "%B %d %Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s[:10] if fmt.startswith("%Y-%m-%d") else s, fmt).date()
        except ValueError:
            continue
    return None


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


_NET = re.compile(r"\bnet\s*(\d{1,3})\b", re.I)
_DAYS = re.compile(r"\b(\d{1,3})\s*days?\b", re.I)
_RECEIPT = re.compile(r"(due\s+(up)?on\s+receipt|upon\s+receipt|on\s+receipt|immediately"
                      r"|\bcod\b|cash\s+on\s+delivery|prepaid|due\s+now)", re.I)


_DISCOUNT_DAYS = re.compile(r"\d+(?:\.\d+)?\s*%[^\d]{0,40}?(\d{1,3})\s*(?:days?)?", re.I)


def _discount_days(text) -> int | None:
    """'2% 10 Net 30' -> 10, '5% 25 Days' -> 25, '2% discount if paid in 90 days' -> 90."""
    m = _DISCOUNT_DAYS.search(str(text or ""))
    return int(m.group(1)) if m else None


def parse_terms_days(text) -> int | None:
    """'Net 30' -> 30, '2% 10 Net 30' -> 30, 'Due on receipt' -> 0, else None."""
    s = str(text or "").strip()
    if not s:
        return None
    m = _NET.search(s)
    if m:
        return int(m.group(1))
    if _RECEIPT.search(s):
        return 0
    m = _DAYS.search(s)
    if m:
        return int(m.group(1))
    if s.isdigit():
        return int(s)
    return None


def normalize_invoice_number(text) -> str:
    s = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    for prefix in _INVOICE_PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break
    return s.lstrip("0") or s


def normalize_vendor(text) -> list[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())
    return [t for t in s.split() if t and t not in _VENDOR_NOISE]


def vendor_matches(a, b) -> bool:
    ta, tb = normalize_vendor(a), normalize_vendor(b)
    if not ta or not tb:
        return True                     # nothing to compare against
    if ta == tb:
        return True
    ja, jb = " ".join(ta), " ".join(tb)
    if ja in jb or jb in ja:
        return True
    sa, sb = set(ta), set(tb)
    return len(sa & sb) / len(sa | sb) >= 0.5


def to_amount(text) -> Decimal | None:
    s = str(text if text is not None else "").strip().replace(",", "").replace("$", "")
    s = re.sub(r"[A-Za-z ]", "", s)
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s:
        return None
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


# --- the comparison ---------------------------------------------------------

def _finding(field, severity, entered, pdf, reason) -> dict:
    return {"field": field, "severity": severity,
            "entered": "" if entered is None else str(entered),
            "pdf": "" if pdf is None else str(pdf), "reason": reason}


def _vendor_policy(vendor, policies: dict | None) -> dict:
    for name, pol in (policies or {}).items():
        if isinstance(pol, dict) and vendor_matches(vendor, name):
            return pol
    return {}


def compare_bill(bill: dict, extracted: dict | None, today: date | None = None,
                 policies: dict | None = None) -> dict:
    bill = bill or {}
    ex = extracted or {}
    findings: list[dict] = []
    expected_due, basis, basis_is_pdf = None, "", False
    policy = _vendor_policy(bill.get("vendor") or (ex or {}).get("vendor"), policies)
    # Vendor exception to the always-take-the-discount house rule: for this
    # vendor the quick-pay discount is NEVER taken — full amount, net terms.
    skip_quickpay = str(policy.get("quickpay") or "").lower() in ("never", "no", "skip") \
        or policy.get("ignore_quickpay") is True
    take_quickpay = str(policy.get("quickpay") or "").lower() in ("take", "always", "yes")
    policy_pct = policy.get("pct")
    policy_note = str(policy.get("notes") or "")
    note_sfx = f" (vendor deal notes: {policy_note})" if policy_note else ""

    if not ex:
        return {"status": "unreadable", "severity": "review", "findings": [
            _finding("document", "review", "", "",
                     "The attachment could not be read — check it by hand.")],
            "fields": _side_by_side(bill, ex, []), "expected_due": None,
            "expected_due_basis": ""}

    if ex.get("is_invoice") is False:
        findings.append(_finding(
            "document", "review", "", "",
            "The attachment doesn't read as an invoice"
            + (f": {ex['notes']}" if ex.get("notes") else "")
            + ". Confirm the right document is attached."))

    # Total — the number that leaves the bank. House rule: an early-payment
    # discount is ALWAYS taken — the amount entered is the discounted total
    # and the due date is the discount cut-off. So with a discount on offer
    # the discounted figures are the expected ones; the full amount / net
    # date is a deviation, not an alternative.
    ent_amt, pdf_amt = to_amount(bill.get("amount")), to_amount(ex.get("total"))
    disc_amt, disc_date = to_amount(ex.get("discount_total")), parse_date(ex.get("discount_date"))
    has_discount = disc_amt is not None and (pdf_amt is None or disc_amt < pdf_amt)
    discount_taken = (has_discount and ent_amt is not None
                      and abs(ent_amt - disc_amt) <= AMOUNT_TOLERANCE)
    saving = (pdf_amt - disc_amt) if (has_discount and pdf_amt is not None) else None
    if pdf_amt is None and not discount_taken:
        findings.append(_finding("amount", "review", bill.get("amount"), "",
                                 "No total found on the PDF."))
    elif discount_taken:
        pass                                     # reduced amount, on purpose
    elif ent_amt is None or abs(ent_amt - pdf_amt) > AMOUNT_TOLERANCE:
        diff = (ent_amt - pdf_amt) if ent_amt is not None else None
        reason = ("Entered total differs from the invoice"
                  + (f" by {diff:+,.2f}" if diff is not None else "") + ".")
        if has_discount:
            reason += (f" The invoice's totals are {pdf_amt:.2f} (full) or {disc_amt:.2f}"
                       + (f" if paid by {disc_date.isoformat()}" if disc_date else " with the early-pay discount")
                       + "; the entry matches neither.")
        findings.append(_finding("amount", "critical", bill.get("amount"), f"{pdf_amt:.2f}", reason))
    elif has_discount and saving is not None and saving > 0 and not skip_quickpay:
        findings.append(_finding(
            "discount", "high", bill.get("amount"), f"{disc_amt:.2f}",
            f"Early-pay discount not taken: enter {disc_amt:.2f} due "
            + (disc_date.isoformat() if disc_date else "by the cut-off")
            + f" (saves {saving:,.2f}). The full amount was entered."))

    # Negotiated-deal verification for vendors on the quick-pay list.
    if take_quickpay and policy_pct is not None:
        if (has_discount and saving is not None and pdf_amt
                and pdf_amt > 0):
            offered = (saving / pdf_amt) * Decimal("100")
            if abs(offered - Decimal(str(policy_pct))) > Decimal("0.3"):
                findings.append(_finding(
                    "discount", "review", f"{policy_pct}% (negotiated deal)",
                    f"{offered:.1f}% offered",
                    f"The invoice's early-pay discount works out to {offered:.1f}%, "
                    f"but the negotiated deal with this vendor is {policy_pct}% — "
                    f"check the terms{note_sfx}."))
        elif (not has_discount and pdf_amt is not None and pdf_amt > 0
                and ex.get("is_invoice") is not False):
            findings.append(_finding(
                "discount", "review", f"{policy_pct}% QP (negotiated deal)",
                "none on the invoice",
                f"This vendor gives a {policy_pct}% quick-pay discount but none "
                f"appears on this invoice — money left on the table unless the "
                f"deal doesn't apply here{note_sfx}."))

    # Invoice number — duplicates and vendor remittance keys hang off it.
    ent_inv, pdf_inv = bill.get("invoice") or "", ex.get("invoice_number") or ""
    if not pdf_inv:
        findings.append(_finding("invoice", "review", ent_inv, "",
                                 "No invoice number found on the PDF."))
    elif normalize_invoice_number(ent_inv) != normalize_invoice_number(pdf_inv):
        findings.append(_finding("invoice", "high", ent_inv, pdf_inv,
                                 "Invoice number differs from the PDF."))

    # Invoice date — the anchor every terms-based due date hangs off.
    ent_idate, pdf_idate = parse_date(bill.get("invoice_date")), parse_date(ex.get("invoice_date"))
    if pdf_idate is None:
        findings.append(_finding("invoice_date", "review", bill.get("invoice_date"), "",
                                 "No invoice date found on the PDF."))
    elif ent_idate is None:
        findings.append(_finding("invoice_date", "high", "", pdf_idate.isoformat(),
                                 "No invoice date entered in Bill.com."))
    elif ent_idate != pdf_idate:
        delta = (ent_idate - pdf_idate).days
        findings.append(_finding(
            "invoice_date", "high", ent_idate.isoformat(), pdf_idate.isoformat(),
            f"Entered invoice date is {abs(delta)} day{'s' if abs(delta) != 1 else ''} "
            f"{'after' if delta > 0 else 'before'} the date on the invoice."))

    # Due date — what the payment date is scheduled from.
    ent_due, pdf_due = parse_date(bill.get("due_date")), parse_date(ex.get("due_date"))
    pdf_terms_days = ex.get("terms_days")
    if pdf_terms_days is None:
        pdf_terms_days = parse_terms_days(ex.get("terms"))
    anchor = pdf_idate or ent_idate
    anchor_note = ""
    if str(policy.get("due_anchor") or "") == "ship_date":
        ship = parse_date(ex.get("ship_date"))
        if ship is not None:
            anchor, anchor_note = ship, " (anchored to the SHIP date, per this vendor's terms)"

    # Vendor policy: this vendor's quick-pay discount is never taken, so the
    # discount deadline is NOT the due date. Expect the NET terms instead —
    # from an explicit "Net N" on the invoice, else the vendor's Bill.com
    # terms. A printed due date at/after net terms is the real (net) due
    # date and still wins.
    terms_text = str(ex.get("terms") or "")
    if skip_quickpay and anchor is not None and (has_discount or "%" in terms_text):
        m = _NET.search(terms_text)
        net_days = int(m.group(1)) if m else bill.get("terms_days")
        if net_days is not None:
            net_due = add_days(anchor, int(net_days))
            if pdf_due is not None and pdf_due >= net_due:
                expected_due, basis, basis_is_pdf = \
                    pdf_due, "the due date printed on the invoice", True
            else:
                expected_due = net_due
                basis = (f"the invoice's net terms (Net {net_days}) from "
                         f"{anchor.isoformat()} — the quick-pay discount is "
                         "never taken for this vendor (policy)")
                basis_is_pdf = True

    if expected_due is not None:
        pass
    elif pdf_due is not None:
        expected_due, basis, basis_is_pdf = pdf_due, "the due date printed on the invoice", True
    elif pdf_terms_days is not None and anchor is not None:
        expected_due = add_days(anchor, int(pdf_terms_days))
        basis = (f"the invoice's terms ({ex.get('terms') or f'Net {pdf_terms_days}'}) "
                 f"from its {anchor.isoformat()} invoice date{anchor_note}")
        basis_is_pdf = True
    elif bill.get("terms_days") is not None and anchor is not None:
        expected_due = add_days(anchor, int(bill["terms_days"]))
        term_label = bill.get("terms") or f"Net {bill['terms_days']}"
        basis = (f"the vendor's terms in Bill.com ({term_label}) "
                 f"from the {anchor.isoformat()} invoice date{anchor_note}")
        basis_is_pdf = False

    if has_discount and not skip_quickpay:
        if disc_date is None:
            disc_days = ex.get("discount_days")
            if disc_days is None:
                disc_days = _discount_days(ex.get("discount_terms") or ex.get("terms"))
            if disc_days is not None and anchor is not None:
                disc_date = add_days(anchor, int(disc_days))
        if disc_date is not None:
            expected_due, basis_is_pdf = disc_date, True
            basis = "the early-pay cut-off on the invoice (the discount is always taken)"
    if ent_due is None:
        findings.append(_finding(
            "due_date", "high", "", expected_due.isoformat() if expected_due else "",
            "No due date entered in Bill.com."))
    elif has_discount and disc_date is not None and not skip_quickpay:
        delta = (ent_due - disc_date).days
        if delta > 0:
            if discount_taken:
                why = ("paying then short-pays the vendor"
                       + (f" by {saving:,.2f}" if saving else "") + ".")
            else:
                why = ("paying then forfeits the discount"
                       + (f" ({saving:,.2f})" if saving else "") + ".")
            findings.append(_finding(
                "due_date", "critical", ent_due.isoformat(), disc_date.isoformat(),
                f"Due date is {delta} day{'s' if delta != 1 else ''} after the "
                f"{disc_date.isoformat()} early-pay cut-off — {why}"))
        elif delta < 0:
            findings.append(_finding(
                "due_date", "high", ent_due.isoformat(), disc_date.isoformat(),
                f"Due date is {-delta} day{'s' if delta != -1 else ''} before the "
                f"{disc_date.isoformat()} early-pay cut-off — the cut-off is the due date to enter."))
    elif expected_due is not None and ent_due != expected_due:
        delta = (ent_due - expected_due).days
        late = delta > 0
        if basis_is_pdf:
            sev = "critical" if late else "high"
        else:
            sev = "review"
        consequence = ("it would be paid late" if late else "it would be paid early")
        findings.append(_finding(
            "due_date", sev, ent_due.isoformat(), expected_due.isoformat(),
            f"Entered due date is {abs(delta)} day{'s' if abs(delta) != 1 else ''} "
            f"{'after' if late else 'before'} {expected_due.isoformat()}, per {basis} — "
            f"{consequence}."))
    elif expected_due is None and ent_idate is not None:
        gap = (ent_due - ent_idate).days
        if gap < 0:
            findings.append(_finding("due_date", "high", ent_due.isoformat(), "",
                                     "Due date is before the invoice date."))
        elif gap > 90:
            findings.append(_finding(
                "due_date", "review", ent_due.isoformat(), "",
                f"Due date is {gap} days after the invoice date and the PDF states "
                "no due date or terms — confirm."))

    # Vendor — paying the right party.
    if ex.get("vendor") and bill.get("vendor") and not vendor_matches(bill["vendor"], ex["vendor"]):
        findings.append(_finding("vendor", "high", bill.get("vendor"), ex.get("vendor"),
                                 "Vendor on the PDF doesn't match the vendor on the bill."))

    # PO — only when both sides have one.
    if bill.get("po") and ex.get("po_number"):
        if normalize_invoice_number(bill["po"]) != normalize_invoice_number(ex["po_number"]):
            findings.append(_finding("po", "review", bill.get("po"), ex.get("po_number"),
                                     "PO number differs from the PDF."))

    cur = str(ex.get("currency") or "").strip().upper()
    if cur and cur not in ("USD", "US$", "$", "US DOLLARS"):
        findings.append(_finding("currency", "review", "USD", cur,
                                 f"Invoice is in {cur} — confirm the entered USD amount."))

    if str(ex.get("confidence") or "").lower() == "low":
        findings.append(_finding("document", "review", "", "",
                                 "Low-confidence read of the attachment"
                                 + (f": {ex['notes']}" if ex.get("notes") else "") + "."))

    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    if any(f["severity"] in ("critical", "high") for f in findings):
        status = "mismatch"
    elif findings:
        status = "review"
    else:
        status = "match"
    severity = findings[0]["severity"] if findings else "clear"
    return {
        "status": status,
        "severity": severity,
        "findings": findings,
        "fields": _side_by_side(bill, ex, findings, discount_taken),
        "expected_due": expected_due.isoformat() if expected_due else None,
        "expected_due_basis": basis,
        "discount_taken": discount_taken,
    }


def _side_by_side(bill: dict, ex: dict, findings: list[dict], discount_taken: bool = False) -> list[dict]:
    by_field = {f["field"]: f for f in findings}
    discount_pdf = ""
    if ex.get("discount_total"):
        discount_pdf = str(ex["discount_total"])
        if ex.get("discount_date"):
            discount_pdf += f" by {ex['discount_date']}"
        if ex.get("discount_terms"):
            discount_pdf += f" ({ex['discount_terms']})"
    pdf_values = {
        "vendor": ex.get("vendor"),
        "invoice": ex.get("invoice_number"),
        "invoice_date": ex.get("invoice_date"),
        "due_date": ex.get("due_date"),
        "amount": ex.get("total"),
        "discount": discount_pdf,
        "terms": ex.get("terms"),
        "po": ex.get("po_number"),
    }
    entered_values = {**bill, "discount": ("taken" if discount_taken else ("not taken" if discount_pdf else ""))}
    rows = []
    for key, label in FIELD_LABELS:
        if key == "discount" and not discount_pdf:
            continue
        entered = entered_values.get(key)
        pdf = pdf_values.get(key)
        f = by_field.get(key)
        if f:
            state = "review" if f["severity"] == "review" else "mismatch"
        elif pdf in (None, ""):
            state = "n/a"
        else:
            state = "match"
        rows.append({"field": key, "label": label,
                     "entered": "" if entered is None else str(entered),
                     "pdf": "" if pdf is None else str(pdf), "state": state,
                     "reason": f["reason"] if f else ""})
    return rows


# --- duplicates across the queue -------------------------------------------

def find_duplicates(bills: list[dict], history: list[dict] | None = None) -> dict[str, list[str]]:
    """bill id -> descriptions of other bills with the same vendor + invoice #.

    `history` is any other bill list (e.g. the Bill.com master index, paid
    bills included) — the classic double-entry is the same invoice keyed in
    twice, weeks apart, by two different people.
    """
    def key(b):
        inv = normalize_invoice_number(b.get("invoice"))
        if not inv:
            return None
        return (" ".join(normalize_vendor(b.get("vendor"))), inv)

    groups: dict[tuple, list[dict]] = {}
    for b in list(bills) + list(history or []):
        k = key(b)
        if k:
            groups.setdefault(k, []).append(b)
    out: dict[str, list[str]] = {}
    seen_ids = set()
    for b in bills:
        k = key(b)
        if not k:
            continue
        others = []
        seen_ids = {b.get("id")}
        for o in groups[k]:
            if o.get("id") in seen_ids:
                continue
            seen_ids.add(o.get("id"))
            others.append(f"{o.get('vendor', '')} #{o.get('invoice', '')} "
                          f"{o.get('amount', '')} dated {o.get('invoice_date', '')}"
                          + (f" ({o['payment_status']})" if o.get("payment_status") else ""))
        if others:
            out[b["id"]] = others
    return out
