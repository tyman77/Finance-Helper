"""Flask review UI tests: upload -> review -> edit -> approve -> download.

Uses the committed samples (fake data, real headers) so these run without any
of the real, gitignored vendor exports.
"""

import io
import json
import re
from datetime import datetime
from decimal import Decimal

import pytest

from finance_helper.models import LineItem, SourceDocument
from finance_helper.web.app import (
    RUNS,
    _format_note,
    _line_candidates,
    _line_status,
    _project_options,
    create_app,
)


@pytest.fixture
def client():
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
    location = resp.headers["Location"]
    run_id = location.rstrip("/").split("/")[-1]
    return run_id


def test_index_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Process a vendor statement" in resp.data
    # Source dropdown is built from config, not hardcoded.
    assert b"UPS" in resp.data
    assert b"United Airlines" in resp.data


def test_upload_missing_file_flashes_and_redirects(client):
    resp = client.post("/upload", data={"source": "ups"}, content_type="multipart/form-data")
    assert resp.status_code == 302
    resp2 = client.get(resp.headers["Location"])
    assert b"Choose a CSV file" in resp2.data


@pytest.mark.parametrize("source,path,total,lines", [
    ("ups", "samples/ups_sample.csv", "83.00", 5),
    ("united", "samples/united_sample.csv", "405.00", 4),
    ("hotel_engine", "samples/hotel_engine_sample.csv", "732.25", 11),
])
def test_upload_and_review_shows_real_totals(client, source, path, total, lines):
    run_id = _upload(client, source, path)
    resp = client.get(f"/review/{run_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert total in body
    assert body.count('name="gl_account_') == lines


def test_index_lists_recent_runs(client):
    _upload(client, "ups", "samples/ups_sample.csv")
    resp = client.get("/")
    assert b'table class="runs"' in resp.data
    assert b"UPS" in resp.data


def test_editing_a_line_persists_and_affects_validation(client, monkeypatch, tmp_path):
    # A tiny fake chart so this test doesn't depend on the real gitignored one.
    chart_dir = tmp_path
    (chart_dir / "chart_of_accounts.json").write_text(
        '{"71000": {"title": "OH - Travel", "require_department": true, '
        '"disallow_direct_posting": false, "status": "Active"}}'
    )
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(chart_dir))

    run_id = _upload(client, "united", "samples/united_sample.csv")
    body = client.get(f"/review/{run_id}").data.decode()
    fields = dict(re.findall(r'name="([a-z_]+_\d+)" value="([^"]*)"', body))

    # Force line 0 onto the one account this fake chart knows about, no dept set.
    fields["gl_account_0"] = "71000"
    fields["department_0"] = ""
    resp = client.post(f"/review/{run_id}/update", data=fields)
    assert resp.status_code == 302

    body2 = client.get(f"/review/{run_id}").data.decode()
    assert re.search(r"account 71000.*requires a department", body2)

    fields["department_0"] = "40"
    client.post(f"/review/{run_id}/update", data=fields)
    body3 = client.get(f"/review/{run_id}").data.decode()
    assert re.search(r"account 71000.*requires a department", body3) is None
    # Department is a <select>; confirm "40" is the one marked selected.
    dept_select = re.search(r'name="department_0">(.*?)</select>', body3, re.S).group(1)
    assert re.search(r'value="40"[^>]*selected', dept_select)


def test_bare_approve_from_stale_page_refuses_to_post_everything(client):
    # A POST without the review form's fields = a page loaded before a
    # deploy. JE 54222 taught us: never fall back to posting the whole doc.
    run_id = _upload(client, "ups", "samples/ups_sample.csv")
    resp = client.post(f"/review/{run_id}/approve", follow_redirects=True)
    assert b"outdated review page" in resp.data
    from finance_helper.web.app import RUNS
    assert RUNS[run_id].get("posted") is None


