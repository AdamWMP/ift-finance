#!/usr/bin/env python3
"""
ONtraport → S26 Finance Dashboard Sync
Image Fitness Training

Fetches live contact and payment data from ONtraport,
rebuilds the Excel dashboard, and sends email alerts for:
  • Students whose cert is now ready (≥50% paid)
  • Students with missing/incomplete course information

Setup:
  1. Add your ONtraport credentials below (or via env vars)
  2. Run once to discover your custom field IDs:
       python3 ontraport_sync.py --discover
  3. Map the discovered field IDs in FIELD_MAP below
  4. Schedule weekly via cron:
       0 9 * * 1 python3 "/path/to/ontraport_sync.py"

ONtraport API docs: https://api.ontraport.com/doc/
"""

import os
import re
import sys
import json
import time
import smtplib
import argparse
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  ←  Fill these in
# ══════════════════════════════════════════════════════════════════════════════

# ONtraport credentials
# Find these in: ONtraport → Admin → Integrations → ONtraport API
OP_APP_ID  = os.environ.get("OP_APP_ID",  "2_98540_7LPYP2Ces")
OP_API_KEY = os.environ.get("OP_API_KEY", "1l3In6M39GMuhtX")

# Email config (Gmail with App Password recommended)
SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587
SMTP_USER  = "adam@imageft.ie"
SMTP_PASS  = os.environ.get("IFT_SMTP_PASS", "")  # or paste directly
TO_EMAIL   = "adam@imageft.ie"

# File paths
FINANCE_DIR  = Path("/Users/adamward/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance")
DASHBOARD    = FINANCE_DIR / "S26_Finance_Dashboard_v1.xlsx"
STATE_FILE   = FINANCE_DIR / ".op_sync_state.json"
FIELD_CACHE  = FINANCE_DIR / ".op_field_cache.json"

# ONtraport API
OP_BASE     = "https://api.ontraport.com/1"
OP_OBJ_CONTACT     = 0
OP_OBJ_TRANSACTION = 46

# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM FIELD MAP
# Run `python3 ontraport_sync.py --discover` to print all your custom fields,
# then fill in the field IDs below. They look like "f1234".
#
# The keys on the left are the column names used in our dashboard/CSV.
# The values on the right are the ONtraport field IDs for your account.
# ══════════════════════════════════════════════════════════════════════════════
FIELD_MAP = {
    # ── Core contact fields ───────────────────────────────────────────────────
    "Contact ID":                        "id",
    "Name":                              "firstname",
    "Last Name":                         "lastname",
    "Email":                             "email",
    "SMS Number":                        "sms_number",

    # ── PT Course fields ──────────────────────────────────────────────────────
    "PT Course Qualifications":          "f2290",
    "PT Course Timetable":               "f2292",
    "PT Course Start Date":              "f2293",
    "PT Course Location":                "f2291",
    "PT Course Price":                   "f2294",
    "PT Course Spent":                   "f2334",   # rollup — auto-calculated
    "PT Course Payment Plan":            "f2296",
    "PT Payment Method":                 "f2537",
    "PT Course Fees":                    "f2456",   # PAID / NOT PAID status

    # ── FBA (Fitness Business Accelerator) ────────────────────────────────────
    "FBA Enrolled":                      "f2614",
    "FBA Start Date":                    "f2615",
    "FBA Price":                         "f2616",
    "FBA Spent":                         "f2617",   # rollup

    # ── Add-on / workshop flags ───────────────────────────────────────────────
    "Studio Cycle":                      "f2621",
    "GroupBox":                          "f2623",
    "HYROX Simulation":                  "f2622",
    "Brand Launch Photoshoot":           "f2611",
    "Programming for Success":           "f2613",
    "AI for Coaches":                    "f2612",

    # ── Pilates Course fields ─────────────────────────────────────────────────
    "Pilates Course Location":           "f2303",
    "Pilates Course Qualifications":     "f2302",
    "Pilates Course Start Date":         "f2305",
    "Pilates Course Payment Plan":       "f2309",
    "Reformer Course Qualification":     "f2592",
    "Pilates Course Timetable":          "f2304",
    "Pilates Course Price":              "f2306",
    "Pilates Course Spent":              "f2725",   # Matwork Pilates Fees Spent — filtered rollup (replaces f2335)
    "Pilates Payment Method":            "f2538",

    # ── Reformer Course fields ────────────────────────────────────────────────
    "Reformer Course Location":          "f2593",
    "Reformer Course Timetable":         "f2594",
    "Reformer Course Start Date":        "f2595",
    "Reformer Course Price":             "f2596",
    "Reformer Pilates Course Spent":     "f2726",   # Reformer Pilates Fees Spent — filtered rollup (replaces f2599)
    "Reformer Pilates Payment Plan":     "f2598",

    # ── Follow-On Courses ─────────────────────────────────────────────────────
    "S&C Course":                        "f2318",
    "S&C Location":                      "f2316",
    "S&C Start Date":                    "f2315",
    "S&C Price":                         "f2319",
    "S&C Spent":                         "f2322",   # rollup
    "S&C Payment Plan":                  "f2321",
    "PPN Course":                        "f2323",
    "PPN Price":                         "f2324",
    "PPN Spent":                         "f2327",   # rollup
    "PPN Payment Plan":                  "f2326",
    "AN Course":                         "f2329",
    "AN Price":                          "f2330",
    "AN Spent":                          "f2333",   # rollup
    "AN Payment Plan":                   "f2332",

    # ── Payment detail helpers ────────────────────────────────────────────────
    "Deposit Amount":                    "f2604",
    "Monthly Payment Amount":            "f2605",
    "Payment Months Remaining":          "f2606",
    "First Instalment Date":             "f2607",

    # ── Internal filters (not shown in dashboard) ─────────────────────────────
    "PT Course Year":                    "f2288",   # 586=2026 — used to filter S26
}

