"""SQLite schema + period derivation. Single source of truth for the dashboard."""
from __future__ import annotations
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

# DATA_DIR is set in production (Docker / Render) to a mounted volume so the
# DB survives restarts. Locally it falls back to next to the code.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ift_finance.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    contact_id TEXT NOT NULL,
    first_name TEXT, last_name TEXT, email TEXT, phone TEXT,
    stream TEXT NOT NULL,                 -- PT | Pilates | Reformer | S&C | PPN | AN | Combo
    qualification TEXT,
    location TEXT,
    start_date TEXT,                      -- ISO yyyy-mm-dd
    timetable TEXT,
    price REAL DEFAULT 0,
    spent REAL DEFAULT 0,
    payment_plan TEXT,
    payment_method TEXT,                  -- Stripe | Cash | Bank | Revolut | SkillNet | DSP
    pathway TEXT,                         -- The Cert | The Career | The Business | The Studio | Reformer | Other
    group_id TEXT,                        -- "{stream} · {location} · {start_date}"
    class_period TEXT,                    -- where they sit (S26, A26, …)
    revenue_period TEXT,                  -- where their money is counted (deferrals differ)
    is_deferral INTEGER DEFAULT 0,
    is_dropoff INTEGER DEFAULT 0,
    is_grant INTEGER DEFAULT 0,
    payment_status TEXT,                  -- paid | partial | collections | declined | unpaid
    cert_issued INTEGER DEFAULT 0,        -- manual tick: physical cert handed over
    cert_issued_at TEXT,
    UNIQUE(contact_id, stream)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    period TEXT,
    direction TEXT NOT NULL,              -- in | out
    category TEXT NOT NULL,               -- course_sale | grant | corporate | marketing | refund | …
    subcategory TEXT,
    amount REAL NOT NULL,
    contact_id TEXT,
    source TEXT,                          -- ontraport | stripe | manual
    status TEXT,                          -- paid | pending | collections | declined
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ad_spend (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,                   -- ISO yyyy-mm-dd (day of spend)
    platform TEXT NOT NULL,               -- meta | google
    ad_account_id TEXT NOT NULL,
    ad_account_label TEXT,                -- 'IFT' | 'Studio' — display shorthand
    campaign_id TEXT,
    campaign_name TEXT NOT NULL,
    spend REAL NOT NULL DEFAULT 0,        -- € spent (currency-normalised at ingest)
    impressions INTEGER,
    clicks INTEGER,
    leads INTEGER,                        -- lead-form results / conversions
    topic TEXT,                           -- classified at ingest: Pilates | PT | Derry Pilates | Swords Studio | ...
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, platform, campaign_id)
);
CREATE INDEX IF NOT EXISTS ix_ad_spend_date ON ad_spend(date);
CREATE INDEX IF NOT EXISTS ix_ad_spend_topic ON ad_spend(topic);
CREATE INDEX IF NOT EXISTS ix_ad_spend_platform ON ad_spend(platform);

CREATE TABLE IF NOT EXISTS ad_accounts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,               -- meta | google
    account_id TEXT NOT NULL,             -- e.g. act_1234567890 or 123-456-7890
    label TEXT NOT NULL,                  -- 'IFT' | 'Studio' — display
    enabled INTEGER DEFAULT 1,
    UNIQUE(platform, account_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    period TEXT NOT NULL,
    sales_count INTEGER,
    revenue REAL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, period)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    contact_id TEXT,
    name TEXT,
    stream TEXT,
    issue TEXT,
    detected_at TEXT DEFAULT (datetime('now')),
    acked_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    contact_id TEXT NOT NULL,
    stream TEXT NOT NULL DEFAULT '',
    body TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (contact_id, stream)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_dismissals (
    contact_id TEXT NOT NULL,
    task TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (contact_id, task)
);

