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
from datetime import datetime, timedelta

from flask import (Blueprint, Response, current_app, flash, redirect,
                   render_template, request, send_file, session, url_for)

from .. import billdotcom_api
from ..billcheck import compare, engine, extract, learn, payrun
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


def _data_dir() -> str:
    return os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))


def _data_json(name: str, default):
    import json
    try:
        with open(os.path.join(_data_dir(), name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _master() -> dict:
    """Bill.com master index: vendors, vendor bank accounts, all bills."""
    master = _data_json("billdotcom_master.json", {})
    return master if isinstance(master, dict) else {}


def _history_bills() -> list[dict]:
    """Bill.com master index (all bills, paid included) for the duplicate scan."""
    return _master().get("bills") or []


def _refresh_master(log) -> None:
    """Re-pull the vendor master before a check, so a bank-account change
    made yesterday is seen on Thursday's review. Failure is logged, not fatal."""
    from .refresh import _is_fresh, _write
    if _is_fresh("billdotcom_master.json"):
        log("· Vendor master is fresh (pulled within the hour)")
        return
    try:
        master = billdotcom_api.fetch_master_index()
        _write("billdotcom_master.json", master)
        log(f"· Vendor master: {len(master['vendors'])} vendors, "
            f"{len(master['bank_accounts'])} bank accounts, {len(master['bills'])} bills"
            + (f" — blocked: {'; '.join(master['gaps'])}" if master.get("gaps") else ""))
    except Exception as exc:
        log(f"· Vendor master refresh FAILED — {str(exc)[:160]} "
            "(fraud checks use the last copy)")


def _run_bill_ids() -> list[str]:
    cfg = payrun.settings()
    pay_date = payrun.next_pay_date(datetime.now().date(), cfg["pay_weekday"])
    return [r["bill_id"] for r in bc_store.list_results()
            if r.get("bill_id") and payrun.in_run(r.get("bill") or {}, pay_date,
                                                  cfg["horizon_days"])]


def _refresh_approvers(log) -> None:
    """Who is on each pay-run bill's approval chain — one Bill.com call per
    bill, so only the coming run's bills are asked. Failure is logged."""
    from .refresh import _write
    ids = _run_bill_ids()
    try:
        approvers, errors = billdotcom_api.fetch_bill_approvers(ids)
    except Exception as exc:
        approvers, errors = {}, [f"approver lookup failed: {str(exc)[:160]}"]
    _write("billdotcom_approvers.json", {
        "fetched": datetime.now().isoformat(timespec="seconds"),
        "approvers": approvers, "errors": errors})
    log(f"· Approvers pulled for {len(approvers)} of {len(ids)} pay-run bills"
        + (f" — {len(errors)} failed (e.g. {errors[0]})" if errors else ""))


def _people() -> list[str]:
    """Employee names, for the vendor-named-like-an-employee check — the
    same sources Cash Proof uses."""
    names: set[str] = set()
    timecards = _data_json("timecards_index.json", {})
    if isinstance(timecards, dict):
        names |= set(timecards.keys())
    for r in _data_json("ramp_reimbursements.json", []) or []:
        if isinstance(r, dict) and r.get("person"):
            names.add(r["person"])
    try:
        from .cashproof import _flight_pairs
        names |= {p for p, _d in _flight_pairs()}
    except Exception:
        pass
    return sorted(n for n in names if n)


def _running_job():
    for jid, job in JOBS.items():
        if job["status"] == "running":
            return jid
    return None


def _execute(job_id, job, who, limit, force):
    log = job["stages"].append
    try:
        _refresh_master(log)
        engine.run_check(billdotcom_api.fetch_open_bills,
                         billdotcom_api.fetch_bill_documents,
                         extract.extract_invoice, log=log, who=who,
                         limit=limit, force=force,
                         history_bills=_history_bills())
        _refresh_approvers(log)
        job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


# --- nightly schedule -------------------------------------------------------
# New bills land in Bill.com every day; a nightly pass catches them while
# the queue is small. In-process timer (one gunicorn worker holds it, same
# reasoning as JOBS above): BILLCHECK_NIGHTLY=0 disables,
# BILLCHECK_NIGHTLY_HOUR_UTC picks the hour (default 08 UTC ≈ 2am Denver).

_nightly_started = False


def _seconds_until(hour_utc: int, now: datetime) -> float:
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _nightly_loop():
    import time
    hour = int(os.environ.get("BILLCHECK_NIGHTLY_HOUR_UTC") or 8)
    while True:
        time.sleep(max(60.0, _seconds_until(hour, datetime.utcnow())))
        if _running_job():
            continue                     # someone's manual run is already going
        ready = _readiness()
        if not (ready["billdotcom"] and ready["claude"]):
            continue
        job_id = "nightly" + uuid.uuid4().hex[:6]
        job = {"status": "running", "stages": [], "error": None,
               "started": datetime.now().isoformat(timespec="seconds")}
        JOBS[job_id] = job
        try:
            _execute(job_id, job, "nightly", DEFAULT_LIMIT, False)
        finally:
            JOBS.pop(job_id, None)       # the stored run summary is the record


def start_nightly():
    global _nightly_started
    if _nightly_started or os.environ.get("BILLCHECK_NIGHTLY", "1") == "0":
        return False
    _nightly_started = True
    threading.Thread(target=_nightly_loop, daemon=True).start()
    return True


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
        default_limit=DEFAULT_LIMIT, is_open=bc_store.is_open,
        proposal_count=len(learn.proposals(config_policies=_config_policies())))


def _config_policies() -> dict:
    from ..recon.settings import recon_config
    return (recon_config().get("billcheck") or {}).get("vendor_policies") or {}


# --- learning: standing notes, proposals, learned policies -------------------

@billcheck_bp.get("/learning")
def learning():
    all_lessons = learn.lessons()
    return render_template(
        "billcheck_learning.html",
        notes=learn.standing_notes(),
        proposals=learn.proposals(all_lessons, _config_policies()),
        learned=learn.learned_policies(),
        stats=learn.stats(all_lessons),
        labels=learn.FIELD_LABELS,
        propose_after=learn.PROPOSE_AFTER,
        adjudicate=learn._adjudicate_enabled())


@billcheck_bp.post("/learning/notes")
def learning_notes():
    learn.save_standing_notes(request.form.get("notes", ""))
    flash("Standing notes saved — every bill is re-judged against them on the next run.")
    return redirect(url_for("billcheck.learning"))


def _back():
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/billcheck/") else url_for("billcheck.learning"))


@billcheck_bp.post("/learning/confirm")
def learning_confirm():
    vendor = (request.form.get("vendor") or "").strip()
    field = (request.form.get("field") or "").strip()
    if not vendor or field not in learn.FIELD_LABELS:
        flash("Pick a vendor and a field.")
        return _back()
    learn.confirm_proposal(vendor, field, _who(), request.form.get("note", ""))
    flash(f"Learned: {learn.FIELD_LABELS[field]} findings are no longer raised for "
          f"{vendor}. Applies from the next run (cached reads, no re-read).")
    return _back()


@billcheck_bp.post("/learning/dismiss")
def learning_dismiss():
    vendor = (request.form.get("vendor") or "").strip()
    field = (request.form.get("field") or "").strip()
    if vendor and field:
        learn.dismiss_proposal(vendor, field, _who())
        flash("Dismissed — the finding keeps being raised for that vendor.")
    return _back()


@billcheck_bp.post("/learning/remove")
def learning_remove():
    vendor = (request.form.get("vendor") or "").strip()
    field = (request.form.get("field") or "").strip()
    if vendor and field and learn.remove_learned(vendor, field):
        flash(f"Un-learned: {learn.FIELD_LABELS.get(field, field)} findings are raised "
              f"again for {vendor} from the next run.")
    return _back()


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
           "started": datetime.now().isoformat(timespec="seconds"),
           "next": "payrun" if request.form.get("next") == "payrun" else ""}
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
    return redirect(url_for("billcheck.payrun_page" if job.get("next") == "payrun"
                            else "billcheck.landing"))


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


