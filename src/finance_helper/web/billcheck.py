"""Bill Check web routes: run the AP review, work the queue, fix and re-verify.

Protected by the app-level login gate (before_request in app.py). Every
disposition records who did it and appends to billcheck/audit.jsonl.
"""

from __future__ import annotations

import csv as _csv
import io as _io
import os
import threading
import uuid
from datetime import datetime

from flask import (Blueprint, Response, current_app, flash, redirect,
                   render_template, request, send_file, session, url_for)

from .. import billdotcom_api
from ..billcheck import compare, engine, extract
from ..billcheck import store as bc_store

billcheck_bp = Blueprint("billcheck", __name__, url_prefix="/billcheck")

DISPOSITION_ACTIONS = ["accept", "fixed", "investigate", "not_an_issue"]
DEFAULT_LIMIT = int(os.environ.get("BILLCHECK_MAX_READS_PER_RUN") or 200)

# Same background-thread pattern as Cash Proof (one gunicorn worker, see
# gunicorn.conf.py): the POST returns at once, the progress page polls.
JOBS: dict[str, dict] = {}


def _who() -> str:
    return session.get("email") or "local"


def _readiness() -> dict:
    return {"billdotcom": billdotcom_api.credentials_present(),
            "claude": extract.credentials_present(),
            "model": extract.model_name()}


