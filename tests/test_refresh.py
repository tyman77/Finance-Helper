"""Auto-refresh of API-backed indexes before a Cash Proof run."""

import io
import json
import os
import time

import pytest

from finance_helper import billdotcom_api, paychex_api, ramp_api
from finance_helper.web import refresh


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(d))
    return d


def _no_sage(monkeypatch):
    from finance_helper.recon import sage_xml
    monkeypatch.setattr(sage_xml, "credentials_present", lambda: False)


def test_refresh_reports_missing_credentials(data_dir, monkeypatch):
    _no_sage(monkeypatch)
    for mod in (ramp_api, billdotcom_api, paychex_api):
        monkeypatch.setattr(mod, "credentials_present", lambda: False)
    msgs = refresh.auto_refresh()
    assert any("Ramp: no credentials" in m for m in msgs)
    assert any("Bill.com: no credentials" in m for m in msgs)
    assert any("Paychex: no credentials" in m for m in msgs)
    assert any("Sage POs: no credentials" in m for m in msgs)


def test_refresh_fetches_writes_and_reports(data_dir, monkeypatch):
    _no_sage(monkeypatch)
    for mod in (ramp_api, billdotcom_api, paychex_api):
        monkeypatch.setattr(mod, "credentials_present", lambda: True)
    monkeypatch.setattr(billdotcom_api, "fetch_master_index",
                        lambda: {"vendors": [], "bills": [], "bank_accounts": [], "gaps": []})
    monkeypatch.setattr(ramp_api, "fetch_index",
                        lambda s, e: [{"person": "J", "date": "2026-07-01",
                                       "amount": "1", "memo": "pd 4499", "project": "4499"}])
    monkeypatch.setattr(billdotcom_api, "fetch_index",
                        lambda: [{"id": "p1", "vendor": "V", "amount": "1.00",
                                  "date": "2026-07-01", "status": ""}])
    monkeypatch.setattr(paychex_api, "fetch_index",
                        lambda s, e: {"J": {"2026-07-01": "4499"}})
    msgs = refresh.auto_refresh()
    assert any(m.startswith("Ramp: 1 reimbursements") for m in msgs)
    assert any(m.startswith("Bill.com: 1 payments") for m in msgs)
    assert any("Timecards: 1 people" in m for m in msgs)
    assert json.load(open(data_dir / "ramp_reimbursements.json"))[0]["project"] == "4499"
    assert json.load(open(data_dir / "timecards_index.json")) == {"J": {"2026-07-01": "4499"}}

    # Second call within the hour: everything skipped, no fetches attempted.
    def boom(*a, **k):
        raise AssertionError("should not fetch when fresh")
    for mod, name in ((ramp_api, "fetch_index"), (billdotcom_api, "fetch_index"),
                      (billdotcom_api, "fetch_master_index"), (paychex_api, "fetch_index")):
        monkeypatch.setattr(mod, name, boom)
    msgs2 = refresh.auto_refresh()
    assert all("fresh (skipped)" in m or "no credentials" in m for m in msgs2)


def test_refresh_failure_is_reported_not_raised(data_dir, monkeypatch):
    _no_sage(monkeypatch)
    for mod in (ramp_api, billdotcom_api, paychex_api):
        monkeypatch.setattr(mod, "credentials_present", lambda: True)
    monkeypatch.setattr(billdotcom_api, "fetch_master_index",
                        lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setattr(ramp_api, "fetch_index",
                        lambda s, e: (_ for _ in ()).throw(RuntimeError("token bad")))
    monkeypatch.setattr(billdotcom_api, "fetch_index",
                        lambda: (_ for _ in ()).throw(RuntimeError("BDC_1102")))
    monkeypatch.setattr(paychex_api, "fetch_index",
                        lambda s, e: (_ for _ in ()).throw(RuntimeError("API-2")))
    msgs = refresh.auto_refresh()
    assert any("Ramp: FAILED — token bad" in m for m in msgs)
    assert any("Bill.com: FAILED" in m for m in msgs)
    assert any("CSV upload still works" in m for m in msgs)


def test_cashproof_run_triggers_refresh_and_flashes(data_dir, monkeypatch):
    from finance_helper.web.app import RUNS, create_app
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(refresh, "auto_refresh", lambda: ["Ramp: 9 reimbursements (3 with project memos)"])
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        data = {"bank_file": (io.BytesIO(open("samples/bank_sample.csv", "rb").read()), "bank.csv")}
        resp = c.post("/cashproof/run", data=data, content_type="multipart/form-data")
        assert resp.status_code == 302
        body = c.get(resp.headers["Location"]).data
        assert b"Data refresh: Ramp: 9 reimbursements" in body
    RUNS.clear()
