# S26 Finance Dashboard — v2

Living Google Sheet + Looker Studio dashboard. Replaces v1's static Excel + manual group-moves + manual paid/collections/declined tracking.

## Decisions locked

| | |
|---|---|
| Sheet owner | adam@imageft.ie |
| Tag/sale feed | **Apps Script webhook** (ONtraport rule → POST → Sheet append) |
| Period model | derived from `start_date` against `periods.csv` config — no manual exports, ever |
| Y/Y vs A25 | dropped — period filter does the same job |
| Payment status (paid / collections / declined) | pulled from ONtraport `Transactions` API, written into `raw_students.payment_status` |
| Board access | published Looker link (read-only). Edits = invited Sheet email |

## What v2 removes from your manual work

| Manual today | Automated in v2 |
|---|---|
| Move person into class group on purchase | `group_id` auto-derived from `stream · location · start_date` |
| Tag period (S26 / A26) | `period` auto-derived from `start_date` ranges in `periods.csv` |
| Mark payment paid / collections / declined | pulled per-charge from ONtraport Transactions API |
| Rebuild Excel weekly | Sheet writes live; Looker auto-refreshes |

## Tabs (in `v2/tabs/`)

| Tab | Purpose | Owner |
|---|---|---|
| `raw_students` | one row per student × stream, decoded | sync (overwrite) |
| `class_groups` | derived: 59 groups today | sync (overwrite) |
| `periods` | config: period code → start-date window | you (manual) |
| `summary_macro` / `summary_location` / `summary_stream` | dashboard feeds | sync (overwrite) |
| `sales_log` | append-only feed of new product purchases | webhook (append) |
| `issues` | missing data flags | sync (overwrite) |
| `overrides` *(coming)* | manual refunds / discounts / write-offs | you (manual) |

## Live snapshot (5 May 26)

- 210 students · €485,544 expected · €266,112 collected · **54.8%**
- **234 S26 enrolments · 5 A26 · 51 unscheduled** (start date missing)
- 59 class groups, 9 locations
- Largest: Dublin – Swords (75 students, €160k expected)
- Top stream: PT (€308k), Pilates (€156k)
- ⚠️ **Data flag:** 47 students under bare `Swords` instead of `Dublin - Swords`. 5-min cleanup in ONtraport.

## Build order — what's done, what's next

✅ **Done**
1. v2 data model + 7 tab CSVs generated from live data
2. Auto-grouping (`group_id` from stream + location + start)
3. Period derivation from `start_date` + configurable `periods.csv`
4. Macro / location / stream summaries

🔜 **Next (waiting on you)**
5. **Create Google Sheet** named `IFT Finance — Live` under adam@imageft.ie. Import the 7 CSVs in `tabs/` as named tabs (File → Import → keep tab name as filename). Tell me when done; I'll need the Sheet URL.
6. **Wire `ontraport_sync.py` to write to the Sheet** via `gspread`. Replaces the CSV/xlsx writers. ~50 lines.
7. **Apps Script webhook** for sales feed. I'll write the script — you paste it into the Sheet's Extensions → Apps Script and deploy as a web app, then add the URL to one ONtraport rule per core-sale tag/product.
8. **Extend sync to fetch ONtraport Transactions** per contact and aggregate paid / collections / declined into `payment_status`. This is what kills the manual payment-status tracking.
9. **Build Looker Studio board view** (4 hero cards, by-stream bars, location small multiples, group table). Lock the design before adding admin extras.
10. **Build Looker admin view** on top: issues queue, sales leaderboard, payment-plan health, sync status, period filter pill.

## How the sales webhook will work (for context)

ONtraport rule: `When product purchased = "S26 PT Combination Course"` → POST to Apps Script URL with payload `{contact_id, product, amount, timestamp}`. Apps Script appends one row to `sales_log`. Looker tile shows it within ~1 minute. Same for Pilates / Reformer / S&C / PPN / AN. One rule per product, all hitting one endpoint.

This means a sale fired at 10:42am shows on the board's "Today" card by 10:43.

## Re-running the local build anytime

```
~/.venvs/ift-finance/bin/python3 v2/build_v2_tabs.py
```

Regenerates everything in `v2/tabs/` from `.op_live_s26.csv`. Useful while we're still bootstrapping; once the Sheet is live, the sync writes there directly and these CSVs become a fallback.
