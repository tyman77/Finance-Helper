"""Finance's "bills to pay" list: parse the weekly export, check it on its
own, and reconcile it with what Bill.com is actually set to pay.

The list is exported from Bill.com when finance builds the week's run, so
reconciling it against Bill.com on review day catches anything added,
removed or changed in between. The line checks need only the file, so they
work even before the Bill.com API is connected.

Expected layout (Bill.com's export): a header row with Invoice no., Vendor,
Invoice date, Due date, Invoice amount, Balance due; the list total in a
cell above the header; free-text notes (e.g. "wire") in unlabeled columns to
the right. CSV with the same headers works too.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .compare import normalize_invoice_number, normalize_vendor

_HEADERS = {
    "invoice": ("invoice no", "invoice number", "invoice #", "invoice no.", "invoice"),
    "vendor": ("vendor", "vendor name", "payee"),
    "invoice_date": ("invoice date", "bill date"),
    "due_date": ("due date",),
    "amount": ("invoice amount", "amount", "bill amount", "total"),
    "balance": ("balance due", "open balance", "amount due", "balance"),
}

# Common US first names — enough to spot a payee that's a person rather than
# a business ("Mitchell Stier"). A person as a vendor is often a legitimate
# contractor, but it's also the shape of a fake payee, so it's worth a look.
_FIRST_NAMES = frozenset("""
aaron adam adrian aidan alan albert alex alexander alexis alice alicia allison amanda amber
amy andrea andrew angela angie anna anne anthony april ashley austin barbara ben benjamin
beth betty bill billy blake bob bobby brad bradley brandon brenda brent brett brian brittany
brooke bruce bryan caleb cameron carl carlos carol caroline carrie casey catherine chad
charles charlie chelsea cheryl chris christian christina christine christopher cindy
claire cody colin connor courtney craig crystal curtis cynthia dale dan dana daniel danielle
darren dave david dawn dean debbie deborah denise dennis derek diana diane diego dominic don
donald donna doug douglas dustin dylan ed eddie edward elizabeth ellen emily emma eric erica
erin ethan eugene evan frank gabriel gail gary george gerald gina glenn grace greg gregory
hannah harold heather heidi helen henry holly hunter ian isaac jack jackie jacob jake james
jamie jane janet janice jared jasmine jason jeff jeffrey jenna jennifer jenny jeremy jerry
jesse jessica jill jim jimmy joan joe joel john johnny jon jonathan jordan jose joseph josh
joshua joyce juan judy julia julie justin karen kate katherine kathleen kathy katie kayla
keith kelly ken kenneth kevin kim kimberly kristen kristin kyle lance larry laura lauren
leah lee leslie linda lindsay lisa logan lori louis lucas luis luke lynn madison marcus
margaret maria marie marissa mark martin mary matt matthew megan melanie melissa michael
michelle mike mitch mitchell molly monica morgan nancy natalie nathan nicholas nick nicole
noah olivia pam pamela pat patricia patrick paul paula peter phil philip rachel randy ray
raymond rebecca regina richard rick ricky rob robert robin rodney roger ron ronald ross roy
russell ruth ryan sally sam samantha samuel sandra sara sarah scott sean seth shane shannon
sharon shawn sheila shelby sherry stacy stephanie stephen steve steven sue susan tammy tanya
tara taylor ted teresa terry thomas tiffany tim timothy tina todd tom tommy tony tracy travis
trevor troy tyler valerie vanessa veronica vicki victor victoria vincent wayne wendy wesley
william zach zachary
""".split())

_BUSINESS_WORDS = frozenset("""
inc llc ltd co corp corporation company group services service supply supplies systems
solutions electronics electric audio video lighting sound music rentals rental freight
logistics law pllc pc llp associates partners studio studios productions media design
technik technologies technology tech international industries labs wire cable store
""".split())

SHARE_FLAG = Decimal("0.20")         # one line >= 20% of the list ...
SHARE_MIN = Decimal("50000")         # ... and at least this much -> high
ROUND_MIN = Decimal("1000")


def _norm_header(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower().replace("_", " "))


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _invoice_text(value) -> str:
    """Excel stores numeric invoice numbers as floats (453123.0)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value if value is not None else "").strip()


