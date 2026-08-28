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

To post for real, put credentials in a `.env` file — **don't `export` them in
your shell**, that's easy to mistype/leave stale. Copy the template and fill
it in with a text editor:
```bash
cp .env.example .env
```
`.env` is loaded automatically and is gitignored, so it never gets committed.

## Web UI

A local review UI wraps the exact same pipeline — upload a CSV, see the
proposed entry as an editable table, fix any flagged GL account/department/
project inline, then Approve & Post (same safety net as `--approve`: it
refuses without real credentials).

```bash
pip install -r requirements-web.txt
PYTHONPATH=src python -m finance_helper.web        # http://127.0.0.1:5000
```

- Upload a CSV and pick its source; the review table shows every line with its
  amount, GL account, department, and project — flagged (⚑) rows and chart-
  of-accounts validation issues (e.g. "requires a department") are highlighted.
- Edit any coding field (autocomplete from the real chart of accounts, once
  fetched) and **Save changes** — this re-runs validation live.
- **Download proposal JSON** gets you the exact payload that would post.
- **Saved reviews persist to disk** (`<FINANCE_HELPER_OUT_DIR>/runs/`): every
  upload and edit is saved automatically, so you can close the tab and come
  back to a review later — it survives a server restart/redeploy, and shows up
  under "Saved reviews" on the home page (with an In progress / Posted / Post
  failed status). Delete one from that list when you're done with it.

Override the host/port/debug via `FINANCE_HELPER_WEB_HOST` /
`FINANCE_HELPER_WEB_PORT` / `FINANCE_HELPER_WEB_DEBUG`.

### Login (Sign in with Google)

Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_ALLOWED_DOMAIN`, and `FINANCE_HELPER_SECRET` in `.env` to put a
Google sign-in in front of every route — only accounts on
`GOOGLE_OAUTH_ALLOWED_DOMAIN` (your Workspace domain) can log in. This is
**required** if the app is reachable by anything other than your own machine
— it can post real journal entries to Sage. With none of these set, it runs
exactly as before (no login, for localhost-only use).

To set it up:
1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   (the same project as the Sheets/Calendar service account, or a new one) —
   **Create Credentials → OAuth client ID → Web application**.
2. Add an **Authorized redirect URI**: `https://<your-deployed-URL>/auth/google/callback`
   (you'll need the URL from hosting it first — see below — then come back
   and add this).
3. Copy the generated **Client ID** and **Client secret** into
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
4. Set `GOOGLE_OAUTH_ALLOWED_DOMAIN` to your company's Workspace domain (e.g.
   `summitintegrated.com`).
5. Generate `FINANCE_HELPER_SECRET`:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

### Hosting it on Railway

`railway.json` and `Procfile` are already in the repo — Railway just needs to
be pointed at it:

1. In Railway: **New Project → Deploy from GitHub repo** and pick this repo
   (or, if you already have a project, **New Service → GitHub Repo** into it).
   Railway auto-detects `railway.json`, which tells it to build with
   `pip install -e .[web]` (installs Flask + gunicorn, not just the CLI-only
   core deps) and start with `gunicorn finance_helper.wsgi:app`.
2. **Add a volume** (service → *Volumes* → *New Volume*) and mount it at, say,
   `/data`. Without this, every redeploy wipes the traveler map, project
   registry, Sage project cache, and saved proposals — they live in the
   container's ephemeral filesystem otherwise.
3. Set environment variables on the service (**Variables** tab) — this is
   your `.env`, entered directly in Railway, never committed or pasted here:
   - `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
     `GOOGLE_OAUTH_ALLOWED_DOMAIN`, `FINANCE_HELPER_SECRET` (see Login above
     — you'll need Railway's URL from step 4 before finishing step 2 of that
     setup)
   - `FINANCE_HELPER_DATA=/data` and `FINANCE_HELPER_OUT_DIR=/data/out` (point
     both at the volume from step 2)
   - `INTACCT_CLIENT_ID`, `INTACCT_CLIENT_SECRET`, `INTACCT_COMPANY_ID`,
     `INTACCT_USER_ID`, `INTACCT_USER_PASSWORD`, `INTACCT_CLEARING_ACCOUNT`
     (and `BILLDOTCOM_*` once that destination is wired up)
   - Railway sets `PORT` itself — the `Procfile`/`railway.json` start command
     already binds to it, nothing to add there.
4. Once deployed, Railway gives you a `*.up.railway.app` URL (or attach a
   custom domain under **Settings → Networking**). Go back to the Google
   Cloud Console OAuth client and add `https://<that URL>/auth/google/callback`
   as an Authorized redirect URI — sign-in will fail with `redirect_uri_mismatch`
   until this matches exactly.
5. **Get the data onto the volume** — use the admin panel below (`/admin`),
   which regenerates all of it directly on the server without needing to
   touch the container's filesystem by hand.

### Admin panel (`/admin`)

