"""Gunicorn settings that must hold regardless of how the process is launched.

The Railway service's start command sets only --bind, so everything else
comes from this file (gunicorn reads it from the working directory).

Cash Proof runs execute on a background thread inside the worker process
(web/cashproof.py JOBS) — requests themselves are all fast now. That design
requires exactly one worker (the progress-poll request must land in the same
process that holds the job) and a threaded worker so polls are served while
the job thread crunches.
"""

workers = 1
threads = 8              # gthread worker: polls answered during a heavy run
timeout = 300            # headroom for any straggler request
graceful_timeout = 30
