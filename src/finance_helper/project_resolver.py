"""Resolve the project / account for a United trip from two sources.

Per the agreed design:
  - INSTALLERS  -> the crew-schedule grid: person + travel dates -> project code
                   -> 52200 COGS Travel, with the project as an Intacct dimension.
  - EVERYONE ELSE -> the Summit Sales Travel calendar: the trip purpose + city
                     around the departure date -> an overhead account, flagged so
                     a human confirms (calendar events give context, not a code).
  - Nothing found -> caller falls back to the traveler's usual account + review.

Both indices are plain data (see scripts/fetch_*.py); this module is pure logic
so it is easy to test.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

STAY_WINDOW_DAYS = 12  # departure through the likely length of a trip

# Calendar trip purpose -> overhead GL account. Always review-flagged: the
# calendar tells us why/where, the human confirms new-vs-existing client etc.
_TRIP_ACCOUNT = [
    (("summithq", "hq", "denver"), "71000"),                    # OH - Travel
    (("conference", "training", "storybrand", "thrive"), "64000"),  # Personal Development
    (("cmn", "multiply", "event"), "73060"),                    # Marketing: Events
    (("first use",), "64301"),                                  # OH Sales: Existing Client
    (("programming", "discovery", "sales", "client"), "64302"),  # OH Sales: New Client
]


def email_for(passenger: str, domain: str = "summitintegrated.com") -> str | None:
    """"CLARK/JOHN" -> "jclark@summitintegrated.com" (first initial + surname)."""
    parts = passenger.split("/")
    if len(parts) < 2:
        return None
    surname = parts[0].strip().split()[0] if parts[0].strip() else ""
    first = parts[1].strip()
    if not surname or not first:
        return None
    return f"{first[0]}{surname}".lower() + "@" + domain


def resolve_schedule(person: str, dep: date, schedule_index: dict) -> dict | None:
    """Most-common numeric project code the installer works during the stay."""
    rows = schedule_index.get(person)
    if not rows:
        return None
    codes: Counter = Counter()
    for i in range(STAY_WINDOW_DAYS + 1):
        cell = (rows.get((dep + timedelta(days=i)).isoformat()) or "").strip()
        if cell.isdigit():
            codes[cell] += 1
    if not codes:
        return None
    project = codes.most_common(1)[0][0]
    return {
        "project": project,
        "account": "52200",
        "source": "schedule",
        "note": f"crew schedule: project {project} during stay -> 52200 COGS",
    }


def _account_for_trip(trip: str) -> str | None:
    t = trip.lower()
    for keys, acct in _TRIP_ACCOUNT:
        if any(k in t for k in keys):
            return acct
    return None


def resolve_calendar(passenger: str, dep: date, calendar_index: list) -> dict | None:
    """Match the traveler's calendar event covering the departure date."""
    email = email_for(passenger)
    if not email:
        return None
    match = None
    for ev in calendar_index:
        start = date.fromisoformat(ev["start"])
        end = date.fromisoformat(ev["end"])  # Google all-day end is exclusive
        if not (start - timedelta(days=1) <= dep < end + timedelta(days=1)):
            continue
        if ev.get("creator", "").lower() == email:
            match = ev
            break
    if not match:
        return None
    summary = match.get("summary", "")
    trip = summary.split("|", 1)[1].strip() if "|" in summary else summary
    return {
        "account": _account_for_trip(trip),
        "source": "calendar",
        "note": f"calendar: {summary!r}"
        + (f" @ {match['location']}" if match.get("location") else "")
        + " — confirm account",
    }
