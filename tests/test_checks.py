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


def test_billcom_tieout_single_and_batch_and_residual():
    d = date
    bank = [
        _t("billcom", "-1234.56", d(2026, 7, 10), "BILL.COM A", "bill.com"),   # 1:1
        _t("billcom", "-3000.00", d(2026, 7, 15), "BILL.COM B", "bill.com"),   # batch of 2
        _t("billcom", "-999.99", d(2026, 7, 20), "BILL.COM C", "bill.com"),    # residual
    ]
    bills = [
        {"id": "p1", "vendor": "Acme Supply", "amount": "1234.56", "date": "2026-07-09"},
        {"id": "p2", "vendor": "CleanCo", "amount": "1800.00", "date": "2026-07-15"},
        {"id": "p3", "vendor": "Yamaha", "amount": "1200.00", "date": "2026-07-15"},
    ]
    out = checks.billcom_tieout(bank, bills)
    assert out["matched"] == 2 and out["checked"] == 3
    assert len(out["findings"]) == 1
    assert out["findings"][0]["severity"] == "critical"
    assert "999.99" in out["findings"][0]["detail"]


def test_billcom_no_data_reports_coverage_gap():
    bank = [_t("billcom", "-100", date(2026, 7, 1), "BILL.COM", "bill.com")]
    out = checks.billcom_tieout(bank, [])
    assert out["coverage"] is False and out["findings"] == []


def test_cross_system_duplicate_same_vendor_same_amount():
    bank = [_t("ach_debit", "-4600.00", date(2026, 7, 20),
               "ACME AV SUPPLY ACH DEBIT", "acme av supply")]
    bills = [{"id": "p9", "vendor": "Acme AV Supply Inc", "amount": "4600.00",
              "date": "2026-07-05"}]
    out = checks.cross_system_duplicates(bank, bills)
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["severity"] == "high" and "Paid twice?" in f["title"]
    # Different vendor, same amount -> no finding.
    bills2 = [{"id": "p9", "vendor": "Totally Different Name", "amount": "4600.00",
               "date": "2026-07-05"}]
    assert checks.cross_system_duplicates(bank, bills2)["findings"] == []


def test_billdotcom_index_from_csv_export_rows():
    from finance_helper import billdotcom_api
    rows = [{"Vendor": "Acme AV Supply", "Payment Date": "07/05/2026",
             "Amount": "$4,600.00", "Payment Confirmation Number": "P123"}]
    idx = billdotcom_api.build_index(rows)
    assert idx == [{"id": "P123", "vendor": "Acme AV Supply", "amount": "4600.00",
                    "date": "2026-07-05", "status": ""}]


def test_billdotcom_falls_back_to_v2_when_v3_login_rejects_key(monkeypatch):
    from finance_helper import billdotcom_api as api
    for k in api._REQUIRED:
        monkeypatch.setenv(k, "x")

    def v3_boom():
        raise RuntimeError('Bill.com login failed: HTTP 400 [{"code":"BDC_1102"...}]')

    monkeypatch.setattr(api, "fetch_payments", v3_boom)
    monkeypatch.setattr(api, "fetch_payments_v2", lambda: [
        {"id": "sp1", "vendorName": "Acme", "amount": 100.0,
         "processDate": "2026-07-01", "status": "1"}])
    idx = api.fetch_index()
    assert idx == [{"id": "sp1", "vendor": "Acme", "amount": "100.00",
                    "date": "2026-07-01", "status": "1"}]

    # Both failing -> combined, actionable error.
    monkeypatch.setattr(api, "fetch_payments_v2",
                        lambda: (_ for _ in ()).throw(RuntimeError("Bill.com v2 error: Invalid API Developer Key")))
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="PRODUCTION API access"):
        api.fetch_index()


