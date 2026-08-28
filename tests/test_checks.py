"""Fraud checks: reimbursement tie-out, phantom per-diem, vendor patterns."""

from datetime import date
from decimal import Decimal

from finance_helper.recon import checks
from finance_helper.recon.models import Txn


def _t(kind, amount, day, raw, norm=None, pending=False):
    return Txn(source="bank", source_id=f"bank:{raw}:{day}", posted_date=day,
               amount=Decimal(amount), counterparty_raw=raw,
               counterparty_norm=norm if norm is not None else raw.lower(),
               kind=kind, pending=pending)


def test_reimbursement_tieout_matches_and_flags():
    bank = [
        _t("ramp_reimbursement", "-250", date(2026, 7, 8),
           "RMPR J CODY ...", "rmpr j cody"),
        _t("ramp_reimbursement", "-400", date(2026, 7, 9),
           "RMPR B FRANKLIN ...", "rmpr b franklin"),
    ]
    ramp = [{"person": "Jake Cody", "date": "2026-07-07", "amount": "250", "memo": "pd"}]
    out = checks.reimbursement_tieout(bank, ramp)
    assert out["matched"] == 1 and out["checked"] == 2
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["severity"] == "critical" and "b franklin" in f["detail"]


def test_reimbursement_duplicate_payout_flagged():
    bank = [
        _t("ramp_reimbursement", "-250", date(2026, 7, 8), "RMPR J CODY", "rmpr j cody"),
        _t("ramp_reimbursement", "-250", date(2026, 7, 10), "RMPR J CODY", "rmpr j cody"),
    ]
    ramp = [{"person": "Jake Cody", "date": "2026-07-07", "amount": "250", "memo": "pd"}]
    out = checks.reimbursement_tieout(bank, ramp)
    assert any(f["kind"] == "reimb_duplicate" and f["severity"] == "high"
               for f in out["findings"])


def test_reimbursement_no_ramp_data_reports_coverage_gap():
    bank = [_t("ramp_reimbursement", "-250", date(2026, 7, 8), "RMPR J CODY", "rmpr j cody")]
    out = checks.reimbursement_tieout(bank, [])
    assert out["coverage"] is False and out["findings"] == []


def test_perdiem_without_any_trip_evidence_is_flagged():
    ramp = [
        {"person": "Jake Cody", "date": "2026-07-08", "amount": "250", "memo": "Per diem"},
        {"person": "Natalie Brady", "date": "2026-07-08", "amount": "175", "memo": "Per diem"},
        {"person": "Lex Bond", "date": "2026-07-08", "amount": "99", "memo": "supplies"},
    ]
    hotel = [{"start": "2026-07-06", "end": "2026-07-09", "project": "4499",
              "guests": ["Natalie Brady"]}]
    flights = [("Jacob Cody", date(2026, 7, 7))]      # fuzzy name still counts
    out = checks.perdiem_no_trip(ramp, hotel, {}, flights)
    assert out["checked"] == 2                        # supplies memo not scanned
    assert out["findings"] == []                      # both corroborated? no —
    # Jake flew, Natalie stayed: neither flagged. Now remove evidence:
    out2 = checks.perdiem_no_trip(ramp, [], {}, [])
    assert {f["title"] for f in out2["findings"]} == {
        "Per diem with no trip evidence: Jake Cody",
        "Per diem with no trip evidence: Natalie Brady"}


def test_perdiem_timecards_count_as_evidence():
    ramp = [{"person": "Jake Cody", "date": "2026-07-08", "amount": "250", "memo": "per diem"}]
    tc = {"Jacob Cody": {"2026-07-07": "4499"}}
    out = checks.perdiem_no_trip(ramp, [], tc, [])
    assert out["findings"] == []


def test_vendor_integrity_patterns():
    d = date
    bank = (
        # New payee inside the window (period ends 8/20).
        [_t("ach_debit", "-2000", d(2026, 8, 10), "SHADY LLC ACH DEBIT", "shady llc")]
        # Threshold hugging: two $4,600 payments days apart (band 4000-5000).
        + [_t("ach_debit", "-4600", d(2026, 8, 1), "ACME SUPPLY", "acme supply"),
           _t("ach_debit", "-4600", d(2026, 8, 5), "ACME SUPPLY", "acme supply")]
        # Old vendor with round dollars (first payment far before period end).
        + [_t("check", "-1500", d(2026, 5, 1), "CLEANCO", "cleanco"),
           _t("check", "-1200", d(2026, 6, 1), "CLEANCO", "cleanco"),
           _t("check", "-900", d(2026, 7, 1), "CLEANCO", "cleanco")]
        + [_t("deposit", "3000", d(2026, 8, 20), "REGULAR DEPOSIT", "regular deposit")]
    )
    out = checks.vendor_integrity(bank)
    kinds = {f["kind"] for f in out["findings"]}
    assert "new_vendor" in kinds
    assert "threshold_split" in kinds
    assert "round_dollar" in kinds
    # CLEANCO is not a new vendor (first seen months before period end).
    assert not any(f["kind"] == "new_vendor" and "CLEANCO" in f["title"]
                   for f in out["findings"])


def test_finding_ids_are_stable_across_runs():
    bank = [_t("ramp_reimbursement", "-250", date(2026, 7, 8), "RMPR J CODY", "rmpr j cody")]
    a = checks.reimbursement_tieout(bank, [{"person": "X", "date": "2026-07-08",
                                            "amount": "1", "memo": ""}])
    b = checks.reimbursement_tieout(bank, [{"person": "X", "date": "2026-07-08",
                                            "amount": "1", "memo": ""}])
    assert a["findings"][0]["id"] == b["findings"][0]["id"]
    assert a["findings"][0]["id"].startswith("check:")
