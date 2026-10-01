"""Bill Check learning loop: prior decisions, proposals, learned policies,
standing notes, and the Claude judgment pass."""

import json
import os

import pytest

from finance_helper.billcheck import engine, learn, store


def _bill(**over):
    base = {"id": "b1", "vendor": "Acme Supply Co", "invoice": "INV-1001",
            "invoice_date": "2026-08-01", "due_date": "2026-09-30",
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
    def __init__(self, pdf=None):
        self.pdf = pdf or _pdf()
        self.reads = 0

    def fetch(self, bill_id):
        return [{"name": "inv.pdf", "media_type": "application/pdf", "data": b"%PDF-1.4 x"}]

    def extract(self, docs):
        self.reads += 1
        return dict(self.pdf)


def _check_and_save(bill, fakes, **kw):
    fakes.pdf["invoice_number"] = bill["invoice"]        # the PDF agrees on the number
    payload, outcome = engine.check_bill(bill, store.load_result(bill["id"]),
                                         fakes.fetch, fakes.extract, **kw)
    if payload is not None:
        store.save_result(bill["id"], payload)
    return store.load_result(bill["id"]), outcome


def _accept(bill_id, note, who="sarah@x.com"):
    assert store.record_disposition(bill_id, "accept", note, who)


# --- lessons & audit log ----------------------------------------------------

def test_disposition_writes_a_lesson_with_bill_context():
    f = Fakes()
    r, _ = _check_and_save(_bill(), f)
    assert r["status"] == "mismatch" and r["comparison"]["findings"][0]["field"] == "due_date"
    _accept("b1", "they bill from ship date")
    rows = learn.lessons()
    assert len(rows) == 1
    l = rows[0]
    assert l["vendor"] == "Acme Supply Co" and l["invoice"] == "INV-1001"
    assert l["fields"] == ["due_date"] and l["action"] == "accept"
    assert "ship date" in l["note"] and l["who"] == "sarah@x.com"


def test_lessons_survive_the_bill_being_retired():
    f = Fakes()
    _check_and_save(_bill(), f)
    _accept("b1", "ship date")
    store.delete_result("b1")                 # bill paid, dropped from the queue
    assert len(learn.lessons_for("ACME Supply")) == 1


def test_old_audit_rows_without_context_are_filled_from_the_result():
    f = Fakes()
    _check_and_save(_bill(), f)
    os.makedirs(store._root(), exist_ok=True)
    with open(os.path.join(store._root(), "audit.jsonl"), "a") as fh:
        fh.write(json.dumps({"bill_id": "b1", "action": "accept", "note": "old row",
                             "who": "sarah@x.com", "when": "2026-09-10T10:00:00"}) + "\n")
        fh.write(json.dumps({"bill_id": "gone", "action": "accept", "note": "x",
                             "who": "sarah@x.com", "when": "2026-09-10T10:00:00"}) + "\n")
    rows = learn.lessons()
    assert len(rows) == 1 and rows[0]["vendor"] == "Acme Supply Co" and rows[0]["fields"] == ["due_date"]


# --- applying prior decisions -----------------------------------------------

def test_first_accept_annotates_second_accept_downgrades():
    f = Fakes()
    r1, _ = _check_and_save(_bill(id="b1", invoice="INV-1001"), f)
    _accept("b1", "they bill from ship date")

    r2, _ = _check_and_save(_bill(id="b2", invoice="INV-1002"), f)
    due = next(x for x in r2["comparison"]["findings"] if x["field"] == "due_date")
    assert due["severity"] == "critical"                       # still flagged
    assert due["prior_accepts"] == 1 and "#INV-1001 by sarah" in due["reason"]
    assert "ship date" in due["reason"]
    assert r2["comparison"]["learned_context"][0]["count"] == 1
    _accept("b2", "ship date again")

    r3, _ = _check_and_save(_bill(id="b3", invoice="INV-1003"), f)
    due = next(x for x in r3["comparison"]["findings"] if x["field"] == "due_date")
    assert due["severity"] == "review" and due["learned"] is True
    assert "Flagged 2 times" in due["reason"]
    assert r3["status"] == "review" and r3["severity"] == "review"
    assert f.reads == 3


def test_other_vendors_and_other_fields_are_untouched():
    f = Fakes()
    _check_and_save(_bill(id="b1"), f)
    _accept("b1", "ship date")
    _check_and_save(_bill(id="b2", invoice="2"), f)
    _accept("b2", "ship date")
    # Different vendor, same kind of finding: nothing learned applies.
    r, _ = _check_and_save(_bill(id="z1", vendor="Zenith Logistics", invoice="9"), f)
    due = next(x for x in r["comparison"]["findings"] if x["field"] == "due_date")
    assert due["severity"] == "critical" and "prior_accepts" not in due
    # Same vendor, a different field (total): still critical.
    r, _ = _check_and_save(_bill(id="b9", invoice="9", amount="999.00"), f)
    amt = next(x for x in r["comparison"]["findings"] if x["field"] == "amount")
    assert amt["severity"] == "critical" and "prior_accepts" not in amt


def test_duplicate_findings_never_learn_away():
    f = Fakes()
    r, _ = _check_and_save(_bill(), f, duplicates=["Acme #INV-1001 1500.00 dated 2026-05-01 (paid)"])
    _accept("b1", "legit")
    _check_and_save(_bill(id="b2", invoice="2"), f, duplicates=["x"])
    _accept("b2", "legit")
    r, _ = _check_and_save(_bill(id="b3", invoice="3"), f, duplicates=["y"])
    dup = next(x for x in r["comparison"]["findings"] if x["field"] == "duplicate")
    assert dup["severity"] == "critical" and r["severity"] == "critical"


def test_learning_change_recompares_without_rereading_and_keeps_disposition():
    f = Fakes()
    r, _ = _check_and_save(_bill(id="b1"), f)
    store.record_disposition("b1", "investigate", "checking", "sarah@x.com")
    # A new standing note changes the learning signature → re-compare.
    learn.save_standing_notes("Acme bills from the ship date")
    r2, outcome = _check_and_save(_bill(id="b1"), f)
    assert outcome == "reused" and f.reads == 1
    assert r2["disposition"]["action"] == "investigate"      # not bumped to history
    assert not r2["history"]
    # Unchanged learning state: skipped outright.
    r3, outcome3 = _check_and_save(_bill(id="b1"), f)
    assert outcome3 == "unchanged"


# --- proposals & confirmation -----------------------------------------------

def test_proposal_after_two_accepts_and_confirm_suppresses():
    f = Fakes()
    _check_and_save(_bill(id="b1"), f)
    _accept("b1", "ship date")
    assert learn.proposals() == []
    _check_and_save(_bill(id="b2", invoice="2"), f)
    _accept("b2", "ship date")
    props = learn.proposals()
    assert len(props) == 1
    p = props[0]
    assert p["vendor"] == "Acme Supply Co" and p["field"] == "due_date" and p["count"] == 2
    assert p["notes"] == ["ship date"] and p["who"] == ["sarah"]

    learn.confirm_proposal("Acme Supply Co", "due_date", "tyson@x.com", "terms run from ship date")
    assert learn.proposals() == []
    pol = learn.learned_policies()["Acme Supply Co"]
    assert pol["ignore_findings"] == ["due_date"] and "ship date" in pol["notes"]

    r, _ = _check_and_save(_bill(id="b3", invoice="3"), f)
    assert r["status"] == "match" and not r["comparison"]["findings"]
    sup = r["comparison"]["suppressed"]
    assert sup[0]["field"] == "due_date" and "vendor policy" in sup[0]["why"]
    assert "ship date" in sup[0]["why"]
    assert not store.is_open(r)


def test_dismiss_hides_proposal_until_removed_and_unlearn_restores():
    f = Fakes()
    for i in (1, 2):
        _check_and_save(_bill(id=f"b{i}", invoice=str(i)), f)
        _accept(f"b{i}", "fine")
    learn.dismiss_proposal("Acme Supply Co", "due_date", "tyson@x.com")
    assert learn.proposals() == []
    learn.confirm_proposal("ACME Supply", "due_date", "tyson@x.com")
    assert learn.remove_learned("acme supply co", "due_date")
    assert learn.learned_policies() == {}
    assert not learn.remove_learned("Nobody", "due_date")
    r, _ = _check_and_save(_bill(id="b3", invoice="3"), f)
    assert r["comparison"]["findings"]                     # flagged again (at review)


def test_merged_policies_layer_learned_over_config():
    learn.confirm_proposal("Acme Supply", "invoice_date", "t@x", "ship date")
    learn.confirm_proposal("Brand New Co", "po", "t@x")
    merged = learn.merged_policies({
        "ACME Supply Co": {"quickpay": "take", "pct": 2, "notes": "deal"},
        "Other": {"skip": True}})
    acme = merged["ACME Supply Co"]
    assert acme["quickpay"] == "take" and acme["ignore_findings"] == ["invoice_date"]
    assert acme["notes"] == "deal; ship date"
    assert merged["Brand New Co"] == {"ignore_findings": ["po"]}
    assert merged["Other"] == {"skip": True}


def test_stats_count_right_and_wrong():
    f = Fakes()
    _check_and_save(_bill(id="b1"), f)
    _accept("b1", "fine")
    _check_and_save(_bill(id="b2", invoice="2"), f)
    store.record_disposition("b2", "fixed", "", "sarah@x.com")
    _check_and_save(_bill(id="b3", invoice="3"), f)
    store.record_disposition("b3", "fixed", "", "tyson@x.com")
    s = learn.stats()
    assert s["total"] == 3 and s["right"] == 2 and s["wrong"] == 1 and s["precision"] == 67
    assert s["by_who"][0] == ("sarah", 2)
    assert s["accepted_by_field"] == [("due date", 1)]
    assert s["recent"][0]["invoice"] == "3"


# --- the Claude judgment pass -----------------------------------------------

class _Verdict:
    def __init__(self, field, verdict, why):
        self.field, self.verdict, self.why = field, verdict, why


class FakeJudge:
    def __init__(self, verdicts):
        self.verdicts, self.calls = verdicts, []
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        parsed = type("P", (), {"verdicts": self.verdicts})()
        return type("R", (), {"parsed_output": parsed, "stop_reason": "end_turn",
                              "model": "judge", "usage": type("U", (), {"input_tokens": 500, "output_tokens": 40})()})()


def test_adjudicate_clears_and_downgrades_only_what_notes_cover():
    f = Fakes()
    learn.save_standing_notes("Acme: terms run from the ship date, so due dates look late")
    judge = FakeJudge([_Verdict("due_date", "clear", "the standing note says Acme runs from ship date"),
                       _Verdict("amount", "downgrade", "note doesn't mention totals")])
    r, _ = _check_and_save(_bill(amount="1400.00"), f, adjudicate_client=judge)
    c = r["comparison"]
    assert [x["field"] for x in c["suppressed"]] == ["due_date"]
    assert "ship date" in c["suppressed"][0]["why"]
    amt = c["findings"][0]
    assert amt["field"] == "amount" and amt["severity"] == "review" and amt["adjudicated"]
    assert c["status"] == "review" and r["severity"] == "review"
    assert c["adjudication"]["model"] == "judge" and len(c["adjudication"]["verdicts"]) == 2
    sent = json.loads(judge.calls[0]["messages"][0]["content"])
    assert "ship date" in sent["standing_notes"]
    assert {x["field"] for x in sent["findings"]} == {"amount", "due_date"}
    assert judge.calls[0]["output_config"] == {"effort": "low"}


def test_adjudicate_skips_without_context_or_without_serious_findings():
    f = Fakes()
    judge = FakeJudge([])
    r, _ = _check_and_save(_bill(id="b1"), f, adjudicate_client=judge)
    assert judge.calls == [] and "adjudication" not in r["comparison"]   # no notes, no lessons
    learn.save_standing_notes("something")
    r, _ = _check_and_save(_bill(id="b2", invoice="2", due_date="2026-08-31"), f,
                           adjudicate_client=judge)
    assert r["status"] == "match" and judge.calls == []                  # nothing to judge


def test_adjudicate_failure_never_sinks_the_bill():
    f = Fakes()
    learn.save_standing_notes("note")

    class Boom(FakeJudge):
        def parse(self, **kw):
            raise RuntimeError("api down")
    r, _ = _check_and_save(_bill(), f, adjudicate_client=Boom([]))
    assert r["status"] == "mismatch" and "api down" in r["comparison"]["adjudication"]["error"]


def test_adjudicate_cannot_raise_and_never_clears_duplicates():
    f = Fakes()
    learn.save_standing_notes("Acme duplicates are fine")
    judge = FakeJudge([_Verdict("duplicate", "clear", "note says fine"),
                       _Verdict("due_date", "keep", "nothing covers it")])
    r, _ = _check_and_save(_bill(), f, duplicates=["Acme #INV-1001 (paid)"], adjudicate_client=judge)
    fields = {x["field"]: x["severity"] for x in r["comparison"]["findings"]}
    assert fields["duplicate"] == "critical" and fields["due_date"] == "critical"
    assert r["comparison"]["suppressed"] == []
