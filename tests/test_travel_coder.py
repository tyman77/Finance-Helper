"""The Claude travel coder: evidence assembly, guardrails, and the enrich
hook. All API traffic is faked — no live calls."""

from datetime import date
from decimal import Decimal

import pytest

from finance_helper import enrich, sources, travel_coder
from finance_helper.models import LineItem, SourceDocument
from finance_helper.travel_coder import LineCoding, TravelCoding


def _doc():
    return SourceDocument(
        source="united", destination="sage", vendor="United Airlines",
        document_id="T-1", currency="USD", line_items=[
            LineItem(description="DILL/MASON DEN MCI DEN", amount=Decimal("440.81"),
                     person="Mason Dill", department="60",
                     raw={"Routing (Origin To To To To )": "DEN MCI DEN",
                          "Departure Date": "08/07/2026"}),
            LineItem(description="LLC       /INFLIGHT WI-FI STR DEN OMA",
                     amount=Decimal("8.00"),
                     note="inflight wifi purchase — auto-accepted, no traveler"),
            LineItem(description="YOCUM/CARSON ICT DEN", amount=Decimal("188.40"),
                     person="Carson Yocum", department="60",
                     raw={"Routing (Origin To To To To )": "ICT DEN",
                          "Departure Date": "08/07/2026"}),
        ])


class FakeResp:
    stop_reason = "end_turn"

    def __init__(self, parsed):
        self.parsed_output = parsed


class FakeClient:
    def __init__(self, parsed):
        self._parsed = parsed
        self.kwargs = None

        class _M:
            def parse(inner, **kw):
                self.kwargs = kw
                return FakeResp(self._parsed)
        self.messages = _M()


REGISTRY = {"registry": {
    "4960": {"client": "Grace Church, KS - North OP Reno"},
    "5232": {"client": "Journey church - Auditorium expansion"},
}}


def test_apply_codes_confident_lines_and_guards_unknown_projects():
    doc = _doc()
    parsed = TravelCoding(lines=[
        LineCoding(line=0, gl_account="52200", department="60", project="4960",
                   candidates="", confidence="high",
                   reason="MCI trip during Grace Church schedule week"),
        LineCoding(line=2, gl_account="52200", department="60", project="9999",
                   candidates="", confidence="high", reason="made-up code"),
    ])
    client = FakeClient(parsed)
    decided = travel_coder.apply(doc, registry=REGISTRY, active_projects=None,
                                 client=client)
    assert decided == 1
    assert doc.line_items[0].project == "4960"
    assert doc.line_items[0].gl_account == "52200"
    assert doc.line_items[0].note == "Claude (high): MCI trip during Grace Church schedule week"
    # 9999 isn't an active project: never accepted, demoted to a suggestion.
    assert doc.line_items[2].project is None
    assert "not an active project" in doc.line_items[2].note

    # The wifi line never reaches the model; the system prompt is cached.
    import json
    payload = json.loads(client.kwargs["messages"][0]["content"])
    assert [l["line"] for l in payload["lines"]] == [0, 2]
    assert client.kwargs["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_apply_low_confidence_keeps_rules_coding_and_offers_chips():
    doc = _doc()
    doc.line_items[2].project = "5232"       # rules-engine result stays
    doc.line_items[2].note = "crew schedule: project 5232 during stay -> 52200 COGS"
    parsed = TravelCoding(lines=[
        LineCoding(line=2, gl_account="", department="", project="",
                   candidates="5232, 4960, 8888", confidence="low",
                   reason="schedule and hotel disagree")])
    travel_coder.apply(doc, registry=REGISTRY, active_projects=None,
                       client=FakeClient(parsed))
    li = doc.line_items[2]
    assert li.project == "5232"
    assert "Claude (low): schedule and hotel disagree" in li.note
    # Chips: only known codes survive the candidate list.
    assert "registry: candidate projects 5232, 4960 — pick one" in li.note


def test_evidence_includes_schedule_hotels_and_history():
    doc = _doc()
    schedule = {"Mason Dill": {"2026-08-08": "4960", "2026-08-09": "4960",
                               "2026-07-01": "1111"}}
    hotels = [{"start": "2026-08-07", "end": "2026-08-10", "project": "4960",
               "city": "Overland Park", "guests": ["Mason Dill"]}]
    payload, covered = travel_coder._evidence(
        doc, schedule, hotels, [], REGISTRY, None,
        {"Mason Dill": ["4960", "3495"]})
    dill = next(l for l in payload["lines"] if l["line"] == 0)
    assert dill["schedule"] == {"2026-08-08": "4960", "2026-08-09": "4960"}
    assert dill["hotel_stays"][0]["project"] == "4960"
    assert dill["historical_projects"] == ["4960", "3495"]
    assert {p["code"] for p in payload["projects"]} == {"4960", "5232"}


def test_enabled_gates_on_key_and_kill_switch(monkeypatch):
    assert not travel_coder.enabled()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert travel_coder.enabled()
    monkeypatch.setenv("FINANCE_HELPER_LLM_CODER", "0")
    assert not travel_coder.enabled()


def test_enrich_runs_claude_pass_and_survives_failure(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = {}

    def fake_apply(doc, **kw):
        calls["history"] = kw.get("history")
        return 5
    monkeypatch.setattr(travel_coder, "apply", fake_apply)
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install",
                         "department_confidence": 1.0, "account_hint": "52200--COGS",
                         "account_confidence": 0.9, "projects": ["4960"], "n": 10}}
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={},
                         roster={}, registry={}, active_projects=None,
                         hotel_index=[], ramp_index=[], timecard_index={})
    assert calls["history"] == {"John Doe": ["4960"]}

    # An API failure leaves the rules-engine coding with a visible note.
    def boom(doc, **kw):
        raise RuntimeError("simulated outage")
    monkeypatch.setattr(travel_coder, "apply", boom)
    doc2 = sources.load("united", "samples/united_sample.csv")
    doc2 = enrich.enrich_united(doc2, tmap, schedule_index={}, calendar_index={},
                                roster={}, registry={}, active_projects=None,
                                hotel_index=[], ramp_index=[], timecard_index={})
    joined = "; ".join(li.note or "" for li in doc2.line_items)
    assert "Claude coder unavailable" in joined
    assert "rules-engine coding kept" in joined
