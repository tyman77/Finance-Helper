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


def test_post_journal_entry_requires_credentials(monkeypatch):
    for var in ("INTACCT_CLIENT_ID", "INTACCT_CLIENT_SECRET", "INTACCT_COMPANY_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError) as exc:
        sage_intacct.post_journal_entry({})
    assert "INTACCT_CLIENT_ID" in str(exc.value)
    assert "credentials missing" in str(exc.value)


def test_network_failure_wrapped_as_runtime_error_not_uncaught(monkeypatch):
    """Regression: a DNS/proxy/timeout failure used to crash with a raw
    requests.exceptions traceback instead of the CLI/web UI's normal
    "Not posted: ..." handling, since only RuntimeError/NotImplementedError
    were caught upstream."""
    import requests

    monkeypatch.setenv("INTACCT_CLIENT_ID", "x")
    monkeypatch.setenv("INTACCT_CLIENT_SECRET", "x")
    monkeypatch.setenv("INTACCT_COMPANY_ID", "x")

    def _boom(*a, **k):
        raise requests.exceptions.ProxyError("simulated network failure")

    monkeypatch.setattr(requests, "post", _boom)
    with pytest.raises(RuntimeError) as exc:
        sage_intacct.post_journal_entry({"lines": []})
    assert "ProxyError" in str(exc.value)
