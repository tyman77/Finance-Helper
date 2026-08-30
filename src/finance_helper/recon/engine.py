"""The matching ladder: tie bank txns to ledger txns, classify the rest.

Passes (each only sees what earlier passes left untied):
  1 exact      same amount, close date, counterparty token overlap -> auto-tied
  3 fuzzy      same amount, wider window, weak/no name signal      -> confirm
  4 split      2-3 same-name ledger items summing to one bank amt  -> confirm
(pass 2, settlement expansion into Ramp/Bill/payroll members, arrives with
those sources in later phases; bank-side those debits already carry a kind.)

Residuals near the period edge are timing, not exceptions — carried visibly,
escalated when aged. Sweep transfers and pending rows never enter matching.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import combinations

from .models import MatchGroup, ReconResult, Txn
from .settings import recon_config

_EXCLUDED_KINDS = {"sweep"}          # internal cash movement, not payments


def _tokens(txn: Txn) -> set[str]:
    return {t for t in txn.counterparty_norm.split() if len(t) >= 3}


def _within(a: Txn, b: Txn, days: int) -> bool:
    return abs((a.posted_date - b.posted_date).days) <= days


def reconcile(bank: list[Txn], ledger: list[Txn],
              progress=None) -> ReconResult:
    cfg = recon_config()["matching"]
    matches: list[MatchGroup] = []
    counter = 0
    note = progress or (lambda msg: None)

    def tie(pass_no: int, bank_txns: list[Txn], ledger_txns: list[Txn], reason: str):
        nonlocal counter
        counter += 1
        mid = f"m{counter:04d}"
        confirmed = pass_no == 1
        for t in bank_txns + ledger_txns:
            t.status = "tied"
            t.match_id = mid
            t.match_pass = pass_no
            t.reason = reason
        matches.append(MatchGroup(
            match_id=mid, match_pass=pass_no,
            bank_ids=[t.source_id for t in bank_txns],
            ledger_ids=[t.source_id for t in ledger_txns],
            reason=reason, confirmed=confirmed,
        ))

    posted = [t for t in bank if not t.pending]
    for t in bank:
        if t.pending:
            t.status = "internal"
            t.reason = "pending — not yet posted, excluded from the tie-out"
        elif t.kind in _EXCLUDED_KINDS:
            t.status = "internal"
            t.reason = "sweep transfer — internal cash movement (companion account proves it)"

    matchable_bank = [t for t in posted if t.status == "untied"]
    period_start = min((t.posted_date for t in matchable_bank), default=None)
    period_end = max((t.posted_date for t in matchable_bank), default=None)

    # No ledger data -> nothing to tie against: this is an activity-only run,
    # and calling every bank line an "exception" would be noise, not findings.
    if not ledger:
        return ReconResult(period_start=period_start, period_end=period_end,
                           bank=bank, ledger=ledger, matches=matches)

    # --- Pass 1: exact -----------------------------------------------------
    by_amount: dict[Decimal, list[Txn]] = {}
    for lt in ledger:
        by_amount.setdefault(lt.amount, []).append(lt)

    for bt in matchable_bank:
        cands = [lt for lt in by_amount.get(bt.amount, [])
                 if lt.status == "untied" and _within(bt, lt, cfg["exact_window_days"])
                 and _tokens(bt) & _tokens(lt)]
        if not cands:
            continue
        best = min(cands, key=lambda lt: abs((bt.posted_date - lt.posted_date).days))
        overlap = ", ".join(sorted(_tokens(bt) & _tokens(best))[:4])
        tie(1, [bt], [best],
            f"exact: {bt.amount} within {cfg['exact_window_days']}d, name overlap [{overlap}]")
    note(f"pass 1 (exact): {sum(1 for m in matches if m.match_pass == 1)} tied")

    # --- Pass 3: fuzzy (amount-only) ---------------------------------------
    for bt in matchable_bank:
        if bt.status != "untied":
            continue
        cands = [lt for lt in by_amount.get(bt.amount, [])
                 if lt.status == "untied" and _within(bt, lt, cfg["fuzzy_window_days"])]
        if not cands:
            continue
        best = min(cands, key=lambda lt: abs((bt.posted_date - lt.posted_date).days))
        gap = abs((bt.posted_date - best.posted_date).days)
        tie(3, [bt], [best],
            f"fuzzy: amount {bt.amount} matches, {gap}d apart, no name overlap — confirm")
    note(f"pass 3 (fuzzy): {sum(1 for m in matches if m.match_pass == 3)} tied")

    # --- Pass 4: split (2-3 ledger items sum to one bank amount) -----------
    # A split's legs share the payee with the bank debit, so only same-name
    # groups are candidates — scanning every vendor's combinations against
    # every debit is both wrong (coincidental sums) and quadratic-cubic slow
    # on a full-year ledger.
    _SPLIT_GROUP_CAP = 20            # closest-by-date legs considered per group

    ledger_by_name: dict[str, list[Txn]] = {}
    name_tokens: dict[str, set[str]] = {}
    for lt in ledger:
        if lt.status == "untied" and lt.counterparty_norm:
            ledger_by_name.setdefault(lt.counterparty_norm, []).append(lt)
    for name in ledger_by_name:
        name_tokens[name] = {t for t in name.split() if len(t) >= 3}

    for bt in matchable_bank:
        if bt.status != "untied":
            continue
        bt_tokens = _tokens(bt)
        found = None
        for name, group in ledger_by_name.items():
            if not (bt_tokens & name_tokens[name]):
                continue
            live = [lt for lt in group
                    if lt.status == "untied" and _within(bt, lt, cfg["split_window_days"])]
            if len(live) > _SPLIT_GROUP_CAP:
                live = sorted(live, key=lambda lt: abs(
                    (bt.posted_date - lt.posted_date).days))[:_SPLIT_GROUP_CAP]
            for n in (2, 3):
                for combo in combinations(live, n):
                    if sum(t.amount for t in combo) == bt.amount:
                        found = list(combo)
                        break
                if found:
                    break
            if found:
                break
        if found:
            parts = " + ".join(str(t.amount) for t in found)
            tie(4, [bt], found, f"split: {parts} = {bt.amount} ({found[0].counterparty_raw[:30]}) — confirm")
    note(f"pass 4 (splits): {sum(1 for m in matches if m.match_pass == 4)} tied")

    # --- Residuals: timing vs exception ------------------------------------
    for t in posted + ledger:
        if t.status != "untied":
            continue
        near_edge = (
            period_start is not None
            and (abs((t.posted_date - period_end).days) <= cfg["timing_window_days"]
                 or abs((t.posted_date - period_start).days) <= cfg["timing_window_days"])
        )
        aged = period_end is not None and (period_end - t.posted_date).days > cfg["aged_timing_days"]
        if near_edge and not aged:
            t.status = "timing"
            t.reason = "near the period boundary — carried forward; escalates if it never ties"
        else:
            t.status = "exception"
            t.reason = ("aged: untied for more than "
                        f"{cfg['aged_timing_days']}d" if aged else "no ledger tie found"
                        ) if t.source == "bank" else (
                        "ledger cash entry with no bank movement")

    return ReconResult(
        period_start=period_start, period_end=period_end,
        bank=bank, ledger=ledger, matches=matches,
    )


def severity_of(txn: Txn) -> str:
    """critical / high / review / timing / internal — for ranking the queue."""
    if txn.status == "internal":
        return "internal"
    if txn.status == "timing":
        return "timing"
    if txn.status == "tied":
        return "review" if txn.match_pass in (3, 4) else "tied"
    if txn.status == "exception":
        if txn.source == "bank" and txn.amount < 0:
            return "critical"
        if txn.source != "bank":
            return "high"
        return "review"          # untied bank inflow: unrecorded receipt
    return "review"
