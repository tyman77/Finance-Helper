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


# Recurring/internal events on personal calendars that never indicate a trip.
_INTERNAL_STOP = (
    "email", "slack", "lunch", "focus time", "deep work", "planning", "check in",
    "checkin", "1:1", "one on one", "stand up", "standup", "followup", "follow up",
    "pto", "ooo", "out of office", "holiday", "birthday",
)


def _looks_physical(location: str) -> bool:
    if not location:
        return False
    low = location.lower()
    return not any(x in low for x in ("http", "zoom", "teams", "meet.google", "webex"))


def _travel_relevant(ev: dict) -> bool:
    summary = (ev.get("summary") or "").lower()
    if not summary or any(w in summary for w in _INTERNAL_STOP):
        return False
    # A physical location, an all-day multi-day block, or external (client)
    # attendees are the signals that an event is an actual trip/engagement.
    return _looks_physical(ev.get("location", "")) or ev.get("all_day") or ev.get("external")


def resolve_calendar(owner_key: str, dep: date, calendar_index: dict) -> dict | None:
    """Surface trip-relevant events from the traveler's OWN calendar as context.

    Personal calendars carry client names, not project codes, so this returns
    review context (which client / where) rather than an account. The reviewer
    codes it; a client->project registry can automate this later.
    """
    events = calendar_index.get(owner_key) or []
    hits = []
    for ev in events:
        start = date.fromisoformat(ev["start"][:10])
        end = date.fromisoformat(ev["end"][:10])
        if start - timedelta(days=1) <= dep <= end + timedelta(days=STAY_WINDOW_DAYS) and _travel_relevant(ev):
            hits.append(ev)
    if not hits:
        return None
    parts = []
    for ev in hits[:2]:
        p = ev.get("summary", "")
        if ev.get("location") and _looks_physical(ev["location"]):
            p += f" @ {ev['location']}"
        parts.append(p)
    return {
        "source": "calendar",
        "events": [h.get("summary", "") for h in hits],
        "note": "calendar context — " + "; ".join(parts) + " — confirm client/account",
    }
