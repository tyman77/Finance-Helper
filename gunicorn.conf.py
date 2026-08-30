"""Gunicorn settings that must hold regardless of how the process is launched.

The Railway service's start command omits --timeout, so workers were killed
at gunicorn's 30s default — fatal for a Cash Proof run, which synchronously
pulls Ramp/Bill.com/Sage and reconciles a year of transactions ("upstream
error" at the edge). Gunicorn reads this file from the working directory
automatically; CLI flags would still win, but the deployed command sets none
of these.
"""

timeout = 300           # a full deep-dive run: API pulls + year reconcile
graceful_timeout = 30