def test_approve_without_credentials_shows_clean_failure(client):
    run_id = _upload(client, "ups", "samples/ups_sample.csv")
    from finance_helper.web.app import RUNS
    doc = RUNS[run_id]["doc"]
    data = {f"gl_account_{i}": (li.gl_account or "")
            for i, li in enumerate(doc.line_items)}
    data["post_0"] = "on"
    resp = client.post(f"/review/{run_id}/approve", data=data)
    assert resp.status_code == 302
    body = client.get(f"/review/{run_id}").data.decode()
    assert "Not posted" in body
    assert "credentials missing" in body


def test_download_returns_current_payload(client):
    run_id = _upload(client, "ups", "samples/ups_sample.csv")
    resp = client.get(f"/review/{run_id}/download")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    data = json.loads(resp.data)
    assert data["destination"] == "bill"
    assert len(data["payload"]["billLineItems"]) == 5


def test_unknown_run_id_redirects_home(client):
    resp = client.get("/review/does-not-exist")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


# --- Status/candidate classification (the readability improvements) -------

def test_line_candidates_extracts_registry_and_title_code_forms():
    assert _line_candidates(
        "account hint '52200--x' (used 90% of trips) — confirm project/COGS; "
        "registry: candidate projects 2626, 2630, 3063 — pick one"
    ) == ["2626", "2630", "3063"]
    assert _line_candidates("calendar title codes 4471, 3831 — pick one") == ["4471", "3831"]
    assert _line_candidates(
        "account hint '71000--x' (used 60% of trips) — confirm project/COGS; "
        "past projects 4804, 3428 — pick one"
    ) == ["4804", "3428"]
    assert _line_candidates("crew schedule: project 4499 during stay -> 52200 COGS") == []
    assert _line_candidates(None) == []


def test_line_status_priority_order():
    unknown = LineItem(description="x", amount=Decimal("1"),
                        note="traveler not found in history — assign department & account")
    assert _line_status(unknown, []) == "unknown"

    pick = LineItem(description="x", amount=Decimal("1"), note="... — pick one")
    assert _line_status(pick, ["4471", "3831"]) == "pick"

    auto = LineItem(description="x", amount=Decimal("1"), project="4804",
                     note="crew schedule: project 4804 during stay -> 52200 COGS")
    assert _line_status(auto, []) == "auto"

    review = LineItem(description="x", amount=Decimal("1"),
                       note="account hint '52200--x' (used 50% of trips) — confirm project/COGS")
    assert _line_status(review, []) == "review"


def test_review_page_renders_status_pills_filters_and_candidate_chips(client):
    doc = SourceDocument(
        source="united", destination="sage", vendor="United Airlines",
        document_id="TEST-1", currency="USD",
        line_items=[
            LineItem(description="Known trip", amount=Decimal("100"), gl_account="52200",
                     department="60", project="4804",
                     note="crew schedule: project 4804 during stay -> 52200 COGS"),
            LineItem(description="Ambiguous client", amount=Decimal("200"), gl_account="52200",
                     note="account hint '52200--x' (used 90% of trips) — confirm project/COGS; "
                          "registry: candidate projects 2626, 3063 — pick one"),
            LineItem(description="Nobody", amount=Decimal("8"),
                     note="traveler not found in history — assign department & account"),
        ],
    )
    RUNS["fixture-run"] = {"doc": doc, "source": "united", "filename": "x.csv",
                            "created": datetime.now(), "posted": None}

    resp = client.get("/review/fixture-run")
    body = resp.data.decode()

    # Status pills for each classification actually present.
    assert 'status-auto">Auto-coded' in body
    assert 'status-pick">Pick one' in body
    assert 'status-unknown">Unknown traveler' in body

    # Filter toolbar shows the right counts, only for statuses present.
    assert 'data-filter="pick">' in body and "Pick one (1)" in body
    assert 'data-filter="unknown">' in body and "Unknown traveler (1)" in body
    assert 'data-filter="review"' not in body  # none of these lines are in "review"

    # Candidate chips are clickable buttons wired to fill the right field.
    assert 'data-fill="project_1" data-value="2626"' in body
    assert 'data-fill="project_1" data-value="3063"' in body

    # Department select, not a free-text input.
    assert '<select name="department_0">' in body
    assert re.search(r'value="60"[^>]*selected', body)


