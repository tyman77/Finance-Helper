"""Human review gate: render a proposed entry and save it for approval."""

from __future__ import annotations

import json
import os

from .models import SourceDocument


def render(doc: SourceDocument, payload: dict) -> str:
    """A readable summary of what WOULD be posted."""
    dest = "Sage Intacct (journal entry)" if doc.destination == "sage" else "Bill.com (bill)"
    lines = [
        "=" * 68,
        f"  {doc.vendor}  ->  {dest}",
        f"  Document: {doc.document_id}    Date: {doc.document_date}    "
        f"Total: {doc.total} {doc.currency}",
        "=" * 68,
        f"  {'':1}{'GL Acct':<8} {'Dept':<6} {'Amount':>11}  Description",
        "  " + "-" * 70,
    ]
    for li in doc.line_items:
        flag = "⚑" if li.needs_review else " "  # ⚑ marks lines to review
        dept = (li.department or "").split("--")[0].strip()
        lines.append(
            f"  {flag}{str(li.gl_account):<8} {dept:<6} "
            f"{str(li.amount):>11}  {li.description[:44]}"
        )
    lines.append("  " + "-" * 70)
    lines.append(f"  {'TOTAL':<17} {str(doc.total):>11}")
    lines.append("=" * 68)
    flagged = sum(1 for li in doc.line_items if li.needs_review)
    if flagged:
        lines.append(f"  ⚑ = needs review ({flagged} line(s))")
        lines.append("=" * 68)
    return "\n".join(lines)


def save_proposal(doc: SourceDocument, payload: dict, out_dir: str | None = None) -> str:
    """Write the proposal JSON to disk for the audit trail. Returns the path.

    Defaults to ./out, overridable with FINANCE_HELPER_OUT_DIR (same convention
    as FINANCE_HELPER_DATA) so a hosted deployment can point it at a persistent
    volume instead of the container's ephemeral filesystem.
    """
    out_dir = out_dir or os.environ.get("FINANCE_HELPER_OUT_DIR", "out")
    os.makedirs(out_dir, exist_ok=True)
    safe_id = "".join(c if c.isalnum() else "_" for c in doc.document_id)
    path = os.path.join(out_dir, f"{doc.source}_{safe_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"source": doc.source, "destination": doc.destination, "payload": payload},
            fh,
            indent=2,
        )
    return path
