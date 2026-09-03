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


def fetch_directory() -> dict:
    """Real {full name (lowercased): primary email} from the Google Workspace
    directory, so the roster carries confirmed addresses instead of guessed
    ones (the convention guess produced e.g. imungia@ vs the real imunguia@).

    Needs the service account authorized for domain-wide delegation and
    GOOGLE_ADMIN_SUBJECT set to a Workspace admin's email to impersonate.
    Returns {} when that isn't configured, so callers can fall back."""
    subject = os.environ.get("GOOGLE_ADMIN_SUBJECT")
    has_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or \
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not subject or not has_key:
        return {}
    from finance_helper import google_auth

    sess = google_auth.session(
        ["https://www.googleapis.com/auth/admin.directory.user.readonly"],
        subject=subject)
    users, page = [], None
    while True:
        params = {"customer": "my_customer", "maxResults": 500}
        if page:
            params["pageToken"] = page
        resp = sess.get("https://admin.googleapis.com/admin/directory/v1/users",
                        params=params, timeout=60)
        if resp.status_code != 200:
            # Google's error body names the actual problem (e.g. "Admin SDK API
            # has not been used in project ... or it is disabled") — surface it.
            try:
                detail = (resp.json().get("error") or {}).get("message") or ""
            except ValueError:
                detail = resp.text[:300]
            raise RuntimeError(f"Directory API HTTP {resp.status_code}: {detail}")
        data = resp.json()
        users.extend(data.get("users") or [])
        page = data.get("nextPageToken")
        if not page:
            break
    directory: dict[str, str] = {}
    for u in users:
        if u.get("suspended"):
            continue
        email = (u.get("primaryEmail") or "").strip().lower()
        name = u.get("name") or {}
        first = (name.get("givenName") or "").strip()
        last = (name.get("familyName") or "").strip()
        keys = [(name.get("fullName") or "").strip()]
        if first and last:
            keys.append(f"{first} {last}")
        for key in keys:
            if key and email:
                directory.setdefault(key.lower(), email)
    return directory


def build(travelers: dict, directory: dict | None = None) -> tuple[dict, list]:
    directory = directory or {}
    surname_to_vanity = {v.lower(): k for k, v in _VANITY.items()}
    roster, review = {}, []
    seen = set()
    for entry in travelers.values():
        person = (entry.get("person") or "").strip()
        if not person or person in seen:
            continue
        seen.add(person)
        parts = person.split()
        dir_email = directory.get(person.lower()) or (
            directory.get(f"{parts[0]} {parts[-1]}".lower()) if len(parts) > 2 else None)
        surname = parts[-1].lower()
        if dir_email:
            roster[person] = dir_email
            conf = "directory"
        elif surname in surname_to_vanity:
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
    data_dir = os.environ.get("FINANCE_HELPER_DATA", "data")
    tmap_path = os.path.join(data_dir, "united_travelers.yml")
    with open(tmap_path, encoding="utf-8") as fh:
        travelers = yaml.safe_load(fh) or {}
    roster, review = build(travelers, fetch_directory())
    out = os.path.join(data_dir, "roster.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(roster, fh, indent=2)
    print("\n".join(review))
    print(f"\nWrote {len(roster)} people to {out} — review the 'convention' rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
