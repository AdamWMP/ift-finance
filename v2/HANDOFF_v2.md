# IFT Finance Dashboard · v2 Handoff

A self-contained brief for resuming work in a new chat. Read top → bottom; nothing assumed.

---

## What this is

A web dashboard that replaces the v1 Excel + manual Google-Sheets-based finance tracker. Pulls live from ONtraport, reads the IFT Sales Board (Apps Script web app), exposes everything via FastAPI on `https://ift-finance.onrender.com` (custom domain `finance.imageft.ie` pending DNS).

Auth: single passphrase `newminds123` behind an `itsdangerous`-signed cookie.

## Where it lives

| Surface | URL / path |
|---|---|
| Production dashboard | `https://ift-finance.onrender.com` (custom domain `finance.imageft.ie` pending) |
| Render service | `ift-finance` web service, plan `starter` ($7/mo), 1GB persistent disk at `/data` |
| GitHub repo | `https://github.com/AdamWMP/ift-finance` (private) |
| Local dev | `~/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance/v2/` |
| Local server | `uvicorn app.main:app --host 127.0.0.1 --port 8765` |
| ONtraport | `app.ontraport.com` — App ID `2_98540_7LPYP2Ces`, Key `1l3In6M39GMuhtX` |
| IFT Sales Board (Apps Script web app) | URL set as env `IFT_SALES_BOARD_URL`, token `newminds123` |
| Gmail SMTP | `smtp.gmail.com:587`, user `adam@imageft.ie`, password = env `IFT_SMTP_PASS` |

## Architecture

```
ONtraport API ─┐
               ├─► refresh_s26_csv()  (writes /data/.op_live_s26.csv)
               │   ingest_csv()       (reads CSV → SQLite `students` table)
               │   apply_tags()       (sets is_dropoff / is_deferral / is_grant)
               │   ingest_invoices()  (paginated per-contact, → `invoices` table)
               │
Sales Board ───┤   sales_board.write_transactions() (reads aggregate cells → `transactions`)
(Apps Script)  └─► sales_board.push_live_money_in() (POSTs L18 = collected/2100)

           ALL above run via app.sync.main() every 10 min (in-process scheduler)
                                ↓
SQLite at /data/ift_finance.db ──► FastAPI app.main ──► Jinja2 templates
                                                    ├─► /board (Dashboard View)
                                                    ├─► /admin/chase (Debt Collection)
                                                    ├─► /admin/work (Admin Work to-dos)
                                                    ├─► /admin/transactions (manual CRUD)
                                                    ├─► /group?id= (cohort drill-down)
                                                    ├─► /pathway?name= (pathway drill-down)
                                                    ├─► /method?name= (payment-method drill)
                                                    └─► /location?name= (location drill)
                                ↓
                      app.digest.send_digest() — daily HTML email (Mon-Fri 09:00 UTC)
                      Triggered by the same in-process scheduler.
```

## Render env vars (set in Render → ift-finance → Environment)

```
DATA_DIR              = /data
PORT                  = 10000
IFT_FIN_PASS          = newminds123     ← dashboard login
IFT_FIN_SECRET        = (auto-generated cookie signing key)
OP_APP_ID             = 2_98540_7LPYP2Ces
OP_API_KEY            = 1l3In6M39GMuhtX
IFT_SALES_BOARD_URL   = https://script.google.com/macros/s/AKfycbxAVdu7RmFBjg3yIu52927IyJjjKCxM8kY-1QR_RdirQOzSSmH7Goy5wUoUm1w78BXa/exec
IFT_SALES_BOARD_TOKEN = newminds123
IFT_SMTP_USER         = adam@imageft.ie
IFT_SMTP_PASS         = (Gmail app password — same as ~/.zshrc)
IFT_DIGEST_TO         = adam@imageft.ie
IFT_DASHBOARD_URL     = https://finance.imageft.ie
IFT_SCHEDULER         = 1
IFT_SYNC_INTERVAL_SEC = 600              ← 10 min
IFT_DIGEST_HOUR_UTC   = 8                ← 09:00 Dublin BST (or "8:30" for 9:30 etc)
```

## Module map