def _grid(data: bytes, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    text = data.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def parse(data: bytes, filename: str = "") -> dict:
    """{"lines": [...], "header_total": Decimal|None, "columns": {...}}.
    Raises ValueError with a plain reason when the layout isn't recognized."""
    grid = _grid(data, filename)
    header_row, cols = None, {}
    for i, row in enumerate(grid[:30]):
        found = {}
        for j, cell in enumerate(row):
            h = _norm_header(cell)
            for field, names in _HEADERS.items():
                if field not in found and h in names:
                    found[field] = j
        if "vendor" in found and "invoice" in found and ("balance" in found or "amount" in found):
            header_row, cols = i, found
            break
    if header_row is None:
        raise ValueError("Couldn't find the header row — expected columns like "
                         "Invoice no., Vendor, Invoice date, Due date, Invoice amount, Balance due.")
    pay_col = cols.get("balance", cols.get("amount"))

    header_total = None
    for row in grid[:header_row]:
        if pay_col < len(row):
            header_total = _dec(row[pay_col]) if _dec(row[pay_col]) is not None else header_total

    used = set(cols.values())
    lines = []
    for n, row in enumerate(grid[header_row + 1:], start=header_row + 2):
        cell = lambda f: row[cols[f]] if f in cols and cols[f] < len(row) else None  # noqa: E731
        vendor = str(cell("vendor") or "").strip()
        if not vendor:
            continue
        amount, balance = _dec(cell("amount")), _dec(cell("balance"))
        notes = [str(c).strip() for j, c in enumerate(row)
                 if j not in used and c not in (None, "") and str(c).strip()]
        lines.append({
            "row": n,
            "invoice": _invoice_text(cell("invoice")),
            "vendor": vendor,
            "invoice_date": _date(cell("invoice_date")),
            "due_date": _date(cell("due_date")),
            "amount": str(amount if amount is not None else balance or ""),
            "balance": str(balance if balance is not None else amount or ""),
            "note": "; ".join(notes),
        })
    if not lines:
        raise ValueError("The header row was found but no bills under it.")
    return {"lines": lines, "header_total": str(header_total) if header_total is not None else None}


def line_key(vendor, invoice) -> str:
    return " ".join(normalize_vendor(vendor)) + "|" + normalize_invoice_number(
        str(invoice or "").rstrip("*").strip())


def looks_like_person(vendor: str) -> bool:
    words = re.findall(r"[A-Za-z]+", vendor or "")
    if not 2 <= len(words) <= 3 or re.search(r"[\d&]", vendor or ""):
        return False
    low = [w.lower() for w in words]
    return low[0] in _FIRST_NAMES and not any(w in _BUSINESS_WORDS for w in low[1:])


def _sig(kind, severity, title, detail) -> dict:
    return {"kind": kind, "severity": severity, "title": title, "detail": detail}


def line_signals(lines: list[dict], pay_date: date, people: list[str] | None = None,
                 history: list[dict] | None = None) -> dict:
    """{row number: [signals]} from the list alone (plus Bill.com bill
    history for the asterisk check, when it's available)."""
    from ..project_resolver import same_person

    out: dict[int, list[dict]] = defaultdict(list)
    total = sum((_dec(x["balance"]) or Decimal(0)) for x in lines) or Decimal(1)
    terms_by_vendor: dict[str, Counter] = defaultdict(Counter)
    for x in lines:
        i, d = _iso(x["invoice_date"]), _iso(x["due_date"])
        if i and d:
            terms_by_vendor[x["vendor"]][(d - i).days] += 1
    seen = Counter(line_key(x["vendor"], x["invoice"]) for x in lines)
    amounts = Counter((x["vendor"], x["balance"]) for x in lines)
    paid_keys = {line_key(b.get("vendor"), b.get("invoice")) for b in history or []}

    for x in lines:
        s = out[x["row"]]
        bal, amt = _dec(x["balance"]) or Decimal(0), _dec(x["amount"]) or Decimal(0)
        inv, note = x["invoice"], (x["note"] or "").lower()

        if seen[line_key(x["vendor"], inv)] > 1:
            s.append(_sig("list_duplicate", "critical", "Listed twice",
                          "The same vendor and invoice number appear more than once on the list."))
        elif amounts[(x["vendor"], x["balance"])] > 1 and bal > 0:
            s.append(_sig("list_same_amount", "high", "Same amount twice from this vendor",
                          f"Another line for {x['vendor']} is also ${bal:,.2f}. Make sure "
                          "it isn't one charge under two invoice numbers."))

        is_date = bool(re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", inv))
        if not is_date and re.search(r"[*#!?+~]|[^A-Za-z0-9]$", inv):
            base = inv.rstrip("*").strip()
            already = line_key(x["vendor"], base) in paid_keys
            s.append(_sig(
                "list_invoice_suffix", "critical" if already else "high",
                f"Invoice # “{inv}” has an added character",
                (f"Invoice {base} is already in Bill.com — this looks like the same "
                 "invoice re-entered to get past the duplicate check."
                 if already else
                 "A symbol added to an invoice number is the usual way to get past "
                 f"Bill.com's duplicate check. Make sure {base} wasn't already paid.")))
        if is_date:
            s.append(_sig("list_invoice_is_date", "high", "Invoice # is a date",
                          f"“{inv}” is a date, not a vendor invoice number — usually typed "
                          "in by hand with no real invoice behind it. Ask for the invoice."))

        if people and any(same_person(p, x["vendor"]) for p in people):
            s.append(_sig("list_vendor_employee", "critical", "Vendor named like an employee",
                          f"{x['vendor']} matches an employee's name. Confirm this isn't an "
                          "employee paying themselves as a vendor."))
        elif looks_like_person(x["vendor"]):
            big = bal >= Decimal("10000")
            s.append(_sig("list_individual", "high" if big else "review",
                          "Paying an individual" + (f" ${bal:,.0f}" if big else ""),
                          f"{x['vendor']} looks like a person, not a business. Confirm who "
                          "they are, what the work was, and who approved it."))

        i, d = _iso(x["invoice_date"]), _iso(x["due_date"])
        if i and d and d == i:
            usual = [t for t, _n in terms_by_vendor[x["vendor"]].most_common() if t > 0]
            if usual:
                s.append(_sig("list_due_entry", "high", "Due date = invoice date",
                              f"{x['vendor']}'s other bills on this list run {usual[0]} days; "
                              f"this one is due the day it was issued. Probably mis-entered "
                              f"(would be {(i + timedelta(days=usual[0])).isoformat()})."))
            # With nothing to compare against it's usually prepay / due on
            # receipt — not worth a flag.
        if d and d < pay_date:
            s.append(_sig("list_past_due", "review", f"Past due ({d.isoformat()})",
                          "Already past due on pay day. If this was an early-pay discount "
                          "date, the discount may be lost."))

        if "wire" in note:
            s.append(_sig("list_wire", "high", "Paid by wire",
                          "Wires can't be pulled back. Check the wire instructions against "
                          "what you've paid this vendor before — never against the invoice "
                          "or the email it came with."))

        if amt and bal < amt:
            s.append(_sig("list_short_pay", "review", f"Paying ${amt - bal:,.2f} less than invoiced",
                          "Balance due is below the invoice amount. Confirm a credit memo "
                          "or partial payment explains it."))
        if bal >= ROUND_MIN and bal % 100 == 0:
            s.append(_sig("list_round", "review", f"Round amount ${bal:,.0f}",
                          "Round amounts are normal for fees and quotes, and common in "
                          "made-up invoices. Confirm you recognize it."))
        share = bal / total
        if share >= SHARE_FLAG and bal >= SHARE_MIN:
            s.append(_sig("list_large_share", "high", f"{share * 100:.0f}% of this run",
                          f"${bal:,.2f} is a large share of the week. Payments this size are "
                          "what bank-change scams target — confirm the bank account by phone "
                          "on a number you already had."))
        elif bal >= SHARE_MIN:
            s.append(_sig("list_large", "review", f"Large payment ${bal:,.0f}",
                          "Confirm the bank account on file hasn't changed recently."))
    return dict(out)


def _iso(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def summary(parsed: dict) -> dict:
    lines = parsed.get("lines") or []
    total = sum((_dec(x["balance"]) or Decimal(0)) for x in lines)
    header = _dec(parsed.get("header_total"))
    return {"count": len(lines), "total": total, "header_total": header,
            "ties": header is None or header == total}
