"""Parse the bank's activity export into canonical Txns.

Built against the real operating-account export:
    "Date","Ref/Check","Description","Amount","Balance","Memo","Category"
with three quirks the parser must handle:
  - "Daily Ledger Bal" marker rows (no amount) — informational, skipped;
  - "Pending: ..." rows — parsed but flagged, excluded from matching;
  - it's a target-balance sweep account, so TRANSFERRED TO/FROM DEPOSIT ACCT
    lines are internal cash movement, not payments — classified `sweep` and
    excluded from the tie-out (proving them needs the companion account).

The Balance column enables a free integrity check: per-day net movement must
equal the change in day-end balance. A break means rows are missing from (or
were edited out of) the export itself.
"""

from __future__ import annotations

import csv
import re
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .models import Txn

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d")

# (pattern, kind) — first hit wins, so specific before generic.
_KIND_RULES = [
    (re.compile(r"^RMPR\b"), "ramp_reimbursement"),
    (re.compile(r"^RAMP\b"), "ramp_settlement"),
    (re.compile(r"BILL[. ]?COM", re.I), "billcom"),
    (re.compile(r"PAYCHEX|PAYLOCITY|\bPAYROLL\b", re.I), "payroll"),
    (re.compile(r"TRANSFERRED (TO|FROM) .*ACCT", re.I), "sweep"),
    (re.compile(r"^WO-"), "wire_out"),
    (re.compile(r"^WI-"), "wire_in"),
    (re.compile(r"^CHECK\b", re.I), "check"),
    (re.compile(r"\bDEPOSIT\b", re.I), "deposit"),
    (re.compile(r"\bINTEREST\b", re.I), "interest"),
    (re.compile(r"\bFEE\b", re.I), "fee"),
    (re.compile(r"ACH DEBIT", re.I), "ach_debit"),
    (re.compile(r"ACH CREDIT", re.I), "ach_credit"),
]

_ENTITIES = [
    (re.compile(r"SUMMIT ASSEMBLY( LLC)?", re.I), "Summit Assembly LLC"),
    (re.compile(r"SUMMIT ?INTEGRATED( SYST\w*)?", re.I), "Summit Integrated"),
]

# Tokens that are bank plumbing, not counterparty identity. recon.yml's
# bank.noise_words adds deployment-specific ones (e.g. the bank's own name,
# which appears inside ACH descriptors).
_NOISE = {
    "ach", "debit", "credit", "online", "trf", "tr", "pending",
    "receivable", "payable", "llc", "inc", "corp",
}


def _extra_noise() -> set[str]:
    from . import settings
    return {w.lower() for w in settings.recon_config().get("bank", {}).get("noise_words", [])}
_MASKED = re.compile(r"^x{2,}\d*$", re.I)
_IDISH = re.compile(r"^(?=.*\d)[a-z0-9]{6,}$", re.I)  # mixed alnum ids with a digit


def _parse_date(value: str):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def classify(description: str) -> str:
    for pat, kind in _KIND_RULES:
        if pat.search(description):
            return kind
    return "other"


def entity_of(description: str) -> str:
    for pat, name in _ENTITIES:
        if pat.search(description):
            return name
    return ""


def normalize_counterparty(description: str) -> str:
    text = description
    for pat, _name in _ENTITIES:            # our own entity isn't the counterparty
        text = pat.sub(" ", text)
    noise = _NOISE | _extra_noise()
    tokens = []
    for tok in re.split(r"[^A-Za-z0-9.]+", text.lower()):
        if not tok or tok in noise or tok.isdigit():
            continue
        if _MASKED.match(tok) or _IDISH.match(tok):
            continue
        tokens.append(tok)
    return " ".join(tokens)


def load_bank_csv(path: str, account_ref: str = "operating") -> list[Txn]:
    txns: list[Txn] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            desc = (row.get("Description") or "").strip()
            if not desc or "daily ledger bal" in desc.lower():
                continue
            pending = desc.startswith("Pending:")
            if pending:
                desc = desc[len("Pending:"):].strip()
            posted = _parse_date(row.get("Date", ""))
            try:
                amount = Decimal((row.get("Amount") or "").replace(",", ""))
            except InvalidOperation:
                continue
            if posted is None:
                continue
            ref = (row.get("Ref/Check") or "").strip()
            kind = "check" if (ref and classify(desc) == "other") else classify(desc)
            txns.append(Txn(
                source="bank",
                source_id=f"bank:{lineno}",
                posted_date=posted,
                amount=amount,
                counterparty_raw=desc if not ref else f"{desc} #{ref}",
                counterparty_norm=normalize_counterparty(desc),
                memo=(row.get("Memo") or "").strip(),
                account_ref=account_ref,
                kind=kind,
                entity=entity_of(desc),
                pending=pending,
            ))
    return txns


def integrity_check(path: str) -> dict:
    """Per-day net movement vs change in reported day-end balance.

    Intra-day ordering in exports is unreliable, so days are the unit: for each
    posted day, sum(amounts) must equal day-end balance minus the previous
    day-end balance. Any break means the export is missing rows — which is
    itself a finding (a doctored export is how an unrecorded disbursement
    would be hidden from this tool).
    """
    days: "OrderedDict[str, dict]" = OrderedDict()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    # Exports arrive newest-first; orient oldest-first by date.
    def keydate(r):
        return _parse_date(r.get("Date", "")) or datetime.min.date()
    if rows and keydate(rows[0]) > keydate(rows[-1]):
        rows = list(reversed(rows))
    for row in rows:
        desc = (row.get("Description") or "").strip()
        if desc.startswith("Pending:"):
            continue
        posted = _parse_date(row.get("Date", ""))
        if posted is None:
            continue
        day = days.setdefault(posted.isoformat(), {"net": Decimal("0"), "end": None})
        try:
            day["net"] += Decimal((row.get("Amount") or "0").replace(",", "") or "0")
        except InvalidOperation:
            pass
        bal = (row.get("Balance") or "").replace(",", "").strip()
        if bal:
            day["end"] = Decimal(bal)  # last listed balance of the day wins

    raw_breaks = []
    prev_end = None
    for iso, d in days.items():
        if d["end"] is None:
            continue
        if prev_end is not None and prev_end + d["net"] != d["end"]:
            raw_breaks.append({
                "date": iso,
                "expected_end": str(prev_end + d["net"]),
                "reported_end": str(d["end"]),
                "gap": d["end"] - (prev_end + d["net"]),
            })
        prev_end = d["end"]

    # A transaction posted on one day but balance-effective the next shows up
    # as two consecutive breaks with offsetting gaps. That's date skew, not a
    # missing row — pair those off; only unpaired gaps are real breaks.
    breaks, skews = [], []
    i = 0
    while i < len(raw_breaks):
        cur = raw_breaks[i]
        if i + 1 < len(raw_breaks) and raw_breaks[i + 1]["gap"] == -cur["gap"]:
            skews.append({"dates": [cur["date"], raw_breaks[i + 1]["date"]],
                          "amount": str(abs(cur["gap"]))})
            i += 2
            continue
        breaks.append({**cur, "gap": str(cur["gap"])})
        i += 1
    return {"days": len(days), "breaks": breaks, "skews": skews, "ok": not breaks}
