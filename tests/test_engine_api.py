"""Engine Reporting API client — every HTTP call faked."""

import pytest

from finance_helper import engine_api


class R:
    def __init__(self, status, json_body=None, text="", content=b""):
        self.status_code = status
        self._json = json_body
        self.text = text
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def fake(monkeypatch, responses):
    """responses: list of (method, path-substring, R) consumed in order."""
    import requests
    calls = []

    def request(method, url, headers=None, timeout=0, **kw):
        calls.append((method, url, headers))
        m, frag, resp = responses.pop(0)
        assert method == m and frag in url, (method, url)
        return resp
    monkeypatch.setattr(requests, "request", request)
    return calls


CSV = (b"Invoice Number,Guest Name,Check In,Hotel\r\n"
       b"260901001,Justin Hitch,2026-09-01,Hampton Inn Cedar Rapids\r\n"
       b"260901002,\"Mason Dill; Zach Kay\",2026-09-03,Courtyard Overland Park\r\n")


def test_full_run_lists_generates_polls_and_downloads(monkeypatch):
    monkeypatch.setenv("ENGINE_API_KEY", "k-123")
    calls = fake(monkeypatch, [
        ("GET", "/api/reporting/v1/saved_reports",
         R(200, {"data": [{"id": 7, "name": "Monthly spend"},
                          {"id": 9, "name": "Scout feed"}]})),
        ("POST", "/api/reporting/v1/saved_reports/9/generate", R(202, {"report": {"id": "r55"}})),
        ("GET", "/api/reporting/v1/reports/r55/download", R(202, {"report": {}})),
        ("GET", "/api/reporting/v1/reports/r55/download", R(200, content=CSV)),
    ])
    monkeypatch.setattr(engine_api.time, "sleep", lambda s: None)
    chosen, rows = engine_api.fetch_report_rows()
    assert chosen == {"id": "9", "name": "Scout feed"}
    assert rows[1]["Guest Name"] == "Mason Dill; Zach Kay"
    assert calls[0][1] == "https://api.engine.com/api/reporting/v1/saved_reports"
    assert calls[0][2]["Authorization"] == "Bearer k-123"


def test_no_rows_and_failures(monkeypatch):
    monkeypatch.setenv("ENGINE_API_KEY", "k")
    fake(monkeypatch, [("GET", "/reports/r1/download", R(204))])
    assert engine_api.download("r1") == ""
    fake(monkeypatch, [("GET", "/reports/r2/download", R(500, text='{"error":"boom"}'))])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        engine_api.download("r2")
    fake(monkeypatch, [("GET", "/saved_reports", R(401, text="nope"))])
    with pytest.raises(RuntimeError, match="missing, wrong, or revoked"):
        engine_api.list_saved_reports()


def test_pick_saved_report_rules(monkeypatch):
    monkeypatch.delenv("ENGINE_SAVED_REPORT", raising=False)
    reps = [{"id": "1", "name": "Spend"}, {"id": "2", "name": "Scout Feed"}]
    assert engine_api.pick_saved_report(reps)["id"] == "2"
    assert engine_api.pick_saved_report(reps, "spend")["id"] == "1"
    assert engine_api.pick_saved_report(reps, "1")["id"] == "1"
    assert engine_api.pick_saved_report([{"id": "5", "name": "Only"}])["id"] == "5"
    with pytest.raises(RuntimeError, match="Saved reports: Spend"):
        engine_api.pick_saved_report([{"id": "1", "name": "Spend"},
                                      {"id": "3", "name": "Other"}])


def test_generate_reads_several_id_shapes(monkeypatch):
    monkeypatch.setenv("ENGINE_API_KEY", "k")
    for body in ({"id": "a"}, {"report_id": "a"}, {"report": {"id": "a"}}, {"data": {"id": "a"}}):
        fake(monkeypatch, [("POST", "/saved_reports/1/generate", R(200, body))])
        assert engine_api.generate("1") == "a"
    fake(monkeypatch, [("POST", "/saved_reports/1/generate", R(200, {"weird": 1}, text='{"weird":1}'))])
    with pytest.raises(RuntimeError, match="Raw response"):
        engine_api.generate("1")


def test_missing_key_is_a_plain_error():
    with pytest.raises(RuntimeError, match="ENGINE_API_KEY isn't set"):
        engine_api.fetch_report_rows()


def test_generate_sends_required_filters(monkeypatch):
    """Engine 422s without filters.date_range_start/end and date_based_on."""
    import requests
    monkeypatch.setenv("ENGINE_API_KEY", "k")
    sent = {}

    def request(method, url, headers=None, timeout=0, **kw):
        sent.update(kw)
        return R(200, {"id": "r1"})
    monkeypatch.setattr(requests, "request", request)
    assert engine_api.generate("9") == "r1"
    f = sent["json"]["filters"]
    assert f["date_based_on"] == "start_of_booking"
    assert f["date_range_start"] < f["date_range_end"]
    assert len(f["date_range_start"]) == 10          # YYYY-MM-DD
