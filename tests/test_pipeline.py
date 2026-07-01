"""End-to-end dry-run tests over the sample files (fake data, real headers)."""

from decimal import Decimal

import pytest

from finance_helper import categorize, destinations, sources

SAMPLES = {
    "ups": ("samples/ups_sample.csv", "bill"),
    "united": ("samples/united_sample.csv", "sage"),
    "national": ("samples/national_sample.csv", "bill"),
    "hotel_engine": ("samples/hotel_engine_sample.csv", "sage"),
}


def _processed(source):
    path, _ = SAMPLES[source]
    return categorize.categorize(sources.load(source, path))


@pytest.mark.parametrize("source,expected", SAMPLES.items())
def test_pipeline_builds_payload(source, expected):
    _, destination = expected
    doc = _processed(source)
    assert doc.destination == destination
    assert doc.line_items
    for li in doc.line_items:
        assert li.gl_account, "every line must get a GL account"
        assert li.category, "every line must get a category"
    assert destinations.build_payload(doc)


def test_united_fee_breakout_and_refund():
    doc = _processed("united")
    cats = {li.category for li in doc.line_items}
    assert "travel_baggage" in cats       # "/SECOND CHECKED BAG"
    assert "travel_seat_fees" in cats     # "/PREFERRED ZONE"
    assert "travel_airfare" in cats
    # The refund row stays negative and nets against the total.
    assert any(li.amount < 0 for li in doc.line_items)
    assert doc.total == Decimal("405.00")  # 400 + 60 + 45 - 100


def test_ups_categorization_rules():
    doc = _processed("ups")
    by_desc = {li.description: li for li in doc.line_items}
    assert by_desc["Fuel Surcharge"].gl_account == "5210"
    assert by_desc["Residential Surcharge"].gl_account == "5220"
    assert by_desc["Ground Commercial"].category == "shipping_freight"


def test_hotel_engine_components_tie_to_total():
    doc = _processed("hotel_engine")
    # Each booking's parts (room remainder + components) must sum to the
    # statement total, and the credit line must reduce it.
    assert doc.total == Decimal("732.25")  # 183.52 + 356.86 + 191.87
    assert any(li.category == "travel_credits" and li.amount < 0 for li in doc.line_items)
    assert any(li.category == "travel_lodging_taxes" for li in doc.line_items)


@pytest.mark.parametrize("source", ["united", "hotel_engine"])
def test_sage_journal_entry_balances(source):
    doc = _processed(source)
    payload = destinations.build_payload(doc)
    debits = sum(Decimal(l["debit"]) for l in payload["lines"])
    credits = sum(Decimal(l["credit"]) for l in payload["lines"])
    assert debits == credits, "journal entry must balance"
