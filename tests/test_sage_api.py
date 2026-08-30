"""recon.sage_api: record->Txn mapping resilience and the web API-pull flow.

All network calls are mocked — no live Sage access in tests.
"""

import io
from datetime import date
from decimal import Decimal

import pytest

from finance_helper.recon import sage_api
from finance_helper.web.app import RUNS, create_app

START, END = date(2026, 6, 1), date(2026, 6, 30)


def test_to_txns_maps_nested_account_and_debit_credit_pair():
    recs = [{
        "key": "1201", "entryDate": "2026-06-21",
        "glAccount": {"id": "10700", "name": "Checking"},
        "creditAmount": "1234.56", "memo": "ACME AV Supply invoice 4471",
    }]
    txns = sage_api.to_txns(recs, START, END)
    assert len(txns) == 1
    t = txns[0]
    assert t.amount == Decimal("-1234.56")       # credit to cash = out
    assert t.account_ref == "10700"
    assert "acme" in t.counterparty_norm


def test_to_txns_maps_txn_type_style_amounts():
    recs = [
        {"id": "9", "entryDate": "2026-06-02", "accountNo": "10700",
         "txnAmount": "3000", "txnType": "debit", "memo": "Customer deposit"},
        {"id": "10", "entryDate": "2026-06-03", "accountNo": "10700",
         "txnAmount": "450", "txnType": "credit", "memo": "Check 2041"},
    ]
    txns = sage_api.to_txns(recs, START, END)
    assert [t.amount for t in txns] == [Decimal("3000"), Decimal("-450")]


def test_to_txns_filters_non_cash_accounts_and_out_of_range_dates():
    recs = [
        {"id": "1", "entryDate": "2026-06-10", "accountNo": "52200",
         "debitAmount": "100", "memo": "expense side"},
        {"id": "2", "entryDate": "2026-09-01", "accountNo": "10700",
         "debitAmount": "100", "memo": "later period"},
    ]
    assert sage_api.to_txns(recs, START, END) == []


def test_fetch_ledger_without_credentials_raises_clear_error(monkeypatch):
    for k in sage_api._REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="credentials missing"):
        sage_api.fetch_ledger(START, END)


def test_fetch_ledger_surfaces_first_raw_record_when_nothing_maps(monkeypatch):
    for k in sage_api._REQUIRED:
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(sage_api, "fetch_gl_records",
                        lambda s, e: [{"weirdField": "1", "someDate": "2026-06-01"}])
    with pytest.raises(RuntimeError, match="weirdField"):
        sage_api.fetch_ledger(START, END)


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


def test_web_run_with_api_pull(client, monkeypatch):
    from finance_helper.recon import sage as sage_csv
    ledger = sage_csv.load_sage_csv("samples/sage_gl_sample.csv")
    called = {}

    def fake_fetch(start, end):
        called["range"] = (start, end)
        return ledger

    from finance_helper.web import cashproof
    monkeypatch.setattr(cashproof, "_sage_fetcher", lambda: (fake_fetch, "Sage API"))
    data = {"bank_file": (io.BytesIO(open("samples/bank_sample.csv", "rb").read()), "bank.csv"),
            "sage_api": "on"}
    resp = client.post("/cashproof/run", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    run_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    body = client.get(f"/cashproof/{run_id}").data
    assert b"Sage API (" in body                 # ledger labeled as API pull
    assert b"GHOST LLC" in body                  # full tie-out ran
    # Range derived from the bank file's posted rows.
    assert called["range"] == (date(2026, 6, 2), date(2026, 6, 30))


def test_web_api_pull_error_flashes_cleanly(client, monkeypatch):
    def boom(start, end):
        raise RuntimeError("Sage token request failed: HTTP 401")
    from finance_helper.web import cashproof
    monkeypatch.setattr(cashproof, "_sage_fetcher", lambda: (boom, "Sage API"))
    data = {"bank_file": (io.BytesIO(open("samples/bank_sample.csv", "rb").read()), "bank.csv"),
            "sage_api": "on"}
    resp = client.post("/cashproof/run", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert b"Sage token request failed" in client.get("/cashproof/").data


def test_username_gets_company_suffix(monkeypatch):
    from finance_helper.intacct_auth import web_services_username
    monkeypatch.setenv("INTACCT_USER_ID", "scout_ws")
    monkeypatch.setenv("INTACCT_COMPANY_ID", "SummitIntegrated")
    assert web_services_username() == "scout_ws@SummitIntegrated"
    # Already-qualified ids pass through untouched.
    monkeypatch.setenv("INTACCT_USER_ID", "scout_ws@SummitIntegrated")
    assert web_services_username() == "scout_ws@SummitIntegrated"
    # No company id -> best effort, unchanged.
    monkeypatch.setenv("INTACCT_USER_ID", "bare")
    monkeypatch.delenv("INTACCT_COMPANY_ID", raising=False)
    assert web_services_username() == "bare"
