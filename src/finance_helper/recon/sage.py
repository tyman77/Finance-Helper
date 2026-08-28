"""Sage Intacct GL-detail CSV -> canonical Txns for the cash account(s).

Column names vary by export template, so the mapping lives in config/recon.yml
(sage.columns). Amounts arrive either as one signed column or a debit/credit
pair; for a cash (asset) account a debit is money IN and a credit is money
OUT, and the loader signs accordingly so bank and ledger amounts compare
directly.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation

from .bank import _parse_date, normalize_counterparty
from .models import Txn
from .settings import recon_config


def _dec(value: str) -> Decimal | None:
    cleaned = (value or "").replace(",", "").replace("$", "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def load_sage_csv(path: str) -> list[Txn]:
    cfg = recon_config()["sage"]
    cols = cfg["columns"]
    cash_accounts = {str(a) for a in cfg.get("cash_accounts") or []}

    txns: list[Txn] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            account = str(row.get(cols["account"], "") or "").strip()
            if cash_accounts and account not in cash_accounts:
                continue
            posted = _parse_date(str(row.get(cols["date"], "") or ""))
            if posted is None:
                continue
            if cols.get("amount"):
                amount = _dec(row.get(cols["amount"], ""))
            else:
                debit = _dec(row.get(cols["debit"], "")) or Decimal("0")
                credit = _dec(row.get(cols["credit"], "")) or Decimal("0")
                amount = debit - credit          # cash account: debit=in, credit=out
                if debit == 0 and credit == 0:
                    amount = None
            if amount is None or amount == 0:
                continue
            desc = str(row.get(cols["description"], "") or "").strip()
            doc = str(row.get(cols.get("doc") or "", "") or "").strip()
            txns.append(Txn(
                source="sage",
                source_id=f"sage:{lineno}",
                posted_date=posted,
                amount=amount,
                counterparty_raw=desc or doc or "(no memo)",
                counterparty_norm=normalize_counterparty(desc),
                memo=str(row.get(cols.get("journal") or "", "") or "").strip(),
                account_ref=account,
            ))
    return txns
