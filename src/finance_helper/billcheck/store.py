"""Persist Bill Check results, cached attachments, dispositions, audit log.

Everything lives under <FINANCE_HELPER_OUT_DIR>/billcheck/ (same volume as
Cash Proof, so it survives redeploys):

    bills/<bill_id>.json   one result per bill: what was entered, what the
                           PDF said, the comparison, disposition, history
    docs/<bill_id>/…       the attachment bytes (served on the detail page)
    audit.jsonl            append-only: every disposition, who/when/why
    last_run.json          the most recent run's summary lines

A result is keyed by the bill's *fingerprint* — the compared fields — so an
unchanged bill is skipped on the next run (no re-read, no cost) and an
edited one is re-verified, with its earlier disposition moved to history.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

from .compare import SEVERITY_ORDER

FINGERPRINT_FIELDS = ("vendor", "invoice", "invoice_date", "due_date", "amount",
                      "terms_days", "po")
_EXT = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg",
        "image/gif": "gif", "image/webp": "webp"}


def _root() -> str:
    return os.path.join(os.environ.get("FINANCE_HELPER_OUT_DIR", "out"), "billcheck")


def _bills_dir() -> str:
    return os.path.join(_root(), "bills")


def _docs_dir(bill_id: str) -> str:
    return os.path.join(_root(), "docs", _safe(bill_id))


def _safe(bill_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(bill_id))[:80]


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def fingerprint(bill: dict, extra=None) -> str:
    parts = json.dumps({**{k: bill.get(k) for k in FINGERPRINT_FIELDS}, "extra": extra},
                       sort_keys=True, default=str)
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]


# --- attachments ------------------------------------------------------------

def save_documents(bill_id: str, documents: list[dict], source: str) -> list[dict]:
    """Write attachment bytes to disk; returns the metadata list to store."""
    folder = _docs_dir(bill_id)
    os.makedirs(folder, exist_ok=True)
    for old in os.listdir(folder):
        os.unlink(os.path.join(folder, old))
    meta = []
    for i, d in enumerate(documents):
        ext = _EXT.get(d.get("media_type", ""), "bin")
        name = f"{i}.{ext}"
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(d["data"])
        meta.append({"name": d.get("name") or name, "media_type": d.get("media_type", ""),
                     "file": name, "source": source, "bytes": len(d["data"])})
    return meta


def load_documents(bill_id: str, meta: list[dict]) -> list[dict]:
    out = []
    for m in meta or []:
        path = os.path.join(_docs_dir(bill_id), m.get("file", ""))
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            out.append({"name": m.get("name"), "media_type": m.get("media_type"),
                        "data": fh.read()})
    return out


def document_path(bill_id: str, index: int) -> tuple[str, str] | None:
    result = load_result(bill_id)
    if not result:
        return None
    docs = result.get("documents") or []
    if index < 0 or index >= len(docs):
        return None
    path = os.path.join(_docs_dir(bill_id), docs[index].get("file", ""))
    if not os.path.exists(path):
        return None
    return path, docs[index].get("media_type") or "application/octet-stream"


# --- results ----------------------------------------------------------------

def save_result(bill_id: str, payload: dict) -> None:
    payload = dict(payload)
    payload["bill_id"] = bill_id
    _write_json(os.path.join(_bills_dir(), _safe(bill_id) + ".json"), payload)


def load_result(bill_id: str) -> dict | None:
    return _read_json(os.path.join(_bills_dir(), _safe(bill_id) + ".json"))


def delete_result(bill_id: str) -> None:
    path = os.path.join(_bills_dir(), _safe(bill_id) + ".json")
    if os.path.exists(path):
        os.unlink(path)


def is_open(result: dict) -> bool:
    return (result.get("status") not in ("match",)
            and not result.get("disposition"))


def list_results() -> list[dict]:
    """Every stored result, open items first, worst severity first, then
    soonest due date."""
    folder = _bills_dir()
    if not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        r = _read_json(os.path.join(folder, name))
        if r:
            out.append(r)
    out.sort(key=lambda r: (
        0 if is_open(r) else 1,
        SEVERITY_ORDER.get(r.get("severity"), 9),
        (r.get("bill") or {}).get("due_date") or "9999",
        (r.get("bill") or {}).get("vendor") or "",
    ))
    return out


def record_disposition(bill_id: str, action: str, note: str, who: str) -> bool:
    result = load_result(bill_id)
    if not result:
        return False
    entry = {"action": action, "note": note, "who": who,
             "when": datetime.now().isoformat(timespec="seconds")}
    result["disposition"] = entry
    save_result(bill_id, result)
    os.makedirs(_root(), exist_ok=True)
    with open(os.path.join(_root(), "audit.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"bill_id": bill_id, "fingerprint": result.get("fingerprint"),
                             **entry}) + "\n")
    return True


def save_run_summary(summary: dict) -> None:
    _write_json(os.path.join(_root(), "last_run.json"), summary)


def load_run_summary() -> dict | None:
    return _read_json(os.path.join(_root(), "last_run.json"))
