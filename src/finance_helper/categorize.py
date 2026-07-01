"""Rules-based categorizer.

For each line item, walk the rules in config/categories.yml top-to-bottom and
apply the first one that matches. If none match, fall back to the source's
`default_category`. This is deterministic and auditable — the right starting
point for accounting. An LLM-assisted pass can be layered on later for the
ambiguous line items (see docs/NEXT_STEPS.md).
"""

from __future__ import annotations

from . import config
from .models import SourceDocument


def _matches(rule: dict, source: str, description: str) -> bool:
    if "source" in rule and rule["source"] != source:
        return False
    if "contains" in rule and rule["contains"].lower() not in description.lower():
        return False
    # A rule with neither condition would match everything; treat as no-match.
    return "source" in rule or "contains" in rule


def categorize(doc: SourceDocument) -> SourceDocument:
    cat_cfg = config.categories()
    rules = cat_cfg.get("rules", [])
    categories = cat_cfg.get("categories", {})
    default_category = config.source_config(doc.source).get(
        "default_category", "uncategorized"
    )

    for li in doc.line_items:
        category = default_category
        gl_account = None
        for rule in rules:
            if _matches(rule, doc.source, li.description):
                category = rule.get("category", category)
                gl_account = rule.get("gl_account")
                break
        # Resolve the GL account: an explicit rule account wins, else the
        # category's configured account, else the uncategorized fallback.
        if gl_account is None:
            gl_account = categories.get(category, {}).get("gl_account")
        if gl_account is None:
            gl_account = categories.get("uncategorized", {}).get("gl_account", "9999")

        li.category = category
        li.gl_account = gl_account

    return doc
