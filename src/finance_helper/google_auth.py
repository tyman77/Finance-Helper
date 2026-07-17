"""Shared Google service-account auth for the Sheets/Calendar fetch scripts.

Uses google-auth + requests (AuthorizedSession) rather than
google-api-python-client, to stay proxy-friendly (see fetch_schedule_index.py
and fetch_calendar_index.py, which originated this pattern).

Two ways to supply the service-account key, so the same code works both on a
laptop (a key file on disk) and on a host like Railway (no local filesystem
to point at, so the key content itself goes into an env var):
    GOOGLE_APPLICATION_CREDENTIALS       path to a service-account JSON file
    GOOGLE_SERVICE_ACCOUNT_JSON          the JSON key content itself
If both are set, the inline JSON wins.
"""

from __future__ import annotations

import json
import os


def credentials(scopes: list[str], subject: str | None = None):
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=scopes
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=scopes
        )
    if subject:
        creds = creds.with_subject(subject)  # domain-wide delegation impersonation
    return creds


def session(scopes: list[str], subject: str | None = None):
    from google.auth.transport.requests import AuthorizedSession

    sess = AuthorizedSession(credentials(scopes, subject))
    ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca:
        sess.verify = ca
    return sess
