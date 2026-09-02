"""Bill Check engine + store: read once, skip unchanged, re-verify edits."""

import json
import os

from finance_helper.billcheck import engine, store


def _bill(**over):
    base = {"id": "b1", "vendor": "Acme Supply Co", "invoice": "INV-1001",
            "invoice_date": "2026-08-01", "due_date": "2026-08-31",
            "amount": "1500.00", "terms": "Net 30", "terms_days": 30, "po": ""}
    base.update(over)
    return base


def _pdf(**over):
    base = {"schema": engine.SCHEMA_VERSION,
            "is_invoice": True, "vendor": "Acme Supply", "invoice_number": "1001",
            "invoice_date": "2026-08-01", "due_date": "2026-08-31", "terms": "Net 30",
            "terms_days": 30, "total": "1500.00", "currency": "USD", "po_number": None,
            "confidence": "high", "notes": ""}
    base.update(over)
    return base


class Fakes:
    def __init__(self, pdf=None, doc_error=None):
        self.pdf = pdf or _pdf()
        self.doc_error = doc_error
        self.fetches = 0
        self.reads = 0

    def fetch(self, bill_id):
        self.fetches += 1
        if self.doc_error:
            raise RuntimeError(self.doc_error)
        return [{"name": "inv.pdf", "media_type": "application/pdf", "data": b"%PDF-1.4 x"}]

    def extract(self, docs):
        self.reads += 1
        assert docs and docs[0]["data"].startswith(b"%PDF")
        return dict(self.pdf)


def test_first_check_reads_and_stores():
    f = Fakes()
    payload, outcome = engine.check_bill(_bill(), None, f.fetch, f.extract, who="t@x")
    assert outcome == "read" and payload["status"] == "match"
    store.save_result("b1", payload)
    saved = store.load_result("b1")
    assert saved["documents"][0]["source"] == "billdotcom"
    assert store.document_path("b1", 0)[1] == "application/pdf"
    assert f.fetches == 1 and f.reads == 1


def test_unchanged_bill_is_skipped_and_costs_nothing():
    f = Fakes()
    payload, _ = engine.check_bill(_bill(), None, f.fetch, f.extract)
    store.save_result("b1", payload)
    again, outcome = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract)
    assert again is None and outcome == "unchanged"
    assert f.fetches == 1 and f.reads == 1


def test_edited_bill_recompares_without_rereading():
    f = Fakes()
    payload, _ = engine.check_bill(_bill(due_date="2026-09-30"), None, f.fetch, f.extract)
    assert payload["status"] == "mismatch" and payload["severity"] == "critical"
    store.save_result("b1", payload)
    store.record_disposition("b1", "fixed", "corrected due date", "clerk@x")
    # Clerk fixed it in Bill.com; the next run sees new fields.
    fixed, outcome = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract)
    assert outcome == "reused" and fixed["status"] == "match"
    assert fixed["disposition"] is None                 # decision belonged to the old entry
    assert fixed["history"][0]["action"] == "fixed"
    assert f.reads == 1


def test_force_rereads_from_billcom():
    f = Fakes()
    payload, _ = engine.check_bill(_bill(), None, f.fetch, f.extract)
    store.save_result("b1", payload)
    _, outcome = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract, force=True)
    assert outcome == "read" and f.fetches == 2 and f.reads == 2


def test_no_document_then_retry_next_run():
    f = Fakes(doc_error="BDC_1234 no attachment")
    payload, outcome = engine.check_bill(_bill(), None, f.fetch, f.extract)
    assert outcome == "no_document" and payload["status"] == "no_document"
    assert "no attachment" in payload["error"]
    store.save_result("b1", payload)
    f.doc_error = None
    payload2, outcome2 = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract)
    assert outcome2 == "read" and payload2["status"] == "match"


def test_read_error_is_recorded():
    f = Fakes()
    def boom(docs):
        raise RuntimeError("Claude read failed: RateLimitError")
    payload, outcome = engine.check_bill(_bill(), None, f.fetch, boom)
    assert outcome == "error" and payload["status"] == "error"
    assert "RateLimitError" in payload["error"]


def test_uploaded_attachment_survives_force():
    docs = [{"name": "manual.pdf", "media_type": "application/pdf", "data": b"%PDF-1.4 manual"}]
    meta = store.save_documents("b1", docs, "upload")
    f = Fakes()
    payload, outcome = engine.check_bill(_bill(), {"documents": meta}, f.fetch, f.extract, force=True)
    assert outcome == "read" and f.fetches == 0
    assert payload["documents"][0]["source"] == "upload"