The indices in `data/*.json` (traveler map, project registry, roster, crew
schedule, calendars, Sage projects, chart of accounts) are gitignored — they
carry employee/client names — so a fresh deploy never has them. The
**Admin** link in the header opens a page to regenerate each one on the
server itself, gated behind the same login as everything else:

- **Historical United export / Chart of accounts** — upload the same CSVs
  you'd otherwise feed to `scripts/build_traveler_map.py` / `build_chart.py`
  locally; no extra credentials needed.
- **Roster** — one click, derived from the traveler map already uploaded.
- **Crew schedule / Traveler calendars** — need Google credentials as
  environment variables, since there's no local key file to point at on a
  host:
  - `GOOGLE_SERVICE_ACCOUNT_JSON` = the **contents** of the service-account
    JSON key file (the same one used locally, just pasted in as the env var
    value instead of a file path)
  - `SCHEDULE_SHEET_ID`, `SCHEDULE_SHEET_RANGE` for the crew-schedule sheet
  - `USE_DWD=1` only if domain-wide delegation is set up (per-traveler
    calendar impersonation); leave unset if each calendar was individually
    shared with the service account instead
  - The calendar fetch can take a few minutes for a large roster or wide
    date range — that's expected, not stuck (gunicorn's timeout is raised to
    5 minutes in `Procfile`/`railway.json` to accommodate it).
- **Sage projects** — one click, reuses the `INTACCT_*` credentials already
  configured for posting.

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
- **offers the traveler's past project codes as one-click "pick one" chips** when
  there's no live schedule/calendar match (below) — a fallback drawn from the
  `Project` column of the history, so projects can be assigned quickly even
  before the Google side is wired up. Archived codes are dropped.
- **cross-references Hotel Engine bookings** to sharpen that fallback: a
  flight's departure date is matched against hotel bookings on the same dates
  (Hotel Engine has no traveler name, so it matches on date + department, not
  person). When the traveler's history and the hotel-that-week agree on exactly
  one project, it's auto-filled (flagged to confirm); when they agree on a few,
  only those are offered. Build the index by uploading Hotel Engine statements
  in **Admin → step 6**; each month accumulates.

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
python scripts/fetch_sage_projects.py                               # active/archived status per project
```

### Archived projects never get auto-coded

`scripts/fetch_sage_projects.py` pulls the Projects list from Sage Intacct
(OAuth2 client-credentials — see `.env.example`) into `data/sage_projects.json`.
Once that file exists:

- The crew-schedule and calendar/registry matchers (`project_resolver.py`) skip
  archived codes when deciding what to auto-code — including resolving what used
  to be a multi-candidate "pick one" (e.g. a client with several old campus
  codes) down to a clean single match once only one of them is still active.
- The web UI's project autocomplete stops suggesting archived codes.
- As a safety net independent of the above — catching a direct vendor-stated
  code, or someone typing an old number into the web UI by hand — `validate.py`
  flags any line whose project is archived, using the same issue-highlighting
  the chart-of-accounts checks already use.

Without `data/sage_projects.json` (not fetched yet), nothing is filtered —
same "no data, don't block on it" convention as the rest of this tool.

## Cash Proof (fraud detection / reconciliation)

The **Cash Proof** page ties every bank movement to a recorded ledger entry and
queues everything that doesn't tie. Upload the bank activity export (the
`"Date","Ref/Check","Description","Amount","Balance",...` format); add a Sage
GL-detail export for the cash account to run the full tie-out — bank alone
gives a cash-activity view (monthly flows, category buckets, largest outflow
counterparties).

- **Matching ladder**: exact (auto-tied) → fuzzy (confirm) → split, 2–3 ledger
  items summing to one debit (confirm). Residuals near the period boundary are
  *timing* items, carried visibly; everything else is an exception, ranked
  critical (bank money out, no ledger tie) / high (ledger entry, no bank
  movement) / review.
- **Export integrity**: per-day net movement is checked against the change in
  day-end balance, so a doctored or truncated bank export is itself a finding.
  Offsetting one-day gaps are recognized as posting-date skew, not breaks.
- **Sweep-aware**: `TRANSFERRED TO/FROM DEPOSIT ACCT` lines on the
  target-balance account are internal cash movement, classified separately and
  never counted as exceptions.
- **Dispositions & audit**: every exception takes Accept (note required) /
  Investigating / Confirmed issue; every action records who/when/why in the
  run and appends to an append-only `out/recon/audit.jsonl`.

Configuration lives in `config/recon.yml`: the Sage GL column mapping, which
GL accounts are cash, matching windows, and bank-specific noise words. See the
Cash Proof design doc for the full phasing (Ramp depth, Bill.com duplicates,
vendor integrity, payroll).

## Configuration

- `config/sources.yml` — per-source CSV column mapping + destination.
- `config/categories.yml` — categorization rules (keyword → GL account / category).
- `config/recon.yml` — Cash Proof: Sage GL mapping, cash accounts, matching windows.
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
