"""Confirm .env is actually auto-loaded — regression guard for the earlier
gap where .env.example existed and README referenced .env, but nothing in
the code called python-dotenv's load_dotenv(), so it silently did nothing.
"""

import os
from pathlib import Path

from finance_helper.cli import main as cli_main
from finance_helper.web.app import create_app

_SAMPLE = str((Path(__file__).parent.parent / "samples" / "ups_sample.csv").resolve())


def test_cli_loads_dotenv_from_cwd(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("FINANCE_HELPER_TEST_VAR=hello_from_dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FINANCE_HELPER_TEST_VAR", raising=False)

    cli_main(["process", "--source", "ups", "--file", _SAMPLE])

    assert os.environ.get("FINANCE_HELPER_TEST_VAR") == "hello_from_dotenv"


def test_web_app_module_loads_dotenv():
    """The web app's module (unlike the CLI's main()) loads .env at import
    time, since create_app() can be called many times in one process (once
    per test, for instance) — re-testing that behaviorally would just be
    testing python-dotenv itself against a stale module import. This instead
    guards against someone removing the load_dotenv() wiring entirely."""
    import inspect

    from finance_helper.web import app as web_app_module

    source = inspect.getsource(web_app_module)
    assert "load_dotenv(" in source
    create_app()  # still exercised, to catch an import-time crash
