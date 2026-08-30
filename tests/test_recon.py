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


def test_ladder_ties_exact_batch_and_fuzzy():
    res = _run()
    by_pass = {}
    for m in res.matches:
        by_pass.setdefault(m.match_pass, []).append(m)
    assert len(by_pass[1]) == 3                    # acme, check, deposit
    assert all(m.confirmed for m in by_pass[1])
    # Paychex -8000: two same-day payroll lines -5000 + -3000 -> batch tie,
    # auto-confirmed (the settlement-expansion pass).
    assert len(by_pass[2]) == 1 and by_pass[2][0].confirmed
    assert "sum to -8000" in by_pass[2][0].reason
    assert len(by_pass[3]) == 1 and not by_pass[3][0].confirmed


def _txn(source, i, day, amount, name, memo="", doc=""):
    from datetime import date as _d
    from finance_helper.recon.models import Txn
    return Txn(source=source, source_id=f"{source}:{i}",
               posted_date=_d(2026, 6, day), amount=Decimal(amount),
               counterparty_raw=name, counterparty_norm=name,
               memo=memo, doc_ref=doc)


def test_batch_pass_ties_doc_group_spanning_days():
    # One bank debit = 3 ledger lines sharing a document number over 2 days.
    bank_txns = [_txn("bank", 1, 10, "-900", "billcom payment")]
    ledger = [_txn("sage", i, d, a, "batch summary 6284", memo="CD", doc="BATCH-77")
              for i, (d, a) in enumerate([(9, "-300"), (9, "-350"), (10, "-250")])]
    res = engine.reconcile(bank_txns, ledger)
    (m,) = res.matches
    assert m.match_pass == 2 and m.confirmed and len(m.ledger_ids) == 3
    assert "doc BATCH-77" in m.reason


def test_batch_pass_keeps_same_day_batches_separate_and_bridges_weeks():
    # Two Bill.com batches posted the SAME day: identical normalized name,
    # unique raw strings. Funding debits hit the bank ~4 weeks later.
    from datetime import date as _d
    raw_a = "Payments(Bank-BNK1): 2026/02/15 15:27:54:1111 Batch Summary"
    raw_b = "Payments(Bank-BNK1): 2026/02/15 16:02:11:2222 Batch Summary"
    ledger = []
    for i, (raw, amts) in enumerate([(raw_a, ["-300", "-200"]),
                                     (raw_b, ["-450", "-50", "-100"])]):
        for j, a in enumerate(amts):
            t = _txn("sage", i * 10 + j, 1, a, "payments bank bnk1 batch summary",
                     memo="CD")
            t.counterparty_raw = raw
            ledger.append(t)
    bank_txns = [_txn("bank", 1, 28, "-500", "billcom payables"),
                 _txn("bank", 2, 28, "-600", "billcom payables")]
    res = engine.reconcile(bank_txns, ledger)
    assert len([m for m in res.matches if m.match_pass == 2]) == 2
    assert all(t.status == "tied" for t in ledger)


def test_untied_aux_clearing_rows_go_internal_not_exception():
    # A Money-In Clearing receipt with no bank movement is clearing churn,
    # not a cash exception; an untied CASH-account row still is one.
    clearing = _txn("sage", 1, 5, "250", "customer receipt via billcom")
    clearing.account_ref = "10704"
    cash = _txn("sage", 2, 5, "-900", "mystery vendor payment")
    cash.account_ref = "10700"
    bank_txns = [_txn("bank", 1, 20, "-123", "unrelated debit")]
    engine.reconcile(bank_txns, [clearing, cash])
    assert clearing.status == "internal"
    assert "clearing" in clearing.reason
    assert cash.status == "exception"


def test_wash_pass_clears_entry_plus_reversal():
    # A voided payment: entry and reversal share the doc, net zero, and no
    # bank movement will ever exist for them.
    ledger = [_txn("sage", 1, 5, "-800", "voided vendor payment", doc="V-9"),
              _txn("sage", 2, 6, "800", "voided vendor payment", doc="V-9")]
    bank_txns = [_txn("bank", 1, 10, "-123", "unrelated debit")]
    res = engine.reconcile(bank_txns, ledger)
    assert all(t.status == "internal" for t in ledger)
    assert "reversal" in ledger[0].reason
    assert not any(t.status == "exception" for t in ledger)


def test_split_pass_ties_same_name_items_across_docs():
    # No shared doc/day group, but 2 same-name items sum to the debit -> pass 4.
    bank_txns = [_txn("bank", 1, 10, "-700", "acme av supply")]
    ledger = [_txn("sage", 1, 10, "-300", "acme av supply", memo="CD", doc="A1"),
              _txn("sage", 2, 11, "-400", "acme av supply", memo="CR", doc="B2")]
    res = engine.reconcile(bank_txns, ledger)
    (m,) = res.matches
    assert m.match_pass == 4 and not m.confirmed and len(m.ledger_ids) == 2


def test_external_entity_carved_out_on_both_sides():
    # TJW's books are outside Sage (config entities.external): its lines get
    # status 'intercompany' on both sides — never exceptions, never tied %.
    bank_txns = [_txn("bank", 1, 10, "-1000", "xit cpa tjw"),
                 _txn("bank", 2, 10, "-500", "acme av supply")]
    bank_txns[0].counterparty_raw = "XIT CPA TJW Inc ACH DEBIT ANC-01800"
    bank_txns[1].counterparty_raw = "ACME AV SUPPLY"
    ledger = [_txn("sage", 1, 4, "-37224.88", "record monthly transfer to tjw"),
              _txn("sage", 2, 10, "-500", "acme av supply")]
    ledger[0].counterparty_raw = "Record monthly transfer to TJW"
    res = engine.reconcile(bank_txns, ledger)
    assert bank_txns[0].status == "intercompany"
    assert ledger[0].status == "intercompany"
    assert "TJW" in bank_txns[0].reason
    assert engine.severity_of(bank_txns[0]) == "intercompany"
    assert res.intercompany and len(res.intercompany) == 2
    # The unrelated pair still ties normally.
    assert bank_txns[1].status == "tied"
    stats = summary.tie_stats(bank_txns)
    assert stats["out_intercompany"] == Decimal("1000")
    assert stats["out_total"] == Decimal("500")     # TJW excluded from the base


def test_split_pass_ignores_unrelated_name_groups():
    # Coincidental sum under a different vendor name must NOT tie.
    bank_txns = [_txn("bank", 1, 10, "-700", "acme av supply")]
    ledger = [_txn("sage", 1, 10, "-300", "other vendor llc"),
              _txn("sage", 2, 11, "-400", "other vendor llc")]
    res = engine.reconcile(bank_txns, ledger)
    assert not res.matches


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
