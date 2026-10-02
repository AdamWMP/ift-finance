"""One-shot loader: reads .op_live_s26.csv → SQLite. Replaces the v1 build_s26 path.
Run: python -m app.ingest
"""
from __future__ import annotations
import csv, re, sys
from pathlib import Path

from .db import DATA_DIR, DB_PATH, get_db, init_db, parse_date, period_for, pathway_for, normalize_location, extract_origin_period

# Resolve path at CALL TIME — refresh_s26_csv writes the CSV to DATA_DIR
# every sync, but on a fresh prod disk that file doesn't exist when this
# module loads. Always prefer DATA_DIR; fall back to the v1 legacy path
# only for local dev.
_LEGACY_CSV = Path(__file__).resolve().parent.parent.parent / ".op_live_s26.csv"

def _live_csv() -> Path:
    p = DATA_DIR / ".op_live_s26.csv"
    return p if p.exists() else _LEGACY_CSV

# Kept for backwards compat with anyone importing LIVE_CSV directly
LIVE_CSV = DATA_DIR / ".op_live_s26.csv"

# Reformer is intentionally pulled from the Sales Board (H22) only — ONtraport
# rollups for it are unreliable, and the Sales Board is the agreed source of
# truth. Same applies to NutriCert (AN) / PPN / S&C / FBA, which never had
# their own raw_students rows because:
#   • The ONtraport rollup fields don't carry per-student spent reliably
#   • Old-term contacts with the marker still set leak into the current view
# Trade-off accepted: per-student drill-down is unavailable for these streams,
# but headline totals match the Sales Board exactly. To re-enable per-student
# ingest, restore the rows from git history (commit 8b11180) and re-add
# backfill_followon_periods to sync.py.
COURSES = [
    ("PT",       "PT Course Location",       "PT Course Qualifications",      "PT Course Start Date",       "PT Course Price",       "PT Course Spent",       "PT Course Payment Plan",       "PT Course Timetable",       "PT Payment Method"),
    ("Pilates",  "Pilates Course Location",  "Pilates Course Qualifications", "Pilates Course Start Date",  "Pilates Course Price",  "Pilates Course Spent",  "Pilates Course Payment Plan",  "Pilates Course Timetable",  "Pilates Payment Method"),
]

# Empty — kept as a constant so backfill_followon_periods in queries.py
# becomes a no-op without needing to delete the function. If we re-enable
# follow-on ingest in the future, repopulate this set to light it back up.
FOLLOWON_STREAMS: set[str] = set()

DEFERRAL_PATTERNS = [
    re.compile(r"\bS\d\d\s*(combo|pilates|reformer|sse)\s*course\s*deferral\b", re.I),
    re.compile(r"\bA\d\d\s*(combo|pilates|reformer|sse)\s*course\s*deferral\b", re.I),
]

def f(row, col): return (row.get(col) or "").strip() if col else ""
def num(v):
    try: return float(v) if v else 0.0
    except ValueError: return 0.0

def derive_payment_status(price: float, spent: float) -> str:
    if price == 0 and spent == 0: return "unpaid"
    if price > 0 and spent >= price: return "paid"
    if spent > 0: return "partial"
    return "unpaid"

def is_grant(plan: str) -> bool:
    p = (plan or "").upper()
    return "DSP" in p or "GRANT" in p

def group_id(stream: str, location: str, start: str) -> str:
    return " · ".join([stream, location or "—", start or "TBD"]).strip()

