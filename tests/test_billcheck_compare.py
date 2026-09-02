"""Bill Check comparison rules: entered Bill.com fields vs what the invoice says."""

from datetime import date

from finance_helper.billcheck import compare


def _bill(**over):
    base = {"id": "b1", "vendor": "Acme Supply Co", "invoice": "INV-1001",
            "invoice_date": "2026-08-01", "due_date": "2026-08-31",
            "amount": "1500.00", "terms": "Net 30", "terms_days": 30, "po": ""}
    base.update(over)
    return base


def _pdf(**over):
    base = {"is_invoice": True, "vendor": "ACME Supply Company, Inc.",
            "invoice_number": "1001", "invoice_date": "2026-08-01",
            "due_date": "2026-08-31", "terms": "Net 30", "terms_days": 30,
            "total": "1500.00", "currency": "USD", "po_number": None,
            "confidence": "high", "notes": ""}
    base.update(over)
    return base


def _by_field(result):
    return {f["field"]: f for f in result["findings"]}


def test_terms_parsing():
    assert compare.parse_terms_days("Net 30") == 30
    assert compare.parse_terms_days("2% 10 Net 45") == 45
    assert compare.parse_terms_days("Due upon receipt") == 0
    assert compare.parse_terms_days("Payment due within 15 days") == 15
    assert compare.parse_terms_days("") is None
    assert compare.parse_terms_days("Thank you") is None


def test_invoice_number_normalization():
    n = compare.normalize_invoice_number
    assert n("INV-000123") == n("123") == n("inv 123") == "123"
    assert n("#A-77") == "A77"
    assert n("") == ""


def test_vendor_matching_tolerates_suffixes_and_case():
    assert compare.vendor_matches("Acme Supply Co., Inc.", "ACME SUPPLY")
    assert compare.vendor_matches("Grainger", "W.W. Grainger Inc")
    assert not compare.vendor_matches("Acme Supply", "Zenith Logistics LLC")


def test_amount_parsing():
    assert str(compare.to_amount("$1,234.5")) == "1234.50"
    assert str(compare.to_amount("(45.00)")) == "-45.00"
    assert compare.to_amount("") is None


def test_clean_match():
    r = compare.compare_bill(_bill(), _pdf())
    assert r["status"] == "match" and r["severity"] == "clear" and not r["findings"]
    assert {row["state"] for row in r["fields"] if row["field"] != "po"} == {"match"}


def test_total_mismatch_is_critical():
    r = compare.compare_bill(_bill(amount="1050.00"), _pdf())
    f = _by_field(r)["amount"]
    assert f["severity"] == "critical" and "-450.00" in f["reason"]
    assert r["status"] == "mismatch"


def test_due_date_later_than_printed_is_critical():
    r = compare.compare_bill(_bill(due_date="2026-09-30"), _pdf())
    f = _by_field(r)["due_date"]
    assert f["severity"] == "critical"
    assert "30 days after 2026-08-31" in f["reason"] and "paid late" in f["reason"]
    assert r["expected_due"] == "2026-08-31"


def test_due_date_earlier_than_printed_is_high():
    r = compare.compare_bill(_bill(due_date="2026-08-15"), _pdf())
    f = _by_field(r)["due_date"]
    assert f["severity"] == "high" and "paid early" in f["reason"]


def test_due_date_derived_from_printed_terms():
    # Invoice prints "Net 45" but no due date; Bill.com used Net 30.
    r = compare.compare_bill(_bill(due_date="2026-08-31"),
                             _pdf(due_date=None, terms="Net 45", terms_days=45))
    f = _by_field(r)["due_date"]
    assert f["severity"] == "high" and f["pdf"] == "2026-09-15"
    assert "Net 45" in r["expected_due_basis"]


def test_due_date_from_billcom_terms_only_is_review():
    # Nothing about due date/terms on the invoice; vendor default is Net 30 in Bill.com.
    r = compare.compare_bill(_bill(due_date="2026-10-15"),
                             _pdf(due_date=None, terms=None, terms_days=None))
    f = _by_field(r)["due_date"]
    assert f["severity"] == "review" and "Bill.com" in f["reason"]


def test_terms_due_anchors_on_invoice_pdf_date_when_entered_date_wrong():
    # Clerk typed the wrong invoice date, and the due date followed it.
    r = compare.compare_bill(_bill(invoice_date="2026-08-10", due_date="2026-09-09"),
                             _pdf(due_date=None))
    fs = _by_field(r)
    assert fs["invoice_date"]["severity"] == "high" and "9 days after" in fs["invoice_date"]["reason"]
    assert fs["due_date"]["pdf"] == "2026-08-31"


