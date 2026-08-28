"""Flask app: upload a vendor CSV, review/edit the proposed coding, approve/post.

Thin wrapper around the existing pipeline — every route just calls into
finance_helper.pipeline / categorize / destinations / validate. State (the
in-progress runs) lives in memory for the life of the process; this is a local,
single-user review tool, not a multi-tenant service.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode

import requests
from dotenv import find_dotenv, load_dotenv
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import config, destinations, insights, pipeline, validate
from .. import review as proposal_review
from . import store
from .admin import admin_bp
from .cashproof import cashproof_bp

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# usecwd=True: search from wherever the server is actually started, not from
# this installed package's location (see cli.py's main() for the same fix).
load_dotenv(find_dotenv(usecwd=True))

RUNS: dict[str, dict] = {}

_CANDIDATES_RE = re.compile(
    r"(?:registry: candidate projects|calendar title codes|past projects"
    r"|hotel-week projects|hotel-stay projects|ramp-memo projects)"
    r" ([\d, ]+) — pick one"
)

# Human labels for the status pill — order matters for the filter toolbar.
STATUS_LABELS = {
    "auto": "Auto-coded",
    "pick": "Pick one",
    "review": "Confirm hint",
    "unknown": "Unknown traveler",
    "wifi": "Wi-Fi / no traveler",
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
    if "inflight wifi" in note:
        return "wifi"
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
    (re.compile(r"^past projects .+ — pick one$"),
     lambda m: "Past projects (from history) — pick one below"),
    (re.compile(r"^hotel-week projects .+ — pick one$"),
     lambda m: "Projects with a hotel booked that week — pick one below"),
    (re.compile(r"^hotel-stay projects .+ — pick one$"),
     lambda m: "This traveler's hotel stays that week — pick one below"),
    (re.compile(r"^hotel booking that week -> project (\S+) \(confirm\)$"),
     lambda m: f"Hotel booked that week → project {m.group(1)} (confirm)"),
    (re.compile(r"^hotel stay names (.+) that week -> project (\S+) \+ 52200 COGS \(confirm\)$"),
     lambda m: f"{m.group(1)}'s hotel stay that week → project {m.group(2)} (confirm)"),
    (re.compile(r"^ramp-memo projects .+ — pick one$"),
     lambda m: "Projects from Ramp per-diem memos — pick one below"),
    (re.compile(r"^ramp per-diem memo -> project (\S+) \+ 52200 COGS \(confirm\)$"),
     lambda m: f"Ramp per-diem memo → project {m.group(1)} (confirm)"),
    (re.compile(r"^timecards: (.+) logged hours to project (\S+) during the stay -> 52200 COGS$"),
     lambda m: f"{m.group(1)} logged hours to project {m.group(2)} that week (timecards)"),
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
    # Railway (and most PaaS hosts) terminate TLS at a proxy and forward over
    # plain HTTP -- without this, url_for(..., _external=True) below builds
    # http:// URLs and Google rejects the OAuth redirect_uri as insecure.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    google_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    allowed_domain = os.environ.get("GOOGLE_OAUTH_ALLOWED_DOMAIN")
    google_login_enabled = bool(google_client_id and google_client_secret)

    secret = os.environ.get("FINANCE_HELPER_SECRET")
    if google_login_enabled and not secret:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID/SECRET are set but FINANCE_HELPER_SECRET is not. "
            "A login without a real session secret can be trivially bypassed by "
            "forging the session cookie -- set FINANCE_HELPER_SECRET to a random "
            "value (e.g. `python3 -c \"import secrets; print(secrets.token_hex(32))\"`) "
            "before running with Google login configured."
        )
    if google_login_enabled and not allowed_domain:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID/SECRET are set but GOOGLE_OAUTH_ALLOWED_DOMAIN is "
            "not -- without it, ANY Google account could log in, not just your company's. "
            "Set GOOGLE_OAUTH_ALLOWED_DOMAIN to your Workspace domain (e.g. "
            "summitintegrated.com)."
        )
    app.secret_key = secret or "dev-local-only-not-a-real-secret"
    app.config["SESSION_COOKIE_SECURE"] = google_login_enabled

    if not google_login_enabled:
        print(
            "WARNING: GOOGLE_OAUTH_CLIENT_ID/SECRET are not set -- this server is "
            "running with NO LOGIN. Fine for localhost-only use; do not expose "
            "this beyond your own machine without setting up Google login.",
            file=sys.stderr,
        )

    @app.before_request
    def _require_login():
        if not google_login_enabled:
            return None
        if request.endpoint in ("login", "auth_google", "auth_google_callback", "static"):
            return None
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.get("/login")
    def login():
        if not google_login_enabled:
            return redirect(url_for("index"))
        return render_template(
            "login.html", allowed_domain=allowed_domain, next=request.args.get("next")
        )

    @app.get("/auth/google")
    def auth_google():
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        session["oauth_next"] = request.args.get("next") or url_for("index")
        params = {
            "client_id": google_client_id,
            "redirect_uri": url_for("auth_google_callback", _external=True),
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "prompt": "select_account",
            "hd": allowed_domain,
        }
        return redirect(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")

    @app.get("/auth/google/callback")
    def auth_google_callback():
        if not request.args.get("state") or request.args.get("state") != session.pop(
            "oauth_state", None
        ):
            flash("Login failed (state mismatch) -- please try again.")
            return redirect(url_for("login"))
        code = request.args.get("code")
        if not code:
            flash("Google sign-in was cancelled or failed.")
            return redirect(url_for("login"))

        try:
            token_resp = requests.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "client_id": google_client_id,
                    "client_secret": google_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": url_for("auth_google_callback", _external=True),
                },
                timeout=10,
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]
            userinfo_resp = requests.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            userinfo_resp.raise_for_status()
            userinfo = userinfo_resp.json()
        except requests.exceptions.RequestException as exc:
            flash(f"Google sign-in failed: {exc}")
            return redirect(url_for("login"))

        email = userinfo.get("email") or ""
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if not userinfo.get("email_verified") or domain != allowed_domain.lower():
            flash(f"{email or 'That Google account'} isn't authorized for this tool.")
            return redirect(url_for("login"))

        next_path = session.pop("oauth_next", None)
        session.clear()
        session["authed"] = True
        session["email"] = email
        session.permanent = True
        return redirect(next_path or url_for("index"))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    app.register_blueprint(admin_bp)
    app.register_blueprint(cashproof_bp)

    @app.get("/")
    def index():
        # Pull in any saved reviews from disk (survives restarts / other workers)
        # without clobbering a run already live in memory.
        for rid, saved in store.load_all_runs().items():
            RUNS.setdefault(rid, saved)
        recent = sorted(RUNS.items(), key=lambda kv: kv[1]["created"], reverse=True)
        return render_template("index.html", sources=config.sources(), recent=recent)

    @app.get("/d/<domain>")
    def domain_page(domain):
        if domain not in insights.DOMAINS:
            return redirect(url_for("index"))
        for rid, saved in store.load_all_runs().items():
            RUNS.setdefault(rid, saved)
        data = insights.build_domain(list(RUNS.items()), domain)
        registry = _load_json_data("project_registry.json").get("registry", {})

        def plabel(code):
            client = (registry.get(code) or {}).get("client")
            return f"{code} — {client}" if client else code

        detail = data.get("detail")
        detail_charts = {}
        if detail and detail.get("kind") == "hotels":
            detail_charts = {
                "components": insights.hbar_chart(detail["components"], label_w=150),
                "hotels": insights.hbar_chart([(n, v) for n, v, _ in detail["hotels"][:8]]),
                "cities": insights.hbar_chart([(n, v) for n, v, _ in detail["cities"][:8]]),
                "departments": insights.hbar_chart(detail["departments"][:8], label_w=190),
            }
        elif detail and detail.get("kind") == "flights":
            with_lead = [p for p in detail["people"] if p["avg_lead"] is not None]
            with_fare = [p for p in detail["people"] if p["avg_fare"] is not None]
            detail_charts = {
                "lead": insights.hbar_chart(
                    [(p["person"], p["avg_lead"]) for p in
                     sorted(with_lead, key=lambda p: p["avg_lead"])[:10]], label_w=190),
                "fare": insights.hbar_chart(
                    [(p["person"], p["avg_fare"]) for p in
                     sorted(with_fare, key=lambda p: -p["avg_fare"])[:10]], label_w=190),
            }
        # For hotels, statement guest columns beat li.person (unset for HE).
        people = data["people"][:8]
        if detail and detail.get("travelers"):
            people = [(n, s, c) for n, s, c in detail["travelers"][:8]]
        return render_template(
            "domain.html",
            d=data,
            domain=domain,
            monthly=insights.monthly_chart(data["months"], data["by_month_group"], [data["group"]]),
            projects_chart=insights.hbar_chart([(plabel(c), v) for c, v in data["projects"][:8]]),
            people_chart=insights.hbar_chart([(n, a) for n, a, _ in people]),
            detail=detail,
            dc=detail_charts,
        )

    @app.post("/d/hotels/guests")
    def hotels_guests_upload():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose the Hotel Engine trips/guests CSV first.")
            return redirect(url_for("domain_page", domain="hotels"))
        import csv as _csv
        import io as _io
        try:
            rows = list(_csv.DictReader(_io.StringIO(file.read().decode("utf-8-sig"))))
            index = insights.build_guest_index(rows)
            total = insights.save_guest_index(index)
            flash(f"Guest list loaded: {len(index)} bookings from this file ({total} total). "
                  "Travelers and occupancy now show on matching stays.")
        except Exception as exc:
            flash(f"Could not read that guest list: {exc}")
        return redirect(url_for("domain_page", domain="hotels"))

    @app.get("/insights")
    def insights_page():
        for rid, saved in store.load_all_runs().items():
            RUNS.setdefault(rid, saved)
        data = insights.build(RUNS.values())

        # Project labels: "4804 — Echo Church" where the registry knows the client.
        registry = _load_json_data("project_registry.json").get("registry", {})

        def plabel(code):
            client = (registry.get(code) or {}).get("client")
            return f"{code} — {client}" if client else code

        top_projects = [(plabel(c), v) for c, v in data["projects"][:10]]
        top_people = data["people"][:10]
        monthly = insights.monthly_chart(data["months"], data["by_month_group"], insights.GROUPS)
        return render_template(
            "insights.html",
            d=data,
            groups=insights.GROUPS,
            monthly=monthly,
            projects_chart=insights.hbar_chart(top_projects),
            people_chart=insights.hbar_chart([(n, a) for n, a, _ in top_people]),
            people_rows=data["people"][:25],
        )

    def _refresh_hotel_index():
        """Rebuild data/hotel_project_index.json from the saved Hotel Engine
        statements, guests included — so United coding always cross-references
        the freshest stays without a separate admin upload. Best-effort: a
        failure here must never block statement processing."""
        try:
            for rid, saved in store.load_all_runs().items():
                RUNS.setdefault(rid, saved)
            docs = [r["doc"] for r in RUNS.values() if r["doc"].source == "hotel_engine"]
            if not docs:
                return
            from .. import insights as _ins
            from ..enrich import _HE_DEPARTMENTS
            detail = _ins.hotels_detail(docs)
            records = []
            for b in detail["bookings"]:
                m = re.search(r"\b(\d{3,5})\b", str(b.get("project") or ""))
                start = _ins._parse_mdy(b["start"])
                if start is None:
                    continue
                # Uncoded stays stay in the index with project=None: they can't
                # tag a flight, but they make "stay found, no project number"
                # diagnosable instead of looking like no stay at all.
                end = _ins._parse_mdy(b["end"]) or start
                dept_id = None
                dn = (b.get("department") or "").lower()
                for key, did in _HE_DEPARTMENTS.items():
                    if key in dn:
                        dept_id = did
                        break
                records.append({
                    "start": start.isoformat(), "end": end.isoformat(),
                    "project": m.group(1) if m else None, "department": dept_id,
                    "city": b.get("city") or "", "guests": b.get("guests") or [],
                    "hotel": b.get("hotel") or "",
                })
            data_dir = os.environ.get(
                "FINANCE_HELPER_DATA",
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
            os.makedirs(data_dir, exist_ok=True)
            with open(os.path.join(data_dir, "hotel_project_index.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(records, fh, indent=2)
        except Exception:
            pass

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

        if source == "united":
            _refresh_hotel_index()      # freshest stays before coding flights
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        try:
            doc = pipeline.process(source, tmp_path)
            with open(tmp_path, "rb") as fh:
                csv_b64 = base64.b64encode(fh.read()).decode("ascii")
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
            "csv_b64": csv_b64,  # kept so the coding can be re-run with newer data
        }
        store.save_run(run_id, RUNS[run_id])
        if source == "hotel_engine":
            _refresh_hotel_index()      # new stays feed future flight coding
        return redirect(url_for("review_page", run_id=run_id))

    @app.post("/review/<run_id>/rerun")
    def rerun(run_id):
        run = _get_run(run_id)
        if not run:
            return redirect(url_for("index"))
        csv_b64 = run.get("csv_b64")
        if not csv_b64:
            flash("This review was saved before re-run existed — re-upload the file "
                  "once to enable re-running.")
            return redirect(url_for("review_page", run_id=run_id))
        if run["source"] == "united":
            _refresh_hotel_index()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            tmp.write(base64.b64decode(csv_b64))
            tmp_path = tmp.name
        try:
            run["doc"] = pipeline.process(run["source"], tmp_path)
            run["posted"] = None
            store.save_run(run_id, run)
            flash("Re-coded with the latest data.")
        except Exception as exc:
            flash(f"Could not re-run: {exc}")
        finally:
            os.unlink(tmp_path)
        return redirect(url_for("review_page", run_id=run_id))

    def _get_run(run_id):
        run = RUNS.get(run_id)
        if not run:
            run = store.load_run(run_id)  # may have been saved by another worker / prior boot
            if run:
                RUNS[run_id] = run
        if not run:
            flash("That review isn't available — it may have been deleted.")
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

        # Traveler autocomplete: everyone the map knows plus anyone already on
        # this document (so a name typed once suggests itself on other lines).
        from .. import enrich as _enrich
        data_dir = os.environ.get(
            "FINANCE_HELPER_DATA",
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
        tmap = _enrich.load_traveler_map(os.path.join(data_dir, "united_travelers.yml"))
        travelers = sorted(
            {v.get("person") for v in tmap.values() if isinstance(v, dict) and v.get("person")}
            | {li.person for li in doc.line_items if li.person})

        return render_template(
            "review.html",
            travelers=travelers,
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
            if f"person_{i}" in request.form:
                li.person = (request.form.get(f"person_{i}") or "").strip() or None
            li.needs_review = request.form.get(f"needs_review_{i}") == "on"
        run["posted"] = None  # edits invalidate a prior post attempt's relevance
        store.save_run(run_id, run)
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
        store.save_run(run_id, run)
        return redirect(url_for("review_page", run_id=run_id))

    @app.post("/review/<run_id>/delete")
    def delete_run(run_id):
        RUNS.pop(run_id, None)
        store.delete_run(run_id)
        flash("Review deleted.")
        return redirect(url_for("index"))

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