def ingest_csv():
    init_db()
    csv_path = _live_csv()
    if not csv_path.exists():
        print(f"!! {csv_path} not found"); return

    rows = list(csv.DictReader(csv_path.open()))
    print(f"loaded {len(rows)} students from {csv_path}")

    with get_db() as c:
        # Full refresh of LIVE rows only. Seeded historical rows (A25/S25
        # imports) must survive the wipe so the ON CONFLICT guard below can
        # refresh their `spent` and nothing else — a plain DELETE would drop
        # them, and the re-insert from ONtraport's current fields would
        # silently re-home them to whatever term those fields now describe.
        c.execute("DELETE FROM students WHERE COALESCE(is_seeded, 0) = 0")
        # Terms that were imported from a Finance Report workbook. For those
        # the workbook is the whole term (Adam, 2 Oct 2026): a live ONtraport
        # row whose dates would land it there is NOT counted — it's held out
        # (revenue_period='') and surfaced as 'unlisted' on the cohort so it
        # can be added to the sheet or corrected in ONtraport.
        from .db import seeded_terms as _seeded_terms
        seeded_terms = _seeded_terms()
        held_out = 0
        ins = 0
        for r in rows:
            for stream, loc_c, qual_c, start_c, price_c, spent_c, plan_c, tt_c, method_c in COURSES:
                qual = f(r, qual_c)
                price = num(f(r, price_c))
                spent = num(f(r, spent_c))
                if not qual and price == 0 and spent == 0:
                    continue

                location = normalize_location(f(r, loc_c))
                # Online cohorts (and the Launchpad Bundle which is online-only)
                # are grouped by stream + start_date alone; the student's home
                # location doesn't matter once they're on an online course.
                if location.lower() == "online" or "launchpad" in (qual or "").lower():
                    location = "Online"
                start_raw = f(r, start_c)
                start_d = parse_date(start_raw)
                start_iso = start_d.isoformat() if start_d else ""
                cls_period = period_for(start_d)
                rev_period = cls_period  # equal unless deferral (set below)

                plan = f(r, plan_c)
                # Deferral detection.
                # A deferred student stays counted in their ORIGINAL term
                # (revenue_period = the term they signed up in), while their
                # class_period moves to whichever term their new start_date
                # lands in. That way:
                #   • S26 sales figures keep counting them
                #   • They show up in their new A26 class group cohort
                #   • A26 term revenue does NOT double-count them
                # The origin term is parsed out of whichever deferral marker
                # text matched (e.g. "S26 Pilates Course Deferral" → S26).
                is_def = 0
                origin_period = ""
                for p in DEFERRAL_PATTERNS:
                    m = p.search(qual) or p.search(plan)
                    if m:
                        is_def = 1
                        origin_period = extract_origin_period(m.group(0))
                        break
                if is_def:
                    rev_period = origin_period or ""  # blank only if origin unparseable
                # Workbook-owned term: the live row's money is not this
                # term's (see seeded_terms above). Class stays, money held out.
                if rev_period and rev_period in seeded_terms:
                    rev_period = ""
                    held_out += 1

                # The CSV's "Name" column already holds the full name; the
                # separate "Last Name" column duplicates the surname. Split
                # cleanly for display.
                full = (r.get("Name") or "").strip()
                last = (r.get("Last Name") or "").strip()
                if full.endswith(" " + last):
                    first = full[: -(len(last) + 1)].strip()
                else:
                    first = full
                c.execute("""
                    INSERT INTO students (
                        contact_id, first_name, last_name, email, phone,
                        stream, qualification, location, start_date, timetable,
                        price, spent, payment_plan, payment_method, pathway,
                        group_id, class_period, revenue_period,
                        is_deferral, is_dropoff, is_grant, payment_status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(contact_id, stream) DO UPDATE SET
                        -- Seeded historical rows (A25/S25 imports): the SHEET owns the
                        -- sale — price, revenue term, drop-off/grant flags, plan, method,
                        -- what was bought. ONtraport owns the CLASS — where the student
                        -- physically sits now (location / start / timetable / class term)
                        -- plus the money (spent) and contact details. A student sold in
                        -- A25 for a Feb-2026 intake therefore keeps A25 revenue and gets
                        -- an S26 class, and is marked cross-term (is_deferral) so S26
                        -- head-counts them without re-counting their money.
                        first_name     = excluded.first_name,
                        last_name      = excluded.last_name,
                        email          = excluded.email,
                        phone          = excluded.phone,
                        qualification  = CASE WHEN students.is_seeded=1 THEN students.qualification  ELSE excluded.qualification  END,
                        location       = CASE WHEN students.is_seeded=1 AND excluded.location   = '' THEN students.location   ELSE excluded.location   END,
                        start_date     = CASE WHEN students.is_seeded=1 AND excluded.start_date = '' THEN students.start_date ELSE excluded.start_date END,
                        timetable      = CASE WHEN students.is_seeded=1 AND excluded.timetable  = '' THEN students.timetable  ELSE excluded.timetable  END,
                        price          = CASE WHEN students.is_seeded=1 THEN students.price          ELSE excluded.price          END,
                        spent          = excluded.spent,
                        payment_plan   = CASE WHEN students.is_seeded=1 THEN students.payment_plan   ELSE excluded.payment_plan   END,
                        payment_method = CASE WHEN students.is_seeded=1 THEN students.payment_method ELSE excluded.payment_method END,
                        pathway        = CASE WHEN students.is_seeded=1 THEN students.pathway        ELSE excluded.pathway        END,
                        group_id       = CASE WHEN students.is_seeded=1 AND excluded.start_date = '' THEN students.group_id     ELSE excluded.group_id     END,
                        class_period   = CASE WHEN students.is_seeded=1 AND excluded.class_period = '' THEN students.class_period ELSE excluded.class_period END,
                        revenue_period = CASE WHEN students.is_seeded=1 THEN students.revenue_period ELSE excluded.revenue_period END,
                        is_deferral    = CASE WHEN students.is_seeded=1
                                              THEN CASE WHEN excluded.class_period != ''
                                                         AND excluded.class_period != COALESCE(students.revenue_period, '')
                                                        THEN 1 ELSE students.is_deferral END
                                              ELSE excluded.is_deferral END,
                        is_grant       = CASE WHEN students.is_seeded=1 THEN students.is_grant       ELSE excluded.is_grant       END,
                        payment_status = CASE WHEN students.is_seeded=1
                                              THEN CASE WHEN students.price <= 0 THEN 'unpaid'
                                                        WHEN excluded.spent >= students.price THEN 'paid'
                                                        WHEN excluded.spent > 0 THEN 'partial'
                                                        ELSE 'unpaid' END
                                              ELSE excluded.payment_status END
                """, (
                    r.get("Contact ID","").strip(),
                    first, last,
                    r.get("Email","").strip(), r.get("SMS Number","").strip(),
                    stream, qual, location, start_iso, f(r, tt_c),
                    price, spent, plan,
                    f(r, method_c) or ("Stripe" if (price > 0 or spent > 0) else ""),
                    pathway_for(qual, stream),
                    group_id(stream, location, start_iso), cls_period, rev_period,
                    is_def, 0, 1 if is_grant(plan) else 0,
                    derive_payment_status(price, spent),
                ))
                ins += 1
        # Real count of LIVE rows held out (the loop counter also ticks for
        # seeded contacts the CSV re-feeds, whose blank the guard discards).
        if seeded_terms:
            qm = ",".join("?" * len(seeded_terms))
            held_out = c.execute(
                f"SELECT COUNT(*) FROM students WHERE COALESCE(is_seeded,0)=0 "
                f"AND class_period IN ({qm}) AND COALESCE(revenue_period,'')=''",
                tuple(sorted(seeded_terms))).fetchone()[0]
        print(f"upserted {ins} student×stream rows → {DB_PATH.name}"
              + (f" · {held_out} live rows held out of workbook-owned terms {sorted(seeded_terms)}" if held_out else ""))


