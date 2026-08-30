"""Canonical transaction record for reconciliation.

Every source (bank export, Sage GL detail, later Ramp/Bill.com/payroll)
normalizes into `Txn` before any matching happens. Matching only ever sees
this shape, so adding a source never touches the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Txn:
    source: str                 # bank | sage | ramp | bill | payroll
    source_id: str              # stable id within the source file/system
    posted_date: date
    amount: Decimal             # signed; negative = cash out
    counterparty_raw: str
    counterparty_norm: str      # lowercase tokens, bank noise stripped
    memo: str = ""
    account_ref: str = ""       # bank account label or GL account number
    doc_ref: str = ""           # ledger document/batch number, if the source has one
    kind: str = "other"         # classification bucket (payroll, ramp_reimbursement, ...)
    entity: str = ""            # which company entity the line names, if any
    pending: bool = False
    # Matching state, filled by the engine:
    status: str = "untied"      # untied | tied | exception | timing | internal | intercompany
    match_id: str | None = None
    match_pass: int | None = None
    reason: str = ""            # human explanation of the tie / exception


@dataclass
class MatchGroup:
    """One tie: N bank txns <-> M ledger txns (usually 1<->1)."""
    match_id: str
    match_pass: int             # 1 exact, 2 batch, 3 fuzzy, 4 split
    bank_ids: list[str]
    ledger_ids: list[str]
    reason: str
    confirmed: bool             # passes 1-2 auto-tie; 3/4 need a human


@dataclass
class ReconResult:
    period_start: date
    period_end: date
    bank: list[Txn]
    ledger: list[Txn]
    matches: list[MatchGroup]
    integrity: dict = field(default_factory=dict)   # bank-file continuity check

    @property
    def exceptions(self) -> list[Txn]:
        return [t for t in self.bank + self.ledger if t.status == "exception"]

    @property
    def timing(self) -> list[Txn]:
        return [t for t in self.bank + self.ledger if t.status == "timing"]

    @property
    def intercompany(self) -> list[Txn]:
        return [t for t in self.bank + self.ledger if t.status == "intercompany"]
