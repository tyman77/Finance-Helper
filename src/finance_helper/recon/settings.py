"""Load config/recon.yml (format mappings + matching parameters)."""

from __future__ import annotations

import os

import yaml

_DEFAULTS = {
    "bank": {"noise_words": []},
    "sage": {
        "columns": {
            "date": "Entry Date",
            "description": "Memo/Description",
            "amount": None,
            "debit": "Debit",
            "credit": "Credit",
            "account": "Account No",
            "journal": "Journal",
            "doc": "Document Number",
        },
        "cash_accounts": [],
    },
    "matching": {
        "exact_window_days": 3,
        "fuzzy_window_days": 7,
        "split_window_days": 5,
        "timing_window_days": 5,
        "aged_timing_days": 30,
    },
}


def _config_path() -> str:
    return os.environ.get(
        "FINANCE_HELPER_RECON_CONFIG",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "recon.yml"),
    )


def recon_config() -> dict:
    """Deep-merge recon.yml over the defaults. Re-read each call — same
    long-running-server rationale as load_traveler_map."""
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    merged["sage"]["columns"] = dict(_DEFAULTS["sage"]["columns"])
    path = _config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        for section, values in loaded.items():
            if isinstance(values, dict) and isinstance(merged.get(section), dict):
                for k, v in values.items():
                    if isinstance(v, dict) and isinstance(merged[section].get(k), dict):
                        merged[section][k].update(v)
                    else:
                        merged[section][k] = v
            else:
                merged[section] = values
    return merged
