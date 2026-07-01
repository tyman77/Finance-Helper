"""Shared processing pipeline: load -> enrich -> categorize.

Kept in one place so the CLI and tests run the exact same steps.
"""

from __future__ import annotations

from . import categorize as _categorize
from . import config, enrich, sources
from .models import SourceDocument


def process(source: str, path: str) -> SourceDocument:
    doc = sources.load(source, path)

    enrichment = config.source_config(source).get("enrich")
    if enrichment == "united_travelers":
        doc = enrich.enrich_united(doc)

    return _categorize.categorize(doc)
