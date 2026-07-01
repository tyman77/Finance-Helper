#!/usr/bin/env python3
"""Fetch the Sage Intacct Projects list into data/sage_projects.json.

Used to exclude archived projects everywhere this tool suggests or auto-codes
a project number (the client->project registry, the web UI's project
autocomplete, and the calendar/registry auto-coding in project_resolver.py).

Auth: OAuth2 client-credentials grant (the server-to-server flow — no
interactive browser login). You need, from Sage Intacct:
  - a Client ID + Secret from an app registered at https://developer.sage.com/intacct/
  - a Web Services User authorized for that app (Company Console -> Web
    Services -> Subscribe/authorize the app), plus that user's password
  - your Company ID

Set as environment variables (see .env.example):
    INTACCT_CLIENT_ID
    INTACCT_CLIENT_SECRET
    INTACCT_COMPANY_ID
    INTACCT_USER_ID
    INTACCT_USER_PASSWORD

Usage:
    python scripts/fetch_sage_projects.py

IMPORTANT — this is a first pass built from Sage's public docs, not a
confirmed-working integration: the exact endpoint path and the field name
Sage uses for a project's active/archived status were not independently
verifiable while writing this (the primary docs blocked automated fetches).
The script always prints the FIRST raw record before writing anything, so you
can eyeball whether "status"-like fields look right. If the token request or
the projects request fails, or the printed fields don't obviously show an
active/archived indicator, paste the output back — the fix is a small,
targeted edit to _TOKEN_URL / _PROJECTS_URL / _STATUS_FIELD_CANDIDATES below,
not a rewrite.
"""

from __future__ import annotations

import json
import os
import sys

_TOKEN_URL = "https://api.intacct.com/ia/api/v1/oauth2/token"
# Best-guess REST endpoint for listing PROJECT records. Override with
# INTACCT_PROJECTS_URL if this turns out to be wrong.
_PROJECTS_URL = os.environ.get(
    "INTACCT_PROJECTS_URL", "https://api.intacct.com/ia/api/v1/objects/project"
)
# Sage Intacct's convention elsewhere in this company's data is a "Status"
# field with the exact string "Active" for active records (confirmed from
# the GL chart-of-accounts export) — tried in order until one is present.
_STATUS_FIELD_CANDIDATES = ("status", "STATUS", "projectStatus", "PROJECTSTATUS")
_ID_FIELD_CANDIDATES = ("projectId", "PROJECTID", "id", "key")
_NAME_FIELD_CANDIDATES = ("name", "NAME", "projectName", "PROJECTNAME")


def _get(record: dict, candidates: tuple) -> str | None:
    for key in candidates:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def get_token() -> str:
    import requests

    client_id = os.environ["INTACCT_CLIENT_ID"]
    client_secret = os.environ["INTACCT_CLIENT_SECRET"]

    # Confirmed live (2026-07-01, via destinations/sage_intacct.py): despite
    # being a "client_credentials" grant, Sage's token endpoint also requires
    # the Web Services User identifying itself in the body (400 "Either
    # username or session_id is required" without it) — client_id/secret
    # identify the app, username/password identify the authorized user.
    data = {"grant_type": "client_credentials"}
    if os.environ.get("INTACCT_USER_ID"):
        data["username"] = os.environ["INTACCT_USER_ID"]
    if os.environ.get("INTACCT_USER_PASSWORD"):
        data["password"] = os.environ["INTACCT_USER_PASSWORD"]

    resp = requests.post(
        _TOKEN_URL,
        auth=(client_id, client_secret),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token request failed: HTTP {resp.status_code}\n{resp.text[:1000]}\n\n"
            "If this looks like a 'grant type not supported' or 'invalid client' "
            "error, the client-credentials app registration may need the Web "
            "Services User authorized in Company Console -> Web Services first."
        )
    return resp.json()["access_token"]


def fetch_projects(token: str) -> list[dict]:
    import requests

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Multi-entity Sage companies may need the entity/company identified
        # explicitly; harmless to send even for single-entity companies.
        "company-id": os.environ.get("INTACCT_COMPANY_ID", ""),
    }
    records: list[dict] = []
    offset = 0
    page_size = 100
    while True:
        resp = requests.get(
            _PROJECTS_URL, headers=headers,
            params={"limit": page_size, "offset": offset}, timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Projects request failed: HTTP {resp.status_code}\n{resp.text[:1000]}")
        data = resp.json()
        # Try the common REST list-envelope shapes.
        batch = data.get("data") or data.get("items") or data.get("ia::result") or data
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        records.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return records


def main(argv):
    token = get_token()
    print("Authenticated OK.")
    records = fetch_projects(token)
    print(f"Fetched {len(records)} project records.")
    if not records:
        print("No records returned — nothing written.")
        return 1

    print("\nFirst raw record (eyeball this for the real field names):")
    print(json.dumps(records[0], indent=2)[:2000])

    out = {}
    missing_status = 0
    for rec in records:
        pid = _get(rec, _ID_FIELD_CANDIDATES)
        if not pid:
            continue
        status = _get(rec, _STATUS_FIELD_CANDIDATES)
        if status is None:
            missing_status += 1
        out[str(pid)] = {
            "name": _get(rec, _NAME_FIELD_CANDIDATES) or "",
            "status": status,
            "raw": rec,
        }

    os.makedirs("data", exist_ok=True)
    with open("data/sage_projects.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)

    active = sum(1 for v in out.values() if (v["status"] or "").lower() == "active")
    print(f"\nWrote {len(out)} projects to data/sage_projects.json "
          f"({active} active, {len(out) - active} not-active-or-unknown)")
    if missing_status:
        print(f"WARNING: {missing_status} records had no recognizable status field — "
              "paste the raw record above back to fix _STATUS_FIELD_CANDIDATES.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
