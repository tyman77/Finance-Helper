#!/usr/bin/env python3
"""Fetch the Summit Sales Travel calendar into data/calendar_index.json.

This is the production fetch step for the "everyone else" project path. It reads
the shared travel calendar over a date range and writes the compact index that
enrich.py consumes:

    [{"creator", "summary", "start", "end", "location"}, ...]

Requires a Google service account / OAuth client with read access to the
calendar. Set:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
    SALES_TRAVEL_CALENDAR_ID=c_57a2ab77...@group.calendar.google.com

Usage:
    python scripts/fetch_calendar_index.py 2026-05-01 2026-07-15

Note: inside a Claude session the calendar can be pulled via the Google Calendar
MCP tool and written to data/calendar_index.json directly; this script is the
standalone/cron equivalent using the google-api-python-client.
"""

from __future__ import annotations

import json
import os
import sys


def fetch(calendar_id: str, start: str, end: str) -> list:
    # Deferred imports so the repo doesn't hard-depend on Google libs.
    from google.oauth2 import service_account            # type: ignore
    from googleapiclient.discovery import build          # type: ignore

    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    svc = build("calendar", "v3", credentials=creds)
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
            events.append(
                {
                    "creator": ev.get("creator", {}).get("email", ""),
                    "summary": ev.get("summary", ""),
                    "start": ev.get("start", {}).get("date")
                    or ev.get("start", {}).get("dateTime", "")[:10],
                    "end": ev.get("end", {}).get("date")
                    or ev.get("end", {}).get("dateTime", "")[:10],
                    "location": ev.get("location", ""),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            return events


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    calendar_id = os.environ.get("SALES_TRAVEL_CALENDAR_ID", "")
    events = fetch(calendar_id, argv[0], argv[1])
    os.makedirs("data", exist_ok=True)
    with open("data/calendar_index.json", "w", encoding="utf-8") as fh:
        json.dump(events, fh, indent=2)
    print(f"Wrote {len(events)} events to data/calendar_index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
