"""Categorizer: assign a category + GL account to every line item.

Two paths:
  - Wide-layout line items arrive with a `category` already set by the source
    (e.g. Hotel Engine "Taxes and Fees"); we just resolve its GL account.
  - Long-layout line items are matched against the keyword rules in
    config/categories.yml (first match wins), falling back to the source's
    `default_category`.

Rules are deterministic and auditable — the right default for accounting. An
LLM-assisted pass for anything landing in `uncategorized` can be layered on
later (see docs/NEXT_STEPS.md), still routed through the review gate.
"""

from __future__ import annotations

from . import config
from .models import SourceDocument


def _matches(rule: dict, source: str, description: str) -> bool:
    if "source" in rule and rule["source"] != source:
        return False
    if "contains" in rule and rule["contains"].lower() not in description.lower():
        return False
    return "source" in rule or "contains" in rule


def _gl_for(category: str, categories: dict) -> str:
    account = categories.get(category, {}).get("gl_account")
    if account is None:
        account = categories.get("uncategorized", {}).get("gl_account", "9999")
    return account


def categorize(doc: SourceDocument) -> SourceDocument:
    cat_cfg = config.categories()
    rules = cat_cfg.get("rules", [])
    categories = cat_cfg.get("categories", {})
    default_category = config.source_config(doc.source).get("default_category", "uncategorized")

    for li in doc.line_items:
        if li.category is None:
            # Long layout: walk keyword rules, else fall back to source default.
            li.category = default_category
            for rule in rules:
                if _matches(rule, doc.source, li.description):
                    li.category = rule.get("category", li.category)
                    if rule.get("gl_account"):
                        li.gl_account = rule["gl_account"]
                    break
        if li.gl_account is None:
            li.gl_account = _gl_for(li.category, categories)

    return doc
