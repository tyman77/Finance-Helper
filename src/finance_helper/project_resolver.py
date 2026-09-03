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

import json
import os
import re
from collections import Counter
from datetime import date, timedelta


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def load_active_projects(path: str | None = None) -> set[str] | None:
    """Project codes NOT archived in Sage, from data/sage_projects.json
    (scripts/fetch_sage_projects.py). Returns None — meaning "skip filtering
    entirely" — if that file doesn't exist, matching the rest of this
    project's "no data -> don't block on it" convention, rather than treating
    an absent file as "everything is archived"."""
    if path is None:
        data_dir = os.environ.get(
            "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data")
        )
        path = os.path.join(data_dir, "sage_projects.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # The index is keyed by Intacct PROJECTID (P000635) but the coding
    # pipeline filters by JOB NUMBER (5368), which lives inside the project
    # NAME ("Emmaus Church | Building Expansion | 5368 |"). An active set of
    # P-numbers alone filtered EVERY candidate to nothing — include both the
    # ids and the numeric tokens from each active project's name.
    active: set[str] = set()
    for code, info in data.items():
        if (info.get("status") or "").lower() != "active":
            continue
        active.add(code)
        active.update(re.findall(r"\b\d{3,5}\b", str(info.get("name") or "")))
    return active


def _filter_active(codes: set[str], active_projects: set[str] | None) -> set[str]:
    if active_projects is None:
        return codes
    return {c for c in codes if c in active_projects}

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


def resolve_schedule(
    person: str, dep: date, schedule_index: dict, active_projects: set[str] | None = None
) -> dict | None:
    """Most-common numeric project code the installer works during the stay,
    skipping any code that's archived in Sage (a stale sheet entry)."""
    rows = schedule_index.get(person)
    if not rows:
        return None
    codes: Counter = Counter()
    for i in range(STAY_WINDOW_DAYS + 1):
        cell = (rows.get((dep + timedelta(days=i)).isoformat()) or "").strip()
        if cell.isdigit() and (active_projects is None or cell in active_projects):
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


def hotel_projects_in_window(
    hotel_index: list, dep: date, window_days: int = 3,
    department: str | None = None, active_projects: set[str] | None = None,
) -> list[str]:
    """Project codes that had a Hotel Engine booking overlapping this trip.

    A United flight and the hotel for the same trip fall on nearly the same
    dates, so a booking whose [start, end] window overlaps [dep-1, dep+N] is a
    strong same-trip signal for its project — even though Hotel Engine data
    carries no traveler name (we match on dates, and optionally department, not
    person). Returns codes most-recent/earliest-overlap first, de-duplicated.
    """
    lo = dep - timedelta(days=1)
    hi = dep + timedelta(days=window_days)
    seen: set = set()
    out: list[str] = []
    for b in hotel_index:
        try:
            start = date.fromisoformat(b["start"])
            end = date.fromisoformat(b.get("end") or b["start"])
        except (KeyError, ValueError):
            continue
        if start > hi or end < lo:
            continue  # no overlap with the trip window
        if department and b.get("department") and b["department"] != department:
            continue
        code = b.get("project")
        if not code or code in seen:
            continue
        if active_projects is not None and code not in active_projects:
            continue
        seen.add(code)
        out.append(code)
    return out


def same_person(a: str, b: str) -> bool:
    """Do two name strings plausibly name the same human?

    Real data never matches exactly: the traveler map says "Jake Cody", the
    hotel guest column says "Jacob Lee Cody" or "Cody, Jake". Tokens are
    compared: exact set match, one name containing the other (middle names),
    or same surname + same first initial (nicknames: Jake/Jacob).
    """
    def toks(name):
        parts = [t for t in re.split(r"[^a-z]+", (name or "").lower()) if t]
        return parts

    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return False
    sa, sb = set(ta), set(tb)
    if sa == sb or sa <= sb or sb <= sa:
        return True

    def surname_first(parts, raw):
        # "Cody, Jake" puts the surname first; otherwise it's last.
        if "," in (raw or ""):
            return parts[0], (parts[1] if len(parts) > 1 else "")
        return parts[-1], parts[0]

    surn_a, first_a = surname_first(ta, a)
    surn_b, first_b = surname_first(tb, b)
    return bool(surn_a and surn_a == surn_b and first_a and first_b
                and first_a[0] == first_b[0])


def person_has_any_stay(hotel_index: list, person: str) -> bool:
    return any(any(same_person(person, g) for g in (b.get("guests") or []))
               for b in hotel_index)


def person_stays_in_window(hotel_index: list, person: str, dep: date,
                           window_days: int = 3) -> list[dict]:
    """Stays naming this person that overlap the trip — coded or not
    (uncoded stays can't tag a flight but explain why nothing did)."""
    lo = dep - timedelta(days=1)
    hi = dep + timedelta(days=window_days)
    out = []
    for b in hotel_index:
        if not any(same_person(person, g) for g in (b.get("guests") or [])):
            continue
        try:
            start = date.fromisoformat(b["start"])
            end = date.fromisoformat(b.get("end") or b["start"])
        except (KeyError, ValueError):
            continue
        if start <= hi and end >= lo:
            out.append(b)
    return out


def hotel_projects_for_person(
    hotel_index: list, person: str, dep: date, window_days: int = 3,
    active_projects: set[str] | None = None,
) -> list[str]:
    """Project codes of hotel stays that NAME this traveler and overlap the trip.

    Statement guest columns give person-level bookings; a stay that names the
    flyer and overlaps the departure is the strongest no-Google project signal
    there is — much stronger than the date+department heuristic in
    hotel_projects_in_window, so callers may auto-fill on a unique hit.
    """
    if not (person or "").strip():
        return []
    lo = dep - timedelta(days=1)
    hi = dep + timedelta(days=window_days)
    seen: set = set()
    out: list[str] = []
    for b in hotel_index:
        if not any(same_person(person, g) for g in (b.get("guests") or [])):
            continue
        try:
            start = date.fromisoformat(b["start"])
            end = date.fromisoformat(b.get("end") or b["start"])
        except (KeyError, ValueError):
            continue
        if start > hi or end < lo:
            continue
        code = b.get("project")
        if not code or code in seen:
            continue
        if active_projects is not None and code not in active_projects:
            continue
        seen.add(code)
        out.append(code)
    return out


def ramp_matches_for_person(
    ramp_index: list, person: str, dep: date,
    days_before: int = 5, days_after: int = 21,
    active_projects: set[str] | None = None,
) -> tuple[list[str], list[dict]]:
    """(project codes from memos, all matching reimbursements) for a trip.

    Per-diem usually pays out during or shortly after travel, so the window
    is asymmetric: a few days before departure through ~3 weeks after. Even a
    code-less reimbursement corroborates that the person traveled then.
    """
    lo = dep - timedelta(days=days_before)
    hi = dep + timedelta(days=days_after)
    codes: list[str] = []
    hits: list[dict] = []
    for r in ramp_index:
        if not same_person(person, r.get("person", "")):
            continue
        try:
            when = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        if not (lo <= when <= hi):
            continue
        hits.append(r)
        code = r.get("project")
        if code and code not in codes and (active_projects is None or code in active_projects):
            codes.append(code)
    return codes, hits


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


def match_project(events: list, registry: dict, active_projects: set[str] | None = None) -> dict | None:
    """Match calendar events to a project code via client name or client domain.

    Archived projects (per active_projects, if supplied) are dropped before
    the single-vs-multiple decision — an archived code never gets suggested,
    and a client with one still-active project among several old ones now
    resolves cleanly instead of surfacing dead codes as "candidates".
    """
    index = registry.get("index", {})
    reg = registry.get("registry", {})
    codes: set = set()
    for ev in events:
        text = _normalize(ev.get("summary", ""))
        haystacks = [text] + [_normalize(d) for d in ev.get("domains", [])]
        for key, key_codes in index.items():
            if any(key in h or h in key for h in haystacks if h):
                codes.update(key_codes)
    codes = _filter_active(codes, active_projects)
    if not codes:
        return None
    if len(codes) == 1:
        code = next(iter(codes))
        client = reg.get(code, {}).get("client", "")
        return {"project": code, "account": "52200",
                "note": f"registry: {client} project {code} -> 52200 COGS"}
    return {"candidates": sorted(codes),
            "note": "registry: candidate projects " + ", ".join(sorted(codes)) + " — pick one"}


def resolve_calendar(
    owner_key: str, dep: date, calendar_index: dict, registry: dict | None = None,
    active_projects: set[str] | None = None,
) -> dict | None:
    """Surface trip-relevant events from the traveler's OWN calendar.

    Returns review context (which client / where). If a client->project registry
    is supplied and a single project matches, also returns project + 52200 so the
    trip auto-codes; multiple matches are surfaced as candidates for review.
    Archived projects (per active_projects) are never returned as a match.
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
        codes = set(extract_leading_codes(ev.get("summary", "")))
        codes = _filter_active(codes, active_projects)
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

    matched = match_project(hits, registry, active_projects) if registry else None
    if matched and matched.get("project"):
        result["project"] = matched["project"]
        result["account"] = matched["account"]
        result["note"] += "; " + matched["note"]
    elif matched:
        result["note"] += "; " + matched["note"]
    else:
        result["note"] += " — confirm client/account"
    return result