def _history_bills() -> list[dict]:
    """Bill.com master index (all bills, paid included) for the duplicate scan."""
    import json
    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
    path = os.path.join(data_dir, "billdotcom_master.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("bills") or []
    except (OSError, ValueError):
        return []


def _running_job():
    for jid, job in JOBS.items():
        if job["status"] == "running":
            return jid
    return None


def _execute(job_id, job, who, limit, force):
    log = job["stages"].append
    try:
        engine.run_check(billdotcom_api.fetch_open_bills,
                         billdotcom_api.fetch_bill_documents,
                         extract.extract_invoice, log=log, who=who,
                         limit=limit, force=force,
                         history_bills=_history_bills())
        job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


@billcheck_bp.get("/")
def landing():
    results = bc_store.list_results()
    show_all = request.args.get("all") == "1"
    open_items = [r for r in results if bc_store.is_open(r)]
    counts = {"critical": 0, "high": 0, "review": 0}
    for r in open_items:
        if r.get("severity") in counts:
            counts[r["severity"]] += 1
    clean = sum(1 for r in results if r.get("status") == "match")
    rows = results if show_all else open_items
    return render_template(
        "billcheck.html", ready=_readiness(), rows=rows, show_all=show_all,
        counts=counts, open_count=len(open_items), clean=clean, total=len(results),
        last_run=bc_store.load_run_summary(), running=_running_job(),
        default_limit=DEFAULT_LIMIT, is_open=bc_store.is_open)


@billcheck_bp.post("/run")
def run():
    ready = _readiness()
    if not ready["billdotcom"] or not ready["claude"]:
        flash("Bill Check needs both Bill.com credentials (BILLDOTCOM_*) and "
              "ANTHROPIC_API_KEY set before it can run.")
        return redirect(url_for("billcheck.landing"))
    running = _running_job()
    if running:
        return redirect(url_for("billcheck.progress", job_id=running))
    try:
        limit = max(1, int(request.form.get("limit") or DEFAULT_LIMIT))
    except ValueError:
        limit = DEFAULT_LIMIT
    force = request.form.get("force") == "on"
    job_id = uuid.uuid4().hex[:12]
    job = {"status": "running", "stages": [], "error": None,
           "started": datetime.now().isoformat(timespec="seconds")}
    JOBS[job_id] = job
    args = (job_id, job, _who(), limit, force)
    if current_app.config.get("TESTING"):
        _execute(*args)
    else:
        threading.Thread(target=_execute, args=args, daemon=True).start()
    return redirect(url_for("billcheck.progress", job_id=job_id))


@billcheck_bp.get("/run/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return redirect(url_for("billcheck.landing"))
    if job["status"] == "running":
        return render_template("billcheck_progress.html", job_id=job_id, job=job)
    JOBS.pop(job_id, None)
    if job["status"] == "error":
        flash(f"Bill Check could not run: {job['error']}")
    else:
        flash(job["stages"][-1] if job["stages"] else "Bill Check finished.")
    return redirect(url_for("billcheck.landing"))


@billcheck_bp.get("/queue.csv")
def queue_csv():
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["severity", "status", "vendor", "invoice", "entered_invoice_date",
                "pdf_invoice_date", "entered_due", "expected_due", "entered_total",
                "pdf_total", "findings", "disposition", "bill_id"])
    for r in bc_store.list_results():
        b, ex, c = r.get("bill") or {}, r.get("extracted") or {}, r.get("comparison") or {}
        d = r.get("disposition") or {}
        w.writerow([r.get("severity"), r.get("status"), b.get("vendor"), b.get("invoice"),
                    b.get("invoice_date"), ex.get("invoice_date"), b.get("due_date"),
                    c.get("expected_due"), b.get("amount"), ex.get("total"),
                    " | ".join(f["reason"] for f in c.get("findings") or []) or r.get("error") or "",
                    f"{d.get('action')} — {d.get('note')} ({d.get('who')})" if d else "",
                    r.get("bill_id")])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=billcheck-queue.csv"})


@billcheck_bp.get("/bill/<bill_id>")
def bill_page(bill_id):
    result = bc_store.load_result(bill_id)
    if not result:
        flash("That bill isn't in the Bill Check queue — run a check first.")
        return redirect(url_for("billcheck.landing"))
    return render_template("billcheck_bill.html", r=result, bill_id=bill_id,
                           labels=dict(compare.FIELD_LABELS), ready=_readiness(),
                           is_open=bc_store.is_open(result))


@billcheck_bp.get("/bill/<bill_id>/document")
def document(bill_id):
    try:
        index = int(request.args.get("i") or 0)
    except ValueError:
        index = 0
    found = bc_store.document_path(bill_id, index)
    if not found:
        flash("No attachment is stored for that bill.")
        return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
    path, media = found
    return send_file(os.path.abspath(path), mimetype=media, as_attachment=False)


@billcheck_bp.post("/bill/<bill_id>/disposition")
def disposition(bill_id):
    action = (request.form.get("action") or "").strip()
    note = (request.form.get("note") or "").strip()
    if action not in DISPOSITION_ACTIONS:
        flash("Pick an action for that bill.")
        return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
    if action in ("accept", "not_an_issue") and not note:
        flash("A note is required — say why the entry is right as is.")
        return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
    if not bc_store.record_disposition(bill_id, action, note, _who()):
        flash("That bill isn't in the queue.")
        return redirect(url_for("billcheck.landing"))
    flash("Recorded." + (" The next run re-verifies the corrected entry."
                         if action == "fixed" else ""))
    return redirect(url_for("billcheck.bill_page", bill_id=bill_id))


def _recheck(bill_id, documents=None, source="billdotcom"):
    existing = bc_store.load_result(bill_id)
    if not existing:
        return None, "That bill isn't in the queue."
    bill = existing.get("bill") or {}
    if documents is not None:
        docs_meta = bc_store.save_documents(bill_id, documents, source)
        existing = {**existing, "documents": docs_meta, "extracted": None}
        fetch = lambda _id: documents            # noqa: E731 — use the upload
    else:
        fetch = billdotcom_api.fetch_bill_documents
    payload, outcome = engine.check_bill(
        bill, existing, fetch, extract.extract_invoice, force=True,
        who=_who(), duplicates=existing.get("duplicates"))
    if payload is not None:
        bc_store.save_result(bill_id, payload)
    return payload, outcome


@billcheck_bp.post("/bill/<bill_id>/upload")
def upload(bill_id):
    file = request.files.get("attachment")
    if not file or not file.filename:
        flash("Choose the invoice PDF (or image) first.")
        return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
    if not extract.credentials_present():
        flash("ANTHROPIC_API_KEY is not set — the attachment can't be read.")
        return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
    data = file.read()
    media = billdotcom_api.sniff_media_type(data, file.mimetype or "")
    payload, outcome = _recheck(bill_id, documents=[{
        "name": file.filename, "media_type": media, "data": data}], source="upload")
    if payload is None:
        flash(outcome)
        return redirect(url_for("billcheck.landing"))
    flash("Attachment read and compared." if outcome == "read"
          else f"Could not read the attachment: {payload.get('error')}")
    return redirect(url_for("billcheck.bill_page", bill_id=bill_id))


@billcheck_bp.post("/bill/<bill_id>/recheck")
def recheck(bill_id):
    ready = _readiness()
    if not ready["billdotcom"] or not ready["claude"]:
        flash("Re-reading needs Bill.com credentials and ANTHROPIC_API_KEY.")
        return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
    payload, outcome = _recheck(bill_id)
    if payload is None:
        flash(outcome)
        return redirect(url_for("billcheck.landing"))
    flash({"read": "Attachment re-read from Bill.com and compared.",
           "no_document": f"Bill.com has no attachment on this bill: {payload.get('error')}",
           }.get(outcome, f"Could not re-read: {payload.get('error')}"))
    return redirect(url_for("billcheck.bill_page", bill_id=bill_id))
