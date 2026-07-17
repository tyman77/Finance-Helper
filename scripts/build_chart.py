#!/usr/bin/env python3
"""Parse the Sage Intacct GL account export into data/chart_of_accounts.json.

Captures the posting rules the tool must respect:
  - require_department: OH accounts need a department dimension on the JE line.
  - disallow_direct_posting / status: can't post to these.

Usage:
    python scripts/build_chart.py <GeneralLedger_account.csv>
Default output: data/chart_of_accounts.json (gitignored).
"""

from __future__ import annotations

import csv
import json
import os
import sys


def _b(value: str) -> bool:
    return str(value).strip().lower() == "true"


def build(path: str) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    chart = {}
    for row in rows:
        num = (row.get("Account number") or "").strip()
        if not num:
            continue
        chart[num] = {
            "title": (row.get("Account title") or "").strip(),
            "normal_balance": (row.get("Normal balance") or "").strip(),
            "require_department": _b(row.get("Require department", "")),
            "disallow_direct_posting": _b(row.get("Disallow direct posting", "")),
            "status": (row.get("Status") or "").strip(),
        }
    return chart


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    chart = build(argv[0])
    out = argv[1] if len(argv) > 1 else os.path.join(
        os.environ.get("FINANCE_HELPER_DATA", "data"), "chart_of_accounts.json"
    )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(chart, fh, indent=2)
    reqd = sum(1 for a in chart.values() if a["require_department"])
    print(f"Wrote {len(chart)} accounts ({reqd} require a department) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
