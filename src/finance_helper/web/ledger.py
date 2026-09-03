"""Durable record of statement lines already posted to Sage.

The posted ✔ stamp on a review line is the duplicate-JE guard, but it lives
inside one run. This ledger persists the same fact independently — keyed by
the line itself (source|date|description|amount) — so the guard survives a
redeploy, a re-run, and even the same statement being uploaded again as a
fresh run. Identical lines (four $8.00 wifi charges on one day) are handled
by count: each posting appends one stamp, and each application consumes one.

Best-effort like the run store: a ledger failure must never block a request.
"""

from __future__ import annotations

import json
import os


def _path() -> str:
    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
    return os.path.join(data_dir, "posted_ledger.json")


def _key(source: str, li) -> str:
    return f"{source}|{li.date}|{li.description}|{li.amount}"


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _save(ledger: dict) -> None:
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def record(source: str, lines, stamp: str) -> None:
    """Remember that these lines went into a journal entry."""
    ledger = load()
    for li in lines:
        ledger.setdefault(_key(source, li), []).append(stamp)
    _save(ledger)


def apply(source: str, doc) -> int:
    """Stamp doc lines the ledger knows were already posted. Returns how many
    were stamped. Consumes stamps per occurrence so N identical lines get at
    most N marks."""
    ledger = load()
    used: dict[str, int] = {}
    stamped = 0
    for li in doc.line_items:
        if getattr(li, "posted_ref", ""):
            continue
        key = _key(source, li)
        stamps = ledger.get(key) or []
        i = used.get(key, 0)
        if i < len(stamps):
            li.posted_ref = stamps[i]
            used[key] = i + 1
            stamped += 1
    return stamped


def clear(source: str, doc) -> int:
    """Forget the posted marks for this doc's lines (both on the lines and in
    the ledger) — for starting over after deleting the entries in Sage."""
    ledger = load()
    cleared = 0
    for li in doc.line_items:
        ref = getattr(li, "posted_ref", "")
        if not ref:
            continue
        key = _key(source, li)
        stamps = ledger.get(key) or []
        if ref in stamps:
            stamps.remove(ref)
            if not stamps:
                ledger.pop(key, None)
        li.posted_ref = ""
        cleared += 1
    _save(ledger)
    return cleared
