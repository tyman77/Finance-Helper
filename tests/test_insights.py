"""Insights aggregation: grouping, dedup, and the /insights page."""

import io
from datetime import date, datetime
from decimal import Decimal

import pytest

from finance_helper import insights
from finance_helper.models import LineItem, SourceDocument
from finance_helper.web.app import RUNS, create_app


def _doc(source="united", doc_id="X", items=None, doc_date=None):
    return SourceDocument(source=source, destination="sage", vendor="United Airlines",
                          document_id=doc_id, currency="USD",
                          line_items=items or [], document_date=doc_date)


def _run(doc, created=None):
    return {"doc": doc, "source": doc.source, "filename": "x.csv",
            "created": created or datetime(2026, 7, 1), "posted": None}


def test_groups_map_categories_to_buckets():
    assert insights.group_for("travel_airfare") == "Flights"
    assert insights.group_for("travel_airfare_fees") == "Flights"
    assert insights.group_for("travel_lodging_taxes") == "Hotels"
    assert insights.group_for("travel_car_rental") == "Cars"
    assert insights.group_for("shipping_freight") == "Shipping"
    assert insights.group_for(None) == "Other"


def test_duplicate_statement_uploads_count_once():
    """The same statement re-processed twice must not double the totals."""
    li = LineItem(description="x", amount=Decimal("100"), category="travel_airfare",
                  date=date(2026, 6, 1))
    old = _run(_doc(items=[li]), created=datetime(2026, 7, 1))
    new = _run(_doc(items=[li, LineItem(description="y", amount=Decimal("50"),
                                        category="travel_airfare", date=date(2026, 6, 2))]),
               created=datetime(2026, 7, 2))
    data = insights.build([old, new])
    assert data["total"] == Decimal("150")  # newest run only, not 100+150
    assert data["run_count"] == 1


def test_build_aggregates_by_month_project_person():
    items = [
        LineItem(description="a", amount=Decimal("100"), category="travel_airfare",
                 date=date(2026, 6, 1), project="4804", person="Jake Cody"),
        LineItem(description="b", amount=Decimal("200"), category="travel_lodging",
                 date=date(2026, 7, 1), person="Jake Cody"),
    ]
    data = insights.build([_run(_doc(items=items))])
    assert data["by_group"]["Flights"] == Decimal("100")
    assert data["by_group"]["Hotels"] == Decimal("200")
    assert data["months"] == ["2026-06", "2026-07"]
    assert data["projects"] == [("4804", Decimal("100"))]
    assert data["people"] == [("Jake Cody", Decimal("300"), 2)]
    assert data["coded_pct"] == pytest.approx(100 * 100 / 300)


def test_monthly_chart_stacks_and_scales():
    by_mg = {"2026-06": {"Flights": Decimal("100"), "Hotels": Decimal("50")}}
    chart = insights.monthly_chart(["2026-06"], by_mg, ["Flights", "Hotels"])
    col = chart["columns"][0]
    assert len(col["segments"]) == 2
    # Stacked: the Hotels segment sits above (smaller y) is false — Flights is
    # drawn first from the baseline, so Hotels has the smaller y.
    flights = next(s for s in col["segments"] if s["group"] == "Flights")
    hotels = next(s for s in col["segments"] if s["group"] == "Hotels")
    assert hotels["y"] < flights["y"]
    assert flights["h"] == pytest.approx(2 * hotels["h"], abs=0.3)


def test_monthly_chart_negative_amounts_clamped():
    by_mg = {"2026-06": {"Flights": Decimal("-25")}}
    chart = insights.monthly_chart(["2026-06"], by_mg, ["Flights"])
    assert chart["columns"][0]["segments"] == []  # nothing drawn below baseline


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


def test_insights_page_empty_state(client):
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert b"No data yet" in resp.data


def test_insights_page_renders_charts_from_a_run(client):
    items = [LineItem(description="a", amount=Decimal("100"), category="travel_airfare",
                      date=date(2026, 6, 1), project="4804", person="Jake Cody")]
    RUNS["r1"] = _run(_doc(items=items))
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert b"Total spend" in resp.data
    assert b"Jake Cody" in resp.data
    assert b"4804" in resp.data
    assert b"<svg" in resp.data