# ══════════════════════════════════════════════════════════════════════════════
# FIELD DECODE MAPS — option IDs → human-readable labels
# ══════════════════════════════════════════════════════════════════════════════
OPT = {
    "f2288": {"586":"2026","543":"2025","507":"2024"},
    "f2289": {"494":"Spring","493":"Summer","492":"Autumn"},
    "f2290": {
        "627":"The Business Bundle","569":"The Career",
        "497":"The Cert","540":"High Performance Bundle",
        "495":"Launchpad Bundle","568":"Fitness Business Accelerator Only",
        "496":"Group Instructor & Personal Trainer",
        "570":"Online Coaching Course","567":"Personal Trainer Course Only",
        "566":"Group Instruction Only",
    },
    "f2291": {
        "544":"Online","503":"Dublin - Swords","502":"Dublin - Tallaght",
        "501":"Cork","500":"Galway","499":"Limerick","498":"Wexford","563":"Belfast",
    },
    "f2292": {
        "597":"Evening & Weekend - Mon + Wed + Sat (8 Weeks)",
        "506":"Monday & Tuesday (8 Weeks)","505":"Thursday & Friday (8 Weeks)",
        "539":"Monday & Wednesday Evenings (16 Weeks)","504":"Saturday (16 Weeks)",
    },
    "f2296": {
        "520":"Monthly","519":"One-Off","521":"FLYEFIT","522":"DSP",
        "523":"TSG","524":"SKILLNET","634":"Instalment Plan",
    },
    "f2300": {"587":"2026","545":"2025","508":"2024"},
    "f2302": {
        "624":"Complete Pilates Coach Course (Mat + Reformer)",
        "512":"Pilates Instructor EQF Level 4",
        "599":"Reformer Pilates Instructor Course",
    },
    "f2303": {
        "516":"Dublin - Swords","515":"Dublin - Tallaght",
        "514":"Cork","513":"Galway",
    },
    "f2304": {
        "625":"Bi-Weekly Saturdays + 3 Weekend Intensive",
        "517":"Bi-Weekly Saturdays","601":"3 Weekend Intensive (Sat & Sun)",
        "600":"Bi Weekly Sat & Sun (Reformer Only)",
        "598":"Tuesday + Thursday Evenings Online","518":"Bi-Weekly Evenings",
    },
    "f2309": {
        "530":"Monthly","529":"One-Off","531":"FLYEFIT",
        "532":"DSP","533":"TSG","534":"SKILLNET","635":"Instalment Plan",
    },
    "f2456": {"542":"NOT PAID","541":"PAID"},
    "f2537": {
        "577":"Stripe","576":"Cash","575":"Partial Cash & Stripe",
        "574":"Bank Transfer","573":"Revolut",
        "572":"Stripe & DSP Transfer","571":"Cash & DSP Transfer",
    },
    "f2538": {
        "584":"Stripe","583":"Cash","582":"Partial Cash & Stripe",
        "581":"Bank Transfer","580":"Revolut",
        "579":"Stripe & DSP Transfer","578":"Cash & DSP Transfer",
    },
    "f2592": {"619":"Reformer Pilates Course (CPD)"},
    "f2593": {
        "622":"Swords","620":"Cork","632":"Kerry",
        "631":"Derry","630":"Clare","621":"Tallaght",
    },
    "f2594": {"623":"Bi Weekly Sat & Sun"},
}

