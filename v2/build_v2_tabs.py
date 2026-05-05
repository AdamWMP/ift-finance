"""
Build v2 Google Sheet tabs from the live ONtraport CSV.

Outputs CSVs in ./tabs/ ready to paste into a Google Sheet (one tab per file).
Run after every ontraport_sync.py to refresh local copies; eventually this
gets replaced by a gspread writer.
"""
from __future__ import annotations
import csv, os, re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_CSV = ROOT / ".op_live_s26.csv"
OUT = Path(__file__).resolve().parent / "tabs"
OUT.mkdir(exist_ok=True)

COURSES = [
    # (stream, location_col, qual_col, start_col, price_col, spent_col, plan_col, timetable_col)
    ("PT",       "PT Course Location",        "PT Course Qualifications",       "PT Course Start Date",       "PT Course Price",       "PT Course Spent",       "PT Course Payment Plan",       "PT Course Timetable"),
    ("Pilates",  "Pilates Course Location",   "Pilates Course Qualifications",  "Pilates Course Start Date",  "Pilates Course Price",  "Pilates Course Spent",  "Pilates Course Payment Plan",  "Pilates Course Timetable"),
    ("Reformer", "Reformer Course Location",  "Reformer Course Qualification",  "Reformer Course Start Date", "Reformer Course Price", "Reformer Pilates Course Spent", "Reformer Pilates Payment Plan", "Reformer Course Timetable"),
    ("S&C",      "S&C Location",              "S&C Course",                     "S&C Start Date",             "S&C Price",             "S&C Spent",             "S&C Payment Plan",             None),
    ("PPN",      None,                        "PPN Course",                     None,                         "PPN Price",             "PPN Spent",             "PPN Payment Plan",             None),
    ("AN",       None,                        "AN Course",                      None,                         "AN Price",              "AN Spent",              "AN Payment Plan",              None),
]

def f(row, col):
    return (row.get(col) or "").strip() if col else ""

