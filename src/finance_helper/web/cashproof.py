"""Cash Proof web routes: run a reconciliation, work the exceptions queue.

Routes are protected by the app-level login gate (before_request in app.py).
Every disposition records who did it (the logged-in email) and appends to the
append-only audit log — see recon/store.py.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import json

from ..recon import bank as bank_mod
from ..recon import checks, engine, sage_api, sage_xml, summary
from ..recon import sage as sage_mod
from ..recon import store as recon_store


def _data_json(name, default):
    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _flight_pairs():
    """(person, date) for every line of every saved United statement."""
    from .app import RUNS
    from . import store as review_store
    for rid, saved in review_store.load_all_runs().items():
        RUNS.setdefault(rid, saved)
    pairs = []
    for run in RUNS.values():
        doc = run.get("doc")
        if doc is None or doc.source != "united":
            continue
        for li in doc.line_items:
            if li.person and li.date:
                pairs.append((li.person, li.date))
    return pairs


def _sage_fetcher():
    """Prefer the XML gateway (this company's proven credential style); fall
    back to REST OAuth when only client id/secret are configured."""
    if sage_xml.credentials_present():
        return sage_xml.fetch_ledger, "Sage API (XML gateway)"
    if sage_api.credentials_present():
        return sage_api.fetch_ledger, "Sage API"
    return None, None

cashproof_bp = Blueprint("cashproof", __name__, url_prefix="/cashproof")

SEVERITY_ORDER = {"critical": 0, "high": 1, "review": 2, "timing": 3}
DISPOSITION_ACTIONS = ["accept", "investigate", "confirmed_issue"]
MATCH_ACTIONS = ["confirm", "reject"]


@cashproof_bp.app_template_filter("money")
def money(value) -> str:
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def _save_upload(file) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        file.save(tmp.name)
        return tmp.name


@cashproof_bp.get("/")
def landing():
    return render_template("cashproof.html", runs=recon_store.list_runs(),
                           sage_api_ready=_sage_fetcher()[0] is not None)


@cashproof_bp.post("/run")
def run():
    bank_file = request.files.get("bank_file")
    if not bank_file or not bank_file.filename:
        flash("Choose a bank statement CSV first.")
        return redirect(url_for("cashproof.landing"))
    sage_file = request.files.get("sage_file")
    use_api = request.form.get("sage_api") == "on"
    has_sage = bool(sage_file and sage_file.filename)

    # One click = full coverage: pull every API-backed index first (staleness-
    # guarded), and say per source what happened — a silent coverage gap is
    # exactly what a fraud sweep must not have.
    from .refresh import auto_refresh
    refresh_msgs = auto_refresh()
    if refresh_msgs:
        flash("Data refresh: " + " · ".join(refresh_msgs))

    bank_path = _save_upload(bank_file)
    sage_path = _save_upload(sage_file) if has_sage else None
    try:
        bank_txns = bank_mod.load_bank_csv(bank_path)
        if not bank_txns:
            flash("No transactions found in that file — is it the bank's CSV export?")
            return redirect(url_for("cashproof.landing"))
        if has_sage:
            ledger_txns = sage_mod.load_sage_csv(sage_path)
            ledger_label = sage_file.filename
        elif use_api:
            fetch, label = _sage_fetcher()
            if fetch is None:
                raise RuntimeError("No Sage credentials configured — set the "
                                   "INTACCT_SENDER_* (XML) or INTACCT_CLIENT_* (REST) variables.")
            posted = [t for t in bank_txns if not t.pending]
            start = min(t.posted_date for t in posted)
            end = max(t.posted_date for t in posted)
            ledger_txns = fetch(start, end)
            ledger_label = f"{label} ({start} → {end})"
        else:
            ledger_txns, ledger_label = [], None
        result = engine.reconcile(bank_txns, ledger_txns)
        result.integrity = bank_mod.integrity_check(bank_path)
    except Exception as exc:
        flash(f"Could not process: {exc}")
        return redirect(url_for("cashproof.landing"))
    finally:
        os.unlink(bank_path)
        if sage_path:
            os.unlink(sage_path)

    run_id = uuid.uuid4().hex[:12]
    recon_store.save_run(run_id, result, {
        "bank_filename": bank_file.filename,
        "sage_filename": ledger_label,
        "created": datetime.now().isoformat(timespec="seconds"),
        "created_by": session.get("email", ""),
    })
    if not has_sage and not use_api:
        flash("Bank statement analyzed. Upload a Sage GL-detail export with it "
              "to run the full tie-out — this run shows cash activity only.")
    return redirect(url_for("cashproof.run_page", run_id=run_id))


@cashproof_bp.get("/<run_id>")
def run_page(run_id):
    payload = recon_store.load_run(run_id)
    if not payload:
        flash("That Cash Proof run isn't available.")
        return redirect(url_for("cashproof.landing"))
    result = payload["result"]
    dispositions = payload.get("dispositions", {})

    exceptions = sorted(
        (t for t in result.bank + result.ledger if t.status == "exception"),
        key=lambda t: (SEVERITY_ORDER.get(engine.severity_of(t), 9), t.posted_date),
    )
    confirms = [m for m in result.matches if not m.confirmed]
    txn_by_id = {t.source_id: t for t in result.bank + result.ledger}
    activity = summary.build(result.bank)

    from collections import Counter
    from ..insights import hbar_chart
    sev_counts = Counter(engine.severity_of(t) for t in exceptions)
    buckets_chart = hbar_chart(
        [(b["label"], b["total"]) for b in activity["buckets"] if b["kind"] != "sweep"][:10],
        label_w=210)

    fraud = checks.run_all(
        result.bank,
        _data_json("ramp_reimbursements.json", []),
        _data_json("hotel_project_index.json", []),
        _data_json("timecards_index.json", {}),
        _flight_pairs(),
        bill_index=_data_json("billdotcom_payments.json", []),
    )

    return render_template(
        "cashproof_run.html",
        run_id=run_id,
        fraud=fraud,
        meta=payload.get("meta", {}),
        result=result,
        has_ledger=bool(result.ledger),
        stats=summary.tie_stats(result.bank),
        activity=activity,
        flow=summary.flow_chart(activity["months"]),
        buckets_chart=buckets_chart,
        sev_counts=sev_counts,
        exceptions=exceptions,
        timing=result.timing,
        confirms=confirms,
        txn_by_id=txn_by_id,
        dispositions=dispositions,
        severity_of=engine.severity_of,
        open_count=sum(1 for t in exceptions if t.source_id not in dispositions),
    )


@cashproof_bp.post("/<run_id>/disposition")
def disposition(run_id):
    source_id = (request.form.get("source_id") or "").strip()
    action = (request.form.get("action") or "").strip()
    note = (request.form.get("note") or "").strip()
    valid = DISPOSITION_ACTIONS + MATCH_ACTIONS
    if not source_id or action not in valid:
        flash("Pick an action for that line.")
        return redirect(url_for("cashproof.run_page", run_id=run_id))
    if action in ("accept", "confirmed_issue") and not note:
        flash("A note is required — say why it's fine (or what was found).")
        return redirect(url_for("cashproof.run_page", run_id=run_id))
    ok = recon_store.record_disposition(
        run_id, source_id, action, note, session.get("email", "local"))
    flash("Recorded." if ok else "That run isn't available.")
    return redirect(url_for("cashproof.run_page", run_id=run_id))
