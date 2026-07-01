# Next steps to go from scaffold → production

The pipeline runs end-to-end in **dry-run** today. To make it actually post,
these are the remaining pieces, roughly in order.

## 1. Lock down the real CSV formats
The `columns` mappings in `config/sources.yml` are educated guesses. Export one
real file from each of United, Hotel Engine, UPS, and National, and update the
column headers to match. The `samples/*.csv` files show the expected shape.

## 2. Put in your real chart of accounts
Replace the placeholder `gl_account` numbers in `config/categories.yml` with your
actual Sage Intacct account numbers, and add rules for any vendor-specific line
descriptions you want split out (taxes, fees, surcharges, etc.).

## 3. Wire live Sage Intacct posting
`destinations/sage_intacct.py::post_journal_entry` is stubbed. To implement:
- Register at https://developer.sage.com/intacct/ and get Sender ID/password,
  Company ID, and a web-services User ID/password.
- Implement session auth + the create-journal-entry REST call against the
  sandbox first. Verify the journal symbol, and any required dimensions
  (location, department, project) for your company.
- Confirm the balancing credit account (`INTACCT_CLEARING_ACCOUNT`).

## 4. Wire live Bill.com posting
`destinations/billdotcom.py::post_bill` is stubbed. To implement:
- Register at https://developer.bill.com/ for a dev key + org.
- Implement v3 login (session id) + `POST /bills` against the stage gateway.
- Add **vendor resolution**: look up the Bill.com `vendorId` by name (create if
  missing) instead of passing `vendorName`.
- Map each line's `gl_account` to the Bill.com `chartOfAccountId`.

## 5. Guardrails before real posting
- **Duplicate detection**: don't post the same invoice number twice.
- **Balance/round checks** already run for Sage; add totals reconciliation for
  Bill.com (line items must equal invoice total).
- **Audit log**: keep the `out/*.json` proposals and record post responses.

## 6. Optional: smarter categorization
The current categorizer is rule-based (deterministic, auditable). For fuzzy line
items — e.g. a hotel folio mixing room/parking/resort-fee/tax — add an
LLM-assisted pass that proposes categories for anything that hits the
`uncategorized` fallback, still routed through the human review gate.

## 7. Optional: no-code shortcut
Zapier has both Sage Intacct and Bill.com integrations. For the structured CSV
sources you could run parts of this via Zapier instead of hosting code. A hybrid
(Zapier for simple routes, this tool for the splitting/categorization logic) is
reasonable.
