"""Domain pages (Flights / Hotels / Rental Cars) and the flow chart builder."""

import io
from decimal import Decimal

import pytest

from finance_helper import insights
from finance_helper.recon import summary as recon_summary
from finance_helper.web.app import RUNS, create_app


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


def _upload(client, source, path):
    with open(path, "rb") as fh:
        data = {"source": source, "file": (io.BytesIO(fh.read()), path.split("/")[-1])}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302


def test_domain_pages_render_and_nav_is_present(client):
    for domain, needle in (("flights", b"Flights"), ("hotels", b"Hotels"), ("cars", b"Rental Cars")):
        resp = client.get(f"/d/{domain}")
        assert resp.status_code == 200
        assert needle in resp.data
    assert client.get("/d/nonsense").status_code == 302


def test_flights_page_shows_united_statement(client):
    _upload(client, "united", "samples/united_sample.csv")
    body = client.get("/d/flights").data
    assert b"Total spend" in body
    assert b"united_sample.csv" in body
    assert b"Open \xe2\x86\x92" in body          # link to the review screen
    # A hotel-engine statement must NOT appear on the Flights page.
    _upload(client, "hotel_engine", "samples/hotel_engine_sample.csv")
    body2 = client.get("/d/flights").data
    assert b"hotel_engine_sample.csv" not in body2
    assert b"hotel_engine_sample.csv" in client.get("/d/hotels").data


def test_build_domain_filters_by_source_group():
    from finance_helper import pipeline
    doc = pipeline.process("united", "samples/united_sample.csv")
    run = {"doc": doc, "source": "united", "filename": "u.csv", "created": None, "posted": None}
    flights = insights.build_domain([("r1", run)], "flights")
    hotels = insights.build_domain([("r1", run)], "hotels")
    assert flights["line_count"] > 0
    assert flights["statements"][0]["run_id"] == "r1"
    assert hotels["line_count"] == 0 and not hotels["statements"]


def test_flow_chart_geometry_scales_both_series():
    months = [{"month": "2026-06", "in": Decimal("1000"), "out": Decimal("-500")},
              {"month": "2026-07", "in": Decimal("0"), "out": Decimal("-2000")}]
    chart = recon_summary.flow_chart(months)
    assert len(chart["columns"]) == 2
    jun = chart["columns"][0]
    assert {b["series"] for b in jun["bars"]} == {"in", "out"}
    jul_out = next(b for b in chart["columns"][1]["bars"] if b["series"] == "out")
    jun_out = next(b for b in jun["bars"] if b["series"] == "out")
    assert jul_out["h"] > jun_out["h"] > 0       # heights scale with magnitude
    assert jul_out["value"] == 2000.0            # positive magnitude for display


def test_hotels_detail_groups_components_into_bookings():
    from finance_helper import pipeline
    doc = pipeline.process("hotel_engine", "samples/hotel_engine_sample.csv")
    detail = insights.hotels_detail([doc])
    assert detail["booking_count"] == 3
    assert detail["total_nights"] == 7            # 1 + 5 + 1
    b2 = next(b for b in detail["bookings"] if b["invoice"] == "260615000002")
    assert b2["nights"] == 5
    assert b2["total"] == Decimal("356.86")       # components net to the row total
    labels = dict(detail["components"])
    assert "Taxes & fees" in labels and "Credits redeemed" in labels
    assert any(d[0] == "Install" for d in detail["departments"])


def test_hotels_page_renders_booking_report(client):
    _upload(client, "hotel_engine", "samples/hotel_engine_sample.csv")
    body = client.get("/d/hotels").data
    assert b"Avg nightly rate" in body
    assert b"What each stay is made of" in body
    assert b"Bookings (3)" in body
    assert b"Fairfield Inn Example" in body
    # Flights page must not grow hotel sections.
    assert b"Avg nightly rate" not in client.get("/d/flights").data
