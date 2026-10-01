"""Orchestrate one Bill Check run: for every open bill, get the attachment,
read it, compare, store. No Flask in here so the loop is testable on its own.

Cost discipline: a bill whose compared fields haven't changed since its last
check is skipped outright; a bill that changed reuses its earlier read of the
attachment (the PDF didn't change, the typing did) and only re-compares. Only
a bill never seen before — or an explicit re-read — costs a Claude call.
"""

from __future__ import annotations

import sys
from datetime import datetime

from . import compare
from . import learn
from . import store
from .extract import SCHEMA_VERSION

SKIP_STATUSES = ("match", "mismatch", "review", "unreadable")


def _match_projects(extracted: dict | None) -> list[dict]:
    """Resolve the job numbers Claude read off the document against Sage —
    e.g. each rental on a National Car Rental bill carries the job it was
    booked for. Never raises: an unmatched ref is shown, not dropped."""
    refs = [r.strip() for r in
            ((extracted or {}).get("project_references") or "").split(",") if r.strip()]
    if not refs:
        return []
    try:
        from ..project_resolver import load_active_projects, project_display_names
        names = project_display_names({})
        active = load_active_projects()
    except Exception:
        names, active = {}, None
    out = []
    for ref in refs:
        code = ref if ref in names else ""
        if not code:                        # "Job #5232" style — digits only
            digits = "".join(ch for ch in ref if ch.isdigit())
            if digits in names:
                code = digits
        out.append({
            "ref": ref,
            "code": code,
            "name": names.get(code, ""),
            "active": (None if active is None or not code else code in active),
        })
    return out


