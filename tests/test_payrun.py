"""Pay Run: which bills go out on pay day, fraud signals, decisions, sign-off."""

import json
import os
from datetime import date, timedelta

import pytest

from finance_helper import billdotcom_api
from finance_helper.billcheck import extract, payrun
from finance_helper.billcheck import store as bc_store
from finance_helper.web.app import RUNS, create_app

THU = date(2026, 10, 1)          # a Thursday
FRI = date(2026, 10, 2)


def _bill(id, vendor="Acme Supply", vendor_id="v1", amount="500.00", due="2026-10-05",
          invoice=None, invoice_date="2026-09-01", **kw):
    return {"id": id, "vendor": vendor, "vendor_id": vendor_id, "amount": amount,
            "due_date": due, "invoice": invoice or f"INV-{id}", "invoice_date": invoice_date,
            "payment_status": "open", "approval_status": "approved", **kw}


def _result(bill, status="match", severity="clear", findings=None, disposition=None):
    return {"bill_id": bill["id"], "bill": bill, "status": status, "severity": severity,
            "comparison": {"findings": findings or []}, "disposition": disposition}


MASTER = {
    "vendors": [
        {"id": "v1", "name": "Acme Supply", "active": True, "created": "2020-01-01"},
        {"id": "v2", "name": "Brand New LLC", "active": True, "created": "2026-09-20"},
        {"id": "v3", "name": "Steady Freight", "active": True, "created": "2019-05-01"},
    ],
    "bank_accounts": [
        {"vendor_id": "v1", "vendor": "Acme Supply", "created": "2021-01-01", "active": True},
        {"vendor_id": "v3", "vendor": "Steady Freight", "created": "2026-09-25", "active": True},
    ],
    "bills": [
        {"id": f"h{i}", "vendor": "Acme Supply", "invoice": f"H{i}",
         "invoice_date": f"2026-0{i}-01", "amount": "480.00"} for i in range(3, 8)
    ] + [{"id": "h-sf", "vendor": "Steady Freight", "invoice": "SF1",
          "invoice_date": "2026-08-01", "amount": "200.00"}],
    "gaps": [],
}


def test_next_pay_date_is_the_coming_friday():
    assert payrun.next_pay_date(THU) == FRI
    assert payrun.next_pay_date(FRI) == FRI
    assert payrun.next_pay_date(FRI + timedelta(days=1)) == FRI + timedelta(days=7)


def test_run_holds_scheduled_and_bills_due_before_next_pay_day():
    assert payrun.in_run(_bill("a", due="2026-10-08"), FRI)          # due next Thu
    assert not payrun.in_run(_bill("b", due="2026-10-09"), FRI)      # next Friday's run
    assert payrun.in_run(_bill("c", due="2026-11-30", payment_status="scheduled"), FRI)
    assert payrun.in_run(_bill("d", due=""), FRI)                    # no due date: look at it
    assert not payrun.in_run(_bill("e", payment_status="paid"), FRI)


def test_clean_bill_from_established_vendor_has_no_signals():
    assert payrun.fraud_signals(_bill("x"), MASTER, [], THU) == []


def test_recent_bank_change_is_critical_even_on_a_known_vendor():
    sig = payrun.fraud_signals(_bill("x", vendor="Steady Freight", vendor_id="v3",
                                     amount="210.00"), MASTER, [], THU)
    assert sig[0]["kind"] == "bank_change" and sig[0]["severity"] == "critical"


def test_new_vendor_first_payment():
    kinds = {s["kind"] for s in payrun.fraud_signals(
        _bill("x", vendor="Brand New LLC", vendor_id="v2"), MASTER, [], THU)}
    assert kinds == {"new_vendor"}            # "first bill" alone is no longer flagged


def test_amount_far_above_vendor_history():
    sig = payrun.fraud_signals(_bill("x", amount="5000.00"), MASTER, [], THU)
    assert [s["kind"] for s in sig] == ["amount_outlier"]
    assert "$480.00" in sig[0]["detail"]


