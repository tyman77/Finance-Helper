#!/usr/bin/env python3
"""Build a client -> project-code registry from historical United coding.

The historical export's `Project` column pairs a client with its code, e.g.
    "Northview Church, IN [3428] Camera Upgrade Project"
    "Little Country Church, Lighting Upgrade, 4173"
This extracts {code: {client, state, name}} plus a normalized client index, so a
calendar event naming a client (or a client-domain attendee) can resolve to a
project code.

Usage:
    python scripts/build_project_registry.py <historical_export.csv>
Default output: data/project_registry.json (gitignored).
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import defaultdict

_CODE_BRACKET = re.compile(r"\[(\d{3,5})\]")
_CODE_TRAILING = re.compile(r",\s*(\d{3,5})\b")
_STATE = re.compile(r",\s*([A-Z]{2})\b")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _client_of(project: str) -> str:
    # Client name is the text before the first comma (drops ", ST [code] desc").
    return project.split(",", 1)[0].strip()


def build(path: str) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    registry: dict[str, dict] = {}
    index: dict[str, set] = defaultdict(set)
    for row in rows:
        project = (row.get("Project") or "").strip()
        if not project:
            continue
        m = _CODE_BRACKET.search(project) or _CODE_TRAILING.search(project)
        if not m:
            continue
        code = m.group(1)
        client = _client_of(project)
        state_m = _STATE.search(project)
        registry.setdefault(code, {
            "client": client,
            "state": state_m.group(1) if state_m else "",
            "name": project,
        })
        key = normalize(client)
        if len(key) >= 5:
            index[key].add(code)

    return {
        "registry": registry,
        # normalized client -> sorted list of codes (a client may have several)
        "index": {k: sorted(v) for k, v in index.items()},
    }


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    data = build(argv[0])
    out = argv[1] if len(argv) > 1 else os.path.join(
        os.environ.get("FINANCE_HELPER_DATA", "data"), "project_registry.json"
    )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"Wrote {len(data['registry'])} project codes, "
          f"{len(data['index'])} client keys to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
