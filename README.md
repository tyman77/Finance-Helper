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

Hotel Engine is enriched from its own columns: **Department Name** → department
dimension (100% of the real statement), and **Project Name** → project code
(direct or via the client→project registry). Overhead names (HQ Visit, OH Sales,
All Staff, SA Hire) set an overhead account instead of a project.

UPS (→ Bill.com) codes each shipment: net = `Billed Charge + Incentive Credit`;
project from **Reference No.1**, inherited from another line with the same
**Tracking Number** (correction rows), or matched on the receiver via the
registry → `51700 COGS Shipping` + project; otherwise `65565 OH Postage &
Shipping` (department inferred from marketing/sales references). Real invoice:
129 lines, $3,547.74, 82 project-coded.

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

## United traveler coding (learned from history)

United tickets are enriched from your historical coding. Regenerate the map from
a historical "United Flights" export whenever it changes:

```bash
python scripts/build_traveler_map.py <historical_export.csv>   # -> data/united_travelers.yml
```

For each ticket the tool then:
- **auto-assigns Person + Department** (department is 100% stable per traveler);
- **suggests the GL account** (the traveler's most-common account) but **flags it
  for review** — historically the account is `52200 COGS Travel` when a project is
  assigned, otherwise an overhead account by trip purpose, which is a human call.

The map contains employee names and is **gitignored**; only the reproducible
script and the non-PII `config/accounts.yml` are committed.

If a traveler's historical `Person` value is blank or junk data (seen in the
real export: literally `"Customer"` for some rows), the builder falls back to
a best-effort guess from the United passenger name itself (`"JUDY/JOSHUA"` →
`"Joshua Judy"`), so they can still be found on the crew schedule / calendar.
When that guess (or the real historical name) doesn't match how someone is
named on the schedule sheet or their calendar — a nickname vs. legal name, e.g.
`"Diego Munguia"` on the crew sheet vs. `"Israel Munguia"` in United history —
add a one-line fix to `data/name_aliases.yml` (gitignored) rather than editing
code:
```yaml
"MUNGUIA/ISRAELDIEGO": "Diego Munguia"
```

### Auto-coding the project (COGS vs overhead)

The account depends on the *trip*, which isn't in the UATP feed — so it's pulled
from two sources by the traveler's team (`project_resolver.py`):

- **Installers** → the crew-schedule grid (`data/schedule_index.json`, built by
  `scripts/fetch_schedule_index.py`): the project code worked during the stay →
  `52200 COGS Travel` + that project as an Intacct dimension.
- **Everyone else** → the traveler's **own** Google Calendar (`data/calendar_index.json`,
  built by `scripts/fetch_calendar_index.py`): trip-relevant events around the
  departure date (internal noise filtered out) become review context. A
  **client→project registry** (`scripts/build_project_registry.py`, learned from
  the historical `Project` column) then maps the client name / client-domain
  attendees to a project code: a single match auto-codes to `52200 COGS`;
  multiple matches (e.g. Life.Church's several campuses) are surfaced as
  candidates to pick.

Travelers are mapped to their calendar via a **roster** (`scripts/build_roster.py`
auto-drafts `data/roster.json` from the work-email convention + known vanity
calendars — review the flagged rows). Match is by departure date over the stay
window. No project match → the traveler's usual account, flagged, with calendar
context attached. All `data/*` indices are gitignored (names/schedules/clients)
and regenerated by the scripts; in production those run on a cron against the
Google Sheets / Calendar APIs.

Builders:
```bash
python scripts/build_traveler_map.py    <historical_united.csv>   # dept + account history
python scripts/build_project_registry.py <historical_united.csv>  # client -> project code
python scripts/build_roster.py                                     # person -> calendar (review it)
python scripts/fetch_schedule_index.py  2026                       # installer crew grid
python scripts/fetch_calendar_index.py  2026-05-01 2026-07-15      # per-person calendars
```

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
