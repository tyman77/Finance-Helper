"""Auto-refresh the API-backed indexes before a Cash Proof run.

One click should mean full coverage: anything with working credentials is
pulled automatically (Ramp reimbursements, Bill.com payments, Paychex
timecards), with a staleness guard so iterative re-runs don't hammer the
APIs. Every source reports one short line — refreshed, skipped-fresh,
no-credentials, or failed (with the reason) — because a silent gap in
coverage is exactly what a fraud tool must not have.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta

_MAX_AGE_HOURS = 1


def _data_dir() -> str:
    return os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))


def _is_fresh(name: str) -> bool:
    path = os.path.join(_data_dir(), name)
    return (os.path.exists(path)
            and (time.time() - os.path.getmtime(path)) < _MAX_AGE_HOURS * 3600)


def _write(name: str, payload) -> None:
    os.makedirs(_data_dir(), exist_ok=True)
    with open(os.path.join(_data_dir(), name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def auto_refresh() -> list[str]:
    """Pull every API-backed index that has credentials. Returns one status
    line per source; never raises."""
    msgs: list[str] = []
    today = date.today()

    from .. import billdotcom_api, paychex_api, ramp_api

    if not ramp_api.credentials_present():
        msgs.append("Ramp: no credentials")
    elif _is_fresh("ramp_reimbursements.json"):
        msgs.append("Ramp: fresh (skipped)")
    else:
        try:
            idx = ramp_api.fetch_index(today - timedelta(days=365), today)
            _write("ramp_reimbursements.json", idx)
            coded = sum(1 for r in idx if r.get("project"))
            msgs.append(f"Ramp: {len(idx)} reimbursements ({coded} with project memos)")
        except Exception as exc:
            msgs.append(f"Ramp: FAILED — {str(exc)[:120]}")

    if not billdotcom_api.credentials_present():
        msgs.append("Bill.com: no credentials")
    elif _is_fresh("billdotcom_payments.json"):
        msgs.append("Bill.com: fresh (skipped)")
    else:
        try:
            idx = billdotcom_api.fetch_index()
            _write("billdotcom_payments.json", idx)
            msgs.append(f"Bill.com: {len(idx)} payments")
        except Exception as exc:
            msgs.append(f"Bill.com: FAILED — {str(exc)[:120]}")

    if billdotcom_api.credentials_present() and not _is_fresh("billdotcom_master.json"):
        try:
            master = billdotcom_api.fetch_master_index()
            _write("billdotcom_master.json", master)
            note = f"{len(master['vendors'])} vendors, {len(master['bills'])} bills"
            if master.get("gaps"):
                note += f" ({len(master['gaps'])} listings blocked)"
            msgs.append(f"Bill.com master: {note}")
        except Exception as exc:
            msgs.append(f"Bill.com master: FAILED — {str(exc)[:120]}")
    elif billdotcom_api.credentials_present():
        msgs.append("Bill.com master: fresh (skipped)")

    from ..recon import sage_xml
    if not sage_xml.credentials_present():
        msgs.append("Sage POs: no credentials")
    elif _is_fresh("sage_pos.json"):
        msgs.append("Sage POs: fresh (skipped)")
    else:
        try:
            pos = sage_xml.fetch_pos(today - timedelta(days=365), today)
            _write("sage_pos.json", pos)
            msgs.append(f"Sage POs: {len(pos)}")
        except Exception as exc:
            msgs.append(f"Sage POs: FAILED — {str(exc)[:120]}")

    if not paychex_api.credentials_present():
        msgs.append("Paychex: no credentials")
    elif _is_fresh("timecards_index.json"):
        msgs.append("Timecards: fresh (skipped)")
    else:
        try:
            idx = paychex_api.fetch_index(today - timedelta(days=365), today)
            _write("timecards_index.json", idx)
            msgs.append(f"Timecards: {len(idx)} people via Paychex")
        except Exception as exc:
            msgs.append(f"Paychex: FAILED — {str(exc)[:120]} (CSV upload still works)")

    return msgs
