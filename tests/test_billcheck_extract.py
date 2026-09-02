"""The Claude read wrapper: request shape, refusal/empty handling, size guard."""

import base64

import pytest

from finance_helper.billcheck import extract


class FakeResp:
    def __init__(self, parsed=None, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = "claude-test"
        self.usage = type("U", (), {"input_tokens": 1200, "output_tokens": 80})()


class FakeClient:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None
        self.messages = self

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


PDF = [{"name": "a.pdf", "media_type": "application/pdf", "data": b"%PDF-1.4 hello"}]


def test_content_blocks_pdf_and_images():
    docs = PDF + [{"name": "p2.png", "media_type": "image/png", "data": b"\x89PNG"}]
    blocks = extract.content_blocks(docs)
    assert [b["type"] for b in blocks] == ["document", "image", "text"]
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"%PDF-1.4 hello"
    with pytest.raises(RuntimeError, match="Unsupported"):
        extract.content_blocks([{"name": "x.tiff", "media_type": "image/tiff", "data": b"x"}])


def test_content_blocks_size_guard(monkeypatch):
    monkeypatch.setattr(extract, "MAX_BYTES", 10)
    with pytest.raises(RuntimeError, match="too large"):
        extract.content_blocks(PDF)


def test_extract_invoice_calls_parse_with_structured_output(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("BILLCHECK_MODEL", "claude-test")
    monkeypatch.setenv("BILLCHECK_EFFORT", "low")
    parsed = extract.InvoiceFields(
        is_invoice=True, vendor="Acme", invoice_number="1", invoice_date="2026-08-01",
        due_date=None, terms="Net 30", terms_days=30, total="10.00",
        discount_total=None, discount_date=None, discount_terms=None, currency="USD",
        po_number=None, confidence="high", notes="")
    client = FakeClient(FakeResp(parsed))
    out = extract.extract_invoice(PDF, client=client)
    assert out["vendor"] == "Acme" and out["terms_days"] == 30
    assert out["model"] == "claude-test" and out["usage"] == {"input": 1200, "output": 80}
    kw = client.kwargs
    assert kw["model"] == "claude-test"
    assert kw["output_format"] is extract.InvoiceFields
    assert kw["output_config"] == {"effort": "low"}
    assert kw["system"] == extract.SYSTEM_PROMPT
    assert kw["messages"][0]["content"][0]["type"] == "document"
    # The entered Bill.com values never reach the model.
    assert "1500" not in str(kw["messages"])


def test_extract_invoice_failure_modes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(RuntimeError, match="declined"):
        extract.extract_invoice(PDF, client=FakeClient(FakeResp(None, "refusal")))
    with pytest.raises(RuntimeError, match="no structured fields"):
        extract.extract_invoice(PDF, client=FakeClient(FakeResp(None)))

    class Boom(FakeClient):
        def parse(self, **kw):
            raise ValueError("bad request")
    with pytest.raises(RuntimeError, match="Claude read failed: ValueError"):
        extract.extract_invoice(PDF, client=Boom(None))
    with pytest.raises(RuntimeError, match="No attachment"):
        extract.extract_invoice([], client=FakeClient(FakeResp(None)))


def test_extract_invoice_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        extract.extract_invoice(PDF, client=FakeClient(FakeResp(None)))


def test_grammar_timeout_is_retried(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("time.sleep", lambda s: None)
    parsed = extract.InvoiceFields(
        is_invoice=True, vendor="Acme", invoice_number="1", invoice_date="2026-08-01",
        due_date=None, terms=None, terms_days=None, total="10.00",
        discount_total=None, discount_date=None, discount_terms=None, currency="USD",
        po_number=None, confidence="high", notes="")

    class Flaky(FakeClient):
        calls = 0
        def parse(self, **kw):
            Flaky.calls += 1
            if Flaky.calls < 3:
                raise ValueError("Error code: 400 - Grammar compilation timed out.")
            return FakeResp(parsed)

    out = extract.extract_invoice(PDF, client=Flaky(None))
    assert out["vendor"] == "Acme" and Flaky.calls == 3

    class AlwaysFlaky(FakeClient):
        def parse(self, **kw):
            raise ValueError("Grammar compilation timed out.")
    with pytest.raises(RuntimeError, match="Grammar compilation"):
        extract.extract_invoice(PDF, client=AlwaysFlaky(None))
