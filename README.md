# Finance Helper

Turns raw vendor CSV files into categorized accounting entries and routes them to
the right system — **Sage Intacct** (journal entries) or **Bill.com** (bills) — with
a human review/approval step before anything posts.

```
raw CSV  →  parse (config-driven)  →  normalize  →  categorize (rules)
         →  REVIEW  →  post to Sage Intacct or Bill.com
```

## Supported flows (v1)

| Source              | Destination   | Input   |
|---------------------|---------------|---------|
| United Airlines     | Sage Intacct  | CSV     |
| Hotel Engine        | Sage Intacct  | CSV     |
| UPS                 | Bill.com      | CSV     |
| National Car Rental | Bill.com      | CSV     |

Each source is just a config entry in `config/sources.yml` (which columns map to
what, and which destination it targets). Adding a new vendor = adding a config
block, not writing new code. Two CSV shapes are handled:

- **long** — one amount column; each row is a line item. A whole file can be a
  *statement* that posts as one entry (e.g. a month of United UATP tickets).
- **wide** — each row is already split across charge columns; every component
  becomes its own categorized line and the room/base remainder is derived so the
  parts tie exactly to the row total (e.g. a Hotel Engine statement broken into
  room / taxes / incidentals / flex / booking fee / travel credits).

Verified end-to-end against real United (91 tickets, net $31,148.79) and Hotel
Engine (32 bookings, $17,790.29) exports — both produce balanced journal entries.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dry run — parses, categorizes, and prints the proposed entry. Posts nothing.
python -m finance_helper process --source ups --file samples/ups_sample.csv

# Post for real (requires credentials in .env and explicit approval)
python -m finance_helper process --source ups --file samples/ups_sample.csv --approve
```

Nothing is ever sent to Sage or Bill.com without `--approve` **and** valid
credentials. Without them, you get a saved proposal JSON in `out/` to eyeball.

## Configuration

- `config/sources.yml` — per-source CSV column mapping + destination.
- `config/categories.yml` — categorization rules (keyword → GL account / category).
- `.env` — API credentials (copy from `.env.example`). Never commit this.

## Status

This is a scaffold. What works today:
- End-to-end **dry-run** pipeline for all four sources.
- Rules-based categorization with a per-source fallback.
- Review gate + proposal output.

What still needs your input (see `docs/NEXT_STEPS.md`):
- Real Sage Intacct + Bill.com credentials and sandbox testing.
- Your actual chart-of-accounts numbers in `config/categories.yml`.
- A real exported CSV from each vendor to lock down the column mappings.
