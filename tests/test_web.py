"""Flask review UI tests: upload -> review -> edit -> approve -> download.

Uses the committed samples (fake data, real headers) so these run without any
of the real, gitignored vendor exports.
"""

import io
import re

import pytest

from finance_helper.web.app import RUNS, create_app


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
    assert 'name="department_0" value="40"' in body3


def test_approve_without_credentials_shows_clean_failure(client):
    run_id = _upload(client, "ups", "samples/ups_sample.csv")
    resp = client.post(f"/review/{run_id}/approve")
    assert resp.status_code == 302
    body = client.get(f"/review/{run_id}").data.decode()
    assert "Not posted" in body
    assert "credentials missing" in body


def test_download_returns_current_payload(client):
    run_id = _upload(client, "ups", "samples/ups_sample.csv")
    resp = client.get(f"/review/{run_id}/download")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    import json
    data = json.loads(resp.data)
    assert data["destination"] == "bill"
    assert len(data["payload"]["billLineItems"]) == 5


def test_unknown_run_id_redirects_home(client):
    resp = client.get("/review/does-not-exist")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
