"""Bill Check web flow: run -> queue -> bill detail -> disposition / upload."""

import io
import json
import os

import pytest

from finance_helper import billdotcom_api
from finance_helper.billcheck import extract
from finance_helper.web.app import RUNS, create_app


BILLS = [
    {"id": "b-late", "vendor": "Acme Supply Co", "invoice": "INV-1001",
     "invoice_date": "2026-08-01", "due_date": "2026-09-30", "amount": "1500.00",
     "terms": "Net 30", "terms_days": 30, "po": "", "approval_status": "approved",
     "payment_status": "open", "description": "August parts"},
    {"id": "b-ok", "vendor": "Zenith Logistics", "invoice": "77", "invoice_date": "2026-08-05",
     "due_date": "2026-09-04", "amount": "250.00", "terms": "Net 30", "terms_days": 30,
     "po": "", "approval_status": "assigned", "payment_status": "open", "description": ""},
]

PDFS = {
    "b-late": {"is_invoice": True, "vendor": "ACME Supply Company", "invoice_number": "1001",
               "invoice_date": "2026-08-01", "due_date": "2026-08-31", "terms": "Net 30",
               "terms_days": 30, "total": "1500.00", "currency": "USD", "po_number": None,
               "confidence": "high", "notes": ""},
    "b-ok": {"is_invoice": True, "vendor": "Zenith Logistics LLC", "invoice_number": "77",
             "invoice_date": "2026-08-05", "due_date": "2026-09-04", "terms": "Net 30",
             "terms_days": 30, "total": "250.00", "currency": "USD", "po_number": None,
             "confidence": "high", "notes": ""},
}


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


@pytest.fixture
def fakes(monkeypatch):
    calls = {"reads": 0, "fetches": 0, "last_docs": None}
    monkeypatch.setattr(billdotcom_api, "credentials_present", lambda: True)
    monkeypatch.setattr(extract, "credentials_present", lambda: True)
    monkeypatch.setattr(billdotcom_api, "fetch_open_bills", lambda: [dict(b) for b in BILLS])

    def fetch_docs(bill_id):
        calls["fetches"] += 1
        return [{"name": f"{bill_id}.pdf", "media_type": "application/pdf",
                 "data": b"%PDF-1.4 " + bill_id.encode()}]

    def read(docs):
        calls["reads"] += 1
        calls["last_docs"] = docs
        bill_id = docs[0]["data"].split(b" ")[-1].decode()
        return dict(PDFS.get(bill_id) or PDFS["b-ok"])

    monkeypatch.setattr(billdotcom_api, "fetch_bill_documents", fetch_docs)
    monkeypatch.setattr(extract, "extract_invoice", read)
    return calls


def _run(client):
    resp = client.post("/billcheck/run", data={"limit": "50"})
    assert resp.status_code == 302 and "/billcheck/run/" in resp.headers["Location"]
    done = client.get(resp.headers["Location"])
    assert done.status_code == 302 and done.headers["Location"].endswith("/billcheck/")


def test_landing_without_credentials_explains(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BILLDOTCOM_DEV_KEY", raising=False)
    body = client.get("/billcheck/").data
    assert b"Not ready to run" in body
    resp = client.post("/billcheck/run", data={})
    assert resp.status_code == 302
    assert b"needs both" in client.get("/billcheck/").data


def test_run_builds_queue_worst_first(client, fakes):
    _run(client)
    body = client.get("/billcheck/").data
    assert b"Acme Supply" in body
    assert b"critical" in body and b"paid late" in body
    assert b"Zenith" not in body                     # clean bill hidden by default
    assert b"Zenith" in client.get("/billcheck/?all=1").data
    assert fakes["reads"] == 2


def test_second_run_is_free_and_progress_flashes(client, fakes):
    _run(client)
    _run(client)
    assert fakes["reads"] == 2 and fakes["fetches"] == 2
    assert b"2 unchanged" in client.get("/billcheck/").data


def test_bill_page_shows_side_by_side_and_document(client, fakes):
    _run(client)
    body = client.get("/billcheck/bill/b-late").data
    assert b"2026-09-30" in body and b"2026-08-31" in body
    assert b"Due date should be" in body
    assert b"Fixed in Bill.com" in body
    doc = client.get("/billcheck/bill/b-late/document?i=0")
    assert doc.status_code == 200 and doc.data.startswith(b"%PDF")
    assert doc.mimetype == "application/pdf"


def test_unknown_bill_redirects(client):
    resp = client.get("/billcheck/bill/nope")
    assert resp.status_code == 302
    assert b"run a check first" in client.get("/billcheck/").data


def test_disposition_requires_note_for_accept_and_persists(client, fakes):
    _run(client)
    resp = client.post("/billcheck/bill/b-late/disposition", data={"action": "accept", "note": ""})
    assert resp.status_code == 302
    assert b"note is required" in client.get("/billcheck/bill/b-late").data
    client.post("/billcheck/bill/b-late/disposition",
                data={"action": "fixed", "note": "changed due date"})
    body = client.get("/billcheck/bill/b-late").data
    assert b"changed due date" in body and b"re-verifies" in body
    assert b"Acme" not in client.get("/billcheck/").data     # dispositioned -> not open
    audit = os.path.join(os.environ["FINANCE_HELPER_OUT_DIR"], "billcheck", "audit.jsonl")
    rows = [json.loads(line) for line in open(audit)]
    assert rows[-1]["bill_id"] == "b-late" and rows[-1]["action"] == "fixed"


def test_fix_in_billcom_is_reverified_next_run(client, fakes, monkeypatch):
    _run(client)
    client.post("/billcheck/bill/b-late/disposition", data={"action": "fixed", "note": "ok"})
    corrected = [dict(b, due_date="2026-08-31") if b["id"] == "b-late" else dict(b) for b in BILLS]
    monkeypatch.setattr(billdotcom_api, "fetch_open_bills", lambda: corrected)
    _run(client)
    body = client.get("/billcheck/bill/b-late").data
    assert b"Everything on the invoice matches" in body
    assert b"Earlier decisions" in body
    assert fakes["reads"] == 2                          # re-compared from the cached read


def test_manual_upload_reads_that_file(client, fakes):
    _run(client)
    data = {"attachment": (io.BytesIO(b"%PDF-1.4 b-ok"), "hand.pdf")}
    resp = client.post("/billcheck/bill/b-late/upload", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 302
    assert fakes["last_docs"][0]["name"] == "hand.pdf"
    body = client.get("/billcheck/bill/b-late").data
    assert b"upload" in body and b"Zenith" in body      # compared against the uploaded file


def test_recheck_pulls_again(client, fakes):
    _run(client)
    resp = client.post("/billcheck/bill/b-ok/recheck")
    assert resp.status_code == 302
    assert fakes["fetches"] == 3 and fakes["reads"] == 3
    assert b"re-read" in client.get("/billcheck/bill/b-ok").data


def test_queue_csv(client, fakes):
    _run(client)
    resp = client.get("/billcheck/queue.csv")
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "b-late" in text and "2026-08-31" in text and "critical" in text


def test_run_error_flashes(client, fakes, monkeypatch):
    def boom():
        raise RuntimeError("Both Bill.com APIs rejected the credentials.")
    monkeypatch.setattr(billdotcom_api, "fetch_open_bills", boom)
    resp = client.post("/billcheck/run", data={})
    body = client.get(resp.headers["Location"], follow_redirects=True).data
    assert b"could not run" in body and b"rejected the credentials" in body