def check_bill(bill: dict, existing: dict | None, fetch_documents, extract_fn,
               force: bool = False, who: str = "", duplicates: list[str] | None = None,
               now: datetime | None = None, all_lessons: list[dict] | None = None,
               adjudicate_client=None) -> tuple[dict | None, str]:
    """Returns (result payload to store, outcome). Payload is None when the
    bill is unchanged and was skipped. Outcome is one of
    unchanged / reused / read / no_document / error."""
    existing = existing or {}
    duplicates = duplicates or []

    from ..recon.settings import recon_config
    bc_cfg = recon_config().get("billcheck") or {}
    policies = learn.merged_policies(bc_cfg.get("vendor_policies") or {})
    aliases = bc_cfg.get("vendor_aliases") or {}
    vendor_lessons = learn.lessons_for(bill.get("vendor", ""), all_lessons)
    # The fingerprint covers the compared fields plus everything the verdict
    # depends on: duplicates, and the learning state for this vendor (notes,
    # prior decisions, learned policy). Any of those changing re-compares
    # from the cached read. Dispositions carry over on the bill fields
    # alone — a note being added must not bump an open decision to history.
    learn_sig = learn.signature(bill.get("vendor", ""), all_lessons)
    fp = store.fingerprint(bill, extra={"dups": sorted(duplicates), "learn": learn_sig})
    bill_fp = store.fingerprint(bill)
    existing_bill_fp = existing.get("bill_fingerprint") or (
        store.fingerprint(existing["bill"]) if existing.get("bill") else None)

    # A vendor excluded from review entirely (policy skip: true) — reviewed
    # by hand elsewhere; never fetch or read its attachments.
    policy = compare._vendor_policy(bill.get("vendor"), policies)
    if policy.get("skip"):
        if existing.get("fingerprint") == fp and existing.get("status") == "skipped" \
                and not force:
            return None, "unchanged"
        return {
            "bill": bill, "fingerprint": fp, "bill_fingerprint": bill_fp, "extracted": None,
            "comparison": None, "status": "skipped", "severity": "clear",
            "error": None, "documents": list(existing.get("documents") or []),
            "duplicates": duplicates,
            "skip_reason": ("excluded from review by vendor policy"
                            + (f" — {policy['notes']}" if policy.get("notes") else "")),
            "checked_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "checked_by": who,
            "disposition": existing.get("disposition"),
            "history": list(existing.get("history") or []),
        }, "skipped"
    cached_read = existing.get("extracted") or {}
    read_current = bool(cached_read) and cached_read.get("schema") == SCHEMA_VERSION
    if (existing.get("fingerprint") == fp and not force and read_current
            and existing.get("status") in SKIP_STATUSES):
        return None, "unchanged"

    docs_meta = list(existing.get("documents") or [])
    uploaded = bool(docs_meta) and docs_meta[0].get("source") == "upload"
    extracted, error, outcome = None, None, ""

    if read_current and not force:
        extracted, outcome = cached_read, "reused"
    else:
        # Only a hand-uploaded file is reused from disk. Anything that came
        # from Bill.com is pulled again — a cached copy of a bad answer
        # (an HTML page instead of the PDF) would otherwise fail forever.
        documents = []
        if uploaded:
            documents = store.load_documents(bill["id"], docs_meta)
        if not documents:
            try:
                documents = fetch_documents(bill["id"])
                docs_meta = store.save_documents(bill["id"], documents, "billdotcom")
            except Exception as exc:
                error = f"No attachment could be pulled from Bill.com: {str(exc)[:2500]}"
                outcome = "no_document"
        if documents and error is None:
            try:
                extracted, outcome = extract_fn(documents), "read"
            except Exception as exc:
                error, outcome = str(exc)[:400], "error"

    if error:
        # Surfaced in the host's log too, so an attachment/API problem can
        # be diagnosed from the server side without opening each bill.
        print(f"[billcheck] bill {bill.get('id')} ({bill.get('vendor')} #{bill.get('invoice')}): "
              f"{outcome}: {error}", file=sys.stderr, flush=True)

    comparison = (compare.compare_bill(bill, extracted, policies=policies,
                                       aliases=aliases,
                                       check_vendor=bc_cfg.get("check_vendor", True))
                  if extracted else None)
    if comparison:
        policy = compare._vendor_policy(bill.get("vendor"), policies)
        learn.apply_lessons(bill, comparison, vendor_lessons, policy)
        if learn._adjudicate_enabled() or adjudicate_client is not None:
            try:
                learn.adjudicate(bill, comparison, extracted, learn.standing_notes(),
                                 vendor_lessons, client=adjudicate_client)
            except Exception as exc:          # never let the judgment pass sink a bill
                comparison["adjudication"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if comparison and duplicates:
        comparison["findings"].insert(0, {
            "field": "duplicate", "severity": "critical", "entered": bill.get("invoice", ""),
            "pdf": "", "reason": "Same vendor + invoice number also entered as: "
                                 + "; ".join(duplicates)})
        comparison["status"], comparison["severity"] = "mismatch", "critical"
    if comparison:
        status, severity = comparison["status"], comparison["severity"]
    else:
        status, severity = outcome, "review"

    same_bill = existing_bill_fp == bill_fp
    disposition = existing.get("disposition") if same_bill else None
    history = list(existing.get("history") or [])
    if existing.get("disposition") and not same_bill:
        history.append({**existing["disposition"], "fingerprint": existing.get("fingerprint"),
                        "bill": existing.get("bill"), "status": existing.get("status")})
    payload = {
        "bill": bill,
        "fingerprint": fp,
        "bill_fingerprint": bill_fp,
        "extracted": extracted,
        "comparison": comparison,
        "projects": _match_projects(extracted),
        "status": status,
        "severity": severity,
        "error": error,
        "documents": docs_meta,
        "duplicates": duplicates,
        "checked_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "checked_by": who,
        "disposition": disposition,
        "history": history,
    }
    return payload, outcome


def run_check(fetch_bills, fetch_documents, extract_fn, log=lambda m: None,
              who: str = "", limit: int = 200, force: bool = False,
              history_bills: list[dict] | None = None) -> dict:
    """One full pass. `log` gets one line per stage; the returned summary is
    what the landing page shows as 'last run'."""
    started = datetime.now()
    log("Pulling open bills from Bill.com…")
    bills = fetch_bills()
    log(f"· {len(bills)} unpaid bills")
    # Soonest due first: the bill about to be paid is the one to catch.
    bills.sort(key=lambda b: (b.get("due_date") or "9999", b.get("vendor") or ""))
    dups = compare.find_duplicates(bills, history_bills or [])
    if dups:
        log(f"· {len(dups)} bills share a vendor + invoice number with another bill")
    all_lessons = learn.lessons()
    prior = sum(1 for l in all_lessons if l.get("action") in learn.LESSON_ACTIONS)
    if prior or learn.standing_notes():
        log(f"· learning from {prior} prior reviewer decisions"
            + (" and the standing notes" if learn.standing_notes() else ""))

    counts = {"unchanged": 0, "reused": 0, "read": 0, "no_document": 0, "error": 0}
    mismatches = 0
    reads = 0
    limit_hit = False
    for i, bill in enumerate(bills, 1):
        if not bill.get("id"):
            continue
        existing = store.load_result(bill["id"])
        cached = (existing or {}).get("extracted") or {}
        needs_read = force or cached.get("schema") != SCHEMA_VERSION
        if needs_read and reads >= limit:
            limit_hit = True
            continue
        payload, outcome = check_bill(bill, existing, fetch_documents, extract_fn,
                                      force=force, who=who, duplicates=dups.get(bill["id"]),
                                      all_lessons=all_lessons)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ("read", "error", "no_document"):
            reads += 1
        if payload is not None:
            store.save_result(bill["id"], payload)
            if payload["status"] == "mismatch":
                mismatches += 1
        if i % 25 == 0:
            log(f"· {i}/{len(bills)} bills processed ({counts['read']} read, "
                f"{counts['unchanged']} unchanged)")

    # Bills that are no longer open (paid / deleted) drop out of the queue.
    open_ids = {b["id"] for b in bills if b.get("id")}
    retired = 0
    for r in store.list_results():
        if r.get("bill_id") not in open_ids:
            store.delete_result(r["bill_id"])
            retired += 1
    if retired:
        log(f"· {retired} bills paid/removed since last run dropped from the queue")

    summary = {
        "when": started.isoformat(timespec="seconds"),
        "by": who,
        "seconds": round((datetime.now() - started).total_seconds(), 1),
        "bills": len(bills),
        "counts": counts,
        "mismatches": mismatches,
        "limit": limit,
        "limit_hit": limit_hit,
        "force": force,
    }
    log(f"Done: {counts['read']} attachments read, {counts['reused']} re-compared, "
        f"{counts['unchanged']} unchanged, {counts['no_document']} without an attachment, "
        f"{counts['error']} read errors.")
    if limit_hit:
        log(f"· Stopped reading new attachments at the per-run limit of {limit}; "
            "run again to continue.")
    store.save_run_summary(summary)
    return summary