# --- Pay Run: Thursday's review of Friday's batch ----------------------------

def _payrun_view(state: dict | None = None):
    cfg = payrun.settings()
    pay_date = payrun.next_pay_date(datetime.now().date(), cfg["pay_weekday"]).isoformat()
    state = state or payrun.load(pay_date)
    approvers = _data_json("billdotcom_approvers.json", {}) or {}
    view = payrun.build(bc_store.list_results(), _master(), _people(),
                        datetime.now().date(), decisions=state.get("decisions"),
                        pay_weekday=cfg["pay_weekday"], horizon_days=cfg["horizon_days"],
                        approvers=approvers.get("approvers") or {},
                        finance_list=state.get("finance_list"))
    return view, state


def _payrun_gaps(view: dict) -> list[str]:
    """Checks that couldn't run on this batch, said out loud — a skipped
    fraud check must never look like a clean one."""
    gaps = list(_master().get("gaps") or [])
    rows = view["rows"]
    lst = view.get("list")
    if lst and not lst["ties"]:
        gaps.insert(0, f"Finance's list doesn't add up: the total at the top says "
                       f"${lst['header_total']:,.2f} but its lines sum to ${lst['total']:,.2f}")
    if lst and lst["mode"] == "list":
        gaps.append("Bill.com isn't connected (or hasn't been refreshed), so this run is "
                    "built from finance's list alone: the invoice, bank-detail and "
                    "same-person checks didn't run — only the list checks did")
        return gaps
    if lst and lst["list_only"]:
        gaps.append(f"{len(lst['list_only'])} bill(s) on finance's list weren't found in "
                    "Bill.com's open bills — see the table below the run")
    if not rows:
        return gaps
    if not any(x["bill"].get("created_by") for x in rows):
        gaps.append("Bills: Bill.com didn't say who entered them — the same-person "
                    "check can't run")
    appr = _data_json("billdotcom_approvers.json", {}) or {}
    have = appr.get("approvers") or {}
    missing = [x for x in rows if x["bill_id"] not in have]
    if missing:
        gaps.append(f"Approvers not pulled for {len(missing)} bill(s) — refresh to "
                    "run the same-person check on them")
    for e in (appr.get("errors") or [])[:3]:
        gaps.append(f"Approver lookup: {e}")
    unread = [x for x in rows if not (x.get("extracted_schema"))]
    if unread:
        gaps.append(f"{len(unread)} bill(s) not yet read with bank-detail extraction — "
                    "their invoice banking details weren't compared")
    return gaps