TIMESTAMP_FIELDS = {"f2293","f2305","f2595","f2315","f2615","f2607"}
PRICE_FIELDS     = {"f2294","f2306","f2596","f2319","f2324","f2330","f2604","f2605","f2606","f2616"}
ROLLUP_FIELDS    = {"f2334","f2725","f2726","f2322","f2327","f2333","f2617"}
CHECK_FIELDS     = {"f2614","f2611","f2612","f2613","f2621","f2622","f2623"}

# ══════════════════════════════════════════════════════════════════════════════
# ONTRAPORT API CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class ONtraportClient:
    def __init__(self, app_id, api_key):
        if not app_id or not api_key:
            raise ValueError(
                "ONtraport credentials not set.\n"
                "  Option A: Set environment variables OP_APP_ID and OP_API_KEY\n"
                "  Option B: Paste them directly into ontraport_sync.py config section"
            )
        self.headers = {
            "Api-Appid": str(app_id),
            "Api-Key":   str(api_key),
            "Content-Type": "application/json",
        }

    def get(self, endpoint, params=None):
        url = f"{OP_BASE}/{endpoint.lstrip('/')}"
        resp = requests.get(url, headers=self.headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def get_meta(self, object_id=0):
        """Return field definitions for an object type."""
        data = self.get(f"/objects/meta", params={"objectID": object_id, "format": "byId"})
        return data.get("data", {})

    def get_contacts_by_ids(self, contact_ids, field_ids=None):
        """
        Fetch specific contacts by ID in batches of 25.
        This is the reliable approach for ONtraport — field-value filtering
        via the search API is unreliable for dropdown fields.
        """
        contacts = []
        fields_param = ",".join(field_ids) if field_ids else None
        batches = [contact_ids[i:i+25] for i in range(0, len(contact_ids), 25)]

        print(f"  Fetching {len(contact_ids)} contacts from ONtraport ({len(batches)} batches)...", end="", flush=True)
        for batch in batches:
            ids_str = ",".join(str(i) for i in batch)
            params = {
                "objectID":   OP_OBJ_CONTACT,
                "ids":        ids_str,
            }
            if fields_param:
                params["listFields"] = fields_param
            try:
                resp = self.get("/objects", params=params)
                data = resp.get("data", [])
                if isinstance(data, dict):
                    data = list(data.values())
                contacts.extend(data)
                print(".", end="", flush=True)
            except Exception as e:
                print(f"x({e})", end="", flush=True)
            time.sleep(0.15)

        print(f" {len(contacts)} loaded.")
        return contacts

    def get_all_contacts(self, field_ids=None):
        """Paginate through all contacts (use get_contacts_by_ids when IDs are known)."""
        contacts = []
        start = 0
        page_size = 50
        fields_param = ",".join(field_ids) if field_ids else None

        print(f"  Fetching contacts from ONtraport...", end="", flush=True)
        while True:
            params = {
                "objectID": OP_OBJ_CONTACT,
                "range":    page_size,
                "start":    start,
            }
            if fields_param:
                params["listFields"] = fields_param

            resp = self.get("/objects", params=params)
            data = resp.get("data", [])
            if not data:
                break
            contacts.extend(data)
            print(f".", end="", flush=True)
            if len(data) < page_size:
                break
            start += page_size
            time.sleep(0.2)

        print(f" {len(contacts)} contacts loaded.")
        return contacts

    def get_transactions(self, contact_id=None):
        """Fetch payment transactions, optionally filtered by contact."""
        params = {"objectID": OP_OBJ_TRANSACTION, "range": 100}
        if contact_id:
            params["search"] = json.dumps({
                "field": {"field": "contact_id"},
                "op":    "=",
                "value": {"value": str(contact_id)},
            })
        resp = self.get("/objects", params=params)
        return resp.get("data", [])

    def get_all_transactions(self):
        """Paginate through all transactions."""
        txns = []
        start = 0
        page_size = 50
        print(f"  Fetching transactions...", end="", flush=True)
        while True:
            params = {
                "objectID": OP_OBJ_TRANSACTION,
                "range":    page_size,
                "start":    start,
                "listFields": "id,contact_id,amount,status,date_created,product_name",
            }
            resp = self.get("/objects", params=params)
            data = resp.get("data", [])
            if not data:
                break
            txns.extend(data)
            print(".", end="", flush=True)
            if len(data) < page_size:
                break
            start += page_size
            time.sleep(0.2)
        print(f" {len(txns)} transactions loaded.")
        return txns


# ══════════════════════════════════════════════════════════════════════════════
# FIELD DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

def discover_fields(client):
    """
    Print all custom fields in your ONtraport account.
    Run with: python3 ontraport_sync.py --discover
    Then copy the field IDs into FIELD_MAP above.
    """
    print("\n" + "="*70)
    print("ONtraport Custom Field Discovery — Image Fitness Training")
    print("="*70)
    print("Copy the relevant field IDs into FIELD_MAP in this script.\n")

    meta = client.get_meta(OP_OBJ_CONTACT)
    fields = meta.get("fields", {})

    # Cache for future use
    with open(FIELD_CACHE, "w") as f:
        json.dump(fields, f, indent=2)

    # Group: standard vs custom
    standard, custom = [], []
    for fid, fdef in fields.items():
        alias = fdef.get("alias", fdef.get("field", fid))
        ftype = fdef.get("type", "")
        entry = (fid, alias, ftype)
        if fid.startswith("f"):
            custom.append(entry)
        else:
            standard.append(entry)

    print(f"{'Field ID':<14} {'Type':<16} {'Alias / Name'}")
    print("-"*70)
    print("── STANDARD FIELDS ──")
    for fid, alias, ftype in sorted(standard, key=lambda x: x[0]):
        print(f"  {fid:<12} {ftype:<16} {alias}")
    print("\n── CUSTOM FIELDS ──")
    for fid, alias, ftype in sorted(custom, key=lambda x: x[0]):
        marker = " ← MATCH" if any(
            alias.lower() in col.lower() or col.lower() in alias.lower()
            for col in FIELD_MAP.keys()
        ) else ""
        print(f"  {fid:<12} {ftype:<16} {alias}{marker}")

    print("\n" + "="*70)
    print("Suggested FIELD_MAP entries (verify aliases match your ONtraport):")
    print("="*70)
    # Try to auto-suggest mappings
    for col_name in FIELD_MAP.keys():
        best = None
        for fid, alias, ftype in custom:
            if alias.lower() == col_name.lower():
                best = (fid, alias)
                break
        if not best:
            for fid, alias, ftype in custom:
                if col_name.lower() in alias.lower() or alias.lower() in col_name.lower():
                    best = (fid, alias)
                    break
        if best:
            print(f'  "{col_name}": "{best[0]}",  # {best[1]}')
        else:
            print(f'  "{col_name}": "",  # NOT FOUND — check manually')
    print()


# ══════════════════════════════════════════════════════════════════════════════
# DATA TRANSFORMATION
# ══════════════════════════════════════════════════════════════════════════════

def clean_euro(v):
    if not v or str(v).strip() in ("", "-", "0", "0.0"):
        return 0.0
    try:
        return float(re.sub(r"[€,\s]", "", str(v)))
    except:
        return 0.0

def decode(fid, val):
    """Decode a raw ONtraport field value to human-readable form."""
    if val is None or str(val).strip() in ("", "0", "None"):
        return ""
    s = str(val).strip()
    if fid in OPT:
        return OPT[fid].get(s, s)
    if fid in TIMESTAMP_FIELDS:
        try:
            ts = int(float(s))
            if ts > 0:
                from datetime import timezone
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d-%-m-%Y")
        except Exception:
            pass
        return s
    if fid in PRICE_FIELDS or fid in ROLLUP_FIELDS:
        try:
            return float(s)
        except Exception:
            return 0.0
    if fid in CHECK_FIELDS:
        return "Yes" if s == "1" else ""
    return s

def build_dataframe(contacts, txn_totals, field_map):
    """
    Convert raw ONtraport contact data into a decoded DataFrame.
    Filters to S26 contacts (PT Course Year == 2026, or Pilates qual set).
    """
    col_to_field = {col: fid for col, fid in field_map.items() if fid}

    rows = []
    for c in contacts:
        # Filter to S26: PT year = 2026, or any PT/Pilates qual present
        pt_year_raw  = str(c.get("f2288", "")).strip()
        pt_qual_raw  = str(c.get("f2290", "")).strip()
        pil_qual_raw = str(c.get("f2302", "")).strip()
        is_s26 = (pt_year_raw == "586") or bool(pt_qual_raw) or bool(pil_qual_raw)
        if not is_s26:
            continue

        row = {}
        for col_name, fid in col_to_field.items():
            raw = c.get(fid, "")
            row[col_name] = decode(fid, raw)

        fname = str(c.get("firstname", "")).strip()
        lname = str(c.get("lastname", "")).strip()
        row["Name"] = f"{fname} {lname}".strip()
        row["Contact ID"] = str(c.get("id", ""))

        # Overlay transaction totals only if rollup fields are empty
        cid = row["Contact ID"]
        if cid in txn_totals:
            tot = txn_totals[cid]
            if not clean_euro(row.get("PT Course Spent", 0)):
                row["PT Course Spent"] = tot.get("pt_total", 0.0)
            if not clean_euro(row.get("Pilates Course Spent", 0)):
                row["Pilates Course Spent"] = tot.get("pil_total", 0.0)

        # Skip test accounts
        if "test" in row["Name"].lower():
            continue

        rows.append(row)

    df = pd.DataFrame(rows)

    for col in FIELD_MAP.keys():
        if col not in df.columns:
            df[col] = ""

    return df

def aggregate_transactions(txns):
    """
    Build a dict of contact_id → payment totals.
    ONtraport transactions have a `product_name` that we use
    to distinguish PT vs Pilates payments.
    """
    totals = {}
    for t in txns:
        status = str(t.get("status", "")).lower()
        if status not in ("paid", "complete", "completed", "1"):
            continue  # only count successful payments

        cid    = str(t.get("contact_id", ""))
        amount = clean_euro(t.get("amount", 0))
        name   = str(t.get("product_name", "")).lower()

        if cid not in totals:
            totals[cid] = {"pt_total": 0.0, "pil_total": 0.0, "other": 0.0}

        if "pilates" in name or "reformer" in name:
            totals[cid]["pil_total"] += amount
        elif "pt" in name or "personal train" in name or "cert" in name or "career" in name:
            totals[cid]["pt_total"] += amount
        else:
            totals[cid]["other"] += amount

    return totals


# ══════════════════════════════════════════════════════════════════════════════
# CERT ALERTS
# ══════════════════════════════════════════════════════════════════════════════

def check_cert_alerts(df, state):
    """
    Find students who have newly crossed the 50% threshold.
    Returns list of newly cert-ready students.
    """
    already_alerted = set(state.get("cert_alerted", []))
    newly_ready = []

    for _, r in df.iterrows():
        cid = str(r.get("Contact ID", "")).strip()
        name = str(r.get("Name", "")).strip()

        # PT cert
        pt_price = clean_euro(r.get("PT Course Price", 0))
        pt_spent = clean_euro(r.get("PT Course Spent", 0))
        if pt_price > 0 and pt_spent / pt_price >= 0.5:
            key = f"PT:{cid}"
            if key not in already_alerted:
                newly_ready.append({
                    "cid":    cid,
                    "name":   name,
                    "email":  r.get("Email", ""),
                    "type":   "PT",
                    "price":  pt_price,
                    "spent":  pt_spent,
                    "pct":    pt_spent / pt_price,
                    "key":    key,
                })

        # Pilates cert
        pil_price = clean_euro(r.get("Pilates Course Price", 0))
        pil_spent = clean_euro(r.get("Pilates Course Spent", 0))
        if pil_price > 0 and pil_spent / pil_price >= 0.5:
            key = f"PIL:{cid}"
            if key not in already_alerted:
                newly_ready.append({
                    "cid":    cid,
                    "name":   name,
                    "email":  r.get("Email", ""),
                    "type":   "Pilates",
                    "price":  pil_price,
                    "spent":  pil_spent,
                    "pct":    pil_spent / pil_price,
                    "key":    key,
                })

    return newly_ready

def check_issues(df):
    issues = []
    for _, r in df.iterrows():
        name = str(r.get("Name", "")).strip()
        if not name or "test" in name.lower():
            continue
        iss = []
        pt_qual  = str(r.get("PT Course Qualifications", "")).strip()
        pil_qual = str(r.get("Pilates Course Qualifications", "")).strip()
        if pt_qual and clean_euro(r.get("PT Course Price", 0)) == 0:
            iss.append("PT price is €0 — not set in ONtraport")
        if pt_qual and not str(r.get("PT Course Start Date", "")).strip():
            iss.append("PT start date missing")
        if pt_qual and not str(r.get("PT Course Location", "")).strip():
            iss.append("PT location missing")
        if pt_qual and not str(r.get("PT Course Timetable", "")).strip():
            iss.append("PT timetable missing")
        if pil_qual and clean_euro(r.get("Pilates Course Price", 0)) == 0 and clean_euro(r.get("Pilates Course Spent", 0)) == 0:
            iss.append("Pilates price is €0 — not set in ONtraport")
        if pil_qual and not str(r.get("Pilates Course Start Date", "")).strip():
            iss.append("Pilates start date missing")
        if pil_qual and not str(r.get("Pilates Course Location", "")).strip():
            iss.append("Pilates location missing")
        if not str(r.get("Email", "")).strip():
            iss.append("No email address")
        plan = " ".join([str(r.get("PT Course Payment Plan", "")), str(r.get("Pilates Course Payment Plan", ""))]).upper()
        for kw in ["FLYEFIT", "SKILLNET", "TSG"]:
            if kw in plan:
                iss.append(f"Referral/deal ({kw}) — verify fees manually")
                break
        if iss:
            cid = str(r.get("Contact ID", "")).strip()
            issues.append({
                "cid":    cid,
                "name":   name,
                "email":  r.get("Email", ""),
                "type":   "PT" if pt_qual else "Pilates",
                "issues": iss,
                "key":    f"{cid}:{','.join(sorted(iss))}",
            })
    return issues


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL ALERTS
# ══════════════════════════════════════════════════════════════════════════════

def send_email(subject, html_body, to=TO_EMAIL):
    smtp_pass = SMTP_PASS or os.environ.get("IFT_SMTP_PASS", "")
    if not smtp_pass:
        print(f"  ⚠️  SMTP not configured — preview saved locally")
        preview = FINANCE_DIR / "s26_alert_preview.html"
        with open(preview, "w") as f:
            f.write(html_body)
        print(f"     {preview}")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, smtp_pass)
        s.sendmail(SMTP_USER, to, msg.as_string())
    return True

