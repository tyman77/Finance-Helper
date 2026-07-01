"""Flask app: upload a vendor CSV, review/edit the proposed coding, approve/post.

Thin wrapper around the existing pipeline — every route just calls into
finance_helper.pipeline / categorize / destinations / validate. State (the
in-progress runs) lives in memory for the life of the process; this is a local,
single-user review tool, not a multi-tenant service.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from .. import config, destinations, pipeline, validate
from .. import review as proposal_review

RUNS: dict[str, dict] = {}


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
    registry = _load_json_data("project_registry.json").get("registry", {})
    return sorted((code, info.get("client", "")) for code, info in registry.items())


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FINANCE_HELPER_SECRET", "dev-local-only-not-a-real-secret")

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
        return render_template(
            "review.html",
            run_id=run_id,
            run=run,
            doc=doc,
            issues_by_line=issues_by_line,
            total_issues=sum(len(v) for v in issues_by_line.values()),
            account_options=_account_options(),
            department_options=_department_options(),
            project_options=_project_options(),
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
