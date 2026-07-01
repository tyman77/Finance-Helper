"""Validate coded lines against Sage Intacct before they'd be posted.

Catches problems that would cause Intacct to reject the journal entry, or that
just shouldn't be posted even if Intacct would accept them:
  - an OH account that requires a department but the line has none;
  - posting to an account that disallows direct posting, is inactive, or is
    unknown to the chart;
  - a project that's archived in Sage — this is the safety net for archived
    projects specifically: it catches one regardless of how it ended up on the
    line (an auto-coded suggestion, a direct vendor-stated code, or someone
    typing an old number into the web UI by hand), since the auto-coding paths
    in project_resolver.py only *avoid picking* archived codes, they don't
    guarantee every code on every line went through that filter.

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


def load_projects(path: str | None = None) -> dict:
    """{"<project_id>": {"name": ..., "status": ...}, ...} from
    scripts/fetch_sage_projects.py. {} if that file doesn't exist yet."""
    path = path or os.path.join(_DATA_DIR, "sage_projects.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_lines(doc: SourceDocument, chart: dict | None = None, projects: dict | None = None) -> list[dict]:
    """Structured per-line issues: [{"index": i, "message": str}, ...].

    Kept separate from `validate()` so callers that need to point at a specific
    row (e.g. the web UI highlighting the offending line) don't have to parse
    the description back out of a formatted string.
    """
    if chart is None:
        chart = load_chart()
    if projects is None:
        projects = load_projects()

    issues = []
    for i, li in enumerate(doc.line_items):
        if chart:
            acct = str(li.gl_account or "").strip()
            info = chart.get(acct)
            if not acct or info is None:
                issues.append({"index": i, "message": f"account {acct!r} not in chart"})
            else:
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

        if projects and li.project:
            proj_info = projects.get(str(li.project))
            status = (proj_info or {}).get("status")
            if proj_info is not None and (status or "").lower() != "active":
                name = proj_info.get("name", "")
                label = f"{li.project} ({name})" if name else str(li.project)
                issues.append({
                    "index": i,
                    "message": f"project {label} is {status or 'not active'} in Sage",
                })

    return issues


def validate(doc: SourceDocument, chart: dict | None = None, projects: dict | None = None) -> list[str]:
    """Human-readable issue strings, as used by the CLI."""
    return [
        f"{doc.line_items[i['index']].description[:40]!r}: {i['message']}"
        for i in validate_lines(doc, chart, projects)
    ]
