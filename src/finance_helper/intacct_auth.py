"""Shared Sage Intacct auth details used by every Intacct caller.

Confirmed live (2026-08-28, first Cash Proof API run): the token endpoint
requires the Web Services User as "user@company" — a bare user id gets
HTTP 400 "The username format is invalid (expected user@company)". People
naturally put just the user id in INTACCT_USER_ID, so the company suffix is
appended here from INTACCT_COMPANY_ID rather than making everyone remember
the format. An id that already contains "@" is passed through untouched.
"""

from __future__ import annotations

import os


def env(name: str, default: str = "") -> str:
    """INTACCT_* env values, stripped — values pasted into hosting dashboards
    routinely pick up a trailing newline/space, which the token endpoint then
    rejects as invalid_client."""
    return (os.environ.get(name) or default).strip()


def web_services_username() -> str:
    user = env("INTACCT_USER_ID")
    company = env("INTACCT_COMPANY_ID")
    if "@" not in user and company:
        return f"{user}@{company}"
    return user
