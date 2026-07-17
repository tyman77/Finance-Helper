"""Admin panel: regenerating data/*.json indices on the server. Uses fake
CSVs/mocked network calls throughout — no real vendor exports or live Sheets/
Calendar/Sage access.
"""

import csv
import json
import os

import pytest
import yaml

from finance_helper.web import admin as admin_module
from finance_helper.web.app import RUNS, create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(data_dir))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c, data_dir
    RUNS.clear()


def _historical_csv(path):
    fieldnames = ["Passenger Name", "Person", "Department", "Account", "Project"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows([
            {"Passenger Name": "JUDY/JOSHUA", "Person": "Joshua Judy",
             "Department": "60--Install Team", "Account": "52200",
             "Project": "Northview Church, IN [3428] Camera Upgrade"},
        ])


def test_admin_page_loads_with_nothing_generated_yet(client):
    c, _ = client
    resp = c.get("/admin/")
    assert resp.status_code == 200
    assert b"not generated yet" in resp.data


def test_traveler_map_upload_builds_map_and_registry(client, tmp_path):
    c, data_dir = client
    csv_path = tmp_path / "hist.csv"
    _historical_csv(csv_path)

    with open(csv_path, "rb") as fh:
        resp = c.post(
            "/admin/traveler-map",
            data={"file": (fh, "hist.csv")},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 302

    tmap = yaml.safe_load((data_dir / "united_travelers.yml").read_text())
    assert tmap["JUDY/JOSHUA"]["person"] == "Joshua Judy"

    registry = json.loads((data_dir / "project_registry.json").read_text())
    assert "3428" in registry["registry"]


def test_traveler_map_upload_without_file_flashes_and_redirects(client):
    c, _ = client
    resp = c.post("/admin/traveler-map", data={}, content_type="multipart/form-data")
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Choose the historical United export" in resp2.data


def test_roster_requires_traveler_map_first(client):
    c, _ = client
    resp = c.post("/admin/roster")
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Build the traveler map first" in resp2.data


def test_roster_builds_from_existing_traveler_map(client, tmp_path):
    c, data_dir = client
    csv_path = tmp_path / "hist.csv"
    _historical_csv(csv_path)
    with open(csv_path, "rb") as fh:
        c.post("/admin/traveler-map", data={"file": (fh, "hist.csv")}, content_type="multipart/form-data")

    resp = c.post("/admin/roster")
    assert resp.status_code == 302
    roster = json.loads((data_dir / "roster.json").read_text())
    assert "Joshua Judy" in roster
    assert roster["Joshua Judy"] == "jjudy@summitintegrated.com"


def test_calendars_requires_dates(client):
    c, _ = client
    resp = c.post("/admin/calendars", data={})
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Pick a start and end date" in resp2.data


def test_calendars_requires_roster_first(client):
    c, _ = client
    resp = c.post("/admin/calendars", data={"start": "2026-01-01", "end": "2026-02-01"})
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Build the roster first" in resp2.data


def test_schedule_requires_sheet_id(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("SCHEDULE_SHEET_ID", raising=False)
    resp = c.post("/admin/schedule", data={"year": "2026"})
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Set SCHEDULE_SHEET_ID" in resp2.data


def test_sage_projects_missing_credentials_flashes_friendly_error(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("INTACCT_CLIENT_ID", raising=False)
    resp = c.post("/admin/sage-projects")
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Could not fetch Sage projects" in resp2.data


def test_sage_projects_success_writes_file(client, monkeypatch):
    c, data_dir = client
    monkeypatch.setattr(admin_module._fetch_sage_projects, "get_token", lambda: "fake-token")
    monkeypatch.setattr(
        admin_module._fetch_sage_projects,
        "fetch_projects",
        lambda token: [{"projectId": "3428", "status": "Active", "name": "Camera Upgrade"}],
    )

    resp = c.post("/admin/sage-projects")
    assert resp.status_code == 302
    out = json.loads((data_dir / "sage_projects.json").read_text())
    assert out["3428"]["status"] == "Active"