def parse_date(s):
    if not s: return None
    for fmt in ("%d-%m-%Y","%Y-%m-%d","%d/%m/%Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: continue
    return None

def load_periods():
    p = OUT/"periods.csv"
    if not p.exists(): return []
    out = []
    for r in csv.DictReader(p.open()):
        out.append((r["period"], parse_date(r["start_from"]), parse_date(r["start_to"])))
    return out

def period_for(start_str, periods):
    d = parse_date(start_str)
    if not d: return ""
    for code, lo, hi in periods:
        if lo and hi and lo <= d <= hi:
            return code
    return ""

def num(v):
    try:
        return float(v) if v else 0.0
    except ValueError:
        return 0.0

def group_id(stream, location, start):
    parts = [stream, location or "—", start or "TBD"]
    s = " · ".join(parts)
    return re.sub(r"\s+", " ", s).strip()

def main():
    rows = list(csv.DictReader(LIVE_CSV.open()))
    periods = load_periods()
    print(f"loaded {len(rows)} students, {len(periods)} periods")

    # ---- raw_students: one row per (student × stream) enrolment -----------
    raw_path = OUT / "raw_students.csv"
    with raw_path.open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow([
            "contact_id","first_name","last_name","email","phone",
            "stream","qualification","location","start_date","timetable",
            "price","spent","outstanding","pct_paid","payment_plan",
            "group_id","period","status","payment_status",
        ])
        for r in rows:
            for stream, loc_c, qual_c, start_c, price_c, spent_c, plan_c, tt_c in COURSES:
                qual = f(r, qual_c)
                price = num(f(r, price_c))
                spent = num(f(r, spent_c))
                if not qual and price == 0 and spent == 0:
                    continue  # not enrolled in this stream
                location = f(r, loc_c)
                start = f(r, start_c)
                outstanding = max(price - spent, 0)
                pct = (spent / price * 100) if price else 0
                status = (
                    "complete"   if price and spent >= price else
                    "cert-ready" if pct >= 50 else
                    "in-progress"if pct > 0 else
                    "unpaid"
                )
                w.writerow([
                    r.get("Contact ID",""), r.get("Name",""), r.get("Last Name",""),
                    r.get("Email",""), r.get("SMS Number",""),
                    stream, qual, location, start, f(r, tt_c),
                    f"{price:.2f}", f"{spent:.2f}", f"{outstanding:.2f}", f"{pct:.1f}",
                    f(r, plan_c),
                    group_id(stream, location, start),
                    period_for(start, periods),
                    status,
                    "",  # payment_status — filled by transactions sync (paid/collections/declined)
                ])
    print(f"wrote {raw_path.name}")

    # ---- class_groups: derived list, dedup'd ------------------------------
    groups = defaultdict(lambda: {"students":0,"price":0.0,"spent":0.0})
    with raw_path.open() as fp:
        for r in csv.DictReader(fp):
            g = groups[r["group_id"]]
            g["students"] += 1
            g["price"] += float(r["price"])
            g["spent"] += float(r["spent"])
            g.setdefault("stream", r["stream"])
            g.setdefault("location", r["location"])
            g.setdefault("start_date", r["start_date"])

    groups_path = OUT / "class_groups.csv"
    with groups_path.open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["group_id","stream","location","start_date","students","expected","collected","outstanding","pct","status_rule"])
        for gid, g in sorted(groups.items()):
            outstanding = g["price"] - g["spent"]
            pct = (g["spent"]/g["price"]*100) if g["price"] else 0
            w.writerow([gid, g["stream"], g["location"], g["start_date"],
                        g["students"], f"{g['price']:.2f}", f"{g['spent']:.2f}",
                        f"{outstanding:.2f}", f"{pct:.1f}",
                        "behind" if pct < 30 else "on-track" if pct < 100 else "complete"])
    print(f"wrote {groups_path.name}  ({len(groups)} groups)")

    # ---- summary_location -------------------------------------------------
    by_loc = defaultdict(lambda: {"students":set(),"price":0.0,"spent":0.0,"groups":set()})
    by_stream = defaultdict(lambda: {"students":set(),"price":0.0,"spent":0.0})
    with raw_path.open() as fp:
        for r in csv.DictReader(fp):
            loc = r["location"] or "—"
            l = by_loc[loc]
            l["students"].add(r["contact_id"])
            l["price"] += float(r["price"])
            l["spent"] += float(r["spent"])
            l["groups"].add(r["group_id"])
            s = by_stream[r["stream"]]
            s["students"].add(r["contact_id"])
            s["price"] += float(r["price"])
            s["spent"] += float(r["spent"])

    with (OUT/"summary_location.csv").open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["location","students","groups","expected","collected","outstanding","pct"])
        for loc, d in sorted(by_loc.items(), key=lambda kv: -kv[1]["price"]):
            out = d["price"]-d["spent"]
            pct = (d["spent"]/d["price"]*100) if d["price"] else 0
            w.writerow([loc, len(d["students"]), len(d["groups"]),
                        f"{d['price']:.2f}", f"{d['spent']:.2f}", f"{out:.2f}", f"{pct:.1f}"])

    with (OUT/"summary_stream.csv").open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["stream","students","expected","collected","outstanding","pct"])
        for stream, d in sorted(by_stream.items(), key=lambda kv: -kv[1]["price"]):
            out = d["price"]-d["spent"]
            pct = (d["spent"]/d["price"]*100) if d["price"] else 0
            w.writerow([stream, len(d["students"]),
                        f"{d['price']:.2f}", f"{d['spent']:.2f}", f"{out:.2f}", f"{pct:.1f}"])

    # ---- summary_macro ----------------------------------------------------
    total_students = len({r["contact_id"] for r in csv.DictReader(raw_path.open())})
    total_price = sum(d["price"] for d in by_stream.values())
    total_spent = sum(d["spent"] for d in by_stream.values())
    with (OUT/"summary_macro.csv").open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["metric","value"])
        w.writerow(["as_of", datetime.now().strftime("%Y-%m-%d %H:%M")])
        w.writerow(["students_total", total_students])
        w.writerow(["expected", f"{total_price:.2f}"])
        w.writerow(["collected", f"{total_spent:.2f}"])
        w.writerow(["outstanding", f"{total_price-total_spent:.2f}"])
        w.writerow(["collection_rate_pct", f"{(total_spent/total_price*100) if total_price else 0:.1f}"])

    # ---- sales_log: skeleton (filled by ONtraport tag webhook) ------------
    sales_log = OUT/"sales_log.csv"
    if not sales_log.exists():
        with sales_log.open("w", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(["timestamp","contact_id","name","tag","stream","amount","group_id","source"])

    # ---- issues -----------------------------------------------------------
    issues_path = OUT/"issues.csv"
    with issues_path.open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["contact_id","name","stream","issue","ack"])
        for r in rows:
            cid = r.get("Contact ID","")
            name = f"{r.get('Name','')} {r.get('Last Name','')}".strip()
            if not r.get("Email"):       w.writerow([cid,name,"-","missing email",""])
            if not r.get("SMS Number"):  w.writerow([cid,name,"-","missing phone",""])
            for stream, _, qual_c, start_c, price_c, *_ in COURSES:
                if f(r, qual_c) and num(f(r, price_c)) == 0:
                    w.writerow([cid, name, stream, "price = 0 with qual set", ""])
                if f(r, qual_c) and start_c and not f(r, start_c):
                    w.writerow([cid, name, stream, "missing start date", ""])

    print("done →", OUT)

if __name__ == "__main__":
    main()
