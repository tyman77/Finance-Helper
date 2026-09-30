"""Engine (Hotel Engine) Reporting API: run a saved report, download its CSV.

Documented (Engine company settings -> Integrations -> Reporting API docs):
  base URL      https://api.engine.com
  auth          Authorization: Bearer <ENGINE_API_KEY>
  generate      POST /api/reporting/v1/saved_reports/{id}/generate  -> report id
  download      GET  /api/reporting/v1/reports/{id}/download
                  200 CSV body · 202 still running (poll) · 204 no rows · 500 failed
The list endpoint path and the JSON field names of the list/generate
responses weren't in the excerpt we had, so both are parsed defensively
and any shape we can't read raises with the raw body — a mismatch is a
one-line fix here, not a mystery. Paths are env-overridable.

Which saved report to run: ENGINE_SAVED_REPORT (its id, or part of its
name); unset, a report with "scout" in its name, or the only one there is.
"""

from __future__ import annotations

import os
import time

_BASE = os.environ.get("ENGINE_API_URL", "https://api.engine.com").rstrip("/")
_PREFIX = "/api/reporting/v1"


def credentials_present() -> bool:
    return bool((os.environ.get("ENGINE_API_KEY") or "").strip())


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['ENGINE_API_KEY'].strip()}",
            "Content-Type": "application/json"}


def _request(method: str, path: str, **kw):
    import requests
    try:
        return requests.request(method, f"{_BASE}{path}", headers=_headers(),
                                timeout=60, **kw)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Engine request failed: {type(exc).__name__}: {exc}") from exc


def _fail(what: str, resp) -> RuntimeError:
    hint = ""
    if resp.status_code == 401:
        hint = " — ENGINE_API_KEY is missing, wrong, or revoked."
    elif resp.status_code == 403:
        hint = " — the key lacks the scope this call needs."
    return RuntimeError(f"Engine {what} failed: HTTP {resp.status_code}{hint}\n"
                        f"{(resp.text or '')[:600]}")


def _items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("saved_reports", "data", "reports", "items", "results"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def list_saved_reports() -> list[dict]:
    path = os.environ.get("ENGINE_SAVED_REPORTS_PATH") or f"{_PREFIX}/saved_reports"
    resp = _request("GET", path)
    if resp.status_code != 200:
        raise _fail("list saved reports", resp)
    rows = []
    for r in _items(resp.json()):
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("saved_report_id") or r.get("uuid")
        if rid:
            rows.append({"id": str(rid),
                         "name": str(r.get("name") or r.get("title") or r.get("report_name") or "")})
    if not rows:
        raise RuntimeError("Engine returned no saved reports we could read. Raw response:\n"
                           + (resp.text or "")[:600])
    return rows


def pick_saved_report(reports: list[dict], wanted: str | None = None) -> dict:
    wanted = (wanted if wanted is not None else os.environ.get("ENGINE_SAVED_REPORT") or "").strip()
    if wanted:
        for r in reports:
            if r["id"] == wanted:
                return r
        hits = [r for r in reports if wanted.lower() in r["name"].lower()]
        if len(hits) == 1:
            return hits[0]
    else:
        hits = [r for r in reports if "scout" in r["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        if len(reports) == 1:
            return reports[0]
    names = "; ".join(f"{r['name'] or '(unnamed)'} [{r['id']}]" for r in reports)
    raise RuntimeError(
        "Couldn't tell which Engine saved report to run"
        + (f" (ENGINE_SAVED_REPORT={wanted!r} matched none or several)" if wanted else "")
        + f". Saved reports: {names}. Name one with 'Scout' in it, or set "
          "ENGINE_SAVED_REPORT in Railway to its id or name.")


def _filters() -> dict:
    """Engine requires a date window and basis on every run (422
    invalid_filters otherwise). Stays starting from ENGINE_REPORT_DAYS back
    (default 180 — covers the statements still being coded) through 60 days
    ahead, by start of booking so a stay lands on the dates it happened."""
    from datetime import date, timedelta
    back = int(os.environ.get("ENGINE_REPORT_DAYS") or 180)
    today = date.today()
    return {"filters": {
        "date_range_start": (today - timedelta(days=back)).isoformat(),
        "date_range_end": (today + timedelta(days=60)).isoformat(),
        "date_based_on": os.environ.get("ENGINE_DATE_BASED_ON") or "start_of_booking",
    }}


def generate(saved_id: str) -> str:
    resp = _request("POST", f"{_PREFIX}/saved_reports/{saved_id}/generate",
                    json=_filters())
    if resp.status_code not in (200, 201, 202):
        raise _fail("generate report", resp)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    cands = [body] if isinstance(body, dict) else []
    for key in ("report", "data"):
        if cands and isinstance(cands[0].get(key), dict):
            cands.append(cands[0][key])
    for c in cands:
        rid = c.get("id") or c.get("report_id")
        if rid:
            return str(rid)
    raise RuntimeError("Engine accepted the generate request but the report id "
                       "wasn't where expected. Raw response:\n" + (resp.text or "")[:600])


def download(report_id: str, max_wait: float = 240.0, poll: float = 3.0,
             sleep=time.sleep) -> str:
    """The report CSV text ("" when it finished with no rows). Polls while
    Engine answers 202; 500 or anything unexpected raises."""
    waited = 0.0
    while True:
        resp = _request("GET", f"{_PREFIX}/reports/{report_id}/download")
        if resp.status_code == 200:
            return resp.content.decode("utf-8-sig", errors="replace")
        if resp.status_code == 204:
            return ""
        if resp.status_code != 202:
            raise _fail("download report", resp)
        if waited >= max_wait:
            raise RuntimeError(f"Engine report {report_id} still wasn't ready after "
                               f"{int(max_wait)}s — try again in a few minutes.")
        sleep(poll)
        waited += poll


def fetch_report_rows() -> tuple[dict, list[dict]]:
    """Run the chosen saved report end to end -> (report info, CSV rows)."""
    import csv
    import io

    if not credentials_present():
        raise RuntimeError("ENGINE_API_KEY isn't set — generate a key in Engine "
                           "(Company settings -> Integrations -> Reporting API) and "
                           "add it in Railway.")
    chosen = pick_saved_report(list_saved_reports())
    text = download(generate(chosen["id"]))
    rows = list(csv.DictReader(io.StringIO(text))) if text.strip() else []
    return chosen, rows
