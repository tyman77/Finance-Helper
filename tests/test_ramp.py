"""Ramp per-diem: index building, flight matching, admin fetch. Network mocked."""

import json
from datetime import date
from decimal import Decimal

import pytest

from finance_helper import ramp_api, sources, enrich, project_resolver


def test_build_index_extracts_person_date_and_memo_code():
    recs = [
        {"user_full_name": "Jake Cody", "transaction_date": "2026-07-08T12:00:00Z",
         "amount": 250, "memo": "Per diem - Northview 4499"},
        {"user": {"first_name": "Natalie", "last_name": "Brady"},
         "created_at": "2026-07-10", "amount": 175, "memo": "per diem"},
    ]
    idx = ramp_api.build_index(recs)
    assert idx[0]["person"] == "Jake Cody" and idx[0]["project"] == "4499"
    assert idx[1]["person"] == "Natalie Brady" and idx[1]["project"] is None


def test_build_index_raises_with_raw_record_when_nothing_maps():
    with pytest.raises(RuntimeError, match="weird_key"):
        ramp_api.build_index([{"weird_key": "x"}])


def test_ramp_matches_use_asymmetric_window_and_fuzzy_names():
    idx = [
        {"person": "Jacob Cody", "date": "2026-07-20", "project": "4499", "memo": "pd"},
        {"person": "Jake Cody", "date": "2026-06-01", "project": "1111", "memo": "pd"},
    ]
    # Departure 7/07: payout on 7/20 (13 days after) is in-window; June's is not.
    codes, hits = project_resolver.ramp_matches_for_person(idx, "Jake Cody", date(2026, 7, 7))
    assert codes == ["4499"] and len(hits) == 1


def _tmap():
    return {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": [], "n": 10}}


def _doc(depart="07/07/2026"):
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = depart
    return doc


def _john(doc):
    return next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")


def test_flight_auto_tags_from_perdiem_memo():
    doc = _doc()
    ramp = [{"person": "John Doe", "date": "2026-07-12", "project": "4499",
             "memo": "Per diem 4499"}]
    enrich.enrich_united(doc, _tmap(), schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=[], ramp_index=ramp)
    john = _john(doc)
    assert john.project == "4499" and john.gl_account == "52200"
    assert "ramp per-diem memo" in john.note


def test_codeless_perdiem_corroborates_but_does_not_tag():
    doc = _doc()
    ramp = [{"person": "John Doe", "date": "2026-07-12", "project": None, "memo": "per diem"}]
    enrich.enrich_united(doc, _tmap(), schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=[], ramp_index=ramp)
    john = _john(doc)
    assert john.project is None
    assert "trip corroborated" in john.note


def test_hotel_stay_outranks_ramp_memo():
    doc = _doc()
    hotel = [{"start": "2026-07-06", "end": "2026-07-09", "project": "3531",
              "guests": ["John Doe"]}]
    ramp = [{"person": "John Doe", "date": "2026-07-12", "project": "4499", "memo": "pd 4499"}]
    enrich.enrich_united(doc, _tmap(), schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=hotel, ramp_index=ramp)
    assert _john(doc).project == "3531"          # hotel stay is the stronger rung


def test_admin_fetch_writes_index(tmp_path, monkeypatch):
    from finance_helper.web import admin as admin_module
    from finance_helper.web.app import RUNS, create_app
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(ramp_api, "fetch_index",
                        lambda s, e: [{"person": "Jake Cody", "date": "2026-07-08",
                                       "amount": "250", "memo": "pd 4499", "project": "4499"}])
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        resp = c.post("/admin/ramp", data={"days": "90"})
        assert resp.status_code == 302
        assert b"1 carry a project" in c.get("/admin/").data
    idx = json.load(open(tmp_path / "data" / "ramp_reimbursements.json"))
    assert idx[0]["project"] == "4499"
    RUNS.clear()
