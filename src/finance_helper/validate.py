"""Validate coded lines against the Sage Intacct chart-of-accounts rules.

Catches problems that would cause Intacct to reject the journal entry:
  - an OH account that requires a department but the line has none;
  - posting to an account that disallows direct posting, is inactive, or is
    unknown to the chart.

Returns a list of human-readable issues; the CLI shows them at review time.
"""

from __future__ import annotations

import json
import os

from .models import SourceDocument

_DATA_DIR = os.environ.get(
    "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data")
)


def load_chart(path: str | None = None) -> dict:
    path = path or os.path.join(_DATA_DIR, "chart_of_accounts.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_lines(doc: SourceDocument, chart: dict | None = None) -> list[dict]:
    """Structured per-line issues: [{"index": i, "message": str}, ...].

    Kept separate from `validate()` so callers that need to point at a specific
    row (e.g. the web UI highlighting the offending line) don't have to parse
    the description back out of a formatted string.
    """
    if chart is None:
        chart = load_chart()
    if not chart:
        return []  # no chart available -> skip validation

    issues = []
    for i, li in enumerate(doc.line_items):
        acct = str(li.gl_account or "").strip()
        info = chart.get(acct)
        if not acct or info is None:
            issues.append({"index": i, "message": f"account {acct!r} not in chart"})
            continue
        if info.get("status") and info["status"].lower() != "active":
            issues.append({"index": i, "message": f"account {acct} is {info['status']}"})
        if info.get("disallow_direct_posting"):
            issues.append({"index": i, "message": f"account {acct} disallows direct posting"})
        if info.get("require_department") and not li.department:
            issues.append({
                "index": i,
                "message": f"account {acct} ({info.get('title', '')}) "
                           f"requires a department, but none is set",
            })
    return issues


def validate(doc: SourceDocument, chart: dict | None = None) -> list[str]:
    """Human-readable issue strings, as used by the CLI."""
    return [
        f"{doc.line_items[i['index']].description[:40]!r}: {i['message']}"
        for i in validate_lines(doc, chart)
    ]