def send_cert_alert(newly_ready, run_date):
    if not newly_ready:
        return
    rows_html = ""
    for s in newly_ready:
        badge_color = "background:#D6EAF8;color:#1A5276" if s["type"] == "PT" else "background:#D1F2EB;color:#0E6655"
        rows_html += f"""<tr>
          <td style="padding:10px">{s['cid']}</td>
          <td style="padding:10px"><strong>{s['name']}</strong><br>
            <span style="font-size:11px;color:#666">{s['email']}</span></td>
          <td style="padding:10px;text-align:center">
            <span style="padding:3px 10px;border-radius:4px;font-size:11px;font-weight:bold;{badge_color}">{s['type']}</span></td>
          <td style="padding:10px;text-align:center">€{s['spent']:,.2f} / €{s['price']:,.2f}</td>
          <td style="padding:10px;text-align:center"><strong style="color:#1A7741">{s['pct']*100:.0f}%</strong></td>
          <td style="padding:10px;text-align:center">
            <span style="background:#D4EDDA;color:#1A7741;padding:4px 12px;border-radius:4px;font-weight:bold">RELEASE ✓</span></td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;background:#f4f6f9;margin:0">
    <div style="max-width:700px;margin:0 auto;background:#fff">
      <div style="background:#0D1B2A;padding:28px 32px">
        <h1 style="color:#D4A017;font-size:22px;margin:0 0 4px">Image Fitness Training</h1>
        <p style="color:#aab4be;font-size:12px;margin:0">S26 Finance · Cert Release Alert · {run_date}</p>
      </div>
      <div style="background:#1A7741;color:#fff;padding:14px 32px;font-size:15px;font-weight:bold">
        🎓 &nbsp; {len(newly_ready)} student{'s are' if len(newly_ready) > 1 else ' is'} ready for cert release
      </div>
      <div style="padding:28px 32px">
        <p style="font-size:14px;color:#333">
          The following students have reached <strong>≥50% of their course fees paid</strong>
          and are now eligible for their certificate.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead style="background:#0D1B2A">
            <tr>
              <th style="padding:10px;color:#fff;text-align:left">ID</th>
              <th style="padding:10px;color:#fff;text-align:left">Student</th>
              <th style="padding:10px;color:#fff;text-align:center">Course</th>
              <th style="padding:10px;color:#fff;text-align:center">Paid / Total</th>
              <th style="padding:10px;color:#fff;text-align:center">% Paid</th>
              <th style="padding:10px;color:#fff;text-align:center">Action</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        <div style="background:#D4EDDA;border-left:4px solid #1A7741;padding:16px 20px;margin-top:24px;border-radius:4px">
          <strong style="color:#1A7741">Next steps:</strong>
          <ol style="margin:8px 0 0;padding-left:20px;font-size:13px;color:#333">
            <li>Verify payment in ONtraport / Stripe</li>
            <li>Issue certificate to student</li>
            <li>Post completion announcement (optional)</li>
          </ol>
        </div>
      </div>
      <div style="background:#f4f6f9;padding:16px 32px;font-size:11px;color:#999;border-top:1px solid #e0e0e0">
        S26 Finance Dashboard · Auto-generated · {run_date}
      </div>
    </div></body></html>"""

    subject = f"🎓 S26 Cert Alert — {len(newly_ready)} student(s) ready for release | {run_date}"
    sent = send_email(subject, html)
    print(f"  {'✅ Cert alert sent' if sent else '📄 Cert alert preview saved'} ({len(newly_ready)} students)")

