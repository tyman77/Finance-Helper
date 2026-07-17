"""End-to-end dry-run tests over the sample files (fake data, real headers)."""

from decimal import Decimal

import pytest

from finance_helper import destinations, enrich, pipeline, sources

SAMPLES = {
    "ups": ("samples/ups_sample.csv", "bill"),
    "united": ("samples/united_sample.csv", "sage"),
    "national": ("samples/national_sample.csv", "bill"),
    "hotel_engine": ("samples/hotel_engine_sample.csv", "sage"),
}


@pytest.mark.parametrize("source,expected", SAMPLES.items())
def test_pipeline_builds_payload(source, expected):
    _, destination = expected
    doc = pipeline.process(source, SAMPLES[source][0])
    assert doc.destination == destination
    assert doc.line_items
    for li in doc.line_items:
        assert li.gl_account, "every line must get a GL account"
        assert li.category, "every line must get a category"
    assert destinations.build_payload(doc)


def test_hotel_engine_components_tie_to_total():
    doc = pipeline.process("hotel_engine", SAMPLES["hotel_engine"][0])
    assert doc.total == Decimal("732.25")  # 183.52 + 356.86 + 191.87
    assert any(li.category == "travel_credits" and li.amount < 0 for li in doc.line_items)
    assert any(li.category == "travel_lodging_taxes" for li in doc.line_items)


def test_enrich_united_from_history():
    """Department is trusted; account is a hint that always needs review."""
    doc = sources.load("united", SAMPLES["united"][0])
    tmap = {
        "DOE/JOHN": {
            "person": "John Doe",
            "department": "10--Sales Team",
            "department_confidence": 1.0,
            "account_hint": "52200--COGS Travel: Flights / Parking",
            "account_confidence": 0.5,
            "n": 10,
        }
    }
    enrich.enrich_united(doc, tmap)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.person == "John Doe"
    assert john.department == "10"  # bare code, normalized from "10--Sales Team"
    assert john.gl_account == "52200"
    assert john.needs_review, "account must be confirmed by a human"
    # Unknown travelers are flagged, not silently coded.
    smith = next(li for li in doc.line_items if "SMITH" in li.raw.get("Passenger Name", ""))
    assert smith.needs_review


def test_past_projects_offered_as_candidates_when_no_live_match():
    """With no schedule/calendar/registry hit, the traveler's own historical
    project codes are surfaced as pick-one candidates, archived ones dropped."""
    doc = sources.load("united", SAMPLES["united"][0])
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH Travel",
                         "account_confidence": 0.6, "projects": ["4804", "3428"], "n": 10}}
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects={"4804"})  # 3428 archived
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert "past projects 4804 — pick one" in john.note
    assert "3428" not in john.note  # archived code never offered


@pytest.mark.parametrize("source", ["united", "hotel_engine"])
def test_sage_journal_entry_balances(source):
    doc = pipeline.process(source, SAMPLES[source][0])
    payload = destinations.build_payload(doc)
    debits = sum(Decimal(l["debit"]) for l in payload["lines"])
    credits = sum(Decimal(l["credit"]) for l in payload["lines"])
    assert debits == credits, "journal entry must balance"


def test_sage_lines_carry_department_dimension():
    doc = sources.load("united", SAMPLES["united"][0])
    enrich.enrich_united(doc, {
        "DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                     "department_confidence": 1.0, "account_hint": "52200--x",
                     "account_confidence": 1.0, "n": 5}
    })
    from finance_helper import categorize
    categorize.categorize(doc)
    payload = destinations.build_payload(doc)
    assert any(line.get("department") == "10" for line in payload["lines"])