def test_missing_due_date_entered_is_high():
    r = compare.compare_bill(_bill(due_date=""), _pdf())
    assert _by_field(r)["due_date"]["severity"] == "high"


def test_no_due_info_anywhere_flags_only_odd_gaps():
    pdf = _pdf(due_date=None, terms=None, terms_days=None)
    assert "due_date" not in _by_field(compare.compare_bill(_bill(terms_days=None), pdf))
    assert _by_field(compare.compare_bill(
        _bill(terms_days=None, due_date="2026-07-20"), pdf))["due_date"]["severity"] == "high"
    assert _by_field(compare.compare_bill(
        _bill(terms_days=None, due_date="2026-12-20"), pdf))["due_date"]["severity"] == "review"


def test_invoice_number_and_vendor_mismatches_are_high():
    r = compare.compare_bill(_bill(invoice="INV-1002"), _pdf(vendor="Zenith Logistics"))
    fs = _by_field(r)
    assert fs["invoice"]["severity"] == "high"
    assert fs["vendor"]["severity"] == "high"


def test_pdf_missing_fields_are_review_not_mismatch():
    r = compare.compare_bill(_bill(), _pdf(invoice_number=None, total=None, invoice_date=None))
    assert r["status"] == "review"
    assert {f["severity"] for f in r["findings"]} == {"review"}


def test_non_invoice_currency_and_low_confidence_are_review():
    r = compare.compare_bill(_bill(), _pdf(is_invoice=False, currency="CAD",
                                           confidence="low", notes="statement"))
    kinds = [(f["field"], f["severity"]) for f in r["findings"]]
    assert ("document", "review") in kinds and ("currency", "review") in kinds
    assert r["status"] == "review"


def test_po_mismatch_is_review():
    r = compare.compare_bill(_bill(po="PO-55"), _pdf(po_number="PO-56"))
    assert _by_field(r)["po"]["severity"] == "review"


def test_unreadable_extraction():
    r = compare.compare_bill(_bill(), None)
    assert r["status"] == "unreadable" and r["findings"][0]["field"] == "document"


def test_findings_sorted_worst_first():
    r = compare.compare_bill(_bill(amount="1.00", invoice="X"), _pdf(currency="EUR"))
    sevs = [f["severity"] for f in r["findings"]]
    assert sevs == sorted(sevs, key=lambda s: compare.SEVERITY_ORDER[s])


def test_find_duplicates_across_queue_and_history():
    bills = [_bill(id="a"), _bill(id="b", invoice="1001", amount="1500.00"),
             _bill(id="c", invoice="9")]
    history = [{"id": "old", "vendor": "ACME Supply", "invoice": "inv-1001",
                "amount": "1500.00", "invoice_date": "2026-05-01", "payment_status": "paid"}]
    dups = compare.find_duplicates(bills, history)
    assert set(dups) == {"a", "b"}
    assert len(dups["a"]) == 2 and any("paid" in d for d in dups["a"])
    assert "c" not in dups


def test_parse_date_formats():
    assert compare.parse_date("08/01/2026") == date(2026, 8, 1)
    assert compare.parse_date("Aug 1, 2026") == date(2026, 8, 1)
    assert compare.parse_date("2026-08-01T00:00:00") == date(2026, 8, 1)
    assert compare.parse_date("soon") is None


# --- early-payment discounts (the Gator Cases case) -------------------------

def _gator_pdf(**over):
    base = dict(vendor="Gator Co.", invoice_number="1386039-IN", invoice_date="2026-08-25",
                due_date=None, terms="5% 25 Days, Net 26", terms_days=26, total="597.45",
                discount_total="567.58", discount_date="2026-09-19",
                discount_terms="5% 25 Days", po_number="PO011224")
    return _pdf(**{**base, **over})


def _gator_bill(**over):
    base = dict(vendor="Gator Cases", invoice="1386039-IN", invoice_date="2026-08-25",
                due_date="2026-09-19", amount="567.58", terms="Net 25", terms_days=25,
                po="PO011224")
    return _bill(**{**base, **over})


def test_discount_taken_with_due_on_cutoff_is_clean():
    r = compare.compare_bill(_gator_bill(), _gator_pdf())
    assert r["status"] == "match" and r["discount_taken"] is True
    assert r["expected_due"] == "2026-09-19"
    row = next(x for x in r["fields"] if x["field"] == "discount")
    assert row["entered"] == "taken" and row["state"] == "match" and "567.58 by 2026-09-19" in row["pdf"]


