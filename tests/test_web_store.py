"""Saved reviews persist to disk and survive a 'restart' (RUNS cleared)."""

import io

import pytest

from finance_helper.web import store
from finance_helper.web.app import RUNS, create_app

_UPS = "samples/ups_sample.csv"


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
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def test_upload_writes_a_run_to_disk(client):
    run_id = _upload(client, "ups", _UPS)
    assert store.load_run(run_id) is not None


def test_saved_review_reopens_after_restart(client):
    run_id = _upload(client, "ups", _UPS)
    # Simulate a redeploy: in-memory state gone, disk remains.
    RUNS.clear()
    resp = client.get(f"/review/{run_id}")
    assert resp.status_code == 200
    assert run_id in RUNS  # rehydrated from disk


def test_edits_persist_across_restart(client):
    run_id = _upload(client, "ups", _UPS)
    # Edit line 0's project, then wipe memory.
    client.post(
        f"/review/{run_id}/update",
        data={"gl_account_0": "51700", "department_0": "20", "project_0": "9999"},
        content_type="application/x-www-form-urlencoded",
    )
    RUNS.clear()
    reloaded = store.load_run(run_id)
    assert reloaded["doc"].line_items[0].project == "9999"


def test_index_lists_saved_runs_after_restart(client):
    run_id = _upload(client, "ups", _UPS)
    RUNS.clear()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Saved reviews" in resp.data
    assert run_id in RUNS


def test_delete_removes_the_run_from_disk_and_memory(client):
    run_id = _upload(client, "ups", _UPS)
    resp = client.post(f"/review/{run_id}/delete")
    assert resp.status_code == 302
    assert store.load_run(run_id) is None
    assert run_id not in RUNS


def test_round_trip_preserves_amounts_and_dimensions(client):
    run_id = _upload(client, "ups", _UPS)
    original = RUNS[run_id]["doc"]
    reloaded = store.load_run(run_id)["doc"]
    assert reloaded.total == original.total
    assert len(reloaded.line_items) == len(original.line_items)
    assert reloaded.line_items[0].amount == original.line_items[0].amount
    assert reloaded.document_id == original.document_id


def test_rerun_reprocesses_from_stored_csv(client):
    run_id = _upload(client, "ups", _UPS)
    # Corrupt the in-memory doc, then re-run: it should be rebuilt from the CSV.
    from finance_helper.web.app import RUNS
    RUNS[run_id]["doc"].line_items = []
    resp = client.post(f"/review/{run_id}/rerun")
    assert resp.status_code == 302
    assert len(RUNS[run_id]["doc"].line_items) > 0  # re-parsed from the stored CSV


def test_rerun_persists_across_restart(client):
    run_id = _upload(client, "ups", _UPS)
    from finance_helper.web.app import RUNS
    RUNS.clear()  # simulate restart; stored CSV must survive
    resp = client.post(f"/review/{run_id}/rerun")
    assert resp.status_code == 302
    reloaded = store.load_run(run_id)
    assert reloaded["csv_b64"] and len(reloaded["doc"].line_items) > 0


def test_rerun_without_stored_csv_flashes_guidance(client):
    run_id = _upload(client, "ups", _UPS)
    # Simulate a legacy saved run that predates csv storage.
    from finance_helper.web.app import RUNS
    RUNS[run_id]["csv_b64"] = None
    resp = client.post(f"/review/{run_id}/rerun")
    assert resp.status_code == 302
    resp2 = client.get(f"/review/{run_id}")
    assert b"re-upload the file" in resp2.data
