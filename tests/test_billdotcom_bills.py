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
    monkeypatch.setattr(api, "_v2_login", lambda http=None: ("key", "sess"))

    def call(path, data, http=None):
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
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: (got.append((url, kw.get("headers")))
                                                            or R(b"%PDF-1.7 doc", "application/pdf")))
    docs = api.fetch_bill_documents("b1")
    assert len(docs) == 1 and docs[0]["media_type"] == "application/pdf"
    assert docs[0]["name"] == "invoice.pdf" and docs[0]["data"].startswith(b"%PDF")
    assert got[0][1]["sessionId"] == "sess" and "Accept" in got[0][1]


def test_fetch_bill_documents_images_walk_every_page(fake_v2, monkeypatch):
    import requests

    class R:
        status_code, text = 200, ""
        content = b"\x89PNG\r\n\x1a\npage"
        headers = {"Content-Type": "image/png"}

    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: R())
    docs = api.fetch_bill_documents("b1")
    assert len(docs) == 2 and {d["media_type"] for d in docs} == {"image/png"}


def test_fetch_bill_documents_reports_missing_url(fake_v2, monkeypatch):
    monkeypatch.setattr(api, "_v2_call", lambda path, data, http=None: {"documentPages": {}})
    with pytest.raises(RuntimeError, match="no file URL"):
        api.fetch_bill_documents("b1")


def test_sniff_media_type():
    assert api.sniff_media_type(b"%PDF-1.4") == "application/pdf"
    assert api.sniff_media_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert api.sniff_media_type(b"????", "image/tiff; charset=x") == "image/tiff"
    assert api.sniff_media_type(b"????") == "application/octet-stream"


def test_absolute_file_url_resolves_relative_paths(monkeypatch):
    monkeypatch.delenv("BILLDOTCOM_V2_URL", raising=False)
    monkeypatch.delenv("BILLDOTCOM_FILE_BASE_URL", raising=False)
    assert api.absolute_file_url("https://x.test/a.pdf") == "https://x.test/a.pdf"
    assert api.absolute_file_url("/api/v2/GetDocumentPages?id=1") == \
        "https://api.bill.com/api/v2/GetDocumentPages?id=1"
    assert api.absolute_file_url("//files.bill.com/a.pdf") == "https://files.bill.com/a.pdf"
    assert api.absolute_file_url("Doc/page1.png") == "https://api.bill.com/api/v2/Doc/page1.png"
    monkeypatch.setenv("BILLDOTCOM_FILE_BASE_URL", "https://app.bill.com")
    assert api.absolute_file_url("/attachment/9") == "https://app.bill.com/attachment/9"


def test_fetch_bill_documents_makes_relative_url_absolute(fake_v2, monkeypatch):
    import requests

    monkeypatch.setattr(api, "_v2_call", lambda path, data, http=None: {
        "documentPages": {"fileUrl": "/api/v2/GetDocumentPages?x=1", "numPages": 1}})

    class R:
        status_code, text = 200, ""
        content = b"%PDF-1.5 x"
        headers = {"Content-Type": "application/pdf"}

    seen = []
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: (seen.append(url) or R()))
    api.fetch_bill_documents("b1")
    assert seen == ["https://api.bill.com/api/v2/GetDocumentPages?x=1"]


class _Resp:
    def __init__(self, content, ctype, status=200, disp=""):
        self.status_code, self.content, self.text = status, content, content.decode("latin-1")
        self.headers = {"Content-Type": ctype}
        if disp:
            self.headers["Content-Disposition"] = disp


def test_download_falls_back_to_query_credentials(fake_v2, monkeypatch):
    import requests

    calls = []

    def get(self, url, **kw):
        calls.append(("GET", url, kw.get("cookies")))
        if "sessionId=sess" in url and "pageNumber=1" in url:
            return _Resp(b"%PDF-1.4 ok", "application/pdf",
                         disp='attachment; filename="invoice 36397.pdf"')
        return _Resp(b"<html><title>Sign in to Bill.com</title></html>", "text/html")

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(requests.Session, "post", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no POST needed")))
    docs = api.fetch_bill_documents("b1")
    assert docs[0]["media_type"] == "application/pdf"
    assert docs[0]["name"] == "invoice.pdf"          # GetDocumentPages name wins
    assert [c[1] for c in calls] == [
        "https://files.test/1",
        "https://files.test/1?pageNumber=1",
        "https://files.test/1?pageNumber=1&sessionId=sess&devKey=key",
    ]


def test_download_reports_every_attempt_when_all_return_html(fake_v2, monkeypatch):
    import requests

    html = _Resp(b"<html><head><title>Bill.com - Login</title></head></html>", "text/html; charset=utf-8")
    monkeypatch.setattr(requests.Session, "get", lambda *a, **kw: html)
    monkeypatch.setattr(requests.Session, "post", lambda *a, **kw: html)
    with pytest.raises(RuntimeError) as exc:
        api.fetch_bill_documents("b1")
    msg = str(exc.value)
    assert "no PDF/image from https://files.test/1" in msg
    assert "Bill.com - Login" in msg and "login cookies held: none" in msg
    # every host x variant is reported: given host, then app.bill.com
    assert "files.test as given" in msg and "files.test pageNumber+session" in msg
    assert "app.bill.com as given" in msg and msg.count("text/html") == 6


def test_redact_url_hides_session():
    out = api._redact_url("https://api.bill.com/x?sessionId=abc123&devKey=k9&id=1")
    assert "abc123" not in out and "k9" not in out and "id=1" in out


def test_candidate_urls_use_login_endpoint_host(monkeypatch):
    monkeypatch.delenv("BILLDOTCOM_FILE_HOSTS", raising=False)
    api._V2_LOGIN_INFO.clear()
    api._V2_LOGIN_INFO["apiEndPoint"] = "https://api-app02.us.bill.com/api/v2"
    try:
        urls = api._candidate_urls("https://api.bill.com/is/BillImageServlet?entityId=X", "k", "s", 2)
    finally:
        api._V2_LOGIN_INFO.clear()
    hosts = []
    for label, _ in urls:
        h = label.split()[0]
        if h not in hosts:
            hosts.append(h)
    assert hosts == ["api.bill.com", "api-app02.us.bill.com", "app.bill.com"]
    assert urls[1][1] == "https://api.bill.com/is/BillImageServlet?entityId=X&pageNumber=2"
    assert urls[2][1].endswith("entityId=X&pageNumber=2&sessionId=s&devKey=k")


def test_fetch_bill_documents_prefers_originals(fake_v2, monkeypatch):
    import requests

    def call(path, data, http=None):
        if path == "GetDocuments.json":
            return {"documents": [{"id": "d1", "fileUrl": "https://files.test/orig",
                                   "name": "vendor-invoice.pdf"}]}
        raise AssertionError("page-render path should not be used when originals exist")

    monkeypatch.setattr(api, "_v2_call", call)

    class R:
        status_code, text = 200, ""
        content = b"%PDF-1.7 full resolution original"
        headers = {"Content-Type": "application/pdf"}

    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: R())
    docs = api.fetch_bill_documents("b1")
    assert len(docs) == 1 and docs[0]["name"] == "vendor-invoice.pdf"
    assert docs[0]["data"].startswith(b"%PDF")
