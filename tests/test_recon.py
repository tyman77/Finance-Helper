"""Cash Proof: bank parser, integrity check, Sage adapter, matching engine."""

from decimal import Decimal

from finance_helper.recon import bank, engine, sage, summary

BANK = "samples/bank_sample.csv"
SAGE = "samples/sage_gl_sample.csv"


def _run():
    b = bank.load_bank_csv(BANK)
    l = sage.load_sage_csv(SAGE)
    return engine.reconcile(b, l)


def test_bank_parser_classifies_and_flags():
    txns = bank.load_bank_csv(BANK)
    kinds = {t.kind for t in txns}
    assert {"ramp_reimbursement", "sweep", "payroll", "check", "deposit"} <= kinds
    assert sum(t.pending for t in txns) == 1
    # Daily Ledger Bal marker rows never become transactions.
    assert not any("daily ledger" in t.counterparty_raw.lower() for t in txns)
    paychex = next(t for t in txns if t.kind == "payroll")
    assert paychex.entity == "Summit Assembly LLC"
    # Bank noise (ACH DEBIT, ids, own entity, bank name) stripped from the norm.
    ramp = next(t for t in txns if t.kind == "ramp_reimbursement")
    assert "ach" not in ramp.counterparty_norm
    assert "flatirons" not in ramp.counterparty_norm  # from recon.yml noise_words


def test_bank_integrity_ok_on_sample():
    chk = bank.integrity_check(BANK)
    assert chk["ok"] and not chk["breaks"]


def test_bank_integrity_detects_missing_row(tmp_path):
    # Drop one posted transaction: the day-end balances no longer reconcile.
    lines = open(BANK).read().splitlines()
    doctored = [l for l in lines if "GHOST" not in l]
    p = tmp_path / "doctored.csv"
    p.write_text("\n".join(doctored) + "\n")
    chk = bank.integrity_check(str(p))
    assert not chk["ok"] and chk["breaks"]


def test_bank_integrity_pairs_offsetting_skew(tmp_path):
    # Same rows, but one balance shifted a day late and back: skew, not break.
    lines = open(BANK).read().splitlines()
    swapped = [l.replace("101800", "102550") if "GHOST" in l else l for l in lines]
    p = tmp_path / "skewed.csv"
    p.write_text("\n".join(swapped) + "\n")
    chk = bank.integrity_check(str(p))
    assert chk["ok"] and len(chk["skews"]) == 1


def test_sage_loader_filters_to_cash_account_and_signs():
    txns = sage.load_sage_csv(SAGE)
    assert all(t.account_ref == "10700" for t in txns)   # 52200 row excluded
    dep = next(t for t in txns if "Northview" in t.counterparty_raw)
    assert dep.amount == Decimal("3000.00")              # debit to cash = in
    chk = next(t for t in txns if "2041" in t.counterparty_raw)
    assert chk.amount == Decimal("-450.00")              # credit to cash = out


def test_ladder_ties_exact_fuzzy_and_split():
    res = _run()
    by_pass = {}
    for m in res.matches:
        by_pass.setdefault(m.match_pass, []).append(m)
    assert len(by_pass[1]) == 3                    # acme, check, deposit
    assert all(m.confirmed for m in by_pass[1])
    assert len(by_pass[3]) == 1 and not by_pass[3][0].confirmed
    assert len(by_pass[4]) == 1                    # paychex -8000 = -5000 + -3000
    assert "= -8000" in by_pass[4][0].reason


def test_residual_classification_and_severity():
    res = _run()
    ghost = next(t for t in res.bank if "GHOST" in t.counterparty_raw)
    assert ghost.status == "exception"
    assert engine.severity_of(ghost) == "critical"
    rent = next(t for t in res.ledger if "rent" in t.counterparty_raw.lower())
    assert rent.status == "exception"
    assert engine.severity_of(rent) == "high"
    lifeway = next(t for t in res.bank if "LIFEWAY" in t.counterparty_raw)
    assert lifeway.status == "timing"
    sweep = next(t for t in res.bank if t.kind == "sweep")
    assert sweep.status == "internal"
    pending = next(t for t in res.bank if t.pending)
    assert pending.status == "internal"


def test_summary_and_tie_stats():
    res = _run()
    act = summary.build(res.bank)
    assert act["txn_count"] == 8                   # pending excluded
    assert act["total_in"] == Decimal("5500")
    assert act["total_out"] == Decimal("-16334.56")
    # Sweeps never appear in the outflow leaderboard.
    assert not any("transferred" in (o.get("label") or "").lower()
                   for o in act["top_outflows"])
    stats = summary.tie_stats(res.bank)
    # Outflows: 16334.56 total; untied only GHOST 750 -> tied 15584.56.
    assert stats["out_total"] == Decimal("16334.56")
    assert stats["out_tied"] == Decimal("15584.56")
