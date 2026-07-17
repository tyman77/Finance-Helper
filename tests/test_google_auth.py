"""google_auth key parsing: raw JSON, base64-of-JSON, and clear errors."""

import base64
import json

import pytest

from finance_helper import google_auth

_KEY = {"type": "service_account", "project_id": "x", "private_key": "-----BEGIN-----\nabc\n-----END-----\n"}


def test_raw_json_is_parsed():
    assert google_auth._load_key_info(json.dumps(_KEY)) == _KEY


def test_base64_json_is_parsed():
    encoded = base64.b64encode(json.dumps(_KEY).encode()).decode()
    assert google_auth._load_key_info(encoded) == _KEY


def test_pretty_multiline_json_still_parses():
    assert google_auth._load_key_info(json.dumps(_KEY, indent=2)) == _KEY


def test_garbage_raises_a_clear_error():
    with pytest.raises(RuntimeError, match="valid JSON or base64"):
        google_auth._load_key_info("this is not json or base64 @@@")


def test_missing_credentials_message(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(RuntimeError, match="aren't configured"):
        google_auth.credentials(["scope"])