# --- Historical-term JSON seed import ---------------------------------------
# Used for back-loading prior terms (A25 etc.) whose contacts aren't
# discoverable via the current ONtraport year-filter. The seed file is built
# by scripts/build_a25_seed.py from the historical Excel reports.

def import_period_seed(seed_path: Path | str, *, force_period: str | None = None) -> dict:
    """Load a {period, students, method_overrides} JSON seed and upsert its
    student rows into the students table.

    `force_period` overrides revenue_period for every row (useful for A25
    where the source data already represents the term and we want to stamp
    everything with revenue_period='A25' regardless of start_date).
    """
    import json
    p = Path(seed_path)
    if not p.exists():
        return {"ok": False, "error": f"seed not found: {p}"}
    seed = json.loads(p.read_text())
    period = force_period or seed.get("period") or ""

    init_db()
    n_students, n_overrides, n_sb, n_snap = 0, 0, 0, 0
    with get_db() as c:
        for r in seed.get("students", []):
            qual     = r.get("qualification", "") or ""
            stream   = r.get("stream", "PT")
            location = normalize_location(r.get("location", "") or "")
            if location.lower() == "online" or "launchpad" in qual.lower():
                location = "Online"
            start_iso = r.get("start_date", "") or ""
            start_d   = parse_date(start_iso) if start_iso else None
            # Historical seed: every row sits in the seeded term regardless of
            # start_date (cohorts that began before the term's nominal window —
            # e.g. a Jun-2025 Swords intake that ran into A25 — are A25 in the
            # workbook and must stay so). class_period tracks the workbook.
            cls_period= period or (period_for(start_d) if start_d else "")
            # Seed override: revenue_period is the term we're seeding
            rev_period= period or cls_period
            if r.get("is_deferral"):
                # Money belongs to the origin term. The seed carries it when a
                # '<term> … Sale' tag exists; otherwise blank, and the normal
                # invoice-based backfill (backfill_deferral_periods) fills it.
                rev_period = (r.get("revenue_period") or "")
                if rev_period == period:
                    rev_period = ""
            price = float(r.get("price", 0) or 0)
            spent = float(r.get("spent", 0) or 0)
            method = (r.get("payment_method", "") or "").strip() \
                     or ("Stripe" if (price > 0 or spent > 0) else "")
            plan = r.get("payment_plan", "") or ""
            c.execute("""
                INSERT INTO students (
                    contact_id, first_name, last_name, email, phone,
                    stream, qualification, location, start_date, timetable,
                    price, spent, payment_plan, payment_method, pathway,
                    group_id, class_period, revenue_period,
                    is_deferral, is_dropoff, is_grant, payment_status, is_seeded
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(contact_id, stream) DO UPDATE SET
                    first_name=excluded.first_name, last_name=excluded.last_name,
                    email=excluded.email, phone=excluded.phone,
                    qualification=excluded.qualification, location=excluded.location,
                    start_date=excluded.start_date, timetable=excluded.timetable,
                    price=excluded.price,
                    -- First import takes the workbook's Paid; once the row is
                    -- live (is_seeded=1) the sync owns `spent` and a re-import
                    -- must not drag it back to the sheet's snapshot.
                    spent=CASE WHEN students.is_seeded=1 THEN students.spent ELSE excluded.spent END,
                    payment_plan=excluded.payment_plan, payment_method=excluded.payment_method,
                    pathway=excluded.pathway, group_id=excluded.group_id,
                    class_period=excluded.class_period, revenue_period=excluded.revenue_period,
                    is_deferral=excluded.is_deferral, is_dropoff=excluded.is_dropoff,
                    is_grant=excluded.is_grant,
                    payment_status=excluded.payment_status, is_seeded=1
            """, (
                r["contact_id"], r.get("first_name", ""), r.get("last_name", ""),
                r.get("email", ""), r.get("phone", ""),
                stream, qual, location, start_iso, r.get("timetable", ""),
                price, spent, plan, method,
                pathway_for(qual, stream),
                group_id(stream, location, start_iso), cls_period, rev_period,
                int(r.get("is_deferral", 0)), int(r.get("is_dropoff", 0)),
                1 if is_grant(plan) else 0,
                derive_payment_status(price, spent),
            ))
            n_students += 1

        # Drop and rewrite this term's seed-method overrides so re-import is
        # idempotent. Tagged with note containing 'A25 Finance Report'.
        c.execute("""DELETE FROM transactions
                     WHERE source='manual' AND period=?
                       AND note LIKE '%Finance Report%'""", (period,))
        for o in seed.get("method_overrides", []):
            c.execute("""
                INSERT INTO transactions
                    (date, period, direction, category, subcategory, amount,
                     contact_id, source, status, note)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (o.get("date") or "2025-09-01", period, "in", "course_sale",
                  o.get("method", ""), float(o.get("amount") or 0),
                  o.get("contact_id", ""), "manual", "paid",
                  o.get("note", "")))
            n_overrides += 1

        # Other-streams totals (Reformer / NutriCert / PPN / S&C / FBA /
        # sponsored / grant) — the cells the workbook's DASHBOARD pulls by
        # IMPORTRANGE. Stored as sales_board transactions so macro() /
        # by_stream() treat them exactly like the live Sales Board does for
        # S26. The static overlap rule still excludes tesg_grants from the
        # headline, matching the sheet (grant is shown separately there too).
        c.execute("""DELETE FROM transactions
                     WHERE source='sales_board' AND period=?
                       AND note LIKE '%Finance Report%'""", (period,))
        for sb in seed.get("sales_board", []):
            amt = float(sb.get("amount") or 0)
            if amt <= 0: continue
            c.execute("""
                INSERT INTO transactions
                    (date, period, direction, category, subcategory, amount,
                     contact_id, source, status, note)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (seed.get("as_of") or "2026-07-29", period, "in", sb["category"], "",
                  amt, "", "sales_board", "paid",
                  f"Imported from {period} Finance Report · {sb.get('label', sb['category'])}"))
            n_sb += 1

        # Monthly 'what & when has money come in' points → trend line.
        for s in seed.get("snapshots", []):
            c.execute("""
                INSERT INTO snapshots (snapshot_date, period, sales_count, revenue)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(snapshot_date, period) DO UPDATE SET
                    sales_count = excluded.sales_count,
                    revenue     = excluded.revenue,
                    created_at  = datetime('now')
            """, (s["date"], period, int(s.get("sales") or 0), float(s.get("revenue") or 0)))
            n_snap += 1
    return {"ok": True, "period": period,
            "students_upserted": n_students,
            "method_overrides_loaded": n_overrides,
            "sales_board_rows": n_sb,
            "snapshots": n_snap}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        print(import_period_seed(sys.argv[2]))
    else:
        ingest_csv()