# --- Note formatting: real strings pulled from an actual review session ---

def test_format_note_simple_schedule_hit():
    got = _format_note("crew schedule: project 4499 during stay -> 52200 COGS")
    assert got["summary"] == "Crew schedule: project 4499"
    assert got["details"] == []


def test_format_note_account_hint_only():
    got = _format_note(
        "account hint '52200--COGS Travel: Flights / Parking' (used 100% of trips) — confirm project/COGS"
    )
    assert got["summary"] == "Usually 52200 (100% of trips)"
    assert got["details"] == []


def test_format_note_candidates_with_calendar_events_real_case():
    # The exact note behind the Cody/Jacoblee row from a real review session.
    note = (
        "account hint '52200--COGS Travel: Flights / Parking' (used 39% of trips) "
        "— confirm project/COGS; calendar context — Traders Point MLC Commissioning; "
        "Remote (Office); registry: candidate projects 3190, 3458, 4048, 4195, 4211 — pick one"
    )
    got = _format_note(note)
    assert got["summary"] == "Usually 52200 (39% of trips)"
    assert "Multiple possible projects — pick one below" in got["details"]
    # Both calendar events preserved, not merged/lost by the "; " collision
    # with the registry segment that follows them.
    assert any("Traders Point MLC Commissioning" in d and "Remote (Office)" in d for d in got["details"])
    assert not any("registry: candidate" in d for d in got["details"])  # replaced, not duplicated


def test_format_note_calendar_context_no_registry_match():
    # The Hargadine row: no registry hit at all, ends in "— confirm client/account".
    note = (
        "account hint '52200--COGS Travel: Flights / Parking' (used 100% of trips) "
        "— confirm project/COGS; calendar context — Happy Hour! (3:45pm in person, 4:00pm online); "
        "Breakaway INSTALL Kickoff 3138 — confirm client/account"
    )
    got = _format_note(note)
    assert got["summary"] == "Usually 52200 (100% of trips)"
    cal_detail = next(d for d in got["details"] if d.startswith("Calendar:"))
    assert "Happy Hour!" in cal_detail and "Breakaway INSTALL Kickoff 3138" in cal_detail
    assert "confirm client/account" not in cal_detail  # trailing instruction stripped, not shown as an "event"


def test_format_note_surname_only_caveat_preserved():
    got = _format_note(
        "account hint '52200--x' (used 92% of trips) — confirm project/COGS; matched by surname only"
    )
    assert any("surname" in d for d in got["details"])


def test_format_note_unrecognized_segment_shown_verbatim():
    # Safety net: an unknown future note format isn't silently dropped.
    got = _format_note("some brand new note format nobody wrote a rule for")
    assert got["summary"] == "some brand new note format nobody wrote a rule for"


# --- Archived-project filtering in the project autocomplete ----------------

def test_project_options_excludes_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path))
    (tmp_path / "project_registry.json").write_text(json.dumps({
        "registry": {
            "4804": {"client": "Echo Church"},
            "3190": {"client": "Red Rocks Church"},
        }
    }))
    (tmp_path / "sage_projects.json").write_text(json.dumps({
        "4804": {"name": "Echo Church", "status": "Active"},
        "3190": {"name": "Red Rocks Church", "status": "Archived"},
    }))
    options = _project_options()
    codes = [code for code, _ in options]
    assert "4804" in codes
    assert "3190" not in codes


