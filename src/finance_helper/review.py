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
        f"  {'GL Acct':<9} {'Category':<28} {'Amount':>12}  Description",
        "  " + "-" * 64,
    ]
    for li in doc.line_items:
        lines.append(
            f"  {str(li.gl_account):<9} {str(li.category):<28} "
            f"{str(li.amount):>12}  {li.description[:40]}"
        )
    lines.append("  " + "-" * 64)
    lines.append(f"  {'TOTAL':<38} {str(doc.total):>12}")
    lines.append("=" * 68)
    return "\n".join(lines)


def save_proposal(doc: SourceDocument, payload: dict, out_dir: str = "out") -> str:
    """Write the proposal JSON to disk for the audit trail. Returns the path."""
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
