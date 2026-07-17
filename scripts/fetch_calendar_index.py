#!/usr/bin/env python3
"""Fetch each traveler's individual calendar into data/calendar_index.json.

Reads each non-installer's OWN calendar for trip context around their travel
dates, via the Calendar REST API with a service account (google-auth + requests).

    data/calendar_index.json -> {"<calendar id>": [ {summary,start,end,location,
                                  all_day,external,domains}, ... ], ...}
    data/roster.json         -> {"<Person Name>": "<calendar id>"}  (input)

Access model (set env):
  - Share each calendar with the service account  -> leave USE_DWD unset.
  - Domain-wide delegation                         -> set USE_DWD=1; the service
    account impersonates each calendar owner (roster value = the owner email).

Credentials, via finance_helper.google_auth:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json, or
    GOOGLE_SERVICE_ACCOUNT_JSON=<the key content>   # for hosts with no local file
    USE_DWD=1            # optional; requires domain-wide delegation authorized

Usage:
    python scripts/fetch_calendar_index.py 2026-05-01 2026-07-15
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

_INTERNAL_DOMAIN = "summitintegrated.com"
_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _session(subject: str | None = None):
    from finance_helper import google_auth
    return google_auth.session(_SCOPES, subject=subject)


def _external_domains(ev: dict) -> list:
    domains = []
    for att in ev.get("attendees", []):
        email = att.get("email", "")
        if "@" in email:
            dom = email.split("@", 1)[1].lower()
            if dom != _INTERNAL_DOMAIN and dom not in domains:
                domains.append(dom)
    return domains


def fetch_calendar(cal_id: str, start: str, end: str, subject: str | None) -> list:
    sess = _session(subject)
    events, page_token = [], None
    base = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal_id)}/events"
    while True:
        params = {
            "timeMin": f"{start}T00:00:00Z", "timeMax": f"{end}T00:00:00Z",
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = sess.get(base, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for ev in data.get("items", []):
            s, e = ev.get("start", {}), ev.get("end", {})
            domains = _external_domains(ev)
            events.append({
                "summary": ev.get("summary", ""),
                "start": s.get("date") or s.get("dateTime", "")[:10],
                "end": e.get("date") or e.get("dateTime", "")[:10],
                "location": ev.get("location", ""),
                "all_day": "date" in s,
                "external": bool(domains),
                "domains": domains,
            })
        page_token = data.get("nextPageToken")
        if not page_token:
            return events


def fetch_all(roster: dict, start: str, end: str, use_dwd: bool, checkpoint_path: str | None = None):
    """Fetch every calendar in the roster; returns (index, skipped_ids). Writes
    a checkpoint after each calendar (if checkpoint_path is given) so a
    timeout partway through doesn't lose the calendars already fetched."""
    ids = sorted(set(roster.values()))
    index: dict = {}
    skipped: list[str] = []
    for i, cal_id in enumerate(ids, 1):
        subject = cal_id if use_dwd else None
        try:
            index[cal_id] = fetch_calendar(cal_id, start, end, subject)
            print(f"  [{i}/{len(ids)}] {cal_id}: {len(index[cal_id])} events", flush=True)
        except Exception as exc:  # keep going; report the calendars we couldn't read
            skipped.append(cal_id)
            print(f"  [{i}/{len(ids)}] {cal_id}: SKIPPED ({type(exc).__name__})", flush=True)
        if checkpoint_path:
            with open(checkpoint_path, "w", encoding="utf-8") as fh:
                json.dump(index, fh, indent=2)
    return index, skipped


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    data_dir = os.environ.get("FINANCE_HELPER_DATA", "data")
    with open(os.path.join(data_dir, "roster.json"), encoding="utf-8") as fh:
        roster = json.load(fh)
    use_dwd = bool(os.environ.get("USE_DWD"))

    out = os.path.join(data_dir, "calendar_index.json")
    index, skipped = fetch_all(roster, argv[0], argv[1], use_dwd, checkpoint_path=out)
    print(f"Wrote {len(index)} calendars to {out} ({len(skipped)} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