def test_discount_taken_but_due_after_cutoff_is_critical():
    r = compare.compare_bill(_gator_bill(due_date="2026-09-25"), _gator_pdf())
    f = _by_field(r)["due_date"]
    assert f["severity"] == "critical" and "short-pays the vendor by 29.87" in f["reason"]
    assert "amount" not in _by_field(r)


def test_full_amount_and_net_date_entered_is_a_deviation():
    # House rule: the discount is always taken, so the full amount on the
    # net date is wrong on both counts.
    r = compare.compare_bill(_gator_bill(amount="597.45", due_date="2026-09-20"), _gator_pdf())
    fs = _by_field(r)
    assert fs["discount"]["severity"] == "high" and "enter 567.58 due 2026-09-19" in fs["discount"]["reason"]
    assert fs["due_date"]["severity"] == "critical" and "forfeits the discount (29.87)" in fs["due_date"]["reason"]
    assert "amount" not in fs
    assert r["expected_due"] == "2026-09-19"


def test_due_before_cutoff_is_high():
    r = compare.compare_bill(_gator_bill(due_date="2026-09-10"), _gator_pdf())
    f = _by_field(r)["due_date"]
    assert f["severity"] == "high" and "before the 2026-09-19 early-pay cut-off" in f["reason"]


def test_cutoff_derived_from_discount_days_when_not_printed():
    pdf = _gator_pdf(discount_date=None, discount_days=None, discount_terms="5% 25 Days")
    r = compare.compare_bill(_gator_bill(due_date="2026-09-19"), pdf)
    assert r["status"] == "match" and r["expected_due"] == "2026-09-19"
    assert compare._discount_days("2% 10 Net 30") == 10
    assert compare._discount_days("2% Discount if paid in 90 days; Net 91.") == 90
    assert compare._discount_days("Net 30") is None


def test_yamaha_case():
    pdf = _pdf(vendor="Yamaha Corporation Of America", invoice_number="8345744",
               invoice_date="2026-05-29", due_date="2026-08-28",
               terms="2% Discount if paid in 90 days; Net 91.", terms_days=91,
               total="21599.28", discount_total="21167.29", discount_date="2026-08-27",
               discount_terms="2% Discount if paid in 90 days")
    bill = _bill(vendor="Yamaha", invoice="8345744", invoice_date="2026-06-01",
                 due_date="2026-08-30", amount="21167.29", terms="Net 90", terms_days=90)
    r = compare.compare_bill(bill, pdf)
    fs = _by_field(r)
    assert fs["due_date"]["severity"] == "critical" and "short-pays the vendor by 431.99" in fs["due_date"]["reason"]
    assert fs["invoice_date"]["severity"] == "high"
    assert r["expected_due"] == "2026-08-27"


def test_amount_matching_neither_total_names_both():
    r = compare.compare_bill(_gator_bill(amount="580.00"), _gator_pdf())
    f = _by_field(r)["amount"]
    assert f["severity"] == "critical" and "597.45 (full) or 567.58 if paid by 2026-09-19" in f["reason"]


def test_no_discount_row_when_invoice_has_none():
    r = compare.compare_bill(_bill(), _pdf())
    assert not any(x["field"] == "discount" for x in r["fields"])

# --- vendor policy: quick-pay never taken (TD Synnex) -----------------------

POLICIES = {"TD SYNNEX": {"quickpay": "never"}}


def test_quickpay_policy_expects_net_terms_not_discount_date():
    # TD Synnex prints the discount deadline as the due date; we never take
    # the discount, so an entered net-terms due date is correct.
    bill = _bill(vendor="TD SYNNEX Corp", due_date="2026-09-15", terms="Net 45",
                 terms_days=45)
    pdf = _pdf(vendor="TD SYNNEX", due_date="2026-08-11",
               terms="2% 10 Net 45", terms_days=45)
    # Without the policy: flagged critical as "would be paid late".
    without = _by_field(compare.compare_bill(bill, pdf))
    assert without["due_date"]["severity"] == "critical"
    # With the policy: clean, and the basis names the policy.
    r = compare.compare_bill(bill, pdf, policies=POLICIES)
    assert "due_date" not in _by_field(r)
    assert "never taken" in r["expected_due_basis"]


