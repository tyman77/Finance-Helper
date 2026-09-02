"""Per-user access control: who may log in at all, and which sections.

The Google OAuth gate (app.py) proves WHO someone is and that they belong
to the Workspace domain; this module decides WHAT they may see. Users live
in users.json under FINANCE_HELPER_DATA (the persistent volume), managed
from Admin -> Users. Someone who authenticates but isn't in the store gets
no access at all.

Bootstrap: emails in FINANCE_HELPER_ADMIN_EMAILS (comma-separated env) are
always admins. If the store is empty and that env is unset, the FIRST
Workspace user to log in becomes admin — the domain gate already limits
who that can be, and somebody has to be able to open Admin -> Users.

A user holding the "admin" section is an admin: they see every section and
manage users. Other users see exactly the sections they're granted.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

SECTIONS = {
    "travel": "Travel (Flights / Hotels / Rental Cars / All spend)",
    "reviews": "Statement reviews & uploads",
    "cashproof": "Cash Proof & fraud checks",
    "billcheck": "Bill Check (invoice verification)",
    "admin": "Admin (data connections & user access)",
}

# App endpoints that belong to a section. Anything unlisted (index, login,
# logout, static) is open to every signed-in user.
_ENDPOINT_SECTIONS = {
    "domain_page": "travel",
    "hotels_guests_upload": "travel",
    "insights_page": "travel",
    "upload": "reviews",
    "rerun": "reviews",
    "review_page": "reviews",
    "update_line": "reviews",
    "approve": "reviews",
    "delete_run": "reviews",
    "download": "reviews",
}


def section_for_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    if endpoint.startswith("cashproof."):
        return "cashproof"
    if endpoint.startswith("billcheck."):
        return "billcheck"
    if endpoint.startswith("admin."):
        return "admin"
    return _ENDPOINT_SECTIONS.get(endpoint)


def _store_path() -> str:
    data_dir = os.environ.get(
        "FINANCE_HELPER_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
    return os.path.join(data_dir, "users.json")


def load_users() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_users(users: dict) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=1)


def env_admins() -> set[str]:
    raw = os.environ.get("FINANCE_HELPER_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def ensure_bootstrap_admin(email: str) -> bool:
    """First user into an empty store (with no env admins) becomes admin."""
    email = email.lower()
    if env_admins() or load_users():
        return False
    save_users({email: {
        "sections": sorted(SECTIONS),
        "added_by": "(bootstrap — first login)",
        "added": datetime.now().isoformat(timespec="seconds"),
    }})
    return True


def sections_for(email: str) -> set[str] | None:
    """The sections this email may use; None = not authorized at all.
    Admins (env or 'admin' section) hold every section."""
    email = (email or "").lower()
    if email in env_admins():
        return set(SECTIONS)
    user = load_users().get(email)
    if user is None:
        return None
    granted = {s for s in (user.get("sections") or []) if s in SECTIONS}
    return set(SECTIONS) if "admin" in granted else granted


def is_admin(email: str) -> bool:
    sections = sections_for(email)
    return sections is not None and "admin" in sections


def allowed(email: str, section: str) -> bool:
    sections = sections_for(email)
    return sections is not None and section in sections
