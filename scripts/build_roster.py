#!/usr/bin/env python3
"""Auto-draft the person -> calendar roster for review.

Maps each traveler (from data/united_travelers.yml) to a calendar id:
  1. the work-email convention (first initial + surname @summitintegrated.com), then
  2. overriding with any first-name vanity calendars found in the account's
     calendar list (e.g. andrew@, deron@) matched by surname/first name.

Emits data/roster.json plus a review table flagging low-confidence rows. Feed the
calendar list as JSON: [{"id","summary"}, ...] (from Google Calendar list) on
argv[1], or rely on the built-in known vanity overrides.

Usage:
    python scripts/build_roster.py [calendar_list.json]
"""

from __future__ import annotations

import json
import os
import sys

import yaml

_DOMAIN = "summitintegrated.com"
# Confirmed first-name vanity calendars (calendar id -> surname to match).
_VANITY = {
    "andrew@summitintegrated.com": "Starke",
    "deron@summitintegrated.com": "Yevoli",
    "tyson@summitintegrated.com": "Wiens",
}


def _email(person: str) -> str | None:
    parts = person.split()
    if len(parts) < 2:
        return None
    return f"{parts[0][0]}{parts[-1]}".lower() + "@" + _DOMAIN


def build(travelers: dict) -> tuple[dict, list]:
    surname_to_vanity = {v.lower(): k for k, v in _VANITY.items()}
    roster, review = {}, []
    seen = set()
    for entry in travelers.values():
        person = (entry.get("person") or "").strip()
        if not person or person in seen:
            continue
        seen.add(person)
        surname = person.split()[-1].lower()
        if surname in surname_to_vanity:
            roster[person] = surname_to_vanity[surname]
            conf = "vanity"
        else:
            email = _email(person)
            if not email:
                continue
            roster[person] = email
            conf = "convention"
        review.append(f"  [{conf:10}] {person:24} -> {roster[person]}")
    return roster, review


def main(argv):
    tmap_path = os.path.join("data", "united_travelers.yml")
    with open(tmap_path, encoding="utf-8") as fh:
        travelers = yaml.safe_load(fh) or {}
    roster, review = build(travelers)
    with open(os.path.join("data", "roster.json"), "w", encoding="utf-8") as fh:
        json.dump(roster, fh, indent=2)
    print("\n".join(review))
    print(f"\nWrote {len(roster)} people to data/roster.json — review the 'convention' rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
