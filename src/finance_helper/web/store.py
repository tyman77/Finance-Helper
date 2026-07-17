"""Persist in-progress review runs to disk so they survive a restart/redeploy.

A run otherwise lives only in the in-memory RUNS dict, which is wiped whenever
the process restarts — and every Railway deploy restarts it. This writes each
run as JSON under <FINANCE_HELPER_OUT_DIR>/runs/ so a review you're partway
through can be reopened later (and so a run created in one gunicorn worker is
visible to the others, which don't share memory).

Everything here is best-effort: a failure to read/write a run file must never
take down the request, just fall back to in-memory-only behavior.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal

from ..models import LineItem, SourceDocument


def _runs_dir() -> str:
    out_dir = os.environ.get("FINANCE_HELPER_OUT_DIR", "out")
    return os.path.join(out_dir, "runs")


def _line_to_dict(li: LineItem) -> dict:
    return {
        "description": li.description,
        "amount": str(li.amount),
        "date": li.date.isoformat() if li.date else None,
        "category": li.category,
        "gl_account": li.gl_account,
        "person": li.person,
        "department": li.department,
        "project": li.project,
        "needs_review": li.needs_review,
        "note": li.note,
        "raw": li.raw,
    }


def _line_from_dict(d: dict) -> LineItem:
    return LineItem(
        description=d["description"],
        amount=Decimal(d["amount"]),
        date=date.fromisoformat(d["date"]) if d.get("date") else None,
        category=d.get("category"),
        gl_account=d.get("gl_account"),
        person=d.get("person"),
        department=d.get("department"),
        project=d.get("project"),
        needs_review=d.get("needs_review", False),
        note=d.get("note"),
        raw=d.get("raw", {}),
    )


def _doc_to_dict(doc: SourceDocument) -> dict:
    return {
        "source": doc.source,
        "destination": doc.destination,
        "vendor": doc.vendor,
        "document_id": doc.document_id,
        "currency": doc.currency,
        "document_date": doc.document_date.isoformat() if doc.document_date else None,
        "line_items": [_line_to_dict(li) for li in doc.line_items],
    }


def _doc_from_dict(d: dict) -> SourceDocument:
    return SourceDocument(
        source=d["source"],
        destination=d["destination"],
        vendor=d["vendor"],
        document_id=d["document_id"],
        currency=d["currency"],
        document_date=date.fromisoformat(d["document_date"]) if d.get("document_date") else None,
        line_items=[_line_from_dict(x) for x in d["line_items"]],
    )


def _payload_to_run(payload: dict) -> dict:
    return {
        "doc": _doc_from_dict(payload["doc"]),
        "source": payload["source"],
        "filename": payload["filename"],
        "created": datetime.fromisoformat(payload["created"]),
        "posted": payload.get("posted"),
    }


def save_run(run_id: str, run: dict) -> None:
    """Write (or overwrite) a run's JSON atomically. Best-effort."""
    try:
        os.makedirs(_runs_dir(), exist_ok=True)
        payload = {
            "run_id": run_id,
            "source": run["source"],
            "filename": run["filename"],
            "created": run["created"].isoformat(),
            "posted": run.get("posted"),
            "doc": _doc_to_dict(run["doc"]),
        }
        path = os.path.join(_runs_dir(), f"{run_id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)  # atomic on POSIX; no half-written file is ever read
    except OSError:
        pass


def load_run(run_id: str) -> dict | None:
    path = os.path.join(_runs_dir(), f"{run_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return _payload_to_run(json.load(fh))
    except (OSError, ValueError, KeyError):
        return None


def load_all_runs() -> dict:
    """Every persisted run, keyed by run_id. Skips any file that won't parse."""
    runs: dict = {}
    directory = _runs_dir()
    if not os.path.isdir(directory):
        return runs
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        run_id = name[: -len(".json")]
        run = load_run(run_id)
        if run is not None:
            runs[run_id] = run
    return runs


def delete_run(run_id: str) -> None:
    path = os.path.join(_runs_dir(), f"{run_id}.json")
    try:
        os.remove(path)
    except OSError:
        pass
