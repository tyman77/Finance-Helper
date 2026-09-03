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


def _hotel_csv(path):
    import csv as _csv
    fieldnames = ["Start Date", "End Date", "Project Name", "Department Name",
                  "Hotel City", "Hotel Name"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerow({"Start Date": "06/01/2026", "End Date": "06/03/2026",
                    "Project Name": "Northview Church [3531]", "Department Name": "Install",
                    "Hotel City": "Denver", "Hotel Name": "Fairfield"})


def test_hotel_index_upload_builds_and_accumulates(client, tmp_path):
    c, data_dir = client
    csv_path = tmp_path / "he.csv"
    _hotel_csv(csv_path)
    with open(csv_path, "rb") as fh:
        resp = c.post("/admin/hotel-index", data={"file": (fh, "he.csv")},
                      content_type="multipart/form-data")
    assert resp.status_code == 302
    idx = json.loads((data_dir / "hotel_project_index.json").read_text())
    assert idx == [{"start": "2026-06-01", "end": "2026-06-03", "project": "3531",
                    "department": "60", "city": "Denver", "guests": []}]

    # Uploading the same file again doesn't duplicate.
    with open(csv_path, "rb") as fh:
        c.post("/admin/hotel-index", data={"file": (fh, "he.csv")},
               content_type="multipart/form-data")
    idx2 = json.loads((data_dir / "hotel_project_index.json").read_text())
    assert len(idx2) == 1


def test_hotel_index_upload_without_file_flashes(client):
    c, _ = client
    resp = c.post("/admin/hotel-index", data={}, content_type="multipart/form-data")
    assert resp.status_code == 302
    resp2 = c.get(resp.headers["Location"])
    assert b"Choose a Hotel Engine statement" in resp2.data


def test_build_roster_directory_beats_guesses():
    """A Workspace-directory hit is the confirmed address; vanity and the
    email convention only fill in when the directory doesn't know the name.
    (The convention guessed imungia@ for a person whose real address is
    imunguia@ — exactly the failure the directory pull removes.)"""
    build = admin_module._build_roster.build
    travelers = {
        "A": {"person": "Isaac Mungia"},
        "B": {"person": "Andrew Starke"},
        "C": {"person": "Joshua Judy"},
    }
    roster, review = build(travelers, {"isaac mungia": "imunguia@summitintegrated.com"})
    assert roster["Isaac Mungia"] == "imunguia@summitintegrated.com"
    assert roster["Andrew Starke"] == "andrew@summitintegrated.com"   # vanity
    assert roster["Joshua Judy"] == "jjudy@summitintegrated.com"      # convention
    assert any("[directory" in line for line in review)


def test_fetch_directory_returns_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADMIN_SUBJECT", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert admin_module._build_roster.fetch_directory() == {}


def test_roster_route_uses_google_directory(client, tmp_path, monkeypatch):
    c, data_dir = client
    csv_path = tmp_path / "hist.csv"
    _historical_csv(csv_path)
    with open(csv_path, "rb") as fh:
        c.post("/admin/traveler-map", data={"file": (fh, "hist.csv")},
               content_type="multipart/form-data")
    monkeypatch.setattr(admin_module._build_roster, "fetch_directory",
                        lambda: {"joshua judy": "josh.judy@summitintegrated.com"})
    resp = c.post("/admin/roster", follow_redirects=True)
    roster = json.loads((data_dir / "roster.json").read_text())
    assert roster["Joshua Judy"] == "josh.judy@summitintegrated.com"
    assert b"confirmed from the Google Workspace directory" in resp.data


def test_calendars_all_skipped_explains_delegation(client, monkeypatch):
    c, data_dir = client
    os.makedirs(data_dir, exist_ok=True)
    (data_dir / "roster.json").write_text(
        json.dumps({"Joshua Judy": "jjudy@summitintegrated.com"}))
    monkeypatch.delenv("USE_DWD", raising=False)
    monkeypatch.setattr(
        admin_module._fetch_calendar_index, "fetch_all",
        lambda roster, s, e, dwd, checkpoint_path=None: ({}, sorted(set(roster.values()))))
    resp = c.post("/admin/calendars",
                  data={"start": "2026-08-01", "end": "2026-09-03"},
                  follow_redirects=True)
    assert b"Domain-wide delegation" in resp.data
    assert b"USE_DWD=1" in resp.data
