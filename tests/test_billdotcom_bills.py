"""Bill.com v2 client: open bills + attachment pull (offline, fake transport)."""

import json

import pytest

from finance_helper import billdotcom_api as api


ENTITIES = {
    "Vendor": [{"id": "v1", "name": "Acme Supply Co", "paymentTermId": "t30"},
               {"id": "v2", "name": "Zenith", "paymentTermId": ""}],
    "PaymentTerm": [{"id": "t30", "name": "Net 30", "dueDays": "30"},
                    {"id": "t0", "name": "Due on receipt", "dueDays": "0"}],
    "Bill": [
        {"id": "b1", "vendorId": "v1", "invoiceNumber": "INV-1", "invoiceDate": "2026-08-01",
         "dueDate": "2026-08-31T00:00:00.000+0000", "amount": 1500, "approvalStatus": "4",
         "paymentStatus": "1", "isActive": "1", "description": "parts", "poNumber": "PO9",
         "createdTime": "2026-08-02T10:00:00", "updatedTime": "2026-08-03T10:00:00"},
        {"id": "b2", "vendorId": "v2", "invoiceNumber": "2", "invoiceDate": "2026-07-01",
         "dueDate": "2026-07-31", "amount": "99.5", "approvalStatus": "1",
         "paymentStatus": "0", "isActive": "1"},                      # paid — excluded
        {"id": "b3", "vendorId": "v2", "invoiceNumber": "3", "invoiceDate": "2026-08-10",
         "dueDate": "2026-08-10", "amount": "10", "approvalStatus": "0",
         "paymentStatus": "4", "isActive": "1", "paymentTermId": "t0"},
    ],
}


@pytest.fixture
def fake_v2(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_v2_login", lambda: ("key", "sess"))

    def call(path, data):
        calls.append((path, data))
        if path.startswith("List/"):
            entity = path[len("List/"):-len(".json")]
            return list(ENTITIES.get(entity, []))
        if path == "GetDocumentPages.json":
            page = json.loads(data["data"])["pageNumber"]
            return {"documentPages": {"fileUrl": f"https://files.test/{page}",
                                      "numPages": 2, "name": "invoice.pdf"}}
        raise RuntimeError("unexpected " + path)

    monkeypatch.setattr(api, "_v2_call", call)
    return calls


def test_fetch_open_bills_normalizes_and_drops_paid(fake_v2, monkeypatch):
    for k in api._REQUIRED:
        monkeypatch.setenv(k, "x")
    bills = api.fetch_open_bills()
    assert [b["id"] for b in bills] == ["b1", "b3"]
    b1 = bills[0]
    assert b1["vendor"] == "Acme Supply Co" and b1["amount"] == "1500.00"
    assert b1["due_date"] == "2026-08-31" and b1["terms_days"] == 30 and b1["terms"] == "Net 30"
    assert b1["approval_status"] == "approved" and b1["payment_status"] == "open"
    assert b1["po"] == "PO9" and b1["updated"] == "2026-08-03"
    b3 = bills[1]
    assert b3["terms_days"] == 0 and b3["payment_status"] == "scheduled"
    assert b3["approval_status"] == "unassigned"
    # The Bill listing asks for active bills only.
    bill_call = next(d for p, d in fake_v2 if p == "List/Bill.json")
    assert json.loads(bill_call["data"])["filters"][0]["field"] == "isActive"


def test_fetch_bill_documents_pdf_stops_after_first_page(fake_v2, monkeypatch):
    import requests

    class R:
        def __init__(self, content, ctype):
            self.status_code, self.content, self.text = 200, content, ""
            self.headers = {"Content-Type": ctype}

    got = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: (got.append((url, kw["headers"]))
                                                            or R(b"%PDF-1.7 doc", "application/pdf")))
    docs = api.fetch_bill_documents("b1")
    assert len(docs) == 1 and docs[0]["media_type"] == "application/pdf"
    assert docs[0]["name"] == "invoice.pdf" and docs[0]["data"].startswith(b"%PDF")
    assert got[0][1] == {"devKey": "key", "sessionId": "sess"}


def test_fetch_bill_documents_images_walk_every_page(fake_v2, monkeypatch):
    import requests

    class R:
        status_code, text = 200, ""
        content = b"\x89PNG\r\n\x1a\npage"
        headers = {"Content-Type": "image/png"}

    monkeypatch.setattr(requests, "get", lambda url, **kw: R())
    docs = api.fetch_bill_documents("b1")
    assert len(docs) == 2 and {d["media_type"] for d in docs} == {"image/png"}


def test_fetch_bill_documents_reports_missing_url(fake_v2, monkeypatch):
    monkeypatch.setattr(api, "_v2_call", lambda path, data: {"documentPages": {}})
    with pytest.raises(RuntimeError, match="no file URL"):
        api.fetch_bill_documents("b1")


def test_sniff_media_type():
    assert api.sniff_media_type(b"%PDF-1.4") == "application/pdf"
    assert api.sniff_media_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert api.sniff_media_type(b"????", "image/tiff; charset=x") == "image/tiff"
    assert api.sniff_media_type(b"????") == "application/octet-stream"
