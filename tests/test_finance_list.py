"""Finance's bills-to-pay list: parse, line checks, reconcile with Bill.com."""

import io
import json
import os
from datetime import date, datetime, timedelta

import openpyxl
import pytest

from finance_helper.billcheck import finance_list as fl
from finance_helper.billcheck import payrun
from finance_helper.billcheck import store as bc_store
from finance_helper.web.app import RUNS, create_app

FRI = date(2026, 10, 2)
THU = date(2026, 10, 1)

ROWS = [   # same shape as Bill.com's "Bills to Pay" export
    (453123.0, "1Source Video", datetime(2026, 9, 15), datetime(2026, 9, 30), 720.05, 720.05, None),
    ("901172250*", "Belden - West Penn Wire", datetime(2026, 9, 24), datetime(2026, 10, 9), 6694.15, 6694.15, None),
    ("09.15.2026", "Mitchell Stier", datetime(2026, 9, 15), datetime(2026, 10, 15), 44295.5, 44295.5, None),
    (73222.0, "Pea Soup", datetime(2026, 8, 27), datetime(2026, 9, 26), 450.0, 450.0, "wire"),
    (531685513.0, "Wesco - Anixter", datetime(2026, 9, 29), datetime(2026, 9, 29), 2089.98, 2089.98, None),
    (531684163.0, "Wesco - Anixter", datetime(2026, 9, 10), datetime(2026, 10, 10), 1049.5, 1049.5, None),
    ("AGK4290", "Almo Corporation", datetime(2026, 9, 16), datetime(2026, 10, 16), 2178.5, 2080.0, None),
    (12816.0, "ROE Visual", datetime(2026, 9, 15), datetime(2026, 10, 15), 200283.5, 200283.5, None),
    ("F1646", "Design Technik Group", datetime(2026, 9, 25), datetime(2026, 10, 10), 2500.0, 2500.0, None),
]


