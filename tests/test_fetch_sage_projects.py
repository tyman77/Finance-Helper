"""Tests for the pure-logic parts of scripts/fetch_sage_projects.py.

No network access — the token/HTTP calls aren't unit-tested here (same
convention as the other fetch scripts); this covers _get(), the part that
survives regardless of which exact field names the real API turns out to use.
"""

import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "fetch_sage_projects",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "fetch_sage_projects.py"),
)
fetch_sage_projects = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_sage_projects)


def test_get_tries_candidates_in_order():
    record = {"STATUS": "Active", "id": "4804"}
    assert fetch_sage_projects._get(record, ("status", "STATUS")) == "Active"
    assert fetch_sage_projects._get(record, ("projectId", "id")) == "4804"


def test_get_skips_blank_values():
    record = {"status": "", "STATUS": "Archived"}
    assert fetch_sage_projects._get(record, fetch_sage_projects._STATUS_FIELD_CANDIDATES) == "Archived"


def test_get_returns_none_when_nothing_matches():
    assert fetch_sage_projects._get({"foo": "bar"}, ("status", "STATUS")) is None
