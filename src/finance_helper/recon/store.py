"""Persist Cash Proof runs, dispositions, and the audit log.

Runs live under <FINANCE_HELPER_OUT_DIR>/recon/ as JSON (same volume as the
review runs, so they survive redeploys). Dispositions never overwrite matching
state — they layer on top, and every disposition also appends a row to an
append-only audit log (audit.jsonl). Nothing here deletes anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from .models import MatchGroup, ReconResult, Txn


def _recon_dir() -> str:
    return os.path.join(os.environ.get("FINANCE_HELPER_OUT_DIR", "out"), "recon")


def _txn_to_dict(t: Txn) -> dict:
    d = asdict(t)
    d["posted_date"] = t.posted_date.isoformat()
    d["amount"] = str(t.amount)
    return d


def _txn_from_dict(d: dict) -> Txn:
    d = dict(d)
    d["posted_date"] = date.fromisoformat(d["posted_date"])
    d["amount"] = Decimal(d["amount"])
    return Txn(**d)


def save_run(run_id: str, result: ReconResult, meta: dict) -> None:
    os.makedirs(_recon_dir(), exist_ok=True)
    payload = {
        "run_id": run_id,
        "meta": meta,                       # filenames, created, created_by
        "period_start": result.period_start.isoformat() if result.period_start else None,
        "period_end": result.period_end.isoformat() if result.period_end else None,
        "integrity": result.integrity,
        "bank": [_txn_to_dict(t) for t in result.bank],
        "ledger": [_txn_to_dict(t) for t in result.ledger],
        "matches": [asdict(m) for m in result.matches],
        "dispositions": {},
    }
    path = os.path.join(_recon_dir(), f"{run_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def load_run(run_id: str) -> dict | None:
    path = os.path.join(_recon_dir(), f"{run_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    payload["result"] = ReconResult(
        period_start=date.fromisoformat(payload["period_start"]) if payload["period_start"] else None,
        period_end=date.fromisoformat(payload["period_end"]) if payload["period_end"] else None,
        bank=[_txn_from_dict(t) for t in payload["bank"]],
        ledger=[_txn_from_dict(t) for t in payload["ledger"]],
        matches=[MatchGroup(**m) for m in payload["matches"]],
        integrity=payload.get("integrity") or {},
    )
    return payload


def list_runs() -> list[dict]:
    directory = _recon_dir()
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                p = json.load(fh)
            out.append({"run_id": p["run_id"], "meta": p.get("meta", {}),
                        "period_start": p.get("period_start"), "period_end": p.get("period_end"),
                        "open_exceptions": sum(
                            1 for t in p.get("bank", []) + p.get("ledger", [])
                            if t.get("status") == "exception"
                            and t.get("source_id") not in p.get("dispositions", {}))})
        except (OSError, ValueError, KeyError):
            continue
    out.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)
    return out


def record_disposition(run_id: str, source_id: str, action: str, note: str, who: str) -> bool:
    """Layer a disposition onto a run and append it to the audit log."""
    path = os.path.join(_recon_dir(), f"{run_id}.json")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    entry = {"action": action, "note": note, "who": who,
             "when": datetime.now().isoformat(timespec="seconds")}
    payload.setdefault("dispositions", {})[source_id] = entry
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    with open(os.path.join(_recon_dir(), "audit.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": run_id, "source_id": source_id, **entry}) + "\n")
    return True
