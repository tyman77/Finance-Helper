"""Loads YAML config (sources + categories) from the config/ directory."""

from __future__ import annotations

import os
from functools import lru_cache

import yaml

_CONFIG_DIR = os.environ.get(
    "FINANCE_HELPER_CONFIG",
    os.path.join(os.path.dirname(__file__), "..", "..", "config"),
)


def _load(name: str) -> dict:
    path = os.path.join(_CONFIG_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def sources() -> dict:
    return _load("sources.yml")


@lru_cache(maxsize=1)
def categories() -> dict:
    return _load("categories.yml")


def source_config(source: str) -> dict:
    cfg = sources()
    if source not in cfg:
        known = ", ".join(sorted(cfg)) or "(none)"
        raise KeyError(f"Unknown source '{source}'. Known sources: {known}")
    return cfg[source]