CREATE TABLE IF NOT EXISTS unmatched_payments (
    id INTEGER PRIMARY KEY,
    stripe_charge_id TEXT UNIQUE,
    date TEXT,
    amount REAL,
    last4 TEXT,
    name TEXT,
    email TEXT,
    student_id_hint TEXT,
    resolved_contact_id TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,            -- ONtraport invoice id
    contact_id TEXT NOT NULL,
    status_code INTEGER,               -- 0..9 per ONtraport
    status TEXT,                       -- decoded label
    total REAL,
    total_paid REAL,
    balance REAL,
    invoice_date TEXT,
    closed_date TEXT,
    due_date TEXT,
    last_recharge_date TEXT,
    recharge_attempts INTEGER,
    fetched_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_invoices_contact ON invoices(contact_id);
CREATE INDEX IF NOT EXISTS ix_invoices_status ON invoices(status_code);

CREATE INDEX IF NOT EXISTS ix_students_period ON students(class_period, revenue_period);
CREATE INDEX IF NOT EXISTS ix_students_loc ON students(location);
CREATE INDEX IF NOT EXISTS ix_students_pathway ON students(pathway);
CREATE INDEX IF NOT EXISTS ix_tx_period ON transactions(period, direction, category);
"""

# ---- Targets and constants -------------------------------------------------
SALE_VALUE = 2100.0  # €1 of revenue ÷ this = "sales-equivalent"
SALES_TARGETS = [200, 250, 300, 350, 400]

# Pathway = the qualification name as it lives in ONtraport, suffixed with the
# stream when the same qual exists across PT and Pilates (e.g. The Cert (PT)
# vs The Cert (Pilates)). No hardcoded taxonomy — clean up labels in ONtraport
# and the dashboard reflects.
DUAL_NAMED_QUALS = {"The Cert", "The Career", "The Launchpad", "Launchpad Bundle"}

def pathway_for(qual: str, stream: str) -> str:
    q = (qual or "").strip()
    if not q: return "Unspecified"
    if q in DUAL_NAMED_QUALS:
        return f"{q} ({stream})"
    return q

# Location normalization. Belfast → Derry per business decision.
LOCATION_RENAMES = {"Belfast": "Derry", "Dublin - Belfast": "Derry"}
KNOWN_LOCATIONS = [
    "Dublin - Swords", "Dublin - Tallaght", "Cork", "Galway", "Limerick",
    "Wexford", "Derry", "Clare", "Kerry", "Manchester", "Online",
]

# Sales-board transaction category → display label on the dashboard.
# Reformer/S&C/etc. don't have raw_students rows; their revenue comes
# entirely from the Sales Board.
CATEGORY_STREAM = {
    "reformer":              "Reformer",
    "fba":                   "FBA",
    "nutricert":             "NutriCert",
    "pre_post_natal":        "PPN",
    "sc_dublin":             "S&C",
    "sc_galway":             "S&C",
    "iftg_global":           "IFTG Online",
    "ai_coaches":            "AI for Coaches",
    "brand_launch":          "Brand Launch",
    "advanced_programming":  "Advanced Programming",
    "online_pilates":        "Online Pilates",
    "tesg_grants":           "Grants (TESG)",
}

def normalize_location(loc: str) -> str:
    if not loc: return ""
    loc = loc.strip()
    return LOCATION_RENAMES.get(loc, loc)

INVOICE_STATUS = {
    0: "Collections", 1: "Closed", 2: "Refunded", 3: "Partially Refunded",
    4: "Voided", 5: "Declined", 6: "Write Off", 7: "Pending",
    8: "Draft", 9: "Open",
}

def aggregate_payment_status(invoices: list[dict]) -> str:
    """Per-contact rollup. Priority order: Collections > Declined > Paid >
    Partial > Open > Unpaid."""
    if not invoices: return "unpaid"
    codes = {int(i.get("status_code") or 0) for i in invoices}
    if 0 in codes: return "collections"
    if 5 in codes and any(float(i.get("balance") or 0) > 0 for i in invoices if int(i.get("status_code") or -1) == 5):
        return "declined"
    closed = [i for i in invoices if int(i.get("status_code") or -1) == 1]
    if closed and all(float(i.get("balance") or 0) == 0 for i in invoices):
        return "paid"
    if closed: return "partial"
    if any(c in codes for c in (7, 9)): return "open"
    return "unpaid"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as c:
        c.executescript(SCHEMA)
        # idempotent column adds for existing DBs
        existing = {r["name"] for r in c.execute("PRAGMA table_info(students)").fetchall()}
        for col, ddl in [("cert_issued", "INTEGER DEFAULT 0"),
                         ("cert_issued_at", "TEXT")]:
            if col not in existing:
                c.execute(f"ALTER TABLE students ADD COLUMN {col} {ddl}")
        # invoice_id on transactions — used to trace an invoice-level payment
        # method tag back to the underlying ONtraport invoice. Nullable so
        # existing manual (non-invoice-linked) transactions keep working.
        existing_tx = {r["name"] for r in c.execute("PRAGMA table_info(transactions)").fetchall()}
        if "invoice_id" not in existing_tx:
            c.execute("ALTER TABLE transactions ADD COLUMN invoice_id INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS ix_tx_invoice ON transactions(invoice_id)")

def period_for(d: date | None) -> str:
    """Feb–Jul → S{yy}, Aug–Dec → A{yy}, Jan → A{yy-1}. None → ''."""
    if not d: return ""
    y, m = d.year, d.month
    if 2 <= m <= 7:  return f"S{y % 100:02d}"
    if 8 <= m <= 12: return f"A{y % 100:02d}"
    return f"A{(y - 1) % 100:02d}"  # January

def set_meta(key: str, value: str) -> None:
    with get_db() as c:
        c.execute("""
            INSERT INTO meta (key, value, updated_at) VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
        """, (key, value))

def get_meta(key: str) -> tuple[str | None, str | None]:
    with get_db() as c:
        r = c.execute("SELECT value, updated_at FROM meta WHERE key=?", (key,)).fetchone()
    return (r["value"], r["updated_at"]) if r else (None, None)

def parse_date(s: str | None) -> date | None:
    if not s: return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: continue
    return None