@billcheck_bp.get("/payrun")
def payrun_page():
    view, state = _payrun_view()
    master = _master()
    return render_template(
        "billcheck_payrun.html", v=view, state=state, gaps=_payrun_gaps(view),
        signed=payrun.signoff_current(state, view),
        history=[h for h in payrun.recent_signoffs() if h.get("pay_date") != view["pay_date"]],
        ready=_readiness(), running=_running_job(), last_run=bc_store.load_run_summary(),
        has_master=bool(master.get("vendors")),
        horizon=payrun.settings()["horizon_days"])


@billcheck_bp.post("/payrun/list")
def payrun_list_upload():
    from ..billcheck import finance_list
    file = request.files.get("list")
    if not file or not file.filename:
        flash("Choose finance's bills-to-pay export (.xlsx or .csv) first.")
        return redirect(url_for("billcheck.payrun_page"))
    data = file.read()
    try:
        parsed = finance_list.parse(data, file.filename)
    except Exception as exc:
        flash(f"Couldn't read {file.filename}: {exc}")
        return redirect(url_for("billcheck.payrun_page"))
    view, _state = _payrun_view()
    payrun.save_list(view["pay_date"], parsed, file.filename, _who(), data)
    s = finance_list.summary(parsed)
    flash(f"Loaded {s['count']} bills (${s['total']:,.2f}) from {file.filename}."
          + ("" if s["ties"] else f" The total at the top (${s['header_total']:,.2f}) "
                                  "doesn't match the lines."))
    return redirect(url_for("billcheck.payrun_page"))


@billcheck_bp.post("/payrun/decide/<bill_id>")
def payrun_decide(bill_id):
    action = (request.form.get("action") or "").strip()
    note = (request.form.get("note") or "").strip()
    view, _state = _payrun_view()
    row = next((x for x in view["rows"] if x["bill_id"] == bill_id), None)
    if row is None:
        flash("That bill isn't in this pay run.")
    elif action not in payrun.DECISIONS:
        flash("Pick release or hold.")
    elif not note:
        flash("A note is required — for a bank change, who you called and on what "
              "number; for a hold, why.")
    else:
        payrun.record_decision(view["pay_date"], bill_id, row["key"], action, note, _who())
        flash(f"{row['bill'].get('vendor')} #{row['bill'].get('invoice')}: "
              + ("released for Friday." if action == "release"
                 else "held — pull it from the payment run in Bill.com."))
    return redirect(url_for("billcheck.payrun_page") + f"#bill-{bill_id}")


@billcheck_bp.post("/payrun/signoff")
def payrun_signoff():
    view, _state = _payrun_view()
    if not view["ready_to_sign"]:
        flash(f"{view['undecided']} flagged bill(s) still need a release or hold decision.")
    elif not view["rows"]:
        flash("Nothing in this pay run to sign off.")
    else:
        payrun.sign_off(view["pay_date"], view, _who(),
                        (request.form.get("note") or "").strip())
        flash(f"Pay run for {view['pay_date']} signed off: "
              f"{view['releasing_count']} bills, ${view['releasing_total']:,.2f}.")
    return redirect(url_for("billcheck.payrun_page"))


@billcheck_bp.get("/payrun.csv")
def payrun_csv():
    view, _state = _payrun_view()
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["pay_date", "severity", "vendor", "invoice", "due_date", "amount",
                "bill_check", "fraud_signals", "decision", "decided_by", "note", "bill_id"])
    for x in view["rows"]:
        b, d = x["bill"], x["decision"] or {}
        w.writerow([view["pay_date"], x["severity"], b.get("vendor"), b.get("invoice"),
                    b.get("due_date"), b.get("amount"), x["check_line"],
                    " | ".join(s["title"] for s in x["signals"]),
                    d.get("action", ""), d.get("who", ""), d.get("note", ""), x["bill_id"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=payrun-{view['pay_date']}.csv"})
