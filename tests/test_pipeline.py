"""End-to-end dry-run tests for the ingest -> categorize -> build payload flow."""

from decimal import Decimal

import pytest

from finance_helper import categorize, destinations, sources

SAMPLES = {
    "ups": ("samples/ups_sample.csv", "bill"),
    "united": ("samples/united_sample.csv", "sage"),
    "national": ("samples/national_sample.csv", "bill"),
    "hotel_engine": ("samples/hotel_engine_sample.csv", "sage"),
}


@pytest.mark.parametrize("source,expected", SAMPLES.items())
def test_pipeline_builds_payload(source, expected):
    path, destination = expected
    doc = sources.load(source, path)
    assert doc.destination == destination
    assert doc.line_items, "should parse at least one line item"

    doc = categorize.categorize(doc)
    for li in doc.line_items:
        assert li.gl_account, "every line must get a GL account"
        assert li.category, "every line must get a category"

    payload = destinations.build_payload(doc)
    assert payload


def test_ups_categorization_rules():
    doc = categorize.categorize(sources.load("ups", "samples/ups_sample.csv"))
    by_desc = {li.description: li for li in doc.line_items}
    # Fuel surcharge rule should win over the shipping_freight default.
    assert by_desc["Fuel Surcharge"].gl_account == "5210"
    assert by_desc["Residential Surcharge"].gl_account == "5220"
    # A plain ground line falls back to the source default (shipping_freight).
    assert by_desc["Ground Commercial"].category == "shipping_freight"


def test_sage_journal_entry_balances():
    doc = categorize.categorize(sources.load("united", "samples/united_sample.csv"))
    payload = destinations.build_payload(doc)
    debits = sum(Decimal(l["debit"]) for l in payload["lines"])
    credits = sum(Decimal(l["credit"]) for l in payload["lines"])
    assert debits == credits, "journal entry must balance"
    assert credits == doc.total