| File | Role |
|---|---|
| `app/main.py` | FastAPI routes, auth middleware, scheduler hookup |
| `app/auth.py` | passphrase + signed cookie |
| `app/db.py` | SQLite schema, period derivation, pathway / location helpers |
| `app/ingest.py` | reads `.op_live_s26.csv` → upserts `students` |
| `app/queries.py` | every read-side query, ~1000 lines, the heart of the data layer |
| `app/ontraport.py` | discovery (`f2288=586`, `f2300=587`, `f2590=616`), contact decode, invoice fetch, tag apply |
| `app/sales_board.py` | reads aggregate cells from Apps Script web app, posts L18 |
| `app/sync.py` | end-to-end sync entry point (called by scheduler + `↻ Refresh`) |
| `app/scheduler.py` | in-process thread: runs sync every 10 min + digest at HH:MM UTC |
| `app/digest.py` | daily HTML email builder + sender |
| `app/templates/_base.html` | shell + topbar (nav + ⌘K + theme + ↻) |
| `app/templates/board.html` | Dashboard View (hero, today, sales target, pathway, money in, location, drop-offs, class groups) |
| `app/templates/group.html` | cohort drill-down (forecast, debt collection, students) |
| `app/templates/pathway.html` | pathway drill-down |
| `app/templates/filter.html` | shared template for /method and /location drill-downs |
| `app/templates/chase.html` | Debt Collection View |
| `app/templates/admin_work.html` | auto-generated admin to-do list |
| `app/templates/transactions.html` | manual transactions CRUD + per-student split |
| `app/static/app.css` | all styling (dark + light theme) |
| `app/static/app.js` | search modal, theme toggle, filter pills, kb nav, notes popover |
| `Dockerfile` | python:3.12-slim + pip install + uvicorn |
| `render.yaml` | Render Blueprint (single web service, persistent disk) |
| `DEPLOY.md` | one-time deploy walkthrough |
| `SETUP_SHEETS.md` | Apps Script setup walkthrough |

## What's working

✅ Auto-discovery of new sales (PT, Pilates, Reformer year=2026)
✅ Auto-grouping `stream · location · start_date` (Online + Launchpad collapsed)
✅ Hero metrics in v1 wording (TOTAL FEES DUE / PAID / OWED, ADJUSTED FOR DROP-OFFS)
✅ Sales/revenue target with milestone €-values + grey-out unreachable + 60-day daily-rate projections
✅ Today + Last 7 days panels (new sales / payments / failures / certs / avg deposit)
✅ Recent new sales feed (8 most recent)
✅ How money has come in (Stripe default + manual splits override)
✅ By stream / pathway / location / payment method — all drillable
✅ Class groups: month-grouped, stream colour dots, filter pills (stream/location/status), CSV export
✅ Group page: forecast card, per-group debt-collection panel, students with cert progress + plan shorthand + 💳 Log payment + 📝 Notes + ↗ ONtraport
✅ Drop-offs section (uses tag id 2031 from ONtraport)
✅ Debt Collection View: deduped per contact, j/k/Enter/w keyboard nav, WhatsApp + ONtraport buttons
✅ Admin Work auto-generated tasks with persistent dismiss + 30s undo
✅ Transactions: manual CRUD; per-student split (auto-Stripe + cash/bank/DSP overrides)
✅ Term selector dropdown (S25, A25, S26, A26 etc, auto-populated)
✅ ⌘K Spotlight-style search (students, groups, invoice IDs)
✅ Theme toggle (dark / light) + `?clean=1` share-friendly mode
✅ Sticky headers (only on class groups table, by request)
✅ Sync auto-runs every 10 min on Render
✅ ↻ Refresh button kicks sync immediately
✅ Last-sync indicator in footer
✅ Single passphrase auth + 30-day signed cookie
✅ HTTPS-only (Cloudflare in front of Render)
✅ Persistent SQLite on Render disk

## Known issues / loose ends

🔧 **Custom domain `finance.imageft.ie` deferred** — user said leave for now. Production runs on `https://ift-finance.onrender.com` directly. Pick up later: Render → ift-finance → Settings → Custom Domains → Add → CNAME `finance` → `ift-finance.onrender.com`.

🔧 **L18 push to Apps Script — fix in progress.** The bug: Render's outbound proxy strips POST body across the Apps Script 302 redirect. Fix shipped:
   - `sales_board.gs` now accepts `?action=write&cell=L18&value=N&token=...` via doGet (GET survives the redirect cleanly).
   - `sales_board.py` switched the primary path from POST to GET.
   - **User must redeploy the Apps Script** (paste latest `sales_board.gs` into the editor → Deploy → Manage deployments → edit → New version → Deploy).