def send_issues_alert(issues, seen_keys, run_date):
    if not issues:
        return
    new_issues   = [i for i in issues if i["key"] not in seen_keys]
    known_issues = [i for i in issues if i["key"] in seen_keys]

    if not new_issues:
        print(f"  ℹ️  {len(known_issues)} known issues unchanged — no email sent")
        return

    def rows_html(items):
        html = ""
        for idx, i in enumerate(items):
            bg = "#fafafa" if idx % 2 == 0 else "#fff"
            badge_color = "background:#D6EAF8;color:#1A5276" if i["type"] == "PT" else "background:#D1F2EB;color:#0E6655"
            iss_html = "".join(f'<div style="color:#8B1A1A">• {x}</div>' for x in i["issues"])
            html += f"""<tr style="background:{bg}">
              <td style="padding:8px 10px">{i['cid']}</td>
              <td style="padding:8px 10px"><strong>{i['name']}</strong></td>
              <td style="padding:8px 10px;text-align:center">
                <span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:bold;{badge_color}">{i['type']}</span></td>
              <td style="padding:8px 10px;font-size:12px">{iss_html}</td></tr>"""
        return html

    html = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;background:#f4f6f9;margin:0">
    <div style="max-width:700px;margin:0 auto;background:#fff">
      <div style="background:#0D1B2A;padding:28px 32px">
        <h1 style="color:#D4A017;font-size:22px;margin:0 0 4px">Image Fitness Training</h1>
        <p style="color:#aab4be;font-size:12px;margin:0">S26 Finance · Issues Alert · {run_date}</p>
      </div>
      <div style="background:#8B1A1A;color:#fff;padding:14px 32px;font-size:14px;font-weight:bold">
        ⚠️ &nbsp; {len(new_issues)} new issue(s) detected  |  {len(known_issues)} previously reported still outstanding
      </div>
      <div style="padding:28px 32px">
        <p style="font-size:14px;color:#333">
          <strong>{len(new_issues)} new student record(s)</strong> have missing or incomplete information in ONtraport.
        </p>
        <div style="font-size:13px;font-weight:bold;color:#0D1B2A;border-bottom:2px solid #D4A017;padding-bottom:6px;margin:0 0 12px">
          🆕 New Issues
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead style="background:#8B1A1A">
            <tr>
              <th style="padding:8px 10px;color:#fff;text-align:left">ID</th>
              <th style="padding:8px 10px;color:#fff;text-align:left">Name</th>
              <th style="padding:8px 10px;color:#fff;text-align:center">Type</th>
              <th style="padding:8px 10px;color:#fff;text-align:left">Issues</th>
            </tr>
          </thead>
          <tbody>{rows_html(new_issues)}</tbody>
        </table>
        {'<div style="font-size:13px;font-weight:bold;color:#555;border-bottom:1px solid #ccc;padding-bottom:6px;margin:24px 0 12px">⏳ Still Outstanding (' + str(len(known_issues)) + ')</div><table style="width:100%;border-collapse:collapse;font-size:12px"><thead style="background:#555"><tr><th style="padding:8px 10px;color:#fff;text-align:left">ID</th><th style="padding:8px 10px;color:#fff;text-align:left">Name</th><th style="padding:8px 10px;color:#fff;text-align:center">Type</th><th style="padding:8px 10px;color:#fff;text-align:left">Issues</th></tr></thead><tbody>' + rows_html(known_issues) + "</tbody></table>" if known_issues else ""}
        <div style="background:#0D1B2A;border-radius:6px;padding:20px 24px;margin-top:24px">
          <a href="https://app.ontraport.com" style="background:#D4A017;color:#0D1B2A;font-weight:bold;
            font-size:13px;padding:10px 20px;border-radius:4px;text-decoration:none;display:inline-block">
            Open ONtraport to fix →
          </a>
        </div>
      </div>
      <div style="background:#f4f6f9;padding:16px 32px;font-size:11px;color:#999;border-top:1px solid #e0e0e0">
        S26 Finance Dashboard · Auto-generated · {run_date}
      </div>
    </div></body></html>"""

    subject = f"⚠️ S26 Issues — {len(new_issues)} new record(s) need attention | {run_date}"
    sent = send_email(subject, html)
    print(f"  {'✅ Issues alert sent' if sent else '📄 Issues alert preview saved'} ({len(new_issues)} new, {len(known_issues)} ongoing)")


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD REBUILD
# ══════════════════════════════════════════════════════════════════════════════

def rebuild_dashboard(df):
    """
    Rebuild the Excel dashboard from live ONtraport data.
    Saves a CSV snapshot then calls build_s26.py to regenerate the workbook.
    """
    import subprocess
    live_csv = FINANCE_DIR / ".op_live_s26.csv"
    df.to_csv(live_csv, index=False)
    print(f"  Saved live data snapshot: {live_csv.name}")

    build_script = FINANCE_DIR / "build_s26.py"
    if not build_script.exists():
        print(f"  ⚠️  build_s26.py not found in Finance dir — skipping Excel rebuild")
        print(f"     Copy build_s26.py to {FINANCE_DIR} to enable auto-rebuild")
        return live_csv

    print(f"  Rebuilding dashboard...")
    # Use the SAME interpreter that's running this script — guarantees the
    # build script sees the same installed packages (pandas, openpyxl).
    result = subprocess.run(
        [sys.executable, str(build_script), str(live_csv)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  ✅ Dashboard rebuilt: S26_Finance_Dashboard_v1.xlsx")
    else:
        print(f"  ⚠️  Dashboard rebuild failed:")
        print(result.stderr[-500:] if result.stderr else "(no output)")

    return live_csv


# ══════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cert_alerted": [], "issues_seen": [], "last_run": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ONtraport → S26 Finance Sync")
    parser.add_argument("--discover",    action="store_true", help="Print all ONtraport custom fields and exit")
    parser.add_argument("--no-email",    action="store_true", help="Skip sending emails (preview only)")
    parser.add_argument("--no-rebuild",  action="store_true", help="Skip Excel rebuild")
    parser.add_argument("--reset-state", action="store_true", help="Clear alert history (re-send all alerts)")
    args = parser.parse_args()

    run_date = datetime.now().strftime("%d %B %Y, %H:%M")
    print(f"\n{'='*60}")
    print(f"  ONtraport Sync — {run_date}")
    print(f"{'='*60}")

    client = ONtraportClient(OP_APP_ID, OP_API_KEY)

    # ── Discovery mode ────────────────────────────────────────────────────────
    if args.discover:
        discover_fields(client)
        return

    # ── Load state ────────────────────────────────────────────────────────────
    state = {} if args.reset_state else load_state()
    cert_alerted = set(state.get("cert_alerted", []))
    issues_seen  = set(state.get("issues_seen", []))

    # ── Pull data from ONtraport ──────────────────────────────────────────────
    needed_fields = ["id", "firstname", "lastname", "email", "sms_number",
                     "f2288", "f2289", "f2290", "f2302"] + \
                    [fid for fid in FIELD_MAP.values() if fid and fid not in ("id", "firstname", "lastname", "email", "sms_number")]

    # Load known S26 contact IDs from existing CSV snapshot (fast path)
    known_ids = []
    for csv_candidate in [FINANCE_DIR / ".op_live_s26.csv",
                           FINANCE_DIR / "S26_Finance_Report_DATA_as_of_27_April_26.csv"]:
        if csv_candidate.exists():
            try:
                id_df = pd.read_csv(csv_candidate, usecols=["Contact ID"], dtype=str)
                known_ids = [str(i).strip() for i in id_df["Contact ID"].dropna().tolist() if str(i).strip()]
                print(f"  Loaded {len(known_ids)} contact IDs from {csv_candidate.name}")
                break
            except Exception as e:
                print(f"  Could not read {csv_candidate.name}: {e}")

    if known_ids:
        contacts = client.get_contacts_by_ids(known_ids, field_ids=needed_fields)
    else:
        print("  No existing CSV found — fetching all contacts (slow).")
        contacts = client.get_all_contacts(field_ids=needed_fields)

    if not contacts:
        print("  No contacts returned — check credentials and field IDs.")
        return

    # Rollup fields (f2334 PT Spent, f2335 Pilates Spent, etc.) are fetched
    # directly from ONtraport contact records — no need to aggregate transactions.
    txn_totals = {}

    # ── Build DataFrame ───────────────────────────────────────────────────────
    print("  Building dataset...")
    df = build_dataframe(contacts, txn_totals, FIELD_MAP)
    print(f"  {len(df)} students processed.")

    # ── Cert alerts ───────────────────────────────────────────────────────────
    print("  Checking cert eligibility...")
    newly_ready = check_cert_alerts(df, state)
    if newly_ready and not args.no_email:
        send_cert_alert(newly_ready, run_date)
    elif newly_ready:
        print(f"  🎓 {len(newly_ready)} cert-ready (--no-email set, skipping)")
    else:
        print("  ✅ No new cert-ready students.")

    # ── Issues alerts ─────────────────────────────────────────────────────────
    print("  Checking for record issues...")
    issues = check_issues(df)
    if issues and not args.no_email:
        send_issues_alert(issues, issues_seen, run_date)
    elif issues:
        print(f"  ⚠️  {len(issues)} issues found (--no-email set, skipping)")
    else:
        print("  ✅ No issues found.")

    # ── Excel rebuild ─────────────────────────────────────────────────────────
    if not args.no_rebuild:
        print("  Saving live data snapshot for dashboard refresh...")
        rebuild_dashboard(df)

    # ── Save state ────────────────────────────────────────────────────────────
    new_state = {
        "cert_alerted": list(cert_alerted | {s["key"] for s in newly_ready}),
        "issues_seen":  list({i["key"] for i in issues}),
        "last_run":     run_date,
        "contacts_synced": len(contacts),
    }
    save_state(new_state)

    print(f"\n  Sync complete. Next run: schedule with cron (see README below).")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