def _xlsx(rows=ROWS, total=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    total = total if total is not None else round(sum(r[5] for r in rows), 2)
    ws.append([None, None, None, None, None, total])
    ws.append(["Invoice no.", "Vendor", "Invoice date", "Due date", "Invoice amount", "Balance due"])
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _kinds(sig):
    return {s["kind"] for s in sig}


def test_parse_bill_com_export():
    p = fl.parse(_xlsx(), "Bills_to_Pay.xlsx")
    first = p["lines"][0]
    assert first["invoice"] == "453123"                     # not 453123.0
    assert first["due_date"] == "2026-09-30" and first["balance"] == "720.05"
    assert p["lines"][3]["note"] == "wire"
    s = fl.summary(p)
    assert s["count"] == 9 and s["ties"] and str(s["total"]) == str(s["header_total"])


def test_parse_csv_and_bad_total():
    csv = ("Invoice no.,Vendor,Invoice date,Due date,Invoice amount,Balance due\n"
           "A1,Acme,9/1/2026,10/1/2026,\"1,000.00\",1000.00\n")
    p = fl.parse(csv.encode(), "list.csv")
    assert p["lines"][0]["invoice_date"] == "2026-09-01" and p["lines"][0]["amount"] == "1000.00"
    assert not fl.summary(fl.parse(_xlsx(total=1.00), "x.xlsx"))["ties"]


def test_parse_rejects_unknown_layout():
    with pytest.raises(ValueError, match="header row"):
        fl.parse(b"a,b,c\n1,2,3\n", "x.csv")


def test_line_checks_catch_this_weeks_problems():
    p = fl.parse(_xlsx(), "x.xlsx")
    sig = fl.line_signals(p["lines"], FRI)
    by = {x["vendor"] + x["invoice"]: _kinds(sig.get(x["row"], [])) for x in p["lines"]}
    assert "list_invoice_suffix" in by["Belden - West Penn Wire901172250*"]
    assert {"list_invoice_is_date", "list_individual"} <= by["Mitchell Stier09.15.2026"]
    assert "list_invoice_suffix" not in by["Mitchell Stier09.15.2026"]
    assert by["Pea Soup73222"] == {"list_wire"}
    assert "list_due_entry" in by["Wesco - Anixter531685513"]
    assert by["Almo CorporationAGK4290"] == set()             # short pay: not flagged
    assert "list_large_share" in by["ROE Visual12816"]
    assert by["Design Technik GroupF1646"] == set()          # round amount: not flagged
    assert by["Wesco - Anixter531684163"] == set()


def test_wesco_entry_error_names_the_likely_due_date():
    p = fl.parse(_xlsx(), "x.xlsx")
    row = next(x["row"] for x in p["lines"] if x["invoice"] == "531685513")
    s = next(s for s in fl.line_signals(p["lines"], FRI)[row] if s["kind"] == "list_due_entry")
    assert s["severity"] == "high" and "2026-10-29" in s["detail"]


def test_asterisk_on_an_invoice_already_in_bill_com_is_critical():
    p = fl.parse(_xlsx(), "x.xlsx")
    hist = [{"vendor": "Belden - West Penn Wire", "invoice": "901172250"}]
    row = next(x["row"] for x in p["lines"] if x["invoice"] == "901172250*")
    s = next(s for s in fl.line_signals(p["lines"], FRI, history=hist)[row]
             if s["kind"] == "list_invoice_suffix")
    assert s["severity"] == "critical"


def test_person_detection_ignores_businesses():
    assert fl.looks_like_person("Mitchell Stier") and fl.looks_like_person("Lisa Pedersen")
    for v in ("Ross Video", "Ace Backstage", "Pea Soup", "Marshall", "Group One LTD",
              "Stachel Law, PLLC", "B&H Photo-Video"):
        assert not fl.looks_like_person(v), v


def test_employee_named_vendor_is_critical():
    p = fl.parse(_xlsx(), "x.xlsx")
    row = next(x["row"] for x in p["lines"] if x["vendor"] == "Mitchell Stier")
    sig = fl.line_signals(p["lines"], FRI, people=["Mitchell Stier"])[row]
    assert "list_vendor_employee" in _kinds(sig)


# --- reconciling with Bill.com -------------------------------------------------

def _result(id, vendor, invoice, amount, due, **kw):
    bill = {"id": id, "vendor": vendor, "vendor_id": "", "invoice": invoice, "amount": amount,
            "due_date": due, "invoice_date": "2026-09-20", "payment_status": "open",
            "approval_status": "approved", **kw}
    return {"bill_id": id, "bill": bill, "status": "match", "severity": "clear",
            "comparison": {"findings": []}}


def test_list_defines_the_run_against_bill_com():
    flist = {**fl.parse(_xlsx(), "x.xlsx"), "filename": "x.xlsx"}
    results = [
        _result("b1", "Wesco - Anixter", "531684163", "1049.50", "2026-10-10"),       # matches
        _result("b2", "Almo Corporation", "AGK4290", "2178.50", "2026-10-16"),        # amount changed
        _result("b3", "Belden", "901172250", "6694.15", "2026-10-09"),                # asterisk / name
        _result("b4", "Sneaky LLC", "X1", "9000.00", "2026-10-05", payment_status="scheduled"),
        _result("b5", "Other Co", "Y2", "100.00", "2026-10-05"),                      # not listed
    ]
    view = payrun.build(results, {}, [], THU, horizon_days=15, finance_list=flist)
    rows = {x["bill_id"]: x for x in view["rows"]}
    assert set(rows) == {"b1", "b2", "b3", "b4"}
    assert rows["b4"]["signals"][0]["kind"] == "not_on_list"
    assert rows["b4"]["severity"] == "critical"
    assert "list_amount_changed" in _kinds(rows["b2"]["signals"])
    assert "list_invoice_suffix" in _kinds(rows["b3"]["signals"])
    assert rows["b1"]["severity"] == "clear"
    assert [b["id"] for b in view["list"]["dropped"]] == ["b5"]
    assert len(view["list"]["list_only"]) == 6 and view["list"]["mode"] == "matched"


def test_without_bill_com_the_list_is_the_run():
    flist = {**fl.parse(_xlsx(), "x.xlsx"), "filename": "x.xlsx"}
    view = payrun.build([], {}, [], THU, horizon_days=15, finance_list=flist)
    assert view["list"]["mode"] == "list" and len(view["rows"]) == 9
    assert str(view["total"]) == "260162.68"
    mitchell = next(x for x in view["rows"] if x["bill"]["vendor"] == "Mitchell Stier")
    assert mitchell["needs_decision"] and mitchell["bill"]["source"] == "list"
    # Stable ids, so decisions survive a re-render.
    again = payrun.build([], {}, [], THU, horizon_days=15, finance_list=flist)
    assert [x["bill_id"] for x in again["rows"]] == [x["bill_id"] for x in view["rows"]]


# --- web ---------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path / "data"))
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


def test_upload_list_decide_and_sign_off(client):
    resp = client.post("/billcheck/payrun/list", data={
        "list": (io.BytesIO(_xlsx()), "Bills_to_Pay_09.30.2026.xlsx")},
        content_type="multipart/form-data")
    assert resp.status_code == 302
    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "Bills_to_Pay_09.30.2026.xlsx" in page and "total ties" in page
    assert "Mitchell Stier" in page and "Invoice # is a date" in page
    assert "built from finance&#39;s list alone" in page or "built from finance's list alone" in page

    view = payrun.build([], {}, [], date.today(), horizon_days=15,
                        finance_list=payrun.load(payrun.next_pay_date(date.today()).isoformat())
                        ["finance_list"])
    for x in view["rows"]:
        if x["needs_decision"]:
            client.post(f"/billcheck/payrun/decide/{x['bill_id']}",
                        data={"action": "release", "note": "checked"})
    client.post("/billcheck/payrun/signoff")
    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "signed off" in page

    out = os.path.join(os.environ["FINANCE_HELPER_OUT_DIR"], "billcheck")
    audit = [json.loads(l) for l in open(os.path.join(out, "audit.jsonl"))]
    assert audit[0]["action"] == "list_upload" and audit[-1]["action"] == "signoff"
    assert audit[-1]["finance_list"]["filename"] == "Bills_to_Pay_09.30.2026.xlsx"
    assert any(n.endswith("-list.xlsx") for n in os.listdir(os.path.join(out, "payruns")))

    # Replacing the list voids the sign-off.
    client.post("/billcheck/payrun/list", data={
        "list": (io.BytesIO(_xlsx(ROWS[:3])), "v2.xlsx")}, content_type="multipart/form-data")
    assert "signed off" not in client.get("/billcheck/payrun").get_data(as_text=True).split("<h1")[1][:200]


def test_upload_rejects_unreadable_file(client):
    client.post("/billcheck/payrun/list", data={"list": (io.BytesIO(b"x,y\n1,2"), "junk.csv")},
                content_type="multipart/form-data")
    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "Couldn&#39;t read junk.csv" in page or "Couldn't read junk.csv" in page


def test_same_day_due_with_nothing_to_compare_is_not_flagged():
    # Applied Electronics: one bill, due the day it was issued (prepay).
    lines = [{"row": 3, "invoice": "82337", "vendor": "Applied Electronics",
              "invoice_date": "2026-09-28", "due_date": "2026-09-28",
              "amount": "3490.00", "balance": "3490.00", "note": ""}]
    kinds = {s["kind"] for s in fl.line_signals(lines, FRI).get(3, [])}
    assert "list_due_entry" not in kinds


def test_slack_message_lists_holds_and_totals():
    flist = {**fl.parse(_xlsx(), "x.xlsx"), "filename": "x.xlsx"}
    view = payrun.build([], {}, [], THU, horizon_days=15, finance_list=flist)
    msg = payrun.slack_message(view)
    assert msg.startswith("*Pay run for Fri 10/2*") and "Review in progress" in msg
    decisions = {}
    for x in view["rows"]:
        if x["needs_decision"]:
            hold = x["bill"]["vendor"] == "Wesco - Anixter"
            decisions[x["bill_id"]] = {"action": "hold" if hold else "release",
                                       "key": x["key"], "note": ""}
    view = payrun.build([], {}, [], THU, horizon_days=15, finance_list=flist,
                        decisions=decisions)
    msg = payrun.slack_message(view)
    assert "reviewed ✅" in msg and "Review in progress" not in msg
    assert "Releasing *8 bills, $258,072.70*" in msg
    assert "• Wesco - Anixter #531685513 — $2,089.98 (Due date = invoice date)" in msg
    assert "4 flagged bill(s) were checked and are OK to pay." in msg
