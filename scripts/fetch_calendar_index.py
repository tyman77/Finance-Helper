#!/usr/bin/env python3
"""Fetch each traveler's individual calendar into data/calendar_index.json.

The "everyone else" project path reads each non-installer's OWN calendar (not the
shared Sales Travel calendar) for trip context around their travel dates. This
writes:

    data/calendar_index.json  -> {"<calendar id>": [ {summary,start,end,location,
                                    all_day,external}, ... ], ...}
    data/roster.json          -> {"<Person Name>": "<calendar id>"}  (person -> calendar)

`roster.json` maps the traveler (as named in the United history) to their calendar
id, since work emails/calendars aren't a clean formula (andrew@ vs jclark@).

Requires a Google service account with read access to each calendar. Set:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
Provide the roster as data/roster.json (person -> calendar id); this script reads
its calendar ids from there.

Usage:
    python scripts/fetch_calendar_index.py 2026-05-01 2026-07-15
"""

from __future__ import annotations

import json
import os
import sys

_INTERNAL_DOMAIN = "summitintegrated.com"


def _external_domains(ev: dict) -> list:
    domains = []
    for att in ev.get("attendees", []):
        email = att.get("email", "")
        if "@" in email:
            dom = email.split("@", 1)[1].lower()
            if dom != _INTERNAL_DOMAIN and dom not in domains:
                domains.append(dom)
    return domains


def fetch_calendar(svc, calendar_id: str, start: str, end: str) -> list:
    events, page_token = [], None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{start}T00:00:00Z",
                timeMax=f"{end}T00:00:00Z",
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            start_d = ev.get("start", {})
            end_d = ev.get("end", {})
            domains = _external_domains(ev)
            events.append(
                {
                    "summary": ev.get("summary", ""),
                    "start": start_d.get("date") or start_d.get("dateTime", "")[:10],
                    "end": end_d.get("date") or end_d.get("dateTime", "")[:10],
                    "location": ev.get("location", ""),
                    "all_day": "date" in start_d,
                    "external": bool(domains),
                    "domains": domains,
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            return events


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    from google.oauth2 import service_account            # type: ignore
    from googleapiclient.discovery import build          # type: ignore

    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    svc = build("calendar", "v3", credentials=creds)

    with open("data/roster.json", encoding="utf-8") as fh:
        roster = json.load(fh)

    index = {}
    for cal_id in sorted(set(roster.values())):
        index[cal_id] = fetch_calendar(svc, cal_id, argv[0], argv[1])
        print(f"  {cal_id}: {len(index[cal_id])} events")

    with open("data/calendar_index.json", "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    print(f"Wrote {len(index)} calendars to data/calendar_index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
