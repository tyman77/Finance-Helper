"""Sage XML gateway: request envelope, response parsing, sign mapping.

All network calls are mocked — no live gateway access in tests.
"""

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import pytest

from finance_helper.recon import sage_xml

START, END = date(2026, 6, 1), date(2026, 6, 30)

_ENV = {
    "INTACCT_SENDER_ID": "summitSender",
    "INTACCT_SENDER_PASSWORD": "sp",
    "INTACCT_COMPANY_ID": "SummitIntegrated",
    "INTACCT_USER_ID": "shopifySIS",
    "INTACCT_USER_PASSWORD": "up",
}


@pytest.fixture
def creds(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def _response(rows_xml: str, numremaining: int = 0, result_id: str = "") -> str:
    return f"""<?xml version="1.0"?>
<response><operation>
  <authentication><status>success</status></authentication>
  <result><status>success</status><function>readByQuery</function>
    <data listtype="gldetail" count="1" numremaining="{numremaining}" resultId="{result_id}">
      {rows_xml}
    </data></result>
</operation></response>"""


_ROW = """<gldetail>
  <RECORDNO>991</RECORDNO><ENTRY_DATE>06/21/2026</ENTRY_DATE>
  <ACCOUNTNO>10700</ACCOUNTNO><TR_TYPE>-1</TR_TYPE><TRX_AMOUNT>1234.56</TRX_AMOUNT>
  <DESCRIPTION>ACME AV Supply invoice 4471</DESCRIPTION><JOURNAL>CD</JOURNAL>
</gldetail>"""


def test_request_envelope_has_sender_login_and_query(creds):
    body = sage_xml._request_xml(lambda fn: sage_xml._el(
        sage_xml._el(fn, "readByQuery"), "query", sage_xml._query_string(START, END)))
    root = ET.fromstring(body)
    assert root.findtext(".//control/senderid") == "summitSender"
    assert root.findtext(".//login/userid") == "shopifySIS"       # bare id, no @company
    assert root.findtext(".//login/companyid") == "SummitIntegrated"
    q = root.findtext(".//readByQuery/query")
    assert "ACCOUNTNO = '10700'" in q and "06/01/2026" in q


def test_fetch_and_map_signs_credits_negative(creds, monkeypatch):
    monkeypatch.setattr(sage_xml, "_post",
                        lambda body: ET.fromstring(_response(_ROW)))
    txns = sage_xml.fetch_ledger(START, END)
    assert len(txns) == 1
    t = txns[0]
    assert t.amount == Decimal("-1234.56")        # TR_TYPE -1 = credit = cash out
    assert t.source_id == "sage-xml:991"
    assert "acme" in t.counterparty_norm


def test_readmore_pagination_follows_result_id(creds, monkeypatch):
    pages = [
        ET.fromstring(_response(_ROW, numremaining=1, result_id="RID1")),
        ET.fromstring(_response(_ROW.replace("991", "992"), numremaining=0)),
    ]
    calls = []

    def fake_post(body):
        calls.append(body)
        return pages[len(calls) - 1]

    monkeypatch.setattr(sage_xml, "_post", fake_post)
    records = sage_xml.fetch_gl_records(START, END)
    assert len(records) == 2 and len(calls) == 2
    assert b"RID1" in calls[1]                    # second call is the readMore


def test_login_failure_raises_clear_error():
    xml = """<?xml version="1.0"?>
<response><operation><authentication><status>failure</status></authentication>
<errormessage><error><description2>Sign-in information is incorrect</description2></error></errormessage>
</operation></response>"""

    class FakeResp:
        status_code = 200
        text = xml

    import requests
    with pytest.raises(RuntimeError, match="Sign-in information is incorrect"):
        orig = requests.post
        requests.post = lambda *a, **k: FakeResp()
        try:
            sage_xml._post(b"<request/>")
        finally:
            requests.post = orig


def test_dispatcher_prefers_xml_gateway(monkeypatch):
    from finance_helper.web import cashproof
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    for k in ("INTACCT_CLIENT_ID", "INTACCT_CLIENT_SECRET"):
        monkeypatch.setenv(k, "also-set")
    fetch, label = cashproof._sage_fetcher()
    assert fetch is sage_xml.fetch_ledger
    assert "XML" in label
    # Without sender creds, falls back to REST.
    monkeypatch.delenv("INTACCT_SENDER_ID")
    fetch2, label2 = cashproof._sage_fetcher()
    assert label2 == "Sage API"


def test_empty_ledger_raises_with_cash_account_candidates(creds, monkeypatch):
    monkeypatch.setattr(sage_xml, "fetch_gl_records", lambda s, e: [])
    monkeypatch.setattr(sage_xml, "list_cash_candidate_accounts",
                        lambda: [("1000", "Checking - Flatirons"), ("1010", "Savings")])
    with pytest.raises(RuntimeError) as err:
        sage_xml.fetch_ledger(START, END)
    msg = str(err.value)
    assert "NO GL rows" in msg
    assert "1000 (Checking - Flatirons)" in msg
    assert "SAGE_CASH_ACCOUNTS" in msg


def test_cash_accounts_env_override(monkeypatch):
    from finance_helper.recon.settings import recon_config
    monkeypatch.setenv("SAGE_CASH_ACCOUNTS", "1000, 1005")
    assert recon_config()["sage"]["cash_accounts"] == ["1000", "1005"]
    monkeypatch.delenv("SAGE_CASH_ACCOUNTS")
    assert recon_config()["sage"]["cash_accounts"] == ["10700"]


def test_annotate_unmatched_says_where_or_nowhere(monkeypatch):
    from datetime import date as _d
    from decimal import Decimal as _D

    from finance_helper.recon.models import Txn

    def txn(i, amount, status="exception"):
        t = Txn(source="bank", source_id=f"bank:{i}", posted_date=_d(2026, 3, 19),
                amount=_D(amount), counterparty_raw="PAYCHEX", counterparty_norm="paychex")
        t.status = status
        t.reason = "no ledger tie found"
        return t

    found = txn(1, "-175023.08")
    nowhere = txn(2, "-53558.54")
    tied = txn(3, "-10", status="tied")

    monkeypatch.setattr(sage_xml, "_account_titles",
                        lambda: {"60100": "Payroll Expense"})

    def fake_probe(amount, around, window_days):
        if amount == _D("-175023.08"):
            return [{"ACCOUNTNO": "60100", "ENTRY_DATE": "03/19/2026",
                     "JOURNAL": "PYR", "TR_TYPE": "-1"}]
        return []

    monkeypatch.setattr(sage_xml, "_amount_probe", fake_probe)
    n = sage_xml.annotate_unmatched([found, nowhere, tied])
    assert n == 2
    assert "60100 (Payroll Expense)" in found.reason
    assert "NOWHERE" in nowhere.reason
    assert tied.reason == "no ledger tie found"        # non-exceptions untouched


def test_aux_clearing_accounts_join_the_pull(monkeypatch):
    from datetime import date as _d

    q = sage_xml._query_string(_d(2026, 6, 1), _d(2026, 6, 30))
    for acct in ("10700", "10704", "10705", "14990"):
        assert f"ACCOUNTNO = '{acct}'" in q
    assert "10706" not in q                     # savings moves via its own stmt

    recs = [
        {"RECORDNO": "1", "ACCOUNTNO": "10705", "ENTRY_DATE": "06/10/2026",
         "TRX_AMOUNT": "500.00", "TR_TYPE": "-1",
         "DESCRIPTION": "Payments(Bank-BNK1): batch", "JOURNAL": "CD"},
        {"RECORDNO": "2", "ACCOUNTNO": "10706", "ENTRY_DATE": "06/10/2026",
         "TRX_AMOUNT": "100.00", "TR_TYPE": "1", "DESCRIPTION": "savings int"},
    ]
    txns = sage_xml.to_txns(recs, _d(2026, 6, 1), _d(2026, 6, 30))
    assert [t.account_ref for t in txns] == ["10705"]

    # SAGE_AUX_ACCOUNTS overrides without a deploy.
    monkeypatch.setenv("SAGE_AUX_ACCOUNTS", "10704")
    q2 = sage_xml._query_string(_d(2026, 6, 1), _d(2026, 6, 30))
    assert "10704" in q2 and "10705" not in q2