def test_project_options_no_sage_data_shows_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path))
    (tmp_path / "project_registry.json").write_text(json.dumps({
        "registry": {"4804": {"client": "Echo Church"}, "3190": {"client": "Red Rocks Church"}}
    }))
    # No sage_projects.json fetched yet -> don't filter anything out.
    options = _project_options()
    assert {code for code, _ in options} == {"4804", "3190"}


def test_wifi_lines_are_auto_accepted_not_unknown():
    from finance_helper import sources, enrich
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Passenger Name"] = "LLC /INFLIGHT WI-FI STR DEN PHX"
    enrich.enrich_united(doc, {}, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=[])
    wifi = next(li for li in doc.line_items
                if "WI-FI" in li.raw.get("Passenger Name", ""))
    assert not wifi.needs_review                  # policy: ignore wifi charges
    assert "auto-accepted" in wifi.note
    assert _line_status(wifi, []) == "wifi"


def test_traveler_column_edits_persist(client):
    run_id = _upload(client, "united", "samples/united_sample.csv")
    body = client.get(f"/review/{run_id}").data.decode()
    assert 'name="person_0"' in body              # editable Traveler column
    fields = dict(re.findall(r'name="([a-z_]+_\d+)" value="([^"]*)"', body))
    fields["person_0"] = "Jeremy McKee"
    resp = client.post(f"/review/{run_id}/update", data=fields)
    assert resp.status_code == 302
    assert RUNS[run_id]["doc"].line_items[0].person == "Jeremy McKee"
    # And the name feeds the autocomplete on the next render.
    assert "Jeremy McKee" in client.get(f"/review/{run_id}").data.decode()


def test_approve_posts_only_checked_lines(client, monkeypatch):
    run_id = _upload(client, "united", "samples/united_sample.csv")
    from finance_helper.web.app import RUNS
    doc = RUNS[run_id]["doc"]
    n = len(doc.line_items)
    posted = {}

    from finance_helper import destinations
    monkeypatch.setattr(destinations, "post",
                        lambda d, payload: posted.setdefault("doc", d) or {"ok": 1})
    # Submit the full form with only line 0 checked.
    data = {f"gl_account_{i}": (doc.line_items[i].gl_account or "")
            for i in range(n)}
    data.update({f"department_{i}": (doc.line_items[i].department or "") for i in range(n)})
    data.update({f"project_{i}": (doc.line_items[i].project or "") for i in range(n)})
    data["post_0"] = "on"
    resp = client.post(f"/review/{run_id}/approve", data=data)
    assert resp.status_code == 302
    assert len(posted["doc"].line_items) == 1
    assert RUNS[run_id]["posted"]["ok"] is True
    assert "1 line(s)" in RUNS[run_id]["posted"]["detail"]

    # Nothing checked: refuses with guidance, nothing posted.
    posted.clear()
    data.pop("post_0")
    client.post(f"/review/{run_id}/approve", data=data, follow_redirects=True)
    assert "doc" not in posted


def test_posted_lines_are_marked_and_never_repost(client, monkeypatch):
    run_id = _upload(client, "united", "samples/united_sample.csv")
    doc = RUNS[run_id]["doc"]
    from finance_helper import destinations
    calls = []
    monkeypatch.setattr(destinations, "post",
                        lambda d, p: calls.append(d) or {"record_no": "54225"})
    data = {f"gl_account_{i}": (li.gl_account or "")
            for i, li in enumerate(doc.line_items)}
    data["post_0"] = "on"
    client.post(f"/review/{run_id}/approve", data=data)
    assert len(calls) == 1
    assert "JE 54225" in doc.line_items[0].posted_ref
    # The page shows it as Posted with no checkbox for that line.
    body = client.get(f"/review/{run_id}").data.decode()
    assert "Posted ✔" in body and 'name="post_0"' not in body
    # Re-selecting it cannot create a duplicate entry.
    resp = client.post(f"/review/{run_id}/approve", data=data,
                       follow_redirects=True)
    assert len(calls) == 1
    assert b"already posted" in resp.data