def test_big_but_normal_for_a_high_variance_vendor_is_quiet():
    # The QSC case: 4x the median, but well inside what the vendor has
    # billed before.
    hist = [{"id": f"q{i}", "vendor": "QSC", "invoice": f"Q{i}",
             "invoice_date": "2026-01-01", "amount": a}
            for i, a in enumerate(["1226.36"] * 780 + ["9800.00", "15400.00"] * 5)]
    master = {**MASTER, "bills": hist}
    sig = payrun.fraud_signals(_bill("x", vendor="QSC", amount="4864.86"), master, [], THU)
    assert "amount_outlier" not in {s["kind"] for s in sig}
    sig = payrun.fraud_signals(_bill("y", vendor="QSC", amount="25000.00"), master, [], THU)
    assert "amount_outlier" in {s["kind"] for s in sig}


def test_same_amount_new_invoice_number():
    sig = payrun.fraud_signals(_bill("x", amount="480.00", invoice_date="2026-07-10"),
                               MASTER, [], THU)
    assert any(s["kind"] == "same_amount" for s in sig)


def test_vendor_master_findings_attach_to_the_vendors_bills():
    finding = {"kind": "vendor_employee_collision", "severity": "critical",
               "title": "Vendor named like an employee", "detail": "...", "vendor_ids": ["v1"]}
    sig = payrun.fraud_signals(_bill("x"), MASTER, [finding], THU)
    assert sig[0]["kind"] == "vendor_employee_collision"


def test_build_combines_bill_check_and_signals_and_gates_signoff():
    results = [
        _result(_bill("ok")),
        _result(_bill("bank", vendor="Steady Freight", vendor_id="v3", amount="210.00")),
        _result(_bill("late", due="2026-12-01")),                      # not in this run
        _result(_bill("wrong", amount="499.00"), status="mismatch", severity="critical",
                findings=[{"reason": "Total differs"}]),
    ]
    view = payrun.build(results, MASTER, [], THU)
    assert view["pay_date"] == "2026-10-02"
    ids = [x["bill_id"] for x in view["rows"]]
    assert set(ids) == {"ok", "bank", "wrong"} and ids[-1] == "ok"
    assert view["undecided"] == 2 and not view["ready_to_sign"]

    keys = {x["bill_id"]: x["key"] for x in view["rows"]}
    decisions = {"bank": {"action": "release", "key": keys["bank"]},
                 "wrong": {"action": "hold", "key": keys["wrong"]}}
    view = payrun.build(results, MASTER, [], THU, decisions=decisions)
    assert view["ready_to_sign"]
    assert view["held_count"] == 1 and str(view["held_total"]) == "499.00"
    assert view["releasing_count"] == 2 and str(view["releasing_total"]) == "710.00"

    # A decision made on different facts no longer counts.
    decisions["bank"]["key"] = "stale"
    assert payrun.build(results, MASTER, [], THU, decisions=decisions)["undecided"] == 1


def test_unverified_invoice_needs_a_decision():
    view = payrun.build([_result(_bill("n"), status="no_document", severity="review")],
                        MASTER, [], THU)
    assert view["rows"][0]["severity"] == "high" and view["undecided"] == 1


# --- web flow ---------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    (data / "billdotcom_master.json").write_text(json.dumps(MASTER))
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(data))
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


def _seed(today):
    soon = (today + timedelta(days=2)).isoformat()
    for r in (_result(_bill("ok", due=soon)),
              _result(_bill("bank", vendor="Steady Freight", vendor_id="v3",
                            amount="210.00", due=soon))):
        bc_store.save_result(r["bill_id"], r)


