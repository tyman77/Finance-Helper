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

import re
from collections import Counter
from datetime import date, timedelta


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

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


_LEADING_CODE = re.compile(r"^\s*(\d{3,5})\b")
_YEAR_RANGE = re.compile(r"^\s*\d{4}\s*[–—-]\s*\d{4}\b")  # "2025–2026 ..." (not a code)


def extract_leading_codes(summary: str) -> list[str]:
    """Pull explicit project code(s) from a calendar event title.

    The team's convention prefixes trip-related events with the project number
    — "4804 - Echo Church - Ready to Finish", "5084 | Westfield Sync up", or a
    bare "4798". This is a stronger, more literal signal than fuzzy client-name
    matching, so it's checked first and isn't subject to the internal-noise
    filter (nobody titles an internal meeting "4804 Lunch").

    Returns [] for a "2025–2026 ..." year-range false positive, and two codes
    for an explicit multi-project title like "4471/3831 ...".
    """
    s = (summary or "").strip()
    if _YEAR_RANGE.match(s):
        return []
    m = _LEADING_CODE.match(s)
    if not m:
        return []
    codes = [m.group(1)]
    m2 = re.match(rf"^\s*{re.escape(m.group(1))}\s*/\s*(\d{{3,5}})\b", s)
    if m2:
        codes.append(m2.group(1))
    return codes


def match_project(events: list, registry: dict) -> dict | None:
    """Match calendar events to a project code via client name or client domain."""
    index = registry.get("index", {})
    reg = registry.get("registry", {})
    codes: set = set()
    for ev in events:
        text = _normalize(ev.get("summary", ""))
        haystacks = [text] + [_normalize(d) for d in ev.get("domains", [])]
        for key, key_codes in index.items():
            if any(key in h or h in key for h in haystacks if h):
                codes.update(key_codes)
    if not codes:
        return None
    if len(codes) == 1:
        code = next(iter(codes))
        client = reg.get(code, {}).get("client", "")
        return {"project": code, "account": "52200",
                "note": f"registry: {client} project {code} -> 52200 COGS"}
    return {"candidates": sorted(codes),
            "note": "registry: candidate projects " + ", ".join(sorted(codes)) + " — pick one"}


def resolve_calendar(owner_key: str, dep: date, calendar_index: dict, registry: dict | None = None) -> dict | None:
    """Surface trip-relevant events from the traveler's OWN calendar.

    Returns review context (which client / where). If a client->project registry
    is supplied and a single project matches, also returns project + 52200 so the
    trip auto-codes; multiple matches are surfaced as candidates for review.
    """
    events = calendar_index.get(owner_key) or []
    window = []
    for ev in events:
        start = date.fromisoformat(ev["start"][:10])
        end = date.fromisoformat(ev["end"][:10])
        if start - timedelta(days=1) <= dep <= end + timedelta(days=STAY_WINDOW_DAYS):
            window.append(ev)
    if not window:
        return None

    # Strongest signal first: an explicit project code prefix on the title,
    # checked against every event in the window (bypasses the noise filter).
    title_codes: set = set()
    title_hits = []
    for ev in window:
        codes = extract_leading_codes(ev.get("summary", ""))
        if codes:
            title_codes.update(codes)
            title_hits.append(ev)
    if len(title_codes) == 1:
        code = next(iter(title_codes))
        return {
            "source": "calendar", "project": code, "account": "52200",
            "events": [h.get("summary", "") for h in title_hits],
            "note": f"calendar title code {code} -> 52200 COGS",
        }
    if len(title_codes) > 1:
        return {
            "source": "calendar", "candidates": sorted(title_codes),
            "events": [h.get("summary", "") for h in title_hits],
            "note": "calendar title codes " + ", ".join(sorted(title_codes)) + " — pick one",
        }

    hits = [ev for ev in window if _travel_relevant(ev)]
    if not hits:
        return None

    parts = []
    for ev in hits[:2]:
        p = ev.get("summary", "")
        if ev.get("location") and _looks_physical(ev["location"]):
            p += f" @ {ev['location']}"
        parts.append(p)
    result = {
        "source": "calendar",
        "events": [h.get("summary", "") for h in hits],
        "note": "calendar context — " + "; ".join(parts),
    }

    matched = match_project(hits, registry) if registry else None
    if matched and matched.get("project"):
        result["project"] = matched["project"]
        result["account"] = matched["account"]
        result["note"] += "; " + matched["note"]
    elif matched:
        result["note"] += "; " + matched["note"]
    else:
        result["note"] += " — confirm client/account"
    return result
