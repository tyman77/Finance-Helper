"""Paychex timecards: index building, top-rung flight resolution, admin routes."""

import io
import json

import pytest

from finance_helper import enrich, paychex_api, sources


def test_build_index_from_csv_style_rows():
    rows = [
        {"Employee Name": "Cody, Jake", "Work Date": "07/07/2026",
         "Job Code": "4499 - Northview Church", "Hours": "8"},
        {"Employee Name": "Cody, Jake", "Work Date": "07/08/2026",
         "Job Code": "4499 - Northview Church", "Hours": "8"},
        {"Employee Name": "Brady, Natalie", "Work Date": "07/07/2026",
         "Job Code": "PTO", "Hours": "8"},                    # no code -> skipped
    ]
    idx = paychex_api.build_index(rows)
    assert idx == {"Jake Cody": {"2026-07-07": "4499", "2026-07-08": "4499"}}


def test_build_index_from_api_style_rows_with_nested_job():
    rows = [{"workerName": "Jake Cody", "businessDate": "2026-07-07",
             "job": {"code": "4499", "name": "Northview"}}]
    idx = paychex_api.build_index(rows)
    assert idx["Jake Cody"]["2026-07-07"] == "4499"


def test_build_index_raises_with_raw_row_when_nothing_maps():
    with pytest.raises(RuntimeError, match="mystery_col"):
        paychex_api.build_index([{"mystery_col": "x"}])


def test_timecards_are_the_top_rung_for_any_department():
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = "07/07/2026"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": [], "n": 10}}
    timecards = {"John Doe": {"2026-07-08": "4499", "2026-07-09": "4499"}}
    # A conflicting hotel stay exists — timecards must still win.
    hotel = [{"start": "2026-07-06", "end": "2026-07-09", "project": "3531",
              "guests": ["John Doe"]}]
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=hotel,
                         ramp_index=[], timecard_index=timecards)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project == "4499"
    assert john.gl_account == "52200"
    assert "logged hours to project 4499" in john.note


def test_timecard_index_matches_fuzzy_person_key():
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = "07/07/2026"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": [], "n": 10}}
    timecards = {"Johnathan Doe": {"2026-07-08": "4499"}}     # payroll legal name
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=[],
                         ramp_index=[], timecard_index=timecards)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project == "4499"


def test_admin_csv_upload_builds_and_accumulates(tmp_path, monkeypatch):
    from finance_helper.web.app import RUNS, create_app
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        body1 = "Employee Name,Work Date,Job Code\nCody, Jake,07/07/2026,4499\n"
        # csv module needs the quoted name; build properly:
        body1 = 'Employee Name,Work Date,Job Code\n"Cody, Jake",07/07/2026,4499\n'
        c.post("/admin/timecards-csv", data={"file": (io.BytesIO(body1.encode()), "t1.csv")},
               content_type="multipart/form-data")
        body2 = 'Employee Name,Work Date,Job Code\n"Cody, Jake",08/04/2026,5083\n'
        c.post("/admin/timecards-csv", data={"file": (io.BytesIO(body2.encode()), "t2.csv")},
               content_type="multipart/form-data")
    idx = json.load(open(tmp_path / "data" / "timecards_index.json"))
    assert idx["Jake Cody"] == {"2026-07-07": "4499", "2026-08-04": "5083"}
    RUNS.clear()