def test_vendor_master_bank_change_and_collision_and_lookalikes():
    master = {
        "vendors": [
            {"id": "v1", "name": "Acme AV Supply", "active": True,
             "created": "2024-01-01", "email": "", "payment_email": ""},
            {"id": "v2", "name": "Acme AV Supplies LLC", "active": True,
             "created": "2026-08-01", "email": "", "payment_email": "shady@gmail.com"},
            {"id": "v3", "name": "Jake Cody", "active": True,
             "created": "2026-06-01", "email": "", "payment_email": ""},
        ],
        "bank_accounts": [
            {"vendor_id": "v1", "vendor": "Acme AV Supply",
             "created": "2026-08-20", "active": True},
        ],
        "bills": [],
    }
    out = checks.vendor_master_checks(master, ["Jacob Cody"], today=date(2026, 8, 28))
    kinds = {f["kind"] for f in out["findings"]}
    assert "vendor_lookalike" in kinds            # Acme vs Acme LLC ("supply/supplies"? shared 'acme')
    assert "vendor_personal_email" in kinds       # gmail payment email
    assert "vendor_employee_collision" in kinds   # Jake Cody the vendor
    assert "vendor_bank_change" in kinds          # new bank acct on old vendor
    coll = next(f for f in out["findings"] if f["kind"] == "vendor_employee_collision")
    assert coll["severity"] == "critical"


def test_bill_checks_duplicates_and_sequences():
    bills = [
        {"id": "b1", "vendor": "CleanCo", "invoice": "1001", "invoice_date": "2026-06-01",
         "amount": "500.00", "po": ""},
        {"id": "b2", "vendor": "CleanCo", "invoice": "1001", "invoice_date": "2026-06-15",
         "amount": "500.00", "po": ""},
        {"id": "b3", "vendor": "CleanCo", "invoice": "1002", "invoice_date": "2026-06-20",
         "amount": "500.00", "po": ""},
        {"id": "b4", "vendor": "ShellCo", "invoice": "88", "invoice_date": "2026-05-01", "amount": "1", "po": ""},
        {"id": "b5", "vendor": "ShellCo", "invoice": "89", "invoice_date": "2026-06-01", "amount": "2", "po": ""},
        {"id": "b6", "vendor": "ShellCo", "invoice": "90", "invoice_date": "2026-07-01", "amount": "3", "po": ""},
        {"id": "b7", "vendor": "ShellCo", "invoice": "91", "invoice_date": "2026-08-01", "amount": "4", "po": ""},
    ]
    out = checks.bill_checks(bills)
    kinds = {f["kind"] for f in out["findings"]}
    assert "bill_dup_invoice" in kinds
    assert "bill_same_amount" in kinds            # 1001 vs 1002, same $500, 5 days apart
    assert "bill_sequential" in kinds             # ShellCo 88-91


def test_po_match_missing_overrun_and_retrofit():
    bills = [
        {"id": "b1", "vendor": "Acme", "invoice": "1", "invoice_date": "2026-06-10",
         "amount": "5500.00", "po": "PO-100"},
        {"id": "b2", "vendor": "Yamaha", "invoice": "2", "invoice_date": "2026-06-10",
         "amount": "900.00", "po": "PO-404"},
        {"id": "b3", "vendor": "CleanCo", "invoice": "3", "invoice_date": "2026-06-01",
         "amount": "100.00", "po": "PO-200"},
        {"id": "b4", "vendor": "NoPo", "invoice": "4", "invoice_date": "2026-06-01",
         "amount": "10.00", "po": ""},
    ]
    pos = [
        {"po": "PO-100", "vendor": "Acme", "total": "5000.00", "date": "2026-06-01"},
        {"po": "PO-200", "vendor": "CleanCo", "total": "100.00", "date": "2026-06-15"},
    ]
    out = checks.po_match(bills, pos)
    kinds = {f["kind"] for f in out["findings"]}
    assert "po_overrun" in kinds                  # 5500 vs 5000 (+10%)
    assert "po_missing" in kinds                  # PO-404 doesn't exist
    assert "po_retrofit" in kinds                 # PO-200 dated after invoice
    assert out["bills_with_po"] == 3 and out["bills_without_po"] == 1
