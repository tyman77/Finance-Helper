"""Shared test setup.

Isolate FINANCE_HELPER_OUT_DIR to a per-test temp dir so saved proposals and
persisted review runs don't accumulate in the repo's ./out during test runs.
Only OUT_DIR is redirected (not FINANCE_HELPER_DATA), so tests that read the
real ./data files still work.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_out_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_OUT_DIR", str(tmp_path / "out"))


@pytest.fixture(autouse=True)
def _isolate_posted_ledger(tmp_path, monkeypatch):
    """The cross-run posted ledger writes under FINANCE_HELPER_DATA, which
    tests deliberately leave pointed at the real ./data — so pin just the
    ledger file to the test's temp dir to keep ✔ stamps from leaking
    between tests (or into the repo)."""
    from finance_helper.web import ledger
    monkeypatch.setattr(ledger, "_path",
                        lambda: str(tmp_path / "posted_ledger.json"))
