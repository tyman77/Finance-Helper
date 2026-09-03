"""Tests for the Sage Intacct destination.

post_journal_entry()'s actual HTTP calls aren't mocked here — same convention
as the fetch scripts (scripts/fetch_calendar_index.py etc.), where the network
layer is verified by a real run, not a mock. This covers everything that
doesn't require a live connection: the payload builder, the REST body mapping
(the part most likely to need a fix once tested live — see the module
docstring), and the credential-check guard clause.
"""

from decimal import Decimal

import pytest

from finance_helper.destinations import sage_intacct
from finance_helper.models import LineItem, SourceDocument


def _doc(lines, currency="USD"):
    return SourceDocument(source="united", destination="sage", vendor="United Airlines",
                          document_id="DOC-1", currency=currency, line_items=lines)


def test_build_journal_entry_balances_and_carries_dimensions():
    lines = [
        LineItem(description="Flight", amount=Decimal("100.00"), gl_account="52200",
                 department="60", project="4804"),
        LineItem(description="Refund", amount=Decimal("-20.00"), gl_account="52200"),
    ]
    payload = sage_intacct.build_journal_entry(_doc(lines))
    debits = sum(Decimal(l["debit"]) for l in payload["lines"])
    credits = sum(Decimal(l["credit"]) for l in payload["lines"])
    assert debits == credits

    flight_line = next(l for l in payload["lines"] if l["debit"] == "100.00")
    assert flight_line["department"] == "60"
    assert flight_line["project"] == "4804"

    refund_line = next(l for l in payload["lines"] if l["credit"] == "20.00")
    assert Decimal(refund_line["debit"]) == 0


def test_to_rest_body_maps_fields_and_preserves_balance():
    lines = [LineItem(description="Hotel", amount=Decimal("50.00"), gl_account="52300",
                      department="30", project="5036")]
    payload = sage_intacct.build_journal_entry(_doc(lines))
    body = sage_intacct._to_rest_body(payload)

    assert body["journalSymbol"] == payload["journal"]
    assert body["referenceNumber"] == "DOC-1"
    assert body["currency"] == "USD"
    assert len(body["lines"]) == len(payload["lines"])

    hotel_line = next(l for l in body["lines"] if l["glAccountNumber"] == "52300")
    assert hotel_line["debitAmount"] == "50.00"
    assert hotel_line["departmentId"] == "30"
    assert hotel_line["projectId"] == "5036"

    debits = sum(Decimal(l["debitAmount"]) for l in body["lines"])
    credits = sum(Decimal(l["creditAmount"]) for l in body["lines"])
    assert debits == credits


def test_to_rest_body_omits_dimension_keys_when_absent():
    lines = [LineItem(description="No dims", amount=Decimal("10.00"), gl_account="99500")]
    payload = sage_intacct.build_journal_entry(_doc(lines))
    body = sage_intacct._to_rest_body(payload)
    line = next(l for l in body["lines"] if l["glAccountNumber"] == "99500")
    assert "departmentId" not in line
    assert "projectId" not in line


_REQUIRED_ENV_VARS = (
    "INTACCT_CLIENT_ID", "INTACCT_CLIENT_SECRET", "INTACCT_COMPANY_ID",
    "INTACCT_USER_ID", "INTACCT_USER_PASSWORD",
)


def test_post_journal_entry_requires_credentials(monkeypatch):
    for var in _REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError) as exc:
        sage_intacct.post_journal_entry({})
    assert "INTACCT_CLIENT_ID" in str(exc.value)
    assert "INTACCT_USER_ID" in str(exc.value)  # confirmed required live, not just client id/secret
    assert "credentials missing" in str(exc.value)