✅ **`Last Invoice Pay URL` ONtraport rule not yet set up.** When done, the chase view's WhatsApp button auto-includes the pay link. ~5 min in ONtraport's campaign builder. Field exists in Contact (`f2624`), just empty.

✅ **Daily digest email confirmed working end-to-end.** SMTP fixed (Gmail required app-specific password — was using regular pass). New app password set in Render env var `IFT_SMTP_PASS`. Manual trigger at `POST /admin/digest/send-now` works; tolerant scheduler fires daily at `IFT_DIGEST_HOUR_UTC` (Mon-Fri).

🔧 **No A26 data yet** in the db. Term comparison UI not built — backend `compare_periods()` is ready.

🔧 **Pathway labels are verbose** ("The Career (Fitness, Group, PT, Nutrition, Fitness Business Accelerator)") because that's the raw qual name in ONtraport. Could simplify by adding a label override map in `db.PATHWAY_MAP`.

## Running locally

```bash
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance/v2"
~/.venvs/ift-finance/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
# → http://127.0.0.1:8765
```

Env vars needed locally for full sync (otherwise just student/invoice ingest works):
```bash
export IFT_SALES_BOARD_URL='https://script.google.com/macros/s/AKfycbxAVdu7RmFBjg3yIu52927IyJjjKCxM8kY-1QR_RdirQOzSSmH7Goy5wUoUm1w78BXa/exec'
export IFT_SALES_BOARD_TOKEN='newminds123'
```

## Updating in production

1. Edit code locally, commit + push to `main` on `AdamWMP/ift-finance`
2. Render auto-deploys on push (~2 min)
3. Watch Render logs for `==> Your service is live 🎉`
4. Hit ↻ Refresh on the dashboard if you want fresh data right away

## Roadmap — what's next (ordered by ROI)

### Phase 1 finish (loose ends)
- [ ] Add `finance.imageft.ie` CNAME → Render
- [ ] Verify daily digest email arrives Mon
- [ ] Set up `Last Invoice Pay URL` automation in ONtraport campaign builder

### Phase 2 — trend + history
- [ ] **Snapshot job** — auto-capture revenue + sales-equiv on 29th + 27 Feb monthly into `snapshots` table
- [ ] **Trend chart** — Chart.js line graph of revenue over time, sparklines on hero cards
- [ ] **Term comparison UI** — Shift+click another term to load side-by-side hero cards with deltas (backend ready)
- [ ] **Pre-payday tracker** — derive from snapshots; small table on board

### Phase 3 — sharpen
- [ ] **Student profile page** at `/contact?id=` (full history per student)
- [ ] **Bulk actions** in tables (multi-select → action toolbar)
- [ ] **P&L view** — combine revenue (ONtraport + Sales Board) − manual transaction expenses
- [ ] **Drill into non-PT/Pilates groups** — Reformer Pilates, S&C, Advanced Programming, FBA, NutriCert
  etc. currently only show as aggregate revenue from Sales Board. Need a per-student view for these:
  read the Sales Board's per-student listings (rows 22+ of the S26 tab) into the dashboard so each can
  drill into their own students table just like PT cohorts do.

### Phase 4 — historical backfill (S25 + A25 + term-over-term)
- [ ] **Backfill S25 + A25 contacts** — discovery currently filters PT 2026 / Pilates 2026 / Reformer 2026.
  Add 2025 option IDs (PT `f2288=543`, Pilates `f2300=545`, Reformer `f2590=…` — verify via `/objects/meta`)
  and run a one-off backfill so any S25/A25 contacts with **active payment plans** appear with their period
  set correctly. The period derivation already handles S25/A25 from start_date so no schema changes needed.
- [ ] **Active-plans-only filter** — many S25/A25 students are fully paid; only show in the dashboard if
  outstanding balance > 0 OR if they have an unpaid invoice. Stops the historical data from drowning S26.
- [ ] **Term comparison UI** — Shift+click another term in the dropdown → load both side-by-side with
  deltas (`S26 vs S25 → +12% revenue, +8% sales, –3pp collection rate`). Backend `compare_periods()` ready.
