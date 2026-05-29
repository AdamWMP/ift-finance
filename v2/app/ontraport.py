"""ONtraport API client + invoice ingest.

Pulls every Invoice (object_id 46), stores it, and aggregates per-contact
payment_status onto raw_students. Replaces the manual paid/collections/
declined tracking.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone

import requests

from .db import INVOICE_STATUS, aggregate_payment_status, get_db

OP_APP_ID  = os.environ.get("OP_APP_ID",  "2_98540_7LPYP2Ces")
OP_API_KEY = os.environ.get("OP_API_KEY", "1l3In6M39GMuhtX")
OP_BASE    = "https://api.ontraport.com/1"
HEADERS    = {"Api-Appid": OP_APP_ID, "Api-Key": OP_API_KEY}
INVOICE_OBJECT_ID = 46
PAGE = 50  # ONtraport's hard cap

DROP_OFF_TAG_ID = 2031  # "Drop off (Not coming back)"
CUSTOMER_TAG_ID = 50    # "Customers" — anyone who's bought a product
DEFERRAL_TAG_PATTERNS = ("deferral",)  # any tag with this substring counts
GRANT_TAG_PATTERNS    = ("dsp", "grant", "tesg", "skillnet")

def _to_float(v) -> float:
    if v in (None, "", "null"): return 0.0
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def _ts_to_iso(v) -> str | None:
    """ONtraport sends timestamps as Unix seconds (string) for some fields,
    YYYY-MM-DD strings for date fields."""
    if not v: return None
    s = str(v)
    if s.isdigit():
        try: return datetime.fromtimestamp(int(s), tz=timezone.utc).date().isoformat()
        except (ValueError, OSError): return None
    return s[:10]  # already a date

def fetch_invoices_for_contacts(contact_ids: list[str]) -> list[dict]:
    """Fetch invoices filtered to a specific list of contact_ids — orders of
    magnitude smaller than the full table.

    ONtraport's `condition` param uses base64-ish JSON. We chunk the IDs to
    keep URL length under 8KB and merge results.
    """
    import time, json as _json
    out = []
    chunks = [contact_ids[i:i+50] for i in range(0, len(contact_ids), 50)]
    for ci, chunk in enumerate(chunks, 1):
        cond = _json.dumps([{
            "field": {"field": "contact_id"},
            "op": "IN",
            "value": {"list": [{"value": c} for c in chunk]},
        }])
        start, sub = 0, []
        while True:
            t0 = time.time()
            r = requests.get(f"{OP_BASE}/objects",
                             params={"objectID": INVOICE_OBJECT_ID, "range": PAGE,
                                     "start": start, "condition": cond},
                             headers=HEADERS, timeout=30)
            r.raise_for_status()
            rows = r.json().get("data", [])
            dt = time.time() - t0
            print(f"  chunk {ci}/{len(chunks)} start={start:>3}  +{len(rows):>2} rows ({dt:.1f}s)", flush=True)
            if not rows: break
            sub.extend(rows)
            if len(rows) < PAGE: break
            start += PAGE
        out.extend(sub)
    return out

def fetch_all_invoices(*_, **__):
    raise RuntimeError("Use fetch_invoices_for_contacts(ids) — full pull is too large.")

def ingest_invoices(contact_ids: list[str] | None = None) -> dict:
    if contact_ids is None:
        with get_db() as c:
            contact_ids = sorted({r["contact_id"] for r in c.execute(
                "SELECT DISTINCT contact_id FROM students WHERE contact_id != ''"
            ).fetchall()})
    print(f"fetching invoices for {len(contact_ids)} contacts ...", flush=True)
    rows = fetch_invoices_for_contacts(contact_ids)
    print(f"fetched {len(rows)} invoices total", flush=True)

    by_contact: dict[str, list[dict]] = {}
    with get_db() as c:
        c.execute("DELETE FROM invoices")
        for r in rows:
            code = int(r.get("status") or 0) if r.get("status") not in (None, "") else None
            inv = {
                "id": int(r["id"]),
                "contact_id": str(r.get("contact_id") or "").strip(),
                "status_code": code,
                "status": INVOICE_STATUS.get(code, str(r.get("status"))),
                "total": _to_float(r.get("total")),
                "total_paid": _to_float(r.get("total_paid")),
                "balance": _to_float(r.get("balance")),
                "invoice_date": _ts_to_iso(r.get("invoice_date") or r.get("date")),
                "closed_date": _ts_to_iso(r.get("closed_date")),
                "due_date": _ts_to_iso(r.get("due_date")),
                "last_recharge_date": _ts_to_iso(r.get("last_recharge_date")),
                "recharge_attempts": int(r.get("recharge_attempts") or 0) if r.get("recharge_attempts") not in (None,"","null") else 0,
            }
            c.execute("""
                INSERT INTO invoices (id, contact_id, status_code, status, total,
                    total_paid, balance, invoice_date, closed_date, due_date,
                    last_recharge_date, recharge_attempts)
                VALUES (:id,:contact_id,:status_code,:status,:total,:total_paid,
                    :balance,:invoice_date,:closed_date,:due_date,
                    :last_recharge_date,:recharge_attempts)
            """, inv)
            by_contact.setdefault(inv["contact_id"], []).append(inv)

        # Aggregate payment_status onto students
        updated = 0
        for cid, invs in by_contact.items():
            status = aggregate_payment_status(invs)
            cur = c.execute(
                "UPDATE students SET payment_status=? WHERE contact_id=?",
                (status, cid)).rowcount
            updated += cur

    summary = {
        "invoices_total": len(rows),
        "contacts_with_invoices": len(by_contact),
        "students_status_updated": updated,
        "by_status": {},
    }
    with get_db() as c:
        for r in c.execute(
            "SELECT status, COUNT(*) AS n, ROUND(SUM(total),0) AS total, "
            "ROUND(SUM(total_paid),0) AS paid, ROUND(SUM(balance),0) AS balance "
            "FROM invoices GROUP BY status_code ORDER BY status_code"
        ).fetchall():
            summary["by_status"][r["status"]] = {
                "count": r["n"], "total": r["total"], "paid": r["paid"], "balance": r["balance"],
            }
    return summary

def fetch_contact_meta() -> dict:
    """Get the Contact (objectID=0) field metadata. Used to decode dropdown
    values (e.g. f2288=586 → "2026")."""
    r = requests.get(f"{OP_BASE}/objects/meta", params={"objectID": 0, "format": "byId"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {}).get("0", {}).get("fields", {}) or {}

# field_id → CSV column name (mirrors v1's CSV layout). Verified against
# /objects/meta — see comment lookup tables in app/ontraport_meta.txt.
FIELD_MAP = {
    "f2290": "PT Course Qualifications",  "f2291": "PT Course Location",
    "f2292": "PT Course Timetable",       "f2293": "PT Course Start Date",
    "f2294": "PT Course Price",           "f2334": "PT Course Spent",
    "f2296": "PT Course Payment Plan",    "f2295": "PT Payment Method",
    "f2288": "PT Course Year",
    "f2302": "Pilates Course Qualifications", "f2303": "Pilates Course Location",
    "f2304": "Pilates Course Timetable",      "f2305": "Pilates Course Start Date",
    "f2306": "Pilates Course Price",          "f2335": "Pilates Course Spent",
    "f2309": "Pilates Course Payment Plan",   "f2538": "Pilates Payment Method",
    "f2300": "Pilates Course Year",
    "f2592": "Reformer Course Qualification", "f2593": "Reformer Course Location",
    "f2594": "Reformer Course Timetable",     "f2595": "Reformer Course Start Date",
    "f2596": "Reformer Course Price",         "f2599": "Reformer Pilates Course Spent",
    "f2598": "Reformer Pilates Payment Plan",
    "f2590": "Reformer Course Year",
    # Follow-on streams — discovered via the "course marker" drop fields; revenue_period
    # comes from earliest paid invoice rather than a stream-specific Year field
    # (see backfill_followon_periods in queries.py).
    "f2318": "S&C Course",          "f2316": "S&C Location",
    "f2317": "S&C Qualification",   "f2315": "S&C Start Date",
    "f2319": "S&C Price",           "f2322": "S&C Spent",
    "f2321": "S&C Payment Plan",
    "f2323": "PPN Course",          "f2324": "PPN Price",
    "f2327": "PPN Spent",           "f2326": "PPN Payment Plan",
    "f2329": "AN Course",           "f2330": "AN Price",
    "f2333": "AN Spent",            "f2332": "AN Payment Plan",
    "f2614": "FBA Enrolled",        "f2615": "FBA Start Date",
    "f2616": "FBA Price",           "f2617": "FBA Spent",
}

# Year option IDs that map to "2026" — different per course (!)
YEAR_2026 = {
    "f2288": "586",  # PT
    "f2300": "587",  # Pilates
    "f2590": "616",  # Reformer
}

# Follow-on stream "course marker" fields — a contact with any of these set
# has bought into that stream at some point. Period assignment happens later
# from their first paid invoice (Adam's model: count revenue in the term it's
# paid in, regardless of when the course actually runs).
FOLLOWON_MARKER_OPTIONS = {
    "f2318": "528",  # S&C Course → "Strength & Conditioning Course"
    "f2323": "529",  # PPN Course → "Pre and Post Natal Online Course"
    "f2329": "530",  # AN Course  → "Advanced Nutrition Course"
    # FBA is a checkbox — discovered separately via f2614=1
}
FBA_ENROLLED_FIELD = "f2614"

def _decode_value(field_id: str, raw, fields_meta: dict):
    """Translate ONtraport raw values to human-readable strings."""
    if raw in (None, "", "0"): return ""
    meta = fields_meta.get(field_id, {})
    ftype = meta.get("type")
    s = str(raw).strip()
    if ftype == "drop":
        opts = meta.get("options") or {}
        return opts.get(s, s)
    if ftype == "fulldate" or ftype == "timestamp":
        if s.isdigit():
            from datetime import datetime, timezone
            try: return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%-d-%-m-%Y")
            except (ValueError, OSError): return ""
        return s
    if ftype == "price":
        try: return f"{float(s):.2f}"
        except ValueError: return s
    return s

def _discover_by_condition(cond_obj: dict, label: str, ids: set[str]) -> None:
    """Helper: paginate a single OP search condition and add matching IDs."""
    import json as _json
    cond = _json.dumps([cond_obj])
    start = 0
    while True:
        r = requests.get(f"{OP_BASE}/objects",
                         params={"objectID": 0, "range": PAGE, "start": start,
                                 "condition": cond, "listFields": "id"},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", []) or []
        for row in rows:
            cid = str(row.get("id", "")).strip()
            if cid: ids.add(cid)
        print(f"  {label}: +{len(rows)} (total {len(ids)})", flush=True)
        if len(rows) < PAGE: break
        start += PAGE


def discover_s26_contact_ids() -> list[str]:
    """Find every contact who is enrolled in a tracked course/stream.

    Discovery returns *candidates*. Period assignment (which term their
    revenue counts towards) happens later from invoice dates — see
    backfill_followon_periods. Old contacts whose first paid invoice is in a
    prior term automatically end up in that prior term, not the current one.
    """
    ids: set[str] = set()

    # Year-tagged streams (PT / Pilates / Reformer) — discovered by year = 2026
    for field_id, value in YEAR_2026.items():
        _discover_by_condition(
            {"field": {"field": field_id}, "op": "=", "value": {"value": value}},
            f"{field_id}={value}", ids,
        )

    # Follow-on streams (S&C / PPN / AN) — course marker dropdown set
    for field_id, option_id in FOLLOWON_MARKER_OPTIONS.items():
        _discover_by_condition(
            {"field": {"field": field_id}, "op": "=", "value": {"value": option_id}},
            f"{field_id}={option_id} (follow-on marker)", ids,
        )

    # FBA — checkbox field, "checked" = "1"
    _discover_by_condition(
        {"field": {"field": FBA_ENROLLED_FIELD}, "op": "=", "value": {"value": "1"}},
        f"{FBA_ENROLLED_FIELD}=1 (FBA Enrolled)", ids,
    )

    return sorted(ids)

def fetch_contacts_full(contact_ids: list[str]) -> list[dict]:
    """Fetch full contact records and decode dropdowns/dates/prices.
    Returns rows shaped like the .op_live_s26.csv columns expected by ingest."""
    if not contact_ids: return []
    fields_meta = fetch_contact_meta()
    out = []
    # ONtraport's batch-by-IDs endpoint accepts up to 50 ids at once
    for chunk_start in range(0, len(contact_ids), 50):
        chunk = contact_ids[chunk_start:chunk_start+50]
        ids_param = ",".join(chunk)
        r = requests.get(f"{OP_BASE}/objects",
                         params={"objectID": 0, "ids": ids_param},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", []) or []
        for c in rows:
            row = {
                "Contact ID":  str(c.get("id", "")),
                "Name":        f"{(c.get('firstname') or '').strip()} {(c.get('lastname') or '').strip()}".strip(),
                "Last Name":   (c.get("lastname") or "").strip(),
                "Email":       (c.get("email") or "").strip(),
                "SMS Number":  (c.get("sms_number") or c.get("cell_phone") or "").strip(),
            }
            for fid, col in FIELD_MAP.items():
                row[col] = _decode_value(fid, c.get(fid), fields_meta)
            out.append(row)
    return out

def refresh_s26_csv() -> dict:
    """Top-level fetcher: discover + fetch + write to .op_live_s26.csv.
    Replaces the v1 ID-list-based sync. Adds new sales automatically."""
    import csv
    from .db import DATA_DIR
    csv_path = DATA_DIR / ".op_live_s26.csv"
    print(f"discovering S26 contacts in ONtraport …", flush=True)
    ids = discover_s26_contact_ids()
    print(f"  found {len(ids)} S26 contact IDs", flush=True)
    rows = fetch_contacts_full(ids)
    print(f"  fetched + decoded {len(rows)} contacts", flush=True)
    if not rows: return {"count": 0}
    # Build CSV columns from union of keys in fetched rows + a stable preferred order
    preferred = ["Contact ID","Name","Last Name","Email","SMS Number"] + list(FIELD_MAP.values())
    extras = sorted({k for r in rows for k in r if k not in preferred})
    cols = preferred + extras
    with csv_path.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows: w.writerow(r)
    print(f"  wrote {csv_path.name}", flush=True)
    return {"count": len(rows), "ids": len(ids)}


def fetch_tags() -> dict[int, str]:
    """All tags in the account → {id: name}."""
    out, start = {}, 0
    while True:
        r = requests.get(f"{OP_BASE}/objects",
                         params={"objectID": 14, "range": PAGE, "start": start},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows: break
        for row in rows:
            try: out[int(row["tag_id"])] = (row.get("tag_name") or "").strip()
            except (KeyError, ValueError, TypeError): continue
        if len(rows) < PAGE: break
        start += PAGE
    return out

def fetch_contact_tag_map(contact_ids: list[str]) -> dict[str, list[int]]:
    """For each contact_id, list of tag IDs they're tagged with.
    Uses the Contact's `contact_cat` field, which contains a comma-list
    of tag IDs (ONtraport convention)."""
    out: dict[str, list[int]] = {}
    for cid in contact_ids:
        try:
            r = requests.get(f"{OP_BASE}/object",
                             params={"objectID": 0, "id": cid},
                             headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json().get("data", {}) or {}
            raw = (data.get("contact_cat") or "").strip().strip("*/")
            if not raw: out[cid] = []; continue
            ids = []
            for tok in raw.replace("*/*", ",").split(","):
                tok = tok.strip().strip("*").strip("/")
                if tok.isdigit(): ids.append(int(tok))
            out[cid] = ids
        except Exception as e:
            print(f"  tag fetch failed for {cid}: {e}", flush=True)
            out[cid] = []
    return out

def apply_tags() -> dict:
    """Set is_dropoff / is_deferral / is_grant flags on students from
    their ONtraport tags."""
    print("fetching tag dictionary …", flush=True)
    tag_names = fetch_tags()
    print(f"  {len(tag_names)} tags loaded", flush=True)

    with get_db() as c:
        contact_ids = [r["contact_id"] for r in c.execute(
            "SELECT DISTINCT contact_id FROM students WHERE contact_id != ''"
        ).fetchall()]
    print(f"fetching tags for {len(contact_ids)} contacts …", flush=True)
    contact_tags = fetch_contact_tag_map(contact_ids)

    counts = {"dropoff": 0, "deferral": 0, "grant": 0}
    with get_db() as c:
        for cid, tag_ids in contact_tags.items():
            names = [tag_names.get(tid, "").lower() for tid in tag_ids]
            joined = " ".join(names)
            is_dropoff  = 1 if (DROP_OFF_TAG_ID in tag_ids) else 0
            is_deferral = 1 if any(p in joined for p in DEFERRAL_TAG_PATTERNS) else 0
            is_grant    = 1 if any(p in joined for p in GRANT_TAG_PATTERNS) else 0
            counts["dropoff"]  += is_dropoff
            counts["deferral"] += is_deferral
            counts["grant"]    += is_grant
            c.execute("""
                UPDATE students
                SET is_dropoff = ?,
                    is_deferral = MAX(is_deferral, ?),
                    is_grant   = MAX(is_grant, ?)
                WHERE contact_id = ?
            """, (is_dropoff, is_deferral, is_grant, cid))
    print("tags applied:", counts, flush=True)
    return counts

if __name__ == "__main__":
    import json, sys
    if "tags" in sys.argv:
        print(json.dumps(apply_tags(), indent=2))
    else:
        print(json.dumps(ingest_invoices(), indent=2))