def test_quickpay_policy_still_flags_beyond_net_terms():
    bill = _bill(vendor="TD SYNNEX", due_date="2026-11-30", terms="Net 45",
                 terms_days=45)
    pdf = _pdf(vendor="TD SYNNEX", due_date="2026-08-11",
               terms="2% 10 Net 45", terms_days=45)
    f = _by_field(compare.compare_bill(bill, pdf, policies=POLICIES))
    assert f["due_date"]["severity"] == "critical"      # genuinely late


def test_quickpay_policy_keeps_printed_net_due_when_later():
    # A printed due date AT/AFTER net terms is the real net due date.
    bill = _bill(vendor="TD SYNNEX", due_date="2026-09-15")
    pdf = _pdf(vendor="TD SYNNEX", due_date="2026-09-20",
               terms="2% 10 Net 45", terms_days=45)
    f = _by_field(compare.compare_bill(bill, pdf, policies=POLICIES))
    assert f["due_date"]["pdf"] == "2026-09-20"


def test_quickpay_policy_leaves_other_vendors_alone():
    bill = _bill(due_date="2026-09-15")
    pdf = _pdf(due_date="2026-08-11", terms="2% 10 Net 45", terms_days=45)
    f = _by_field(compare.compare_bill(bill, pdf, policies=POLICIES))
    assert f["due_date"]["severity"] == "critical"      # Acme has no policy


def test_quickpay_policy_overrides_always_take_rule_end_to_end():
    # TD Synnex with a full discount offer on the invoice: entering the FULL
    # amount at NET terms is clean — no 'discount not taken', no due-date flag.
    pdf = _pdf(vendor="TD SYNNEX", invoice_date="2026-08-01", due_date="2026-08-11",
               terms="2% 10 Net 45", terms_days=45, total="1500.00",
               discount_total="1470.00", discount_date="2026-08-11")
    bill = _bill(vendor="TD SYNNEX Corporation", due_date="2026-09-15",
                 amount="1500.00", terms="Net 45", terms_days=45)
    r = compare.compare_bill(bill, pdf, policies=POLICIES)
    fs = _by_field(r)
    assert "discount" not in fs and "due_date" not in fs and "amount" not in fs
    assert r["expected_due"] == "2026-09-15"


VENDOR_DEALS = {
    "Gator": {"quickpay": "take", "pct": 5},
    "Shure": {"quickpay": "take", "pct": 2},
    "Yamaha": {"quickpay": "take", "pct": 2,
               "notes": "no QP on warranty work"},
    "Elite Core": {"due_anchor": "ship_date"},
}


def test_deal_pct_mismatch_is_flagged_for_review():
    # Gator's negotiated deal is 5%; this invoice only offers 3%.
    pdf = _gator_pdf(discount_total="579.53", discount_terms="3% 25 Days")
    r = compare.compare_bill(_gator_bill(amount="579.53"), pdf,
                             policies=VENDOR_DEALS)
    f = _by_field(r)["discount"]
    assert f["severity"] == "review"
    assert "3.0% offered" in f["entered"] + f["pdf"]
    assert "negotiated deal with this vendor is 5%" in f["reason"]


def test_deal_vendor_invoice_without_discount_flags_missed_savings():
    pdf = _pdf(vendor="Yamaha Corporation", discount_total=None,
               discount_date=None, discount_terms=None)
    bill = _bill(vendor="Yamaha")
    f = _by_field(compare.compare_bill(bill, pdf, policies=VENDOR_DEALS))["discount"]
    assert f["severity"] == "review"
    assert "money left on the table" in f["reason"]
    assert "no QP on warranty work" in f["reason"]     # deal notes echoed


def test_matching_deal_pct_stays_quiet():
    # 5% offered, 5% negotiated: no deal finding on a clean Gator bill.
    r = compare.compare_bill(_gator_bill(), _gator_pdf(), policies=VENDOR_DEALS)
    assert r["status"] == "match"


def test_ship_date_anchor_policy():
    # Elite Core: Net 30 runs from the SHIP date, not the invoice date.
    pdf = _pdf(vendor="Elite Core Audio", due_date=None, terms="Net 30",
               terms_days=30, ship_date="2026-08-10")
    bill = _bill(vendor="Elite Core", due_date="2026-09-09")   # ship + 30
    r = compare.compare_bill(bill, pdf, policies=VENDOR_DEALS)
    assert "due_date" not in _by_field(r)
    assert r["expected_due"] == "2026-09-09"
    assert "SHIP date" in r["expected_due_basis"]
