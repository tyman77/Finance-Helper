"""Flask app: upload a vendor CSV, review/edit the proposed coding, approve/post.

Thin wrapper around the existing pipeline — every route just calls into
finance_helper.pipeline / categorize / destinations / validate. State (the
in-progress runs) lives in memory for the life of the process; this is a local,
single-user review tool, not a multi-tenant service.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime

from dotenv import find_dotenv, load_dotenv
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

from .. import config, destinations, pipeline, validate
from .. import review as proposal_review

# usecwd=True: search from wherever the server is actually started, not from
# this installed package's location (see cli.py's main() for the same fix).
load_dotenv(find_dotenv(usecwd=True))

RUNS: dict[str, dict] = {}

_CANDIDATES_RE = re.compile(
    r"(?:registry: candidate projects|calendar title codes) ([\d, ]+) — pick one"
)

# Human labels for the status pill — order matters for the filter toolbar.
STATUS_LABELS = {
    "auto": "Auto-coded",
    "pick": "Pick one",
    "review": "Confirm hint",
    "unknown": "Unknown traveler",
}


def _line_candidates(note: str | None) -> list[str]:
    """Pull the candidate project codes out of a "— pick one" note, so the UI
    can offer them as one-click buttons instead of making someone retype a
    number out of a wall of text."""
    m = _CANDIDATES_RE.search(note or "")
    if not m:
        return []
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _line_status(li, candidates: list[str]) -> str:
    """Classify a line for the status pill / filter toolbar, purely from
    signals already on the line — no change to the underlying coding logic."""
    note = li.note or ""
    if "not found in history" in note:
        return "unknown"
    if candidates:
        return "pick"
    if li.project:
        return "auto"
    return "review"


# --- Human-readable note formatting ----------------------------------------
#
# enrich.py builds `li.note` by concatenating several distinct facts with
# "; " for the CLI's plain-text output. The calendar-context piece uses that
# same separator internally for its own event list, so a naive split blurs
# "here's why" together with "here's the raw calendar data" and repeats
# information already shown elsewhere in the row (the account title, the
# candidate codes). This rewrites each known segment into a short phrase and
# routes calendar events into their own bucket, instead of parsing prose.

_CAL_CTX_RE = re.compile(r"(?:; )?calendar context — (.+?)(?=; registry: |$)")

_NOTE_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"^account hint '(\d+)--[^']*' \(used (\d+)% of trips\) — confirm project/COGS$"),
     lambda m: f"Usually {m.group(1)} ({m.group(2)}% of trips)"),
    (re.compile(r"^crew schedule: project (\S+) during stay -> 52200 COGS$"),
     lambda m: f"Crew schedule: project {m.group(1)}"),
    (re.compile(r"^calendar title code (\S+) -> 52200 COGS$"),
     lambda m: f"Calendar: project {m.group(1)}"),
    (re.compile(r"^registry: (.+) project (\S+) -> 52200 COGS$"),
     lambda m: f'Matched client "{m.group(1)}": project {m.group(2)}'),
    (re.compile(r"^(?:registry: candidate projects|calendar title codes) .+ — pick one$"),
     lambda m: "Multiple possible projects — pick one below"),
    (re.compile(r"^traveler not found in history — assign department & account$"),
     lambda m: "No historical match"),
    (re.compile(r"^matched by surname only$"),
     lambda m: "⚠ surname match only"),
    (re.compile(r"^low-confidence department$"),
     lambda m: "⚠ low-confidence department match"),
    (re.compile(r"^HE '(.+)' -> project (\S+) \(COGS Travel: Hotel\)$"),
     lambda m: f'"{m.group(1)}" → project {m.group(2)} (COGS Travel: Hotel)'),
    (re.compile(r"^HE '(.+)'$"),
     lambda m: f'"{m.group(1)}"'),
    (re.compile(r"^overhead (\S+)$"),
     lambda m: f"Overhead account: {m.group(1)}"),
    (re.compile(r"^UPS -> project (\S+) via (.+) \(COGS Shipping\)$"),
     lambda m: f"Matched via {m.group(2)}: project {m.group(1)}"),
    (re.compile(r"^UPS overhead ref '(.*)' -> (\S+)( \(needs department\))?$"),
     lambda m: f'Overhead ref "{m.group(1)}": {m.group(2)}'
               + (" — needs a department" if m.group(3) else "")),
]


def _extract_calendar_events(note: str) -> tuple[str, list[str]]:
    """Pull the "calendar context — E1; E2[...]" chunk out of a note string
    (it's embedded with the same "; " separator as everything else), leaving
    the rest of the note intact for normal segment-by-segment formatting."""
    m = _CAL_CTX_RE.search(note)
    if not m:
        return note, []
    raw = re.sub(r" — confirm client/account$", "", m.group(1))
    events = [e.strip() for e in raw.split("; ") if e.strip()]
    return _CAL_CTX_RE.sub("", note, count=1), events


def _format_note(note: str | None) -> dict:
    """{"summary": <first, most important fact>, "details": [<everything
    else>, ...]} — the template shows summary always, details behind a
    <details> toggle. Any segment that doesn't match a known pattern is
    still shown verbatim (as a safety net) rather than silently dropped."""
    if not note:
        return {"summary": "", "details": []}
    remaining, calendar_events = _extract_calendar_events(note)
    parts: list[str] = []
    for segment in remaining.split("; "):
        segment = segment.strip("; ").strip()
        if not segment:
            continue
        for pattern, transform in _NOTE_RULES:
            m = pattern.match(segment)
            if m:
                parts.append(transform(m))
                break
        else:
            parts.append(segment)
    details = parts[1:]
    if calendar_events:
        details.append("Calendar: " + "; ".join(calendar_events))
    return {"summary": parts[0] if parts else "", "details": details}


def _load_json_data(name: str) -> dict:
    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
    )
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _account_options() -> list[tuple[str, str]]:
    """(code, title) pairs — the full real chart if fetched, else the curated
    subset in config/accounts.yml, so the dropdown works either way."""
    merged: dict[str, str] = dict(config.accounts().get("accounts", {}))
    for code, info in validate.load_chart().items():
        merged[code] = info.get("title") or merged.get(code, code)
    return sorted(merged.items())


def _department_options() -> list[tuple[str, str]]:
    return sorted(config.accounts().get("departments", {}).items())


def _project_options() -> list[tuple[str, str]]:
    """(code, client) pairs for the project autocomplete — excludes anything
    confirmed archived in Sage. If data/sage_projects.json hasn't been fetched
    yet, nothing is filtered (same "no data -> don't block on it" convention
    as the rest of this tool)."""
    registry = _load_json_data("project_registry.json").get("registry", {})
    sage_projects = _load_json_data("sage_projects.json")
    options = []
    for code, info in registry.items():
        if sage_projects:
            proj = sage_projects.get(code)
            if proj is not None and (proj.get("status") or "").lower() != "active":
                continue  # archived in Sage — don't offer it
        options.append((code, info.get("client", "")))
    return sorted(options)


def create_app() -> Flask:
    app = Flask(__name__)

    password = os.environ.get("FINANCE_HELPER_WEB_PASSWORD")
    secret = os.environ.get("FINANCE_HELPER_SECRET")
    if password and not secret:
        raise RuntimeError(
            "FINANCE_HELPER_WEB_PASSWORD is set but FINANCE_HELPER_SECRET is not. "
            "A login without a real session secret can be trivially bypassed by "
            "forging the session cookie -- set FINANCE_HELPER_SECRET to a random "
            "value (e.g. `python3 -c \"import secrets; print(secrets.token_hex(32))\"`) "
            "before running with a password configured."
        )
    app.secret_key = secret or "dev-local-only-not-a-real-secret"

    if not password:
        print(
            "WARNING: FINANCE_HELPER_WEB_PASSWORD is not set -- this server is "
            "running with NO LOGIN. Fine for localhost-only use; do not expose "
            "this beyond your own machine without setting a password.",
            file=sys.stderr,
        )

    @app.before_request
    def _require_login():
        if not password:
            return None
        if request.endpoint in ("login", "static"):
            return None
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if request.form.get("password") == password:
                session.clear()
                session["authed"] = True
                session.permanent = True
                return redirect(request.args.get("next") or url_for("index"))
            return render_template("login.html", error="Wrong password."), 401
        return render_template("login.html", error=None)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        recent = sorted(RUNS.items(), key=lambda kv: kv[1]["created"], reverse=True)
        return render_template("index.html", sources=config.sources(), recent=recent)

    @app.post("/upload")
    def upload():
        source = request.form.get("source", "")
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a CSV file to upload.")
            return redirect(url_for("index"))
        try:
            config.source_config(source)
        except KeyError as exc:
            flash(str(exc))
            return redirect(url_for("index"))

        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        try:
            doc = pipeline.process(source, tmp_path)
        except Exception as exc:  # surface a friendly error instead of a 500
            flash(f"Could not process {file.filename}: {exc}")
            return redirect(url_for("index"))
        finally:
            os.unlink(tmp_path)

        run_id = uuid.uuid4().hex[:12]
        RUNS[run_id] = {
            "doc": doc,
            "source": source,
            "filename": file.filename,
            "created": datetime.now(),
            "posted": None,
        }
        return redirect(url_for("review_page", run_id=run_id))

    def _get_run(run_id):
        run = RUNS.get(run_id)
        if not run:
            flash("That run isn't available (the server may have restarted since).")
        return run

    @app.get("/review/<run_id>")
    def review_page(run_id):
        run = _get_run(run_id)
        if not run:
            return redirect(url_for("index"))
        doc = run["doc"]
        issues_by_line: dict[int, list[str]] = {}
        for issue in validate.validate_lines(doc):
            issues_by_line.setdefault(issue["index"], []).append(issue["message"])

        candidates_by_line = {i: _line_candidates(li.note) for i, li in enumerate(doc.line_items)}
        candidates_by_line = {i: c for i, c in candidates_by_line.items() if c}
        statuses = [_line_status(li, candidates_by_line.get(i, [])) for i, li in enumerate(doc.line_items)]
        status_counts = Counter(statuses)
        notes = [_format_note(li.note) for li in doc.line_items]

        account_options = _account_options()
        account_titles = dict(account_options)

        return render_template(
            "review.html",
            run_id=run_id,
            run=run,
            doc=doc,
            issues_by_line=issues_by_line,
            total_issues=sum(len(v) for v in issues_by_line.values()),
            account_options=account_options,
            account_titles=account_titles,
            department_options=_department_options(),
            project_options=_project_options(),
            candidates_by_line=candidates_by_line,
            statuses=statuses,
            status_counts=status_counts,
            status_labels=STATUS_LABELS,
            notes=notes,
        )

    @app.post("/review/<run_id>/update")
    def update_line(run_id):
        run = _get_run(run_id)
        if not run:
            return redirect(url_for("index"))
        doc = run["doc"]
        for i, li in enumerate(doc.line_items):
            li.gl_account = (request.form.get(f"gl_account_{i}") or "").strip() or None
            li.department = (request.form.get(f"department_{i}") or "").strip() or None
            li.project = (request.form.get(f"project_{i}") or "").strip() or None
            li.needs_review = request.form.get(f"needs_review_{i}") == "on"
        run["posted"] = None  # edits invalidate a prior post attempt's relevance
        flash("Changes saved.")
        return redirect(url_for("review_page", run_id=run_id))

    @app.post("/review/<run_id>/approve")
    def approve(run_id):
        run = _get_run(run_id)
        if not run:
            return redirect(url_for("index"))
        doc = run["doc"]
        payload = destinations.build_payload(doc)
        proposal_review.save_proposal(doc, payload)
        try:
            result = destinations.post(doc, payload)
            run["posted"] = {"ok": True, "detail": str(result)}
        except (RuntimeError, NotImplementedError) as exc:
            run["posted"] = {"ok": False, "detail": str(exc)}
        return redirect(url_for("review_page", run_id=run_id))

    @app.get("/review/<run_id>/download")
    def download(run_id):
        run = _get_run(run_id)
        if not run:
            return redirect(url_for("index"))
        doc = run["doc"]
        payload = destinations.build_payload(doc)
        body = json.dumps(
            {"source": doc.source, "destination": doc.destination, "payload": payload}, indent=2
        )
        filename = f"{doc.source}_{doc.document_id}".replace("/", "_") + ".json"
        return Response(
            body, mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app