def test_get_token_sends_username_and_password(monkeypatch):
    """Regression: Sage's token endpoint 400s with "Either username or
    session_id is required" for a pure client_credentials request — the Web
    Services User has to be identified in the body too."""
    import requests

    monkeypatch.setenv("INTACCT_CLIENT_ID", "cid")
    monkeypatch.setenv("INTACCT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("INTACCT_USER_ID", "wsuser")
    monkeypatch.setenv("INTACCT_USER_PASSWORD", "wspass")

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok"}

    def _fake_post(url, auth=None, data=None, **kwargs):
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr(requests, "post", _fake_post)
    assert sage_intacct._get_token() == "tok"
    assert captured["data"]["username"] == "wsuser"
    assert captured["data"]["password"] == "wspass"
    assert captured["data"]["grant_type"] == "client_credentials"


def test_network_failure_wrapped_as_runtime_error_not_uncaught(monkeypatch):
    """Regression: a DNS/proxy/timeout failure used to crash with a raw
    requests.exceptions traceback instead of the CLI/web UI's normal
    "Not posted: ..." handling, since only RuntimeError/NotImplementedError
    were caught upstream."""
    import requests

    for var in _REQUIRED_ENV_VARS:
        monkeypatch.setenv(var, "x")

    def _boom(*a, **k):
        raise requests.exceptions.ProxyError("simulated network failure")

    monkeypatch.setattr(requests, "post", _boom)
    with pytest.raises(RuntimeError) as exc:
        sage_intacct.post_journal_entry({"lines": []})
    assert "ProxyError" in str(exc.value)


def test_post_prefers_xml_gateway_and_builds_glbatch(monkeypatch):
    """With sender credentials present (this company's actual setup), posting
    goes through the XML gateway as a GLBATCH create — the REST token flow
    (which 401s invalid_client without a registered app) is never touched."""
    import xml.etree.ElementTree as ET

    from finance_helper.recon import sage_xml

    monkeypatch.setattr(sage_xml, "credentials_present", lambda: True)
    monkeypatch.setattr(sage_intacct, "_get_token",
                        lambda: (_ for _ in ()).throw(AssertionError("REST used")))
    sent = {}

    def fake_post(body):
        sent["xml"] = body
        return ET.fromstring(
            "<response><operation><result><status>success</status>"
            "<data><glbatch><RECORDNO>4471</RECORDNO></glbatch></data>"
            "</result></operation></response>")

    monkeypatch.setattr(sage_xml, "_post", fake_post)
    monkeypatch.setenv("INTACCT_DEFAULT_LOCATION", "100--Design & Install")
    payload = sage_intacct.build_journal_entry(_doc([
        LineItem(description="Hotel", amount=Decimal("100.00"),
                 gl_account="52200--COGS Travel: Flights / Parking",
                 department="20--Integration", project="P000635"),
        LineItem(description="Refund", amount=Decimal("-25.00"), gl_account="52200"),
    ]))
    out = sage_intacct.post_journal_entry(payload)
    assert out["posted_via"] == "xml_gateway" and out["record_no"] == "4471"

    root = ET.fromstring(sent["xml"])
    batch = root.find(".//function/create/GLBATCH")
    assert batch is not None
    assert batch.findtext("JOURNAL") == "GJ"
    entries = batch.findall("ENTRIES/GLENTRY")
    # Their real JE shape: dimensioned lines, then undimensioned mirrors on
    # the opposite side of the SAME account — no clearing account anywhere.
    assert [(e.findtext("TR_TYPE"), e.findtext("TRX_AMOUNT")) for e in entries] \
        == [("1", "100.00"), ("-1", "25.00"), ("-1", "100.00"), ("1", "25.00")]
    assert all(e.findtext("ACCOUNTNO") == "52200" for e in entries)
    assert entries[0].findtext("DEPARTMENT") == "20"
    assert entries[0].findtext("LOCATION") == "100"
    assert entries[0].findtext("PROJECTID") == "P000635"   # already an Intacct id
    # Mirrors keep dept/location (dept-required accounts like 71000 reject
    # bare lines; per-dept net is zero either way) but never the project.
    assert entries[2].findtext("DEPARTMENT") == "20"
    assert entries[2].findtext("LOCATION") == "100"
    assert entries[2].findtext("PROJECTID") is None


def test_job_number_translates_to_intacct_project_id(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path))
    (tmp_path / "sage_projects.json").write_text(json.dumps({
        "P000635": {"name": "Emmaus Church, GA | Building Expansion | 5368 |"},
        "P000654": {"name": "Grace Community Church, TX | Lighting Upgrade | 5369"},
    }))
    assert sage_intacct._intacct_project_id("5368") == "P000635"
    assert sage_intacct._intacct_project_id("P000654") == "P000654"
    assert sage_intacct._intacct_project_id("9999") == ""   # unmapped: no id
    # "53" appears in both names -> ambiguous, safer to post without one.
    assert sage_intacct._intacct_project_id("53") == ""


def test_inactive_projects_never_map(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path))
    (tmp_path / "sage_projects.json").write_text(json.dumps({
        "P000158": {"name": "Old Job | 4100", "status": "inactive"},
        "P000700": {"name": "Live Job | 4200", "status": "active"},
        "P000701": {"name": "No Status Job | 4300"},
    }))
    assert sage_intacct._intacct_project_id("4100") == ""       # closed: skip
    assert sage_intacct._intacct_project_id("P000158") == ""    # even literal
    assert sage_intacct._intacct_project_id("4200") == "P000700"
    assert sage_intacct._intacct_project_id("4300") == "P000701"  # no status = ok
