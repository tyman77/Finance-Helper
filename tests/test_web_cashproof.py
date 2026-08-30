"""Cash Proof web flow: run -> dashboard -> disposition -> audit log."""

import io
import json
import os

import pytest

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


def _post_run(client, with_sage=True):
    data = {"bank_file": (io.BytesIO(open("samples/bank_sample.csv", "rb").read()), "bank.csv")}
    if with_sage:
        data["sage_file"] = (io.BytesIO(open("samples/sage_gl_sample.csv", "rb").read()), "gl.csv")
    resp = client.post("/cashproof/run", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def test_landing_loads(client):
    resp = client.get("/cashproof/")
    assert resp.status_code == 200
    assert b"Run Cash Proof" in resp.data


def test_run_without_file_flashes(client):
    resp = client.post("/cashproof/run", data={}, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert b"bank statement CSV" in client.get("/cashproof/").data


def test_bank_only_run_shows_activity(client):
    run_id = _post_run(client, with_sage=False)
    body = client.get(f"/cashproof/{run_id}").data
    assert b"Cash activity" in body
    assert b"Payroll" in body               # kind buckets rendered
    assert b"Exceptions" not in body        # no ledger -> no tie-out


def test_full_run_shows_exceptions_and_confirms(client):
    run_id = _post_run(client)
    body = client.get(f"/cashproof/{run_id}").data
    assert b"GHOST LLC" in body
    assert b"critical" in body
    assert b"Office rent adjustment" in body
    assert b"Needs confirmation" in body
    assert b"Timing" in body


def test_disposition_requires_note_for_accept(client):
    run_id = _post_run(client)
    resp = client.post(f"/cashproof/{run_id}/disposition",
                       data={"source_id": "bank:9", "action": "accept", "note": ""})
    assert resp.status_code == 302
    assert b"note is required" in client.get(f"/cashproof/{run_id}").data


def test_disposition_persists_and_hits_audit_log(client):
    run_id = _post_run(client)
    # Find the ghost txn's source_id from the rendered form.
    body = client.get(f"/cashproof/{run_id}").data.decode()
    import re
    sid = re.search(r'name="source_id" value="(bank:\d+)"', body).group(1)
    resp = client.post(f"/cashproof/{run_id}/disposition",
                       data={"source_id": sid, "action": "investigate", "note": "checking"})
    assert resp.status_code == 302
    body2 = client.get(f"/cashproof/{run_id}").data
    assert b"investigate" in body2
    audit = os.path.join(os.environ["FINANCE_HELPER_OUT_DIR"], "recon", "audit.jsonl")
    rows = [json.loads(l) for l in open(audit)]
    assert rows[-1]["source_id"] == sid and rows[-1]["action"] == "investigate"


def test_runs_listed_on_landing(client):
    run_id = _post_run(client)
    body = client.get("/cashproof/").data
    assert run_id.encode() in body or b"open" in body


def test_progress_page_renders_while_job_running(client):
    from finance_helper.web import cashproof
    cashproof.JOBS["abc123def456"] = {
        "status": "running", "stages": ["Reading the bank statement…"],
        "notices": [], "error": None, "started": "2026-08-30T10:00:00"}
    try:
        body = client.get("/cashproof/abc123def456").data
        assert b"running" in body
        assert "Reading the bank statement".encode() in body
    finally:
        cashproof.JOBS.clear()


def test_errored_job_flashes_and_redirects_to_landing(client):
    from finance_helper.web import cashproof
    cashproof.JOBS["bad1bad1bad1"] = {
        "status": "error", "stages": [], "notices": [],
        "error": "Sage exploded", "started": "2026-08-30T10:00:00"}
    resp = client.get("/cashproof/bad1bad1bad1", follow_redirects=True)
    assert b"Sage exploded" in resp.data
    assert "bad1bad1bad1" not in cashproof.JOBS   # consumed, not re-flashed


def test_fraud_checks_render_on_run_page(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path / "data"))
    import json as _json
    import os as _os
    _os.makedirs(tmp_path / "data", exist_ok=True)
    # A per-diem with no trip evidence anywhere -> one review finding.
    (_json.dump([{"person": "Ghost Rider", "date": "2026-06-15",
                  "amount": "250", "memo": "per diem"}],
                open(tmp_path / "data" / "ramp_reimbursements.json", "w")))
    run_id = _post_run(client)
    body = client.get(f"/cashproof/{run_id}").data
    assert b"Fraud checks" in body
    assert b"Per diem with no trip evidence: Ghost Rider" in body
    # Disposition attaches to the finding id and survives re-render.
    import re as _re
    fid = _re.search(rb'value="(check:[0-9a-f]+)"', body).group(1).decode()
    client.post(f"/cashproof/{run_id}/disposition",
                data={"source_id": fid, "action": "investigate", "note": "who is this"})
    body2 = client.get(f"/cashproof/{run_id}").data
    assert b"investigate" in body2 and b"who is this" in body2
