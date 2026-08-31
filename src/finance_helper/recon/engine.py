"""The matching ladder: tie bank txns to ledger txns, classify the rest.

Passes (each only sees what earlier passes left untied):
  1 exact      same amount, close date, counterparty token overlap -> auto-tied
  2 batch      a ledger batch (doc no / batch memo / day+journal)
               sums exactly to one bank movement                   -> auto-tied
  3 fuzzy      same amount, wider window, weak/no name signal      -> confirm
  4 split      2-3 same-name ledger items summing to one bank amt  -> confirm

Pass 2 is what makes a real GL usable: payroll runs, Ramp settlements and
Bill.com funding hit the bank as ONE consolidated debit but post to the GL
as per-item lines ("Batch Summary" rows). An exact-to-the-cent n-way sum
within the window is strong evidence, so it auto-ties like pass 1.

Residuals near the period edge are timing, not exceptions — carried visibly,
escalated when aged. Sweep transfers and pending rows never enter matching.
"""

from __future__ import annotations

import re
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
        confirmed = pass_no in (1, 2)
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

    # Related entities whose books live OUTSIDE Sage (config: entities.external)
    # can never auto-tie — carve them out up front, on both sides, so they
    # don't flood the exception queue run after run. They keep their own
    # status ('intercompany') and are NOT counted as tied dollars.
    ext_pats = [(name, re.compile(rf"\b{re.escape(str(name))}\b", re.I))
                for name in recon_config().get("entities", {}).get("external", [])]

    def _external_entity(t: Txn) -> str | None:
        for name, pat in ext_pats:
            if pat.search(t.counterparty_raw):
                return str(name)
        return None

    posted = [t for t in bank if not t.pending]
    for t in bank:
        if t.pending:
            t.status = "internal"
            t.reason = "pending — not yet posted, excluded from the tie-out"
        elif t.kind in _EXCLUDED_KINDS:
            t.status = "internal"
            t.reason = "sweep transfer — internal cash movement (companion account proves it)"
        else:
            ext = _external_entity(t)
            if ext:
                t.status = "intercompany"
                t.reason = (f"names {ext}, whose books are outside Sage — "
                            "verify against that entity's own records")
    for t in ledger:
        ext = _external_entity(t)
        if ext:
            t.status = "intercompany"
            t.reason = (f"names {ext}, whose books are outside Sage — "
                        "verify against that entity's own records")
        elif "funds transfer" in t.counterparty_norm:
            # GL mirror of an account-to-account transfer: the bank side is a
            # sweep (internal), and the sweep statement cross-proof is what
            # verifies the cash — these rows are bookkeeping, not payments.
            t.status = "internal"
            t.reason = ("GL side of an account transfer — the bank side is a "
                        "sweep; the sweep-statement cross-proof verifies the cash")

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

    # --- Pass 2: batch (one bank movement = many ledger lines) -------------
    # Batch identity, strongest first: document number; the EXACT raw
    # description (each Bill.com batch stamps one unique "Payments(...):
    # <timestamp> Batch Summary" string on all its lines — the normalized
    # name would merge same-day batches); then (day, description) and
    # (day, journal). The GL posts batches weeks before the cash moves, so
    # the bank-vs-ledger gap uses batch_window_days, not the fuzzy window.
    bw = cfg.get("batch_window_days", 45)

    def _identity_groups(rows: list[Txn]) -> dict[tuple, list[Txn]]:
        groups: dict[tuple, list[Txn]] = {}
        for lt in rows:
            if lt.doc_ref:
                groups.setdefault(("doc", lt.doc_ref), []).append(lt)
            if len(lt.counterparty_raw) >= 20:
                groups.setdefault(("stmt", lt.counterparty_raw), []).append(lt)
        return groups

    # 2a — wash: an identity group holding both directions that nets to
    # exactly zero is an entry+reversal pair (voided payment, resync). No
    # bank movement will ever exist for it; clear it as internal.
    washed = 0
    for (_kind, _key), rows in _identity_groups(
            [lt for lt in ledger if lt.status == "untied"]).items():
        if (len(rows) >= 2 and any(t.amount > 0 for t in rows)
                and any(t.amount < 0 for t in rows)
                and sum(t.amount for t in rows) == 0
                and all(t.status == "untied" for t in rows)):
            for t in rows:
                t.status = "internal"
                t.reason = ("offsetting ledger batch — entry and reversal net "
                            "to zero; no bank movement expected")
            washed += len(rows)
    # 2a.2 — reversal pairing: a "Reversed Payments(...)" batch posts under
    # its OWN timestamp, so raw-string identity can't see that its lines
    # cancel the original batch's. Pair each reversal line with the nearest
    # equal-and-opposite original line (same description family, digits
    # ignored) — both are bookkeeping, no bank movement.
    pool2 = [lt for lt in ledger if lt.status == "untied"]
    originals: dict[tuple, list[Txn]] = {}
    for t in pool2:
        if not t.counterparty_norm.startswith("reversed"):
            originals.setdefault((t.counterparty_norm, t.amount), []).append(t)
    paired = 0
    for t in pool2:
        if t.status != "untied" or not t.counterparty_norm.startswith("reversed"):
            continue
        base = t.counterparty_norm[len("reversed"):].strip()
        cands = [o for o in originals.get((base, -t.amount), [])
                 if o.status == "untied"
                 and abs((o.posted_date - t.posted_date).days) <= bw]
        if cands:
            o = min(cands, key=lambda x: abs((x.posted_date - t.posted_date).days))
            for x in (t, o):
                x.status = "internal"
                x.reason = ("payment line and its reversal — net zero; "
                            "no bank movement expected")
            paired += 2
    if paired:
        note(f"pass 2a (reversals): {paired} entry+reversal lines cleared")
    if washed:
        note(f"pass 2a (wash): {washed} offsetting ledger lines cleared")

    # 2b — sum-match what's left against untied bank amounts.
    pool = [lt for lt in ledger if lt.status == "untied"]
    all_groups: list[tuple[str, list[Txn]]] = []
    for (kind, key), rows in _identity_groups(pool).items():
        label = f"doc {key}" if kind == "doc" else f"batch “{str(key)[:40]}”"
        all_groups.append((label, rows))
    day_name_groups: dict[tuple, list[Txn]] = {}
    day_jrnl_groups: dict[tuple, list[Txn]] = {}
    for lt in pool:
        if lt.counterparty_norm:
            day_name_groups.setdefault(
                (lt.posted_date, lt.counterparty_norm), []).append(lt)
        if lt.memo:
            day_jrnl_groups.setdefault((lt.posted_date, lt.memo), []).append(lt)
    for key, rows in day_name_groups.items():
        all_groups.append((f"day-batch of {key[0]}", rows))
    for key, rows in day_jrnl_groups.items():
        all_groups.append((f"{key[1]} journal on {key[0]}", rows))

    by_sum: dict[Decimal, list[tuple[str, list[Txn]]]] = {}
    for label, rows in all_groups:
        if len(rows) < 2 and not (
                # a one-bill batch still deserves the wide batch window
                len(rows) == 1 and "batch" in rows[0].counterparty_raw.lower()):
            continue
        total = sum(t.amount for t in rows)
        if total:
            by_sum.setdefault(total, []).append((label, rows))

    for bt in matchable_bank:
        if bt.status != "untied":
            continue
        for label, rows in by_sum.get(bt.amount, []):
            if any(t.status != "untied" for t in rows):
                continue                     # partially consumed elsewhere
            if not all(_within(bt, t, bw) for t in rows):
                continue
            tie(2, [bt], rows,
                f"batch: {len(rows)} ledger lines ({label}) sum to {bt.amount}")
            break

    # 2d — processor-fee deposits: the books record a deposit/receipt batch
    # GROSS; the bank receives NET after the processor (Shopify, card
    # settlement) withholds its fee. A batch grossing slightly MORE than a
    # bank credit (≤5% short, inside the window) is that payout — tie as a
    # confirmable naming the withheld amount.
    _FEE_MAX = Decimal("0.05")
    dep_groups = [(label, rows, sum(t.amount for t in rows))
                  for label, rows in all_groups
                  if rows and all(t.amount > 0 for t in rows)]
    for bt in matchable_bank:
        if bt.status != "untied" or bt.amount <= 0:
            continue
        best = None
        for label, rows, total in dep_groups:
            if total <= bt.amount:
                continue
            shortfall = total - bt.amount
            pct = shortfall / total
            if pct > _FEE_MAX:
                continue
            if any(t.status != "untied" for t in rows):
                continue
            if not all(_within(bt, t, bw) for t in rows):
                continue
            if best is None or pct < best[3]:
                best = (label, rows, total, pct)
        if best is not None:
            label, rows, total, pct = best
            tie(3, [bt], rows,
                f"deposit batch ({label}) grosses {total}; bank received "
                f"{bt.amount} — {total - bt.amount} ({pct * 100:.1f}%) "
                "withheld as processor fees — confirm")

    # 2b' — near-miss batches: a group summing within $1 of a bank movement
    # is almost always the same money with a rounding/true-up error in the
    # JE (two owner-distribution entries of 37,224.88 vs a 74,449.36 bank
    # transfer = 40 cents apart, unexplained on both sides all year).
    # Surface it as a confirmable tie that names the delta.
    _TOL = Decimal("1.00")
    sums_list = [(total, label, rows)
                 for total, entries in by_sum.items() for label, rows in entries]
    for bt in matchable_bank:
        if bt.status != "untied":
            continue
        for total, label, rows in sums_list:
            delta = total - bt.amount
            if delta == 0 or abs(delta) > _TOL:
                continue
            if any(t.status != "untied" for t in rows):
                continue
            if not all(_within(bt, t, bw) for t in rows):
                continue
            tie(3, [bt], rows,
                f"near-miss batch: {len(rows)} ledger lines ({label}) sum to "
                f"{total}, off by {delta} vs {bt.amount} — confirm & true up the JE")
            break

    # 2c — one GL batch = SEVERAL bank movements (a day's deposit batch in
    # the books arrives at the bank as multiple credits). Try 2-4 same-sign
    # untied bank txns near the batch summing to the group total.
    for label, rows in all_groups:
        if len(rows) < 2 or any(t.status != "untied" for t in rows):
            continue
        total = sum(t.amount for t in rows)
        if not total:
            continue
        lo = min(t.posted_date for t in rows)
        hi = max(t.posted_date for t in rows)
        bcands = sorted(
            (bt for bt in matchable_bank
             if bt.status == "untied" and (bt.amount > 0) == (total > 0)
             and (lo - timedelta(days=10)) <= bt.posted_date <= (hi + timedelta(days=10))),
            key=lambda bt: bt.posted_date)[:40]
        found = None
        for n in (2, 3, 4):
            for combo in combinations(bcands, n):
                if sum(b.amount for b in combo) == total:
                    found = list(combo)
                    break
            if found:
                break
        if found:
            tie(2, found, rows,
                f"batch: {len(rows)} ledger lines ({label}) = "
                f"{len(found)} bank movements summing to {total}")
    note(f"pass 2 (batches): {sum(1 for m in matches if m.match_pass == 2)} tied")

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
    # 3b — monthly summary JEs ("Record monthly rent") post weeks away from
    # the standing bank transfer they mirror: exact amount + a shared name
    # token earns the wide batch window (still needs a human confirm).
    for bt in matchable_bank:
        if bt.status != "untied":
            continue
        cands = [lt for lt in by_amount.get(bt.amount, [])
                 if lt.status == "untied" and _within(bt, lt, bw)
                 and _tokens(bt) & _tokens(lt)]
        if not cands:
            continue
        best = min(cands, key=lambda lt: abs((bt.posted_date - lt.posted_date).days))
        gap = abs((bt.posted_date - best.posted_date).days)
        overlap = ", ".join(sorted(_tokens(bt) & _tokens(best))[:4])
        tie(3, [bt], [best],
            f"amount {bt.amount} + name [{overlap}], {gap}d apart (wide window) — confirm")
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
    # Aux (clearing/limbo) rows exist to EXPLAIN bank movements: tied ones
    # helped; untied ones net out within their own account over time and are
    # not cash exceptions. Only the real cash account earns "ledger cash
    # entry with no bank movement".
    aux_accounts = {str(a) for a in recon_config()["sage"].get("aux_accounts") or []}
    for t in posted + ledger:
        if t.status != "untied":
            continue
        if t.source != "bank" and t.account_ref in aux_accounts:
            t.status = "internal"
            t.reason = (f"sibling/clearing-account activity ({t.account_ref}) "
                        "with no movement in THIS statement — its own "
                        "statement/clearing cycle covers it")
            continue
        near_edge = (
            period_start is not None
            and (abs((t.posted_date - period_end).days) <= cfg["timing_window_days"]
                 or abs((t.posted_date - period_start).days) <= cfg["timing_window_days"])
        )
        # A GL batch entry posted within the batch-funding window of the
        # statement's end CANNOT tie yet — its bank movement likely falls
        # after the export. Timing, not an exception.
        recent_batch = (
            t.source != "bank" and period_end is not None
            and (period_end - t.posted_date).days <= bw
            and "batch summary" in t.counterparty_raw.lower()
        )
        aged = period_end is not None and (period_end - t.posted_date).days > cfg["aged_timing_days"]
        if (near_edge or recent_batch) and not aged:
            t.status = "timing"
            t.reason = ("posted within the batch-funding window of the statement "
                        "end — its bank movement likely falls after this export"
                        if recent_batch and not near_edge else
                        "near the period boundary — carried forward; escalates if it never ties")
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
    if txn.status == "intercompany":
        return "intercompany"
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
