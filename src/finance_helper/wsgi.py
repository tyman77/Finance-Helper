"""WSGI entrypoint for production hosting.

    gunicorn finance_helper.wsgi:app

Flask's built-in dev server (python -m finance_helper.web) explicitly warns
against production use — no concurrency, no hardening. gunicorn is a real
WSGI server; this module just exposes the app object it needs.
"""

from .web.app import create_app

app = create_app()
