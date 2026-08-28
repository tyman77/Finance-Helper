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


def web_services_username() -> str:
    user = os.environ["INTACCT_USER_ID"]
    company = os.environ.get("INTACCT_COMPANY_ID", "")
    if "@" not in user and company:
        return f"{user}@{company}"
    return user