def test_duplicates_become_a_critical_finding():
    f = Fakes()
    payload, _ = engine.check_bill(_bill(), None, f.fetch, f.extract,
                                   duplicates=["Acme Supply Co #INV-1001 1500.00 dated 2026-05-01 (paid)"])
    assert payload["status"] == "mismatch" and payload["comparison"]["findings"][0]["field"] == "duplicate"


def test_run_check_counts_limit_and_retires_paid_bills():
    f = Fakes()
    bills = [_bill(id="b1"), _bill(id="b2", invoice="2", due_date="2026-08-20"),
             _bill(id="b3", invoice="3")]
    log = []
    summary = engine.run_check(lambda: list(bills), f.fetch, f.extract,
                               log=log.append, who="t@x", limit=2)
    assert summary["counts"]["read"] == 2 and summary["limit_hit"]
    assert f.reads == 2
    assert store.load_result("b2") is not None            # soonest due read first
    # Second run: the remaining bill gets read, the others are unchanged.
    summary2 = engine.run_check(lambda: list(bills), f.fetch, f.extract, limit=2)
    assert summary2["counts"] == {"unchanged": 2, "reused": 0, "read": 1,
                                  "no_document": 0, "error": 0}
    # b3 paid: drops out of the queue.
    engine.run_check(lambda: [b for b in bills if b["id"] != "b3"], f.fetch, f.extract)
    assert store.load_result("b3") is None
    assert store.load_run_summary()["bills"] == 2
    assert any("Done:" in line for line in log)


def test_list_results_orders_open_worst_first():
    f = Fakes()
    for bid, due in (("clean", "2026-08-31"), ("late", "2026-09-30"), ("early", "2026-08-10")):
        payload, _ = engine.check_bill(_bill(id=bid, due_date=due), None, f.fetch, f.extract)
        store.save_result(bid, payload)
    store.record_disposition("early", "accept", "vendor agreed", "t@x")
    ids = [r["bill_id"] for r in store.list_results()]
    assert ids == ["late", "clean", "early"] or ids == ["late", "early", "clean"]
    assert ids[0] == "late"
    assert store.is_open(store.load_result("late"))
    assert not store.is_open(store.load_result("early"))
    assert not store.is_open(store.load_result("clean"))


def test_disposition_hits_audit_log():
    f = Fakes()
    payload, _ = engine.check_bill(_bill(amount="9.00"), None, f.fetch, f.extract)
    store.save_result("b1", payload)
    assert store.record_disposition("b1", "investigate", "asked vendor", "t@x")
    assert not store.record_disposition("nope", "investigate", "", "t@x")
    audit = os.path.join(os.environ["FINANCE_HELPER_OUT_DIR"], "billcheck", "audit.jsonl")
    rows = [json.loads(line) for line in open(audit)]
    assert rows[-1]["bill_id"] == "b1" and rows[-1]["action"] == "investigate"


def test_fingerprint_ignores_noise_fields():
    a = store.fingerprint(_bill(approval_status="approved", updated="2026-08-02"))
    b = store.fingerprint(_bill(approval_status="approving", updated="2026-08-03"))
    assert a == b
    assert store.fingerprint(_bill(due_date="2026-09-01")) != a
    assert store.fingerprint(_bill(), extra=["dup"]) != a


def test_failed_read_refetches_from_billcom_instead_of_cached_copy():
    """A bad cached attachment (Bill.com answered with HTML) must not be
    re-read forever: the retry pulls the attachment again."""
    class Flaky(Fakes):
        def __init__(self):
            super().__init__()
            self.bad = True
        def fetch(self, bill_id):
            if self.bad:
                self.fetches += 1
                return [{"name": "page-1", "media_type": "text/html", "data": b"<html>login</html>"}]
            return super().fetch(bill_id)
        def extract(self, docs):
            if docs[0]["media_type"] == "text/html":
                raise RuntimeError("Unsupported attachment type text/html")
            return super().extract(docs)

    f = Flaky()
    payload, outcome = engine.check_bill(_bill(), None, f.fetch, f.extract)
    assert outcome == "error" and "text/html" in payload["error"]
    store.save_result("b1", payload)
    f.bad = False
    payload2, outcome2 = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract)
    assert outcome2 == "read" and payload2["status"] == "match"
    assert f.fetches == 2
    assert payload2["documents"][0]["media_type"] == "application/pdf"


def test_read_from_an_older_schema_is_refreshed_once():
    f = Fakes()
    payload, _ = engine.check_bill(_bill(), None, f.fetch, f.extract)
    payload["extracted"]["schema"] = 1                 # stored by an older build
    store.save_result("b1", payload)
    again, outcome = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract)
    assert outcome == "read" and f.reads == 2
    store.save_result("b1", again)
    third, outcome3 = engine.check_bill(_bill(), store.load_result("b1"), f.fetch, f.extract)
    assert third is None and outcome3 == "unchanged" and f.reads == 2
