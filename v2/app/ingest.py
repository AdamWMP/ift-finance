"""One-shot loader: reads .op_live_s26.csv → SQLite. Replaces the v1 build_s26 path.
Run: python -m app.ingest
"""
from __future__ import annotations
import csv, re
from pathlib import Path

from .db import DATA_DIR, DB_PATH, get_db, init_db, parse_date, period_for, pathway_for, normalize_location

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
        c.execute("DELETE FROM students")
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
                # Deferral detection — placeholder until tag fetch lands.
                # Once tags arrive, deferral truth comes from the tag list.
                is_def = 0
                if any(p.search(qual) or p.search(plan) for p in DEFERRAL_PATTERNS):
                    is_def = 1
                    rev_period = ""  # money belongs to original (unknown here) term

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
                        first_name=excluded.first_name, last_name=excluded.last_name,
                        email=excluded.email, phone=excluded.phone,
                        qualification=excluded.qualification, location=excluded.location,
                        start_date=excluded.start_date, timetable=excluded.timetable,
                        price=excluded.price, spent=excluded.spent,
                        payment_plan=excluded.payment_plan, payment_method=excluded.payment_method,
                        pathway=excluded.pathway,
                        group_id=excluded.group_id, class_period=excluded.class_period,
                        revenue_period=excluded.revenue_period,
                        is_deferral=excluded.is_deferral, is_grant=excluded.is_grant,
                        payment_status=excluded.payment_status
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
        print(f"upserted {ins} student×stream rows → {DB_PATH.name}")

if __name__ == "__main__":
    ingest_csv()
