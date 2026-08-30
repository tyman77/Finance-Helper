"""Sweep-account cross-proof.

The checking-side Cash Proof trusts every "TRANSFERRED TO/FROM" line as
internal on the theory that the companion (sweep) account proves it. This
module actually proves it, given the sweep account's own statement:

  - every checking-side sweep must have an equal-and-opposite movement in
    the sweep account within a couple of days — otherwise money left
    checking LABELED as a sweep and never arrived (-> exception, critical);
  - sweep-side transfers with no checking counterpart ("orphans") and any
    sweep-account activity that ISN'T a transfer with checking ("foreign",
    e.g. a wire out of the sweep account) are surfaced for review, because
    they bypass the checking-based proof entirely.
"""

from __future__ import annotations

from .models import Txn


def _row(t: Txn) -> dict:
    return {"date": t.posted_date.isoformat(), "amount": str(t.amount),
            "desc": t.counterparty_raw[:80]}


def cross_proof(main: list[Txn], sweep: list[Txn], window_days: int = 2) -> dict:
    main_sweeps = [t for t in main if t.kind == "sweep" and not t.pending]
    sweep_side = [t for t in sweep if not t.pending]
    sweep_sweeps = [t for t in sweep_side if t.kind == "sweep"]

    used: set[str] = set()
    verified = 0
    for t in main_sweeps:
        match = next(
            (s for s in sweep_sweeps
             if s.source_id not in used and s.amount == -t.amount
             and abs((s.posted_date - t.posted_date).days) <= window_days),
            None)
        if match is not None:
            used.add(match.source_id)
            verified += 1
        else:
            t.status = "exception"
            t.reason = ("sweep-labeled transfer with NO companion movement in "
                        "the sweep account statement — where did it go?")

    orphans = [s for s in sweep_sweeps if s.source_id not in used]
    foreign = [s for s in sweep_side if s.kind != "sweep"]
    return {
        "checked": len(main_sweeps),
        "verified": verified,
        "unverified": len(main_sweeps) - verified,
        "orphans": [_row(s) for s in orphans],
        "foreign": [_row(s) for s in foreign],
    }