- [ ] **Year-over-year card on hero** — once at least one prior comparable term has data, show
  "vs S25 same week-of-term" sparkline + delta on the headline metrics.

### Phase 4 polish
- [ ] **Export class group as attendance template (CSV/PDF)** — "Download attendance sheet" on each
  group page. Generates a printable / editable template tutors can fill out per session. Same student
  list, plus blank columns for week-by-week attendance ticks.

### Phase 5 — education data
- [ ] **Attendance + progress tracking on class groups** — wire up education data alongside finance:
  attendance per session, assignments handed in, exam scores, cert-ready (already tracked by us) +
  cert-issued (already tracked). Whether this lives in ONtraport or a new module TBD.
- [ ] **Student progress timeline** — on student profile, show a unified timeline:
  enrolled → first payment → cert-ready (50%) → final payment → cert-issued, plus attendance dots.

### Stretch
- [ ] Real-time new-sale notifications (ONtraport webhook → dashboard → optional Slack)
- [ ] Public read-only board view at `finance.imageft.ie/public` (no passphrase, sanitised)
- [ ] Cohort analytics (drop-off rate per location, avg days-to-cert-ready, etc.)
- [ ] Sales celebration overlay (confetti when a new sale lands)
- [ ] Mobile polish pass

## How the data flows on every sync (10-min cadence)

1. `refresh_s26_csv()` — search ONtraport for `f2288=586 OR f2300=587 OR f2590=616`, fetch all matching contacts in batches of 50, decode dropdowns/timestamps/prices, write to `/data/.op_live_s26.csv`. Currently captures **370 contacts**.
2. `ingest_csv()` — for each contact + each course stream they're enrolled in, upsert one `students` row. ~396 rows.
3. `apply_tags()` — fetch all 336 tags, then for each contact look up their tag list; set `is_dropoff` (id 2031), `is_deferral` (name contains "deferral"), `is_grant` (name contains DSP/grant/TESG/Skillnet).
4. `ingest_invoices()` — for each contact, fetch all their invoices via condition `contact_id IN (...)`, decode statuses (Closed/Collections/Declined etc), write to `invoices` table.
5. `sales_board.write_transactions()` — GET 11 named cells (B20, C20, D20, F20, H19, I19, J19, H22, I22, J22, K22) from the Apps Script web app, write to `transactions` table with source=`sales_board`.
6. `sales_board.push_live_money_in()` — compute `total_collected ÷ 2100`, POST to L18. **Currently logs warning, doesn't update sheet.**
7. `set_meta("last_sync_at", now)` — used by the dashboard footer.

## Quick test commands

```bash
# Just smoke-test discovery
python -c "from app.ontraport import discover_s26_contact_ids as f; print(len(f()))"
# Re-ingest students from existing CSV
python -m app.ingest
# Run full sync (everything)
python -m app.sync
# Build digest preview to /tmp
python -m app.digest S26 --preview
```

## Credentials lookup

| Where | Value |
|---|---|
| Render | OAuth via GitHub `AdamWMP` |
| GitHub | `AdamWMP/ift-finance` (private) |
| ONtraport API | App `2_98540_7LPYP2Ces` / Key `1l3In6M39GMuhtX` |
| Apps Script SECRET | `newminds123` (must match `IFT_SALES_BOARD_TOKEN`) |
| Dashboard passphrase | `newminds123` (`IFT_FIN_PASS`) |
| Gmail app password | in `~/.zshrc` as `IFT_SMTP_PASS` |
| Drop-off tag id | 2031 (hardcoded in `ontraport.py`) |
| Customer tag id | 50 (constant in `ontraport.py`, not currently used in discovery) |
| Year option IDs | PT 2026 = 586, Pilates 2026 = 587, Reformer 2026 = 616 |

## Field IDs reference (verified against ONtraport `/objects/meta`)

```
PT:       qual=f2290 location=f2291 timetable=f2292 start=f2293 price=f2294
          spent=f2334 plan=f2296 method=f2295 year=f2288
Pilates:  qual=f2302 location=f2303 timetable=f2304 start=f2305 price=f2306
          spent=f2335 plan=f2309 method=f2538 year=f2300
Reformer: qual=f2592 location=f2593 timetable=f2594 start=f2595 price=f2596
          spent=f2599 plan=f2598 year=f2590
Pay URL:  Contact f2624 (set by an ONtraport rule that's not yet built)
```
