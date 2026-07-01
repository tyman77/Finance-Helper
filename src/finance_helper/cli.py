"""Command-line entry point.

    python -m finance_helper process --source ups --file samples/ups_sample.csv
    python -m finance_helper process --source ups --file ... --approve

Without --approve, nothing is posted: you get a printed summary and a saved
proposal JSON in out/. With --approve, the tool attempts to post (and will tell
you clearly if credentials aren't set up yet).
"""

from __future__ import annotations

import argparse
import sys

from . import categorize, destinations, review, sources


def _cmd_process(args: argparse.Namespace) -> int:
    doc = sources.load(args.source, args.file)
    doc = categorize.categorize(doc)
    payload = destinations.build_payload(doc)

    print(review.render(doc, payload))
    proposal_path = review.save_proposal(doc, payload)
    print(f"\nProposal saved to: {proposal_path}")

    if not args.approve:
        print("\nDRY RUN — nothing was posted. Re-run with --approve to post.")
        return 0

    print("\n--approve set: posting...")
    try:
        result = destinations.post(doc, payload)
    except (RuntimeError, NotImplementedError) as exc:
        print(f"\nNot posted: {exc}")
        return 2
    print(f"Posted. Response: {result}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finance_helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("process", help="Process one vendor CSV file.")
    p.add_argument("--source", required=True, help="Source key (ups, united, hotel_engine, national)")
    p.add_argument("--file", required=True, help="Path to the CSV file")
    p.add_argument("--approve", action="store_true", help="Actually post (default is dry run)")
    p.set_defaults(func=_cmd_process)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
