"""Shared Google service-account auth for the Sheets/Calendar fetch scripts.

Uses google-auth + requests (AuthorizedSession) rather than
google-api-python-client, to stay proxy-friendly (see fetch_schedule_index.py
and fetch_calendar_index.py, which originated this pattern).

Two ways to supply the service-account key, so the same code works both on a
laptop (a key file on disk) and on a host like Railway (no local filesystem
to point at, so the key content itself goes into an env var):
    GOOGLE_APPLICATION_CREDENTIALS       path to a service-account JSON file
    GOOGLE_SERVICE_ACCOUNT_JSON          the JSON key content, raw OR base64
If both are set, the inline JSON wins. The inline value may be either the raw
JSON or a base64 encoding of it — base64 avoids the newline/quote mangling
that env-var UIs often inflict on a pasted multi-line key.
"""

from __future__ import annotations

import base64
import binascii
import json
import os


def _load_key_info(raw: str) -> dict:
    """Parse GOOGLE_SERVICE_ACCOUNT_JSON, accepting raw JSON or base64-of-JSON."""
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON or base64-encoded "
                "JSON. Paste the service-account key file's contents, or its "
                "base64 encoding (`base64 -i key.json`)."
            ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON didn't parse as JSON. If you pasted the "
            "key file directly and it got mangled, try the base64 form instead "
            "(`base64 -i key.json`)."
        ) from exc


def credentials(scopes: list[str], subject: str | None = None):
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if raw:
        creds = service_account.Credentials.from_service_account_info(
            _load_key_info(raw), scopes=scopes
        )
    elif key_file:
        creds = service_account.Credentials.from_service_account_file(key_file, scopes=scopes)
    else:
        raise RuntimeError(
            "Google credentials aren't configured. Set GOOGLE_SERVICE_ACCOUNT_JSON "
            "(paste the service-account key file's contents) — or, running locally, "
            "GOOGLE_APPLICATION_CREDENTIALS with a path to the key file."
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