def test_payrun_page_decide_and_sign_off(client, monkeypatch):
    # Bank change dated relative to today so the test doesn't age out.
    master = json.loads(json.dumps(MASTER))
    master["bank_accounts"][1]["created"] = (date.today() - timedelta(days=3)).isoformat()
    (open(os.path.join(os.environ["FINANCE_HELPER_DATA"], "billdotcom_master.json"), "w")
     .write(json.dumps(master)))
    _seed(date.today())

    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "Steady Freight" in page and "Bank details added" in page
    # Hooks the in-place Release/Hold script swaps (no full page reload).
    for hook in ('id="payrun-kpis"', 'id="payrun-signoff"', 'class="decision-cell"',
                 'class="decide-form"', 'getAttribute("action")',
                 'id="run-filters"', 'data-state="needs"', 'data-state="ok"'):
        assert hook in page, hook
    assert "still need a release or hold" in page
    # Checks that couldn't run are listed, not silently skipped.
    assert "who entered them" in page and "Approvers not pulled for 2" in page

    resp = client.post("/billcheck/payrun/signoff")
    assert resp.status_code == 302

    # No note needed — one click records who decided and when.
    client.post("/billcheck/payrun/decide/bank", data={"action": "hold", "note": ""})
    assert "HELD" in client.get("/billcheck/payrun").get_data(as_text=True)

    client.post("/billcheck/payrun/decide/bank",
                data={"action": "release", "note": "Called Jo at 555-0100 from our file"})
    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "Released" in page and "Sign off: release 2 bills" in page

    client.post("/billcheck/payrun/signoff", data={"note": "ok"})
    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "signed off" in page

    audit = os.path.join(os.environ["FINANCE_HELPER_OUT_DIR"], "billcheck", "audit.jsonl")
    lines = [json.loads(line) for line in open(audit)]
    assert [x["action"] for x in lines] == ["hold", "release", "signoff"]
    assert all(x["kind"] == "payrun" for x in lines)

    csv = client.get("/billcheck/payrun.csv").get_data(as_text=True)
    assert "Called Jo" in csv

    # The batch changing voids the sign-off.
    soon = (date.today() + timedelta(days=1)).isoformat()
    r = _result(_bill("added", due=soon))
    bc_store.save_result("added", r)
    page = client.get("/billcheck/payrun").get_data(as_text=True)
    assert "no longer applies" in page


def test_refresh_runs_bill_check_and_returns_to_payrun(client, monkeypatch):
    monkeypatch.setattr(billdotcom_api, "credentials_present", lambda: True)
    monkeypatch.setattr(extract, "credentials_present", lambda: True)
    monkeypatch.setattr(billdotcom_api, "fetch_open_bills", lambda: [])
    pulled = []
    monkeypatch.setattr(billdotcom_api, "fetch_master_index",
                        lambda: pulled.append(1) or MASTER)
    os.utime(os.path.join(os.environ["FINANCE_HELPER_DATA"], "billdotcom_master.json"),
             (0, 0))                                         # stale -> re-pulled
    resp = client.post("/billcheck/run", data={"next": "payrun"})
    done = client.get(resp.headers["Location"])
    assert done.headers["Location"].endswith("/billcheck/payrun")
    assert pulled == [1]


# --- invoice bank details vs Bill.com ----------------------------------------

ACCTS = [{"vendor_id": "v1", "account_last4": "4321", "routing": "111000025", "active": True}]
VENDOR = {"id": "v1", "zip": "80202", "address": "1 Main St, Denver, CO, 80202"}


def _kinds(sig):
    return [s["kind"] for s in sig]


def test_invoice_without_bank_details_is_quiet():
    assert payrun.remit_signals({"remit_account_last4": ""}, VENDOR, ACCTS) == []
    assert payrun.remit_signals(None, VENDOR, ACCTS) == []


def test_invoice_bank_matching_bill_com_is_quiet():
    ex = {"remit_account_last4": "4321", "remit_routing_number": "111000025",
          "remit_address": "1 Main St, Denver CO 80202"}
    assert payrun.remit_signals(ex, VENDOR, ACCTS) == []


def test_invoice_bank_account_differs_is_critical():
    sig = payrun.remit_signals({"remit_account_last4": "9999", "remit_bank_name": "Chase"},
                               VENDOR, ACCTS)
    assert _kinds(sig) == ["invoice_bank_mismatch"] and sig[0]["severity"] == "critical"
    assert "9999" in sig[0]["title"] and "4321" in sig[0]["title"]


def test_same_last4_different_routing_is_critical():
    sig = payrun.remit_signals({"remit_account_last4": "4321",
                                "remit_routing_number": "021000021"}, VENDOR, ACCTS)
    assert _kinds(sig) == ["invoice_routing_mismatch"]


def test_invoice_bank_but_vendor_paid_by_check():
    sig = payrun.remit_signals({"remit_account_last4": "9999"}, VENDOR, [])
    assert _kinds(sig) == ["invoice_bank_no_account"] and sig[0]["severity"] == "high"


def test_bill_com_hides_account_numbers():
    # Nothing to compare against: reported once as a gap, not on every bill.
    sig = payrun.remit_signals({"remit_account_last4": "9999"}, VENDOR,
                               [{"vendor_id": "v1", "account_last4": "", "active": True}])
    assert sig == []


def test_bank_change_notice_on_invoice():
    sig = payrun.remit_signals({"bank_change_notice": True,
                                "bank_change_text": "Please update our banking info"},
                               VENDOR, ACCTS)
    assert sig[0]["kind"] == "invoice_bank_notice" and sig[0]["severity"] == "critical"
    assert "update our banking" in sig[0]["detail"]


def test_remit_zip_differs_is_not_flagged():
    sig = payrun.remit_signals({"remit_address": "PO Box 9, Dallas, TX 75201-1234"},
                               VENDOR, ACCTS)
    assert sig == []


# --- same person ---------------------------------------------------------------

USERS = {"u1": {"name": "Pat Clerk"}, "u2": {"name": "Sam Boss"}}


def test_one_person_every_step_is_critical():
    sig = payrun.same_person_signals({"created_by": "u1"}, {"created_by": "u1"},
                                     [{"user_id": "u1"}, {"user_id": "u2"}], USERS)
    assert _kinds(sig) == ["same_person_all"] and "Pat Clerk" in sig[0]["title"]


def test_entered_and_approves_is_high():
    sig = payrun.same_person_signals({"created_by": "u1"}, {"created_by": "u2"},
                                     [{"user_id": "u1"}], USERS)
    assert _kinds(sig) == ["same_person_enter_approve"]


def test_approver_set_up_the_vendor_is_high():
    sig = payrun.same_person_signals({"created_by": "u1"}, {"created_by": "u2"},
                                     [{"user_id": "u2"}], USERS)
    assert _kinds(sig) == ["same_person_vendor_approve"] and "Sam Boss" in sig[0]["title"]


def test_independent_approval_is_quiet():
    assert payrun.same_person_signals({"created_by": "u1"}, {"created_by": "u3"},
                                      [{"user_id": "u2"}], USERS) == []
    # Set up the vendor and entered the bill, but someone else approves.
    assert payrun.same_person_signals({"created_by": "u1"}, {"created_by": "u1"},
                                      [{"user_id": "u2"}], USERS) == []


def test_unknown_creators_raise_nothing():
    assert payrun.same_person_signals({}, {}, [{"user_id": "u1"}], USERS) == []


def test_build_wires_extracted_and_approvers_through():
    master = {**MASTER, "users": [{"id": "u1", "name": "Pat Clerk"}],
              "vendors": [dict(v, created_by="u1") if v["id"] == "v1" else v
                          for v in MASTER["vendors"]],
              "bank_accounts": [{"vendor_id": "v1", "account_last4": "4321",
                                 "created": "2021-01-01", "active": True}]}
    r = _result(_bill("x", created_by="u1"))
    r["extracted"] = {"remit_account_last4": "9999", "schema": extract.SCHEMA_VERSION}
    view = payrun.build([r], master, [], THU, approvers={"x": [{"user_id": "u1"}]})
    kinds = _kinds(view["rows"][0]["signals"])
    assert kinds[:2] == ["invoice_bank_mismatch", "same_person_all"]
    assert view["rows"][0]["severity"] == "critical"


def test_configured_window_covers_two_weeks_ahead():
    # AP pays about two weeks ahead: the 10/2 run carried bills due through
    # 10/16 (the second Friday after), none later.
    h = payrun.settings()["horizon_days"]
    assert payrun.in_run(_bill("a", due="2026-10-16"), FRI, h)
    assert not payrun.in_run(_bill("b", due="2026-10-17"), FRI, h)
    view = payrun.build([_result(_bill("a", due="2026-10-16"))], MASTER, [], THU,
                        horizon_days=h)
    assert view["due_through"] == "2026-10-16" and len(view["rows"]) == 1


def test_approval_in_progress_is_not_flagged():
    sig = payrun.fraud_signals(_bill("x", approval_status="approving"), MASTER, [], THU)
    assert sig == []
