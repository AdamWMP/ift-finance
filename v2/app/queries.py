"""Read-side queries for the dashboard. Period-aware."""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import date, timedelta
from .db import CATEGORY_STREAM, SALE_VALUE, SALES_TARGETS, get_db


def shorten_plan(s: str | None) -> str:
    """Compress messy payment-plan strings into a short readable form.

    Examples handled:
      "300 + 10 x 205"             → "€300 + 10×€205"
      "500 dep + 262.50 x 8 months"→ "€500 + 8×€263"
      "Paid in Full — €1000"       → "Paid in full · €1,000"
      "€2000 deposit + €160.00/mo x 5 months (billed 30th)" → "€2,000 + 5×€160"
    """
    if not s: return ""
    raw = s.strip()
    if not raw: return ""
    # Paid in full
    if re.search(r"paid\s*in\s*full", raw, re.I):
        m = re.search(r"€?\s*([\d,]+(?:\.\d+)?)", raw)
        return f"Paid in full · €{int(float(m.group(1).replace(',',''))):,}" if m else "Paid in full"
    # Extract numeric tokens (deposit + instalments)
    # Patterns we look for: "N x €Y" / "€Y x N" / "N * Y" / "N x Y months"
    nums = re.findall(r"([\d,]+(?:\.\d+)?)", raw)
    if not nums: return raw[:30]
    try: deposit = int(float(nums[0].replace(",", "")))
    except (ValueError, IndexError): deposit = 0
    # try N × amount
    m = re.search(r"(\d+)\s*[x*×]\s*€?\s*([\d,.]+)", raw)
    if m:
        try:
            n = int(m.group(1)); amt = int(float(m.group(2).replace(",", "")))
            return f"€{deposit:,} + {n}×€{amt:,}"
        except ValueError: pass
    # try amount × N
    m = re.search(r"€?([\d,.]+)\s*[x*×]\s*(\d+)", raw)
    if m:
        try:
            n = int(m.group(2)); amt = int(float(m.group(1).replace(",", "")))
            return f"€{deposit:,} + {n}×€{amt:,}"
        except ValueError: pass
    # First-day style: "1500+1300(First Day)"
    if re.search(r"first\s*day", raw, re.I) and len(nums) >= 2:
        try:
            d = int(float(nums[0].replace(",", "")))
            f = int(float(nums[1].replace(",", "")))
            return f"€{d:,} + €{f:,} day 1"
        except ValueError: pass
    return raw[:32] + ("…" if len(raw) > 32 else "")


def deposit_from_plan(s: str | None) -> float:
    if not s: return 0.0
    m = re.search(r"([\d,]+(?:\.\d+)?)", s)
    if not m: return 0.0
    try: return float(m.group(1).replace(",", ""))
    except ValueError: return 0.0


def avg_deposit(period: str) -> dict:
    """Average deposit per S26 student with a payment plan."""
    with get_db() as c:
        rs = c.execute("""
            SELECT payment_plan, spent FROM students
            WHERE revenue_period=? AND is_dropoff=0 AND payment_plan != ''
        """, (period,)).fetchall()
    deposits = []
    for r in rs:
        d = deposit_from_plan(r["payment_plan"])
        if 0 < d <= (r["spent"] or 0) + 50:  # plausible: deposit must be ≤ what they paid
            deposits.append(d)
    if not deposits:
        return {"count": 0, "avg": 0, "min": 0, "max": 0}
    return {
        "count": len(deposits),
        "avg": sum(deposits) / len(deposits),
        "min": min(deposits),
        "max": max(deposits),
    }


def search(q: str, limit: int = 12) -> list[dict]:
    """Quick search across students, groups and invoices for the ⌘K palette."""
    needle = (q or "").strip().lower()
    if len(needle) < 2: return []
    like = f"%{needle}%"
    out = []
    with get_db() as c:
        for r in c.execute("""
            SELECT contact_id, first_name, last_name, email, stream, group_id
            FROM students
            WHERE LOWER(first_name||' '||last_name) LIKE ?
               OR LOWER(email) LIKE ?
               OR contact_id = ?
            GROUP BY contact_id LIMIT ?
        """, (like, like, needle, limit)).fetchall():
            name = f"{r['first_name']} {r['last_name']}".strip() or f"Contact {r['contact_id']}"
            out.append({"kind": "student", "label": name,
                        "sub": f"{r['stream']} · CID {r['contact_id']} · {r['email'] or ''}",
                        "url": f"/group?id={r['group_id']}#cid-{r['contact_id']}"})
        for r in c.execute("""
            SELECT DISTINCT group_id, stream, location, start_date, timetable
            FROM students WHERE LOWER(group_id) LIKE ? LIMIT ?
        """, (like, limit)).fetchall():
            out.append({"kind": "group", "label": friendly_group_label(r["stream"], r["location"], r["start_date"], r["timetable"]),
                        "sub": r["group_id"],
                        "url": f"/group?id={r['group_id']}"})
        if needle.isdigit():
            for r in c.execute("SELECT id, contact_id, total, status FROM invoices WHERE id=? LIMIT 1", (int(needle),)).fetchall():
                out.append({"kind": "invoice", "label": f"Invoice #{r['id']}",
                            "sub": f"€{r['total'] or 0:.0f} · {r['status']}",
                            "url": f"https://app.ontraport.com/#!/invoice/edit&id={r['id']}"})
    return out[:limit]


def today_panel(period: str | None = None) -> dict:
    """Activity windows: today, last 7 days. New sales = contacts whose
    very first invoice falls in the window."""
    today_iso = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    with get_db() as c:
        # Payments closed today + last 7d.
        # Fall back to invoice_date when closed_date isn't populated — a chunk
        # of ONtraport's Closed invoices ship with closed_date NULL, and the
        # strict-equality filter on closed_date was silently dropping them
        # (~€16k/wk gap vs the Activity feed cross-check).
        paid_today = c.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(total_paid),0) AS amt
            FROM invoices
            WHERE status_code=1
              AND COALESCE(closed_date, invoice_date, '') = ?
        """, (today_iso,)).fetchone()
        paid_week = c.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(total_paid),0) AS amt
            FROM invoices
            WHERE status_code=1
              AND COALESCE(closed_date, invoice_date, '') >= ?
        """, (week_ago,)).fetchone()

        # New sales: contacts whose first ever invoice is within window
        new_sales_today = c.execute("""
            SELECT COUNT(DISTINCT first_inv.contact_id) AS n,
                   COALESCE(SUM(first_inv.total),0) AS amt
            FROM (
                SELECT contact_id, MIN(invoice_date) AS first_date, total
                FROM invoices GROUP BY contact_id
            ) first_inv
            WHERE first_inv.first_date = ?
        """, (today_iso,)).fetchone()
        new_sales_week = c.execute("""
            SELECT COUNT(DISTINCT first_inv.contact_id) AS n,
                   COALESCE(SUM(first_inv.total),0) AS amt
            FROM (
                SELECT contact_id, MIN(invoice_date) AS first_date, total
                FROM invoices GROUP BY contact_id
            ) first_inv
            WHERE first_inv.first_date >= ?
        """, (week_ago,)).fetchone()

        failures_week = c.execute("""
            SELECT COUNT(DISTINCT contact_id) AS n FROM invoices
            WHERE status_code IN (0, 5) AND last_recharge_date >= ?
        """, (week_ago,)).fetchone()

        certs_today = c.execute("""
            SELECT COUNT(*) AS n FROM students
            WHERE cert_issued=1 AND substr(cert_issued_at,1,10) = ?
        """, (today_iso,)).fetchone()

        # 5 most recent new sales for the live feed
        recent = c.execute("""
            SELECT first_inv.contact_id, first_inv.first_date, first_inv.total,
                   s.first_name, s.last_name, s.stream, s.location, s.start_date
            FROM (
                SELECT contact_id, MIN(invoice_date) AS first_date, MAX(total) AS total
                FROM invoices GROUP BY contact_id
            ) first_inv
            LEFT JOIN students s ON s.contact_id = first_inv.contact_id
            WHERE first_inv.first_date >= ?
            ORDER BY first_inv.first_date DESC LIMIT 8
        """, (week_ago,)).fetchall()

    return {
        "today": {
            "new_payments":  paid_today["n"]  or 0,
            "new_payments_amt": paid_today["amt"] or 0,
            "new_sales":     new_sales_today["n"] or 0,
            "new_sales_amt": new_sales_today["amt"] or 0,
            "certs_issued":  certs_today["n"] or 0,
        },
        "week": {
            "new_payments":  paid_week["n"]  or 0,
            "new_payments_amt": paid_week["amt"] or 0,
            "new_sales":     new_sales_week["n"] or 0,
            "new_sales_amt": new_sales_week["amt"] or 0,
            "failures":      failures_week["n"] or 0,
        },
        "recent_sales": [
            {
                "contact_id": r["contact_id"],
                "name": (f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or f"Contact {r['contact_id']}"),
                "date": r["first_date"],
                "total": r["total"] or 0,
                "stream": r["stream"] or "—",
                "location": r["location"] or "—",
                "url": f"/group?id={(r['stream'] or '')} · {(r['location'] or '—')} · {(r['start_date'] or 'TBD')}",
            }
            for r in recent
        ],
    }


def cohort_forecast(group_id: str) -> dict | None:
    """Linear projection: at current daily collection rate, when does the
    cohort hit 100%?"""
    with get_db() as c:
        rs = c.execute("""
            SELECT MIN(start_date) AS start, SUM(price) AS expected, SUM(spent) AS collected
            FROM students WHERE group_id=?
        """, (group_id,)).fetchone()
    if not rs or not rs["expected"]: return None
    expected = rs["expected"] or 0
    collected = rs["collected"] or 0
    pct = (collected / expected * 100) if expected else 0
    try:
        start = date.fromisoformat((rs["start"] or "")[:10])
    except (ValueError, TypeError): return None
    days_running = max(1, (date.today() - start).days + 30)  # +30 so very-fresh cohorts don't divide by ~0
    daily_rate = collected / days_running if days_running else 0
    if daily_rate <= 0:
        return {"pct": pct, "summary": f"{pct:.0f}% collected · no payments yet, can't forecast"}
    days_remaining = (expected - collected) / daily_rate
    target_date = date.today() + timedelta(days=int(days_remaining))
    return {
        "pct": pct,
        "daily_rate": daily_rate,
        "target_date": target_date.isoformat(),
        "summary": f"At current rate (~€{daily_rate:.0f}/day) this cohort lands at 100% by {target_date.strftime('%-d %b %Y')}",
    }


def get_note(contact_id: str, stream: str = "") -> str:
    with get_db() as c:
        r = c.execute("SELECT body FROM notes WHERE contact_id=? AND stream=?",
                      (contact_id, stream)).fetchone()
    return (r["body"] if r else "") or ""


def set_note(contact_id: str, stream: str, body: str) -> None:
    with get_db() as c:
        c.execute("""
            INSERT INTO notes (contact_id, stream, body, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(contact_id, stream) DO UPDATE
              SET body=excluded.body, updated_at=datetime('now')
        """, (contact_id, stream, body or ""))


def all_notes_for_contacts(contact_ids: list[str]) -> dict[str, str]:
    if not contact_ids: return {}
    placeholders = ",".join("?" * len(contact_ids))
    with get_db() as c:
        rs = c.execute(
            f"SELECT contact_id, stream, body FROM notes WHERE contact_id IN ({placeholders})",
            contact_ids,
        ).fetchall()
    return {r["contact_id"]: r["body"] or "" for r in rs}


def record_snapshot(period: str, snapshot_date: str | None = None) -> dict:
    """Capture today's sales-equivalent + revenue for `period`.
    Same-day re-runs overwrite (date+period is the unique key), so the
    10-min sync loop keeps the trailing point fresh until the day rolls over."""
    s = sales_summary(period)
    d = snapshot_date or date.today().isoformat()
    with get_db() as c:
        c.execute("""
            INSERT INTO snapshots (snapshot_date, period, sales_count, revenue)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(snapshot_date, period) DO UPDATE SET
                sales_count = excluded.sales_count,
                revenue     = excluded.revenue,
                created_at  = datetime('now')
        """, (d, period, int(round(s["sales"])), float(s["collected"])))
    return {"date": d, "period": period, "sales": s["sales"], "revenue": s["collected"]}


def trend(period: str, days: int = 90) -> list[dict]:
    """Snapshot series for the last `days` days, ascending by date."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_db() as c:
        rs = c.execute("""
            SELECT snapshot_date, sales_count, revenue
            FROM snapshots
            WHERE period=? AND snapshot_date >= ?
            ORDER BY snapshot_date ASC
        """, (period, cutoff)).fetchall()
    return [{"date": r["snapshot_date"], "sales": r["sales_count"] or 0,
             "revenue": r["revenue"] or 0.0} for r in rs]


def compare_periods(a: str, b: str) -> dict:
    """Side-by-side hero metrics for two periods."""
    return {"a": macro(a), "b": macro(b), "a_label": a, "b_label": b}


def manual_transactions(period: str | None = None) -> list[dict]:
    """Manually-entered transactions (cash drop-offs, corporate B2B, marketing
    spend, etc.) — anything not in ONtraport."""
    sql = """
        SELECT id, date, period, direction, category, subcategory, amount,
               contact_id, source, status, note, created_at
        FROM transactions
        WHERE source = 'manual'
    """
    args = []
    if period:
        sql += " AND period = ?"
        args.append(period)
    sql += " ORDER BY date DESC, id DESC"
    with get_db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def manual_transactions_summary(period: str | None = None) -> dict:
    rows = manual_transactions(period)
    in_total  = sum(r["amount"] for r in rows if r["direction"] == "in")
    out_total = sum(r["amount"] for r in rows if r["direction"] == "out")
    by_cat = {}
    for r in rows:
        key = (r["direction"], r["category"])
        by_cat[key] = by_cat.get(key, 0) + r["amount"]
    return {"count": len(rows), "in": in_total, "out": out_total, "by_cat": by_cat}

# Categories that overlap with raw_students (Pilates / PT) — counted there
# already, so MUST NOT be re-added to macro.collected. They still appear in
# the per-stream / per-method breakdowns for visibility, just not in totals.
#
# NOTE: Once we confirm the follow-on-stream live ingest is producing real
# `spent` values per contact (i.e. ONtraport rollup fields f2322/f2327/f2333/
# f2599/f2617 are populated end-to-end), the SB categories below should be
# added back here so we stop double-counting. Until then, keep the SB amounts
# in totals — better to slightly over-count for now than to silently drop
# follow-on revenue from the "Money in" / "Fees paid" headline figures.
OVERLAPPING_SB_CATEGORIES = {"online_pilates", "tesg_grants", "iftg_global"}

def _sales_board_by_category(period: str, *, exclude_overlapping: bool = True) -> dict[str, float]:
    with get_db() as c:
        rs = c.execute("""
            SELECT category, COALESCE(SUM(amount),0) AS amt
            FROM transactions
            WHERE period=? AND direction='in' AND source='sales_board'
            GROUP BY category
        """, (period,)).fetchall()
    out = {r["category"]: r["amt"] or 0 for r in rs}
    if exclude_overlapping:
        out = {k: v for k, v in out.items() if k not in OVERLAPPING_SB_CATEGORIES}
    return out

def macro(period: str) -> dict:
    with get_db() as c:
        r = c.execute("""
            SELECT
                COUNT(DISTINCT contact_id) AS students,
                COALESCE(SUM(price),0)     AS expected,
                COALESCE(SUM(spent),0)     AS collected
            FROM students
            WHERE revenue_period = ? AND is_dropoff = 0
        """, (period,)).fetchone()
    sb_extra = sum(_sales_board_by_category(period).values())
    expected  = (r["expected"] or 0) + sb_extra      # no separate "expected"
    collected = (r["collected"] or 0) + sb_extra
    return {
        "students": r["students"] or 0,
        "expected": expected,
        "collected": collected,
        "outstanding": expected - collected,
        "rate_pct": (collected / expected * 100) if expected else 0.0,
    }

def by_stream(period: str) -> list[dict]:
    """PT/Pilates from raw_students; everything else (Reformer, S&C, NutriCert,
    PPN, FBA, etc.) from sales_board transactions, mapped via CATEGORY_STREAM."""
    out: dict[str, dict] = {}
    with get_db() as c:
        rs = c.execute("""
            SELECT stream,
                   COUNT(*) AS students,
                   COALESCE(SUM(price),0) AS expected,
                   COALESCE(SUM(spent),0) AS collected
            FROM students
            WHERE revenue_period=? AND is_dropoff=0
            GROUP BY stream
        """, (period,)).fetchall()
    for r in rs:
        out[r["stream"]] = {
            "stream": r["stream"], "students": r["students"],
            "expected": r["expected"] or 0, "collected": r["collected"] or 0,
        }
    for cat, amt in _sales_board_by_category(period).items():
        label = CATEGORY_STREAM.get(cat, cat.title())
        d = out.setdefault(label, {"stream": label, "students": 0, "expected": 0, "collected": 0})
        d["expected"]  += amt   # treat collected as expected for sales-board streams
        d["collected"] += amt
    return [_row_with_pct(d) for d in sorted(out.values(), key=lambda x: -x["expected"])]

def by_location(period: str) -> list[dict]:
    with get_db() as c:
        rs = c.execute("""
            SELECT COALESCE(NULLIF(location,''),'—') AS location,
                   COUNT(DISTINCT contact_id) AS students,
                   COUNT(DISTINCT group_id)   AS groups,
                   COALESCE(SUM(price),0)     AS expected,
                   COALESCE(SUM(spent),0)     AS collected
            FROM students
            WHERE revenue_period = ? AND is_dropoff = 0
            GROUP BY location ORDER BY expected DESC
        """, (period,)).fetchall()
    return [_row_with_pct(r) for r in rs]

def by_pathway(period: str) -> list[dict]:
    with get_db() as c:
        rs = c.execute("""
            SELECT pathway,
                   COUNT(DISTINCT contact_id) AS sales,
                   COALESCE(SUM(price),0) AS expected,
                   COALESCE(SUM(spent),0) AS collected
            FROM students
            WHERE revenue_period = ? AND is_dropoff = 0
            GROUP BY pathway ORDER BY collected DESC
        """, (period,)).fetchall()
    return [_row_with_pct(r) for r in rs]

def pathway_detail(pathway: str, period: str) -> dict:
    with get_db() as c:
        rs = c.execute("""
            SELECT contact_id, first_name, last_name, email, phone,
                   stream, qualification, location, start_date, group_id,
                   price, spent, payment_plan, payment_status,
                   COALESCE(cert_issued,0) AS cert_issued, cert_issued_at,
                   ROUND(CASE WHEN price>0 THEN spent*100.0/price ELSE 0 END, 1) AS pct
            FROM students
            WHERE pathway=? AND revenue_period=? AND is_dropoff=0
            ORDER BY last_name, first_name
        """, (pathway, period)).fetchall()
    students = []
    for r in rs:
        d = dict(r)
        d["name"] = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        d["outstanding"] = (d["price"] or 0) - (d["spent"] or 0)
        d["ontraport_url"] = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        d["group_label"] = friendly_group_label(d["stream"], d["location"], d["start_date"], None)
        if d["price"] and d["spent"] >= d["price"]: d["status"] = "paid"
        elif d["price"] and d["spent"] >= d["price"] * 0.5: d["status"] = "cert-ready"
        elif d["spent"] > 0: d["status"] = "in-progress"
        else: d["status"] = "unpaid"
        students.append(d)
    expected = sum(s["price"] or 0 for s in students)
    collected = sum(s["spent"] or 0 for s in students)
    return {
        "pathway": pathway, "period": period,
        "students": students, "n": len(students),
        "expected": expected, "collected": collected,
        "outstanding": expected - collected,
        "pct": (collected / expected * 100) if expected else 0,
        "counts": {
            "paid":       sum(1 for s in students if s["status"] == "paid"),
            "cert_ready": sum(1 for s in students if s["status"] == "cert-ready"),
            "in_progress":sum(1 for s in students if s["status"] == "in-progress"),
            "unpaid":     sum(1 for s in students if s["status"] == "unpaid"),
            "cert_issued":sum(1 for s in students if s["cert_issued"]),
        },
    }

def sales_summary(period: str) -> dict:
    """Sales-equivalent progress against the milestones.

    The targets (200, 250, 300, 350, 400) are *revenue-based sales*:
        1 sale = €{SALE_VALUE} of revenue collected.
    So the headline number on the bar is `sales_equivalent_collected` =
    (raw_students.spent + sales_board.amounts) / SALE_VALUE — the same value
    that gets pushed to the Sales Board's L18 cell.

    `actual_paying` (head-count of paying contacts) is also returned for
    reference but doesn't drive the bar.
    """
    with get_db() as c:
        r = c.execute("""
            SELECT COUNT(DISTINCT contact_id) AS actual_paying,
                   COUNT(DISTINCT contact_id) FILTER (WHERE qualification != '') AS potential,
                   COALESCE(SUM(spent),0) AS rs_collected,
                   COALESCE(SUM(price),0) AS rs_expected
            FROM students
            WHERE revenue_period = ? AND is_dropoff = 0
        """, (period,)).fetchone()
    sb_extra = sum(_sales_board_by_category(period).values())
    collected = (r["rs_collected"] or 0) + sb_extra
    expected  = (r["rs_expected"]  or 0) + sb_extra
    sales = round(collected / SALE_VALUE, 1)
    expected_sales = round(expected / SALE_VALUE, 1)

    # Pick the next target above the live sales-equivalent count.
    next_target = next((t for t in SALES_TARGETS if t > sales), SALES_TARGETS[-1])
    next_idx    = SALES_TARGETS.index(next_target)
    floor       = SALES_TARGETS[next_idx - 1] if next_idx > 0 else 0
    progress_pct = ((sales - floor) / (next_target - floor) * 100) if next_target > floor else 100

    # Daily-collection-rate projection — based on the LAST 60 DAYS of paid
    # invoices, so older history doesn't drag the velocity to near-zero.
    daily_rate = _recent_daily_rate(60)
    daily_sales = daily_rate / SALE_VALUE if daily_rate > 0 else 0
    milestones = []
    for t in SALES_TARGETS:
        target_revenue = t * SALE_VALUE
        # Active (non-drop-off) potential — what we could realistically collect.
        unreachable = expected_sales < t
        if sales >= t:
            milestones.append({"target": t, "hit": True, "label": "Hit",
                               "revenue": target_revenue, "unreachable": False})
        elif unreachable:
            shortfall = target_revenue - expected
            milestones.append({"target": t, "hit": False, "unreachable": True,
                               "revenue": target_revenue,
                               "label": f"Need €{shortfall/1000:.0f}k more potential"})
        elif daily_sales <= 0:
            milestones.append({"target": t, "hit": False, "unreachable": False,
                               "revenue": target_revenue, "label": "—"})
        else:
            days_left = (t - sales) / daily_sales
            d = date.today() + timedelta(days=int(days_left))
            milestones.append({"target": t, "hit": False, "unreachable": False,
                               "revenue": target_revenue,
                               "label": d.strftime("%-d %b %Y"), "days": int(days_left)})

    return {
        "sales": sales,                          # ← bar uses this
        "expected_sales": expected_sales,
        "actual_paying": r["actual_paying"] or 0,
        "potential": r["potential"] or 0,
        "remaining_potential": max((r["potential"] or 0) - (r["actual_paying"] or 0), 0),
        "collected": collected,
        "expected": expected,
        "sale_value": SALE_VALUE,
        "targets": SALES_TARGETS,
        "next_target": next_target,
        "to_next_target": max(round(next_target - sales, 1), 0),
        "progress_pct": max(0, min(progress_pct, 100)),
        "daily_rate": daily_rate,
        "daily_sales": daily_sales,
        "milestones": milestones,
    }


def _recent_daily_rate(days: int = 60) -> float:
    """Average daily collected revenue over the last N days of paid invoices."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_db() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(total_paid),0) AS s FROM invoices "
            "WHERE status_code=1 AND COALESCE(closed_date, invoice_date, '') >= ?",
            (cutoff,)).fetchone()
    return (r["s"] or 0) / days


def students_filtered(period: str, *, payment_method: str | None = None,
                      location: str | None = None, pathway: str | None = None,
                      stream: str | None = None) -> dict:
    """Generic filter — used by drill-down pages for payment methods, locations,
    pathways, and streams alike."""
    sql = """
        SELECT contact_id, first_name, last_name, email, phone,
               stream, qualification, location, start_date, group_id,
               price, spent, payment_plan, payment_method, payment_status,
               COALESCE(cert_issued,0) AS cert_issued,
               ROUND(CASE WHEN price>0 THEN spent*100.0/price ELSE 0 END, 1) AS pct
        FROM students
        WHERE revenue_period=? AND is_dropoff=0
    """
    args: list = [period]
    if payment_method:
        sql += " AND COALESCE(NULLIF(payment_method,''),'Unspecified') = ?"
        args.append(payment_method)
    if location:
        sql += " AND COALESCE(NULLIF(location,''),'—') = ?"
        args.append(location)
    if pathway:
        sql += " AND pathway = ?"
        args.append(pathway)
    if stream:
        sql += " AND stream = ?"
        args.append(stream)
    sql += " ORDER BY last_name, first_name"
    with get_db() as c:
        rs = c.execute(sql, args).fetchall()
    students = []
    for r in rs:
        d = dict(r)
        d["name"] = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        d["outstanding"] = (d["price"] or 0) - (d["spent"] or 0)
        d["ontraport_url"] = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        d["group_label"] = friendly_group_label(d["stream"], d["location"], d["start_date"], None)
        d["plan_short"] = shorten_plan(d.get("payment_plan"))
        if d["price"] and d["spent"] >= d["price"]: d["status"] = "paid"
        elif d["price"] and d["spent"] >= d["price"] * 0.5: d["status"] = "cert-ready"
        elif d["spent"] > 0: d["status"] = "in-progress"
        else: d["status"] = "unpaid"
        students.append(d)
    expected  = sum(s["price"]  or 0 for s in students)
    collected = sum(s["spent"]  or 0 for s in students)
    title_bits = [period]
    if payment_method: title_bits.append(f"Payment method: {payment_method}")
    if location:       title_bits.append(f"Location: {location}")
    if pathway:        title_bits.append(f"Pathway: {pathway}")
    if stream:         title_bits.append(f"Stream: {stream}")
    return {
        "students": students, "n": len(students),
        "expected": expected, "collected": collected,
        "outstanding": expected - collected,
        "pct": (collected / expected * 100) if expected else 0,
        "title": " · ".join(title_bits),
        "period": period,
        "filter_kind": ("method" if payment_method else "location" if location
                        else "pathway" if pathway else "stream" if stream else "all"),
        "filter_value": payment_method or location or pathway or stream or "",
        "counts": {
            "paid":       sum(1 for s in students if s["status"] == "paid"),
            "cert_ready": sum(1 for s in students if s["status"] == "cert-ready"),
            "in_progress":sum(1 for s in students if s["status"] == "in-progress"),
            "unpaid":     sum(1 for s in students if s["status"] == "unpaid"),
            "cert_issued":sum(1 for s in students if s["cert_issued"]),
        },
    }

def friendly_group_label(stream: str, location: str, start_date: str, timetable: str | None = None) -> str:
    """Turn 'PT · Dublin - Swords · 2026-04-27' into something humans read."""
    from datetime import date
    loc = (location or "").strip()
    if " - " in loc:
        loc = loc.split(" - ", 1)[1]
    elif loc.lower().startswith("dublin"):
        loc = "Dublin"
    if not loc: loc = "—"
    when = "TBD"
    try:
        d = date.fromisoformat((start_date or "")[:10])
        when = d.strftime("%-d %b %Y")
    except (ValueError, TypeError): pass
    bits = [f"{loc} {stream}", when]
    tt = (timetable or "").strip()
    if tt:
        # collapse the busiest descriptors to short forms
        short = tt.replace("Evening & Weekend - ", "").replace(" (8 Weeks)", "")
        if len(short) > 38: short = short[:35] + "…"
        bits.insert(1, short)
    return " · ".join(bits)

def group_detail(group_id: str) -> dict | None:
    with get_db() as c:
        students = c.execute("""
            SELECT contact_id, first_name, last_name, email, phone,
                   stream, qualification, location, start_date, timetable,
                   price, spent, payment_plan, payment_method,
                   pathway, class_period, revenue_period,
                   is_deferral, payment_status,
                   COALESCE(cert_issued, 0) AS cert_issued, cert_issued_at,
                   ROUND(CASE WHEN price>0 THEN spent*100.0/price ELSE 0 END, 1) AS pct
            FROM students
            WHERE group_id = ?
            ORDER BY last_name, first_name
        """, (group_id,)).fetchall()
        if not students: return None
        agg = c.execute("""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(price),0) AS expected,
                   COALESCE(SUM(spent),0) AS collected,
                   SUM(CASE WHEN price>0 AND spent>=price THEN 1 ELSE 0 END) AS paid_count,
                   SUM(CASE WHEN price>0 AND spent>=price*0.5 AND spent<price THEN 1 ELSE 0 END) AS cert_ready_count,
                   SUM(CASE WHEN spent>0 AND spent<price*0.5 THEN 1 ELSE 0 END) AS in_progress_count,
                   SUM(CASE WHEN spent=0 THEN 1 ELSE 0 END) AS unpaid_count
            FROM students WHERE group_id = ?
        """, (group_id,)).fetchone()
    expected = agg["expected"] or 0
    collected = agg["collected"] or 0
    students_out = []
    for r in students:
        d = dict(r)
        d["name"] = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        d["outstanding"] = (d["price"] or 0) - (d["spent"] or 0)
        d["ontraport_url"] = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        if d["price"] and d["spent"] >= d["price"]:
            d["status"] = "paid"
        elif d["price"] and d["spent"] >= d["price"] * 0.5:
            d["status"] = "cert-ready"
        elif d["spent"] > 0:
            d["status"] = "in-progress"
        else:
            d["status"] = "unpaid"
        students_out.append(d)
    for s in students_out:
        s["plan_short"] = shorten_plan(s.get("payment_plan"))
    cert_issued_count = sum(1 for s in students_out if s.get("cert_issued"))
    return {
        "group_id": group_id,
        "label": friendly_group_label(students[0]["stream"], students[0]["location"],
                                      students[0]["start_date"], students[0]["timetable"]),
        "stream": students[0]["stream"],
        "location": students[0]["location"],
        "start_date": students[0]["start_date"],
        "timetable": students[0]["timetable"],
        "period": students[0]["class_period"],
        "students": students_out,
        "n": agg["n"], "expected": expected, "collected": collected,
        "outstanding": expected - collected,
        "pct": (collected / expected * 100) if expected else 0,
        "counts": {
            "paid": agg["paid_count"] or 0,
            "cert_ready": agg["cert_ready_count"] or 0,
            "in_progress": agg["in_progress_count"] or 0,
            "unpaid": agg["unpaid_count"] or 0,
            "cert_issued": cert_issued_count,
        },
    }

# Manual transaction category → display method label
METHOD_FROM_CATEGORY = {
    "cash_dropoff":  "Cash",
    "bank_transfer": "Bank Transfer",
    "stripe":        "Stripe",
    "revolut":       "Revolut",
    "skillnet":      "SkillNet",
    "dsp":           "DSP",
    "grant":         "Grant",
}

def _manual_payments_per_contact(period: str) -> dict[str, dict[str, float]]:
    """{contact_id → {method_label → amount}} from the manual transactions log."""
    out: dict[str, dict[str, float]] = {}
    with get_db() as c:
        rs = c.execute("""
            SELECT contact_id, category, SUM(amount) AS amt
            FROM transactions
            WHERE source='manual' AND direction='in' AND period=?
                  AND contact_id IS NOT NULL AND contact_id != ''
            GROUP BY contact_id, category
        """, (period,)).fetchall()
    for r in rs:
        meth = METHOD_FROM_CATEGORY.get(r["category"], r["category"].title())
        out.setdefault(r["contact_id"], {})[meth] = (r["amt"] or 0)
    return out

def by_payment_method(period: str) -> list[dict]:
    """How money has come in. Combines:
      • students.payment_method × spent (default Stripe for empty)
      • Manual per-student transactions (Cash, Bank Transfer, etc.) which
        re-allocate that student's spent away from the auto-assigned method.
    """
    manual = _manual_payments_per_contact(period)
    with get_db() as c:
        students = c.execute("""
            SELECT contact_id, COALESCE(NULLIF(payment_method,''),'Stripe') AS method,
                   COALESCE(SUM(spent),0) AS spent
            FROM students
            WHERE revenue_period=? AND is_dropoff=0
            GROUP BY contact_id, method
        """, (period,)).fetchall()
    by_method: dict[str, float] = {}
    for r in students:
        cid = r["contact_id"]
        spent = r["spent"] or 0
        method = r["method"]
        # Pull off any manually-recorded amounts for this contact
        for m, amt in manual.get(cid, {}).items():
            by_method[m] = by_method.get(m, 0) + amt
            spent -= amt  # remainder stays under their default method
        spent = max(spent, 0)
        by_method[method] = by_method.get(method, 0) + spent
    # Sales Board categories (Reformer, NutriCert, PPN, FBA, S&C, etc.) are
    # collected revenue too — without this they're counted under Active Fees
    # Paid but never appear under "How money has come in", and the totals stop
    # reconciling. Surface them as a synthetic "Sales Board (other streams)"
    # bucket, drillable into the by-stream breakdown.
    sb_extra = sum(_sales_board_by_category(period).values())
    if sb_extra > 0:
        by_method["Sales Board (other streams)"] = (
            by_method.get("Sales Board (other streams)", 0) + sb_extra
        )
    total = sum(by_method.values()) or 1
    out = [{"method": m, "collected": v, "pct": v / total * 100}
           for m, v in by_method.items() if v > 0]
    out.sort(key=lambda x: -x["collected"])
    return out

def per_student_payment_split(contact_id: str, period: str) -> dict:
    """For a single contact: their default-method spent + any manual logged
    payments. Used by the per-student log-payment popover."""
    with get_db() as c:
        srs = c.execute("""
            SELECT COALESCE(NULLIF(payment_method,''),'Stripe') AS method,
                   COALESCE(SUM(spent),0) AS spent
            FROM students
            WHERE contact_id=? AND revenue_period=?
            GROUP BY method
        """, (contact_id, period)).fetchall()
        manuals = c.execute("""
            SELECT id, date, category, amount, note
            FROM transactions
            WHERE source='manual' AND direction='in' AND period=? AND contact_id=?
            ORDER BY date DESC, id DESC
        """, (period, contact_id)).fetchall()
    manual_total = sum(r["amount"] or 0 for r in manuals)
    auto_method = (srs[0]["method"] if srs else "Stripe")
    auto_remaining = max(sum(r["spent"] or 0 for r in srs) - manual_total, 0)
    return {
        "auto_method": auto_method,
        "auto_amount": auto_remaining,
        "manual": [dict(r) for r in manuals],
        "manual_total": manual_total,
    }

def drop_offs(period: str) -> dict:
    """Students tagged as dropped-off — fees lost (price minus what they actually paid)."""
    with get_db() as c:
        rs = c.execute("""
            SELECT contact_id, first_name, last_name, stream, qualification, location,
                   start_date, price, spent, group_id
            FROM students
            WHERE is_dropoff = 1 AND revenue_period = ?
            ORDER BY (COALESCE(price,0) - COALESCE(spent,0)) DESC
        """, (period,)).fetchall()
    rows = []
    for r in rs:
        d = dict(r)
        d["name"] = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        d["lost"] = max((d["price"] or 0) - (d["spent"] or 0), 0)
        d["ontraport_url"] = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        rows.append(d)
    return {
        "rows": rows,
        "count": len(rows),
        "potential_total": sum((r["price"] or 0) for r in rs),
        "fees_spent_total": sum((r["spent"] or 0) for r in rs),
        "money_lost": sum(d["lost"] for d in rows),
    }

def fees_summary(period: str) -> dict:
    """Top-line FEES DUE / PAID / OWED in the same shape as Adam's old dashboard.
    Includes drop-off-adjusted figures."""
    with get_db() as c:
        # everything (incl. drop-offs)
        all_r = c.execute("""
            SELECT COALESCE(SUM(price),0) AS due, COALESCE(SUM(spent),0) AS paid
            FROM students WHERE revenue_period=?
        """, (period,)).fetchone()
        # active students only
        active = c.execute("""
            SELECT COALESCE(SUM(price),0) AS due, COALESCE(SUM(spent),0) AS paid
            FROM students WHERE revenue_period=? AND is_dropoff=0
        """, (period,)).fetchone()
    sb_extra = sum(_sales_board_by_category(period).values())
    all_due  = (all_r["due"] or 0) + sb_extra
    all_paid = (all_r["paid"] or 0) + sb_extra
    act_due  = (active["due"] or 0) + sb_extra
    act_paid = (active["paid"] or 0) + sb_extra
    drop = drop_offs(period)
    return {
        "total":  {"due": all_due, "paid": all_paid, "owed": all_due - all_paid},
        "active": {"due": act_due, "paid": act_paid, "owed": act_due - act_paid},
        "drop":   drop,
    }

def fees_by_stream_named(period: str) -> list[dict]:
    """Stream rollups labelled with the user's preferred terminology
    (Combo / Pilates / Online-Belfast / etc.)."""
    label = {
        "PT": "Combo",
        "Pilates": "Pilates",
        "Reformer": "Reformer",
        "S&C": "S&C",
        "PPN": "PPN",
        "AN": "AN",
    }
    raw = by_stream(period)
    out = []
    for r in raw:
        nice = label.get(r["stream"], r["stream"])
        out.append({**r, "label": nice})
    return out

# Which streams are eligible for cert ordering. User asked for the four
# main accredited courses (PT, Mat Pilates, Reformer Pilates, S&C). PPN / AN
# / FBA also have student rows but are typically not printed as certificates
# in batches — they're issued as completions roll in.
CERT_EXPORT_STREAMS = {"PT", "Pilates", "Reformer", "S&C"}


def _split_quals(qualification: str | None) -> list[str]:
    """Split an ONtraport qualification name into individual cert quals.

    The qual name often packs multiple sub-quals in parentheses:
        "The Cert (Fitness Instructor, Group Instructor, Personal Trainer)"
            → ["Fitness Instructor", "Group Instructor", "Personal Trainer"]
        "(Launchpad Bundle) Fitness Instructor & Personal Trainer"
            → ["Fitness Instructor", "Personal Trainer"]
        "Strength & Conditioning Course"
            → ["Strength & Conditioning Course"]
        "Reformer Pilates Course (CPD)"
            → ["Reformer Pilates Course"]
    """
    q = (qualification or "").strip()
    if not q:
        return []
    # A parenthetical is a cert breakdown ONLY when the outer text names a known
    # bundle. Otherwise it could be a schedule ("Phase 1 (Fri, Sat, Sun)"),
    # accreditation metadata ("(Active IQ, RQF L4)"), or a level tag ("(CPD)").
    ACCRED_HINTS = ("active iq", "rqf", "qqi", "eqf", "cpd", "ofqual", "level ")
    BUNDLE_OUTER_RE = re.compile(
        r"^\s*(the\s+(cert|career|business|studio)|launchpad(\s+bundle)?|"
        r"high\s+performance\s+bundle|bundle:|combination\s+course|combo\s+course)",
        re.I,
    )
    m = re.search(r"\(([^)]+)\)", q)
    paren = m.group(1) if m else ""
    outer_for_check = re.sub(r"\([^)]*\)", "", q).strip()
    paren_is_breakdown = (
        bool(paren) and "," in paren
        and BUNDLE_OUTER_RE.search(outer_for_check) is not None
        and not any(h in paren.lower() for h in ACCRED_HINTS)
    )
    if paren_is_breakdown:
        parts = re.split(r"\s*,\s*", paren)
    else:
        # Strip any parentheticals (CPD / L4 / bundle name) from the outer label.
        outer = re.sub(r"\([^)]*\)", "", q).strip()
        # Split on "&" or "+" ONLY when a *bundle* parenthetical was present —
        # e.g. "(Launchpad Bundle) Fitness Instructor & Personal Trainer" → 2 quals.
        # Accreditation parentheticals like "(Active IQ, RQF L4)" don't count, so
        # "Level 4 Strength & Conditioning (Active IQ, RQF L4)" stays whole.
        paren_is_bundle = bool(paren) and not any(
            h in paren.lower() for h in ACCRED_HINTS
        )
        if paren_is_bundle:
            parts = re.split(r"\s*(?:&|\+)\s*", outer)
        else:
            parts = [outer]
    out = []
    for p in parts:
        s = p.strip(" -·")
        if s and s.lower() not in {"course", "cpd", "l4", "level 4"}:
            out.append(s)
    # de-duplicate while preserving order
    seen = set()
    dedup = []
    for s in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            dedup.append(s)
    return dedup


# --- Follow-on stream period backfill ---------------------------------------
# Cohort streams (PT/Pilates/Reformer) derive `revenue_period` from start_date.
# Follow-on streams (S&C/PPN/AN/FBA) are rolling — Adam's rule is "the term the
# money was paid in." This pass fills in `revenue_period` for those rows from
# the contact's invoice activity.
#
# Heuristic (v1): the contact's most recent paid invoice ≥ €50 is the proxy
# for "when their follow-on payment came in." For students with `spent == 0`
# (potential sales), we use today's period so they appear in the current
# dashboard as outstanding potential. Edge case: a contact who paid for both
# a main course and a follow-on across different terms can have their
# follow-on misattributed by one term — fixable later by ingesting purchase
# line-items (ONtraport object 17) so we can match invoices to products.

MIN_INVOICE_FOR_PERIOD = 50.0  # ignore €0–€49 transactions when picking the proxy

def backfill_followon_periods() -> dict:
    """Set `revenue_period` for every follow-on-stream student row.

    Returns {assigned, fallback_today, still_blank}.
    """
    from .db import period_for
    from .ingest import FOLLOWON_STREAMS  # local import to avoid module-cycle risk
    from datetime import date
    stats = {"assigned": 0, "fallback_today": 0, "still_blank": 0}
    with get_db() as c:
        rows = c.execute(f"""
            SELECT s.contact_id, s.stream, s.spent, s.price,
                   COALESCE(s.start_date,'') AS start_date
            FROM students s
            WHERE s.stream IN ({",".join("?"*len(FOLLOWON_STREAMS))})
              AND (s.revenue_period IS NULL OR s.revenue_period = '')
              AND s.is_dropoff = 0
        """, tuple(FOLLOWON_STREAMS)).fetchall()
        today_period = period_for(date.today())
        for r in rows:
            cid    = r["contact_id"]
            spent  = float(r["spent"] or 0)
            start_d= r["start_date"] or ""
            # Prefer the stream's start_date if ONtraport has one (S&C / FBA).
            if start_d:
                p = period_for(date.fromisoformat(start_d[:10])) if start_d[:10] else ""
                if p:
                    c.execute("UPDATE students SET revenue_period=?, class_period=? "
                              "WHERE contact_id=? AND stream=?",
                              (p, p, cid, r["stream"]))
                    stats["assigned"] += 1
                    continue
            # Otherwise: most recent paid invoice ≥ MIN_INVOICE_FOR_PERIOD
            if spent > 0:
                inv = c.execute("""
                    SELECT COALESCE(closed_date, invoice_date) AS d
                    FROM invoices
                    WHERE contact_id=? AND status_code=1
                      AND COALESCE(total_paid,0) >= ?
                    ORDER BY COALESCE(closed_date, invoice_date) DESC LIMIT 1
                """, (cid, MIN_INVOICE_FOR_PERIOD)).fetchone()
                if inv and inv["d"]:
                    try:
                        p = period_for(date.fromisoformat(inv["d"][:10]))
                    except (ValueError, TypeError):
                        p = ""
                    if p:
                        c.execute("UPDATE students SET revenue_period=?, class_period=COALESCE(NULLIF(class_period,''),?) "
                                  "WHERE contact_id=? AND stream=?",
                                  (p, p, cid, r["stream"]))
                        stats["assigned"] += 1
                        continue
            # Fallback: potential sale → current term
            if today_period:
                c.execute("UPDATE students SET revenue_period=?, class_period=COALESCE(NULLIF(class_period,''),?) "
                          "WHERE contact_id=? AND stream=?",
                          (today_period, today_period, cid, r["stream"]))
                stats["fallback_today"] += 1
            else:
                stats["still_blank"] += 1
    return stats


# --- "Board snapshot for Simon" panel ---------------------------------------
# Reproduces the layout of Adam's previous A25 Dashboard tab so the board can
# see total + COMBO + PILATES + ONLINE/BELFAST broken down at a glance.

def _prev_period(period: str) -> str:
    """S26 → A25, A25 → S25, S25 → A24, etc."""
    if len(period) != 3:
        return ""
    season, yy = period[0].upper(), int(period[1:])
    if season == "S":
        return f"A{yy-1:02d}"
    if season == "A":
        return f"S{yy:02d}"
    return ""

def _segment_fees(period: str, where_sql: str, params: tuple) -> dict:
    """Per-segment Due/Paid/Owed + sales-equivalent. `where_sql` is appended to
    a WHERE that already filters revenue_period and excludes drop-offs."""
    with get_db() as c:
        r = c.execute(f"""
            SELECT COUNT(DISTINCT contact_id) AS contacts,
                   COALESCE(SUM(price),0) AS due,
                   COALESCE(SUM(spent),0) AS paid
            FROM students
            WHERE revenue_period=? AND is_dropoff=0 {where_sql}
        """, (period, *params)).fetchone()
    due, paid = r["due"] or 0, r["paid"] or 0
    potential_sales = round(due / SALE_VALUE, 1)
    actual_sales    = round(paid / SALE_VALUE, 1)
    return {
        "contacts": r["contacts"] or 0,
        "due": due, "paid": paid, "owed": max(due - paid, 0),
        "potential_sales": potential_sales,
        "actual_sales": actual_sales,
        "remaining_sales": max(potential_sales - actual_sales, 0),
    }


def simon_panel(period: str) -> dict:
    """One-screen board-level snapshot: per-segment fees and sales, plus a
    'what & when has money come in' history pulled from the snapshots table.

    Segments:
      • TOTAL — everything in the current revenue_period (incl. sales board)
      • COMBO — contacts enrolled in BOTH PT and Pilates streams
      • PILATES — Pilates-only contacts (excludes combo)
      • ONLINE/BELFAST — location in ('Online','Derry') OR stream-pathway 'Online'
    """
    # TOTAL: reuse macro() so sales-board categories are included
    m = macro(period)
    total = {
        "contacts": m["students"],
        "due": m["expected"], "paid": m["collected"], "owed": max(m["outstanding"], 0),
        "potential_sales": round(m["expected"]  / SALE_VALUE, 1),
        "actual_sales":    round(m["collected"] / SALE_VALUE, 1),
        "remaining_sales": max(round((m["expected"] - m["collected"]) / SALE_VALUE, 1), 0),
    }

    # COMBO: contacts that have BOTH a PT row and a Pilates row in this period.
    with get_db() as c:
        combo_ids_rows = c.execute("""
            SELECT contact_id
            FROM students
            WHERE revenue_period=? AND is_dropoff=0 AND stream IN ('PT','Pilates')
            GROUP BY contact_id
            HAVING COUNT(DISTINCT stream) = 2
        """, (period,)).fetchall()
        combo_ids = [r["contact_id"] for r in combo_ids_rows]
    if combo_ids:
        qmarks = ",".join("?" for _ in combo_ids)
        combo = _segment_fees(period, f"AND contact_id IN ({qmarks})", tuple(combo_ids))
    else:
        combo = _segment_fees(period, "AND 1=0", ())

    # PILATES standalone: stream=Pilates AND contact NOT in combo set
    if combo_ids:
        qmarks = ",".join("?" for _ in combo_ids)
        pilates = _segment_fees(period,
            f"AND stream='Pilates' AND contact_id NOT IN ({qmarks})", tuple(combo_ids))
    else:
        pilates = _segment_fees(period, "AND stream='Pilates'", ())

    # ONLINE/BELFAST: Online location, Derry (renamed from Belfast), or anything
    # whose pathway has "Online" in it (covers Online combo pathways).
    online = _segment_fees(period,
        "AND (location IN ('Online','Derry') OR pathway LIKE '%Online%')", ())

    # Money-in summary — top rows + everything else into "Other"
    money_in_rows = by_payment_method(period)

    # Snapshot history (4 most recent month-ends if we have them, else last 4 points)
    with get_db() as c:
        rs = c.execute("""
            SELECT snapshot_date, sales_count, revenue
            FROM snapshots WHERE period=?
            ORDER BY snapshot_date DESC LIMIT 12
        """, (period,)).fetchall()
    history = [{"date": r["snapshot_date"], "sales": r["sales_count"] or 0,
                "revenue": r["revenue"] or 0.0} for r in rs]
    # keep one per calendar month, most recent kept
    seen_months, monthly = set(), []
    for h in history:
        ym = h["date"][:7]
        if ym in seen_months:
            continue
        seen_months.add(ym)
        monthly.append(h)
    monthly = list(reversed(monthly[:4]))  # ascending, max 4

    # % change vs previous term — uses existing macro() so it's tolerant of empty A25
    prev = _prev_period(period)
    prev_m = macro(prev) if prev else {"collected": 0, "expected": 0, "students": 0}
    def _delta(curr, prior):
        if not prior: return None
        return (curr - prior) / prior * 100.0

    deltas = {
        "total_paid":      _delta(total["paid"],      prev_m["collected"]),
        "total_due":       _delta(total["due"],       prev_m["expected"]),
        # segment-level deltas would require segmenting the previous period the
        # same way — backfill task. Show — for now.
    }

    return {
        "period": period,
        "prev_period": prev,
        "segments": {
            "TOTAL":          total,
            "COMBO":          combo,
            "PILATES":        pilates,
            "ONLINE/BELFAST": online,
        },
        "money_in": money_in_rows,
        "history": monthly,
        "deltas": deltas,
    }


# --- Recent transactions / invoice activity feed ----------------------------
# A searchable list of invoice-level money events: closed, declined, in
# collections, etc. Used as the "what just happened" feed when board members
# ask "show me where this €X came from".

def recent_transactions(*, q: str = "", days: int = 30, status: str = "",
                         stream: str = "", limit: int = 500) -> dict:
    """List invoices joined to student bio, filtered by free-text query +
    optional date window / status / stream.

    `q` matches against name (case-insensitive substring), email, contact_id,
    and invoice id. Returns the most recent matches first.
    """
    args: list = []
    where_parts = ["1=1"]
    # Date window — keyed on closed_date when present, else invoice_date.
    if days and days > 0:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        where_parts.append(
            "(COALESCE(i.closed_date, i.invoice_date, '') >= ?)"
        )
        args.append(cutoff)
    if status:
        # Accept either a status name ("Closed", "Declined") or a status_code int
        try:
            code = int(status)
            where_parts.append("i.status_code = ?")
            args.append(code)
        except ValueError:
            where_parts.append("i.status = ?")
            args.append(status)
    if stream:
        where_parts.append("s.stream = ?")
        args.append(stream)
    if q:
        like = f"%{q.strip()}%"
        where_parts.append("""(
            COALESCE(s.first_name,'') LIKE ? OR
            COALESCE(s.last_name,'')  LIKE ? OR
            COALESCE(s.email,'')      LIKE ? OR
            i.contact_id              LIKE ? OR
            CAST(i.id AS TEXT)        LIKE ?
        )""")
        args.extend([like, like, like, like, like])
    where_sql = " AND ".join(where_parts)
    sql = f"""
        SELECT i.id          AS invoice_id,
               i.contact_id,
               i.status_code, i.status,
               i.total, i.total_paid, i.balance,
               i.invoice_date, i.closed_date, i.due_date,
               i.last_recharge_date, i.recharge_attempts,
               s.first_name, s.last_name, s.email, s.phone,
               s.stream, s.location, s.start_date, s.group_id,
               s.revenue_period
        FROM invoices i
        LEFT JOIN students s ON s.contact_id = i.contact_id
        WHERE {where_sql}
        ORDER BY COALESCE(i.closed_date, i.invoice_date) DESC, i.id DESC
        LIMIT ?
    """
    args.append(limit)
    with get_db() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["name"] = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip() \
                    or f"Contact {d['contact_id']}"
        d["display_date"] = d["closed_date"] or d["invoice_date"] or ""
        d["ontraport_invoice_url"] = f"https://app.ontraport.com/#!/invoice/edit&id={d['invoice_id']}"
        d["ontraport_contact_url"] = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        out.append(d)
    # Summary roll-up of what's in the result set
    summary = {
        "count": len(out),
        "total_paid":   sum((r["total_paid"] or 0) for r in out),
        "total_value":  sum((r["total"] or 0)      for r in out),
        "balance_open": sum((r["balance"] or 0)    for r in out),
        "by_status":    {},
    }
    for r in out:
        st = r["status"] or "—"
        summary["by_status"][st] = summary["by_status"].get(st, 0) + 1
    return {
        "rows": out,
        "summary": summary,
        "filters": {"q": q, "days": days, "status": status, "stream": stream, "limit": limit},
    }


# --- Attendance export ------------------------------------------------------
# Tutors get a downloadable attendance sheet per cohort, with one row per
# student and one column per session date. Plus theory + practical exam +
# notes columns at the right. Session dates are generated from the cohort's
# timetable string — see _generate_session_dates for the parsing rules.

_WEEKDAY_TOKENS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _weekdays_in_text(text: str) -> list[int]:
    """Return weekday indices (0=Mon..6=Sun) appearing in the text, in order
    they first appear. Matches whole-word tokens so 'Sunday' doesn't double-
    match 'sun'."""
    t = (text or "").lower()
    found: list[int] = []
    for tok, idx in _WEEKDAY_TOKENS.items():
        # word boundary check
        if re.search(rf"\b{tok}\b", t) and idx not in found:
            found.append(idx)
    return found


def _generate_session_dates(stream: str, timetable: str, qualification: str,
                            start_date: str) -> list[date]:
    """Best-effort: turn (stream, timetable, qualification, start_date) into
    a list of class session dates.

    Patterns we recognise:
      • "(N Weeks)"            → N-week course span (default 16 for PT cohort
                                 streams, 8 for shorter blocks)
      • "Bi-Weekly" / "Bi Weekly" / "Fortnight" → every 2 weeks
      • "Intensive" / "5 Days" / "3 Weekend Intensive"
                               → consecutive-day or weekend-block layouts
      • S&C "Phase 1 (Fri, Sat, Sun)" → 3 consecutive days
      • S&C "Phase 2 (Sat, Sun)"      → 2 consecutive days
      • S&C "Phase 1 & 2 (5 Days)"    → 5 consecutive days

    Falls back to weekly sessions on the start-date's weekday for 16 weeks
    if nothing parses. Returns a sorted list of distinct dates.
    """
    if not start_date:
        return []
    try:
        d0 = date.fromisoformat(start_date[:10])
    except ValueError:
        return []

    tt = (timetable or "").lower()
    qual = (qualification or "").lower()
    combined = f"{tt} {qual}"

    # Multi-day intensive blocks
    if "phase 1 & 2" in qual or "phase 1 and 2" in qual or "5 day" in tt or "5 days" in tt:
        return [d0 + timedelta(days=i) for i in range(5)]
    if "phase 1" in qual and "phase 2" not in qual:
        # Fri/Sat/Sun
        return [d0 + timedelta(days=i) for i in range(3)]
    if "phase 2" in qual:
        # Sat/Sun
        return [d0 + timedelta(days=i) for i in range(2)]

    # "3 Weekend Intensive (Sat & Sun)" — three Sat+Sun pairs, two weeks apart
    if "weekend intensive" in tt or "3 weekend" in tt:
        out = []
        cur = d0
        for _ in range(3):
            out.append(cur)
            out.append(cur + timedelta(days=1))
            cur += timedelta(days=14)
        return sorted(set(out))

    # Extract week count
    m_weeks = re.search(r"(\d+)\s*week", tt)
    weeks = int(m_weeks.group(1)) if m_weeks else None

    is_biweekly = bool(re.search(r"bi[- ]?weekly|fortnight", tt))

    # Weekday tokens — what days do sessions fall on?
    days = _weekdays_in_text(combined)
    if not days:
        days = [d0.weekday()]
    days.sort()

    # Default duration
    if weeks is None:
        if is_biweekly:
            weeks = 16          # standard Pilates/Reformer cert span
        elif stream in {"PT", "Pilates", "Reformer"}:
            weeks = 16
        else:
            weeks = 8

    step_days = 14 if is_biweekly else 7
    horizon = d0 + timedelta(weeks=weeks)

    sessions: list[date] = []
    for dow in days:
        # First occurrence of this weekday on/after the start date
        first = d0 + timedelta(days=(dow - d0.weekday()) % 7)
        cur = first
        while cur < horizon:
            sessions.append(cur)
            cur += timedelta(days=step_days)

    return sorted(set(sessions))


def attendance_export(group_id: str) -> dict:
    """Build the attendance-sheet payload for a class group.

    Returns:
      {
        label, stream, location, timetable, start_date,
        session_dates: [date,...],
        students: [{contact_id, name, email, phone, payment_status, status, ...}],
      }
    Returns None if the group doesn't exist.
    """
    g = group_detail(group_id)
    if not g:
        return None
    sessions = _generate_session_dates(
        g["stream"], g.get("timetable"), g["students"][0].get("qualification", ""),
        g["start_date"]
    )
    return {
        "group_id": group_id,
        "label": g["label"],
        "stream": g["stream"],
        "location": g["location"],
        "timetable": g.get("timetable") or "",
        "qualification": g["students"][0].get("qualification", "") if g["students"] else "",
        "start_date": g["start_date"],
        "session_dates": sessions,
        "students": [
            {
                "contact_id":     s["contact_id"],
                "name":           s["name"],
                "email":          s.get("email") or "",
                "phone":          s.get("phone") or "",
                "payment_status": s.get("payment_status") or "",
                "status":         s.get("status") or "",
            }
            for s in g["students"]
        ],
    }


def cert_export_groups(period: str) -> list[dict]:
    """All cohorts eligible for cert export, grouped by stream for the UI picker."""
    with get_db() as c:
        rs = c.execute("""
            SELECT group_id, stream, location, start_date, timetable,
                   COUNT(*) AS students,
                   SUM(CASE WHEN cert_issued=1 THEN 1 ELSE 0 END) AS already_issued
            FROM students
            WHERE class_period = ? AND is_dropoff = 0
            GROUP BY group_id ORDER BY start_date, stream, location
        """, (period,)).fetchall()
    out = []
    for r in rs:
        if r["stream"] not in CERT_EXPORT_STREAMS:
            continue
        d = dict(r)
        d["label"] = friendly_group_label(r["stream"], r["location"],
                                          r["start_date"], r["timetable"])
        out.append(d)
    return out


def cert_export_rows(group_ids: list[str]) -> tuple[list[dict], int]:
    """Per-student rows for a list of group_ids.

    Returns (rows, max_quals) — max_quals is the widest qual count across all rows,
    so the CSV writer can pad columns uniformly.
    """
    if not group_ids:
        return [], 0
    qmarks = ",".join("?" for _ in group_ids)
    with get_db() as c:
        rs = c.execute(f"""
            SELECT contact_id, first_name, last_name, email, phone,
                   stream, qualification, location, timetable, start_date, group_id
            FROM students
            WHERE group_id IN ({qmarks}) AND is_dropoff = 0
            ORDER BY stream, location, start_date, last_name, first_name
        """, group_ids).fetchall()
    rows: list[dict] = []
    max_quals = 0
    for r in rs:
        d = dict(r)
        d["name"] = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip()
        d["quals"] = _split_quals(d["qualification"])
        if len(d["quals"]) > max_quals:
            max_quals = len(d["quals"])
        rows.append(d)
    return rows, max(max_quals, 1)


def class_groups(period: str) -> list[dict]:
    with get_db() as c:
        rs = c.execute("""
            SELECT group_id, stream, location, start_date, timetable,
                   COUNT(*) AS students,
                   COALESCE(SUM(price),0) AS expected,
                   COALESCE(SUM(spent),0) AS collected
            FROM students
            WHERE class_period = ? AND is_dropoff = 0
            GROUP BY group_id ORDER BY start_date, stream, location
        """, (period,)).fetchall()
    out = []
    for r in rs:
        d = _row_with_pct(r)
        d["label"] = friendly_group_label(r["stream"], r["location"], r["start_date"], r["timetable"])
        out.append(d)
    return out

def chase_list(period: str | None = None) -> list[dict]:
    """Outstanding-balance invoices, deduped to one row per contact.
    Multiple failed-retry attempts on the same student show as a single row
    with summed balance and max attempts. Most recent invoice id is kept so
    the 'Open in ONtraport' link points at the latest."""
    sql = """
        SELECT i.contact_id,
               COUNT(*)               AS invoice_count,
               SUM(i.balance)         AS balance,
               MAX(i.recharge_attempts) AS recharge_attempts,
               MAX(i.last_recharge_date) AS last_recharge_date,
               MAX(i.id)              AS invoice_id,
               -- if any invoice is in collections, show that; else declined
               MIN(i.status_code)     AS status_code,
               s.first_name, s.last_name, s.email, s.phone,
               s.stream, s.location, s.start_date, s.group_id,
               s.revenue_period, s.payment_plan
        FROM invoices i
        LEFT JOIN students s ON s.contact_id = i.contact_id
        WHERE i.status_code IN (0, 5) AND i.balance > 0
    """
    args = []
    if period:
        sql += " AND (s.revenue_period = ? OR s.revenue_period IS NULL)"
        args.append(period)
    sql += " GROUP BY i.contact_id ORDER BY MAX(i.last_recharge_date) DESC, SUM(i.balance) DESC"
    with get_db() as c:
        rs = c.execute(sql, args).fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["status"] = "Collections" if d["status_code"] == 0 else "Declined"
        d["name"] = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        d["ontraport_invoice_url"]  = f"https://app.ontraport.com/#!/invoice/edit&id={d['invoice_id']}"
        d["ontraport_contact_url"]  = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        digits = "".join(c for c in (d.get("phone") or "") if c.isdigit())
        first = (d.get("first_name") or "").strip() or "there"
        msg = (
            f"Hi {first}, this is Adam from Image Fitness Training. "
            f"Quick note — there's an outstanding payment of €{d['balance']:.0f} on your course "
            f"({d['invoice_count']} attempt{'s' if d['invoice_count']!=1 else ''} not gone through). "
            f"You can settle it here: [paste invoice link]. Let me know if any issues. — Adam"
        )
        d["whatsapp_url"] = f"https://wa.me/{digits}?text=" + _urlencode(msg) if digits else ""
        out.append(d)
    return out

def admin_work(period: str | None = None) -> list[dict]:
    """Surface students with missing/broken data so they can be tidied up
    in ONtraport. One row per (contact × stream × issue type)."""
    sql = """
        SELECT contact_id, first_name, last_name, email, phone,
               stream, qualification, location, start_date, timetable,
               price, spent, payment_plan, group_id, revenue_period
        FROM students
        WHERE is_dropoff = 0
    """
    args = []
    if period:
        sql += " AND (revenue_period = ? OR revenue_period IS NULL OR revenue_period = '')"
        args.append(period)
    with get_db() as c:
        rs = c.execute(sql, args).fetchall()

    issues = []
    for r in rs:
        d = dict(r)
        name = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        url = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        ctx = {"contact_id": d["contact_id"], "name": name,
               "stream": d["stream"], "url": url, "group_id": d["group_id"],
               "spent": d["spent"] or 0, "price": d["price"] or 0}

        # price = 0 but spent / qual set → real student missing price
        if (d["spent"] or 0) > 0 and (d["price"] or 0) == 0:
            issues.append({**ctx, "task": "Set course price",
                "detail": f"Paid €{d['spent']:.0f} but price field is empty"})
        elif (d["qualification"] or "").strip() and (d["price"] or 0) == 0:
            issues.append({**ctx, "task": "Set course price",
                "detail": f"Qualification '{d['qualification']}' but no price"})

        # missing payment plan with non-zero balance
        if (d["price"] or 0) > (d["spent"] or 0) + 0.01 and not (d["payment_plan"] or "").strip():
            issues.append({**ctx, "task": "Set up payment plan",
                "detail": f"Outstanding €{(d['price'] or 0) - (d['spent'] or 0):.0f}, no plan recorded"})

        # missing qualification (= course name)
        if not (d["qualification"] or "").strip():
            issues.append({**ctx, "task": "Set course / qualification",
                "detail": f"{d['stream']} student without a qualification value"})

        # missing start date
        if not (d["start_date"] or "").strip() and d["stream"] in ("PT", "Pilates"):
            issues.append({**ctx, "task": "Set start date",
                "detail": f"{d['stream']} enrolment without a start date"})

        # missing email
        if not (d["email"] or "").strip():
            issues.append({**ctx, "task": "Add email address",
                "detail": "No email on file — can't send invoice / reminders"})

        # missing phone
        if not (d["phone"] or "").strip():
            issues.append({**ctx, "task": "Add phone number",
                "detail": "No phone on file — WhatsApp follow-ups blocked"})

    # Dedup identical (contact, task) pairs (PT + Pilates dual enrol students
    # often share the same missing field).
    seen = set()
    deduped = []
    for it in issues:
        key = (it["contact_id"], it["task"])
        if key in seen: continue
        seen.add(key)
        deduped.append(it)

    # Apply dismissals — hide items dismissed > 30 seconds ago, keep recent ones
    # so the row can show its undo button.
    from datetime import datetime, timedelta
    with get_db() as c:
        rows = c.execute("SELECT contact_id, task, dismissed_at FROM admin_dismissals").fetchall()
    dismissals = {(r["contact_id"], r["task"]): r["dismissed_at"] for r in rows}
    now = datetime.now()
    out = []
    for it in deduped:
        d = dismissals.get((it["contact_id"], it["task"]))
        it["dismissed_at"] = d
        if d:
            try:
                age = (now - datetime.fromisoformat(d)).total_seconds()
            except ValueError: age = 99
            if age <= 30:
                it["undo_seconds_left"] = max(0, int(30 - age))
                it["dismissed"] = True
                out.append(it)
            # else: hide permanently
        else:
            it["dismissed"] = False
            out.append(it)
    out.sort(key=lambda x: (x["dismissed"], x["task"], x["name"]))
    return out

def admin_work_summary(period: str | None = None) -> dict:
    rows = admin_work(period)
    by_task = {}
    for r in rows:
        by_task[r["task"]] = by_task.get(r["task"], 0) + 1
    return {"count": len(rows), "by_task": by_task}

def chase_for_group(group_id: str) -> list[dict]:
    """Chase items for a class group, deduped to one row per contact."""
    sql = """
        SELECT i.contact_id,
               COUNT(*)               AS invoice_count,
               SUM(i.balance)         AS balance,
               MAX(i.recharge_attempts) AS recharge_attempts,
               MAX(i.last_recharge_date) AS last_recharge_date,
               MAX(i.id)              AS invoice_id,
               MIN(i.status_code)     AS status_code,
               s.first_name, s.last_name, s.phone, s.stream, s.location
        FROM invoices i
        JOIN students s ON s.contact_id = i.contact_id
        WHERE s.group_id = ? AND i.status_code IN (0, 5) AND i.balance > 0
        GROUP BY i.contact_id
        ORDER BY MAX(i.last_recharge_date) DESC, SUM(i.balance) DESC
    """
    with get_db() as c:
        rs = c.execute(sql, (group_id,)).fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["status"] = "Collections" if d["status_code"] == 0 else "Declined"
        d["name"] = f"{d.get('first_name','') or ''} {d.get('last_name','') or ''}".strip() or f"Contact {d['contact_id']}"
        d["ontraport_invoice_url"] = f"https://app.ontraport.com/#!/invoice/edit&id={d['invoice_id']}"
        d["ontraport_contact_url"] = f"https://app.ontraport.com/#!/contact/edit&id={d['contact_id']}"
        digits = "".join(ch for ch in (d.get("phone") or "") if ch.isdigit())
        d["whatsapp_url"] = f"https://wa.me/{digits}" if digits else ""
        out.append(d)
    return out

def chase_summary(period: str | None = None) -> dict:
    rows = chase_list(period)
    return {
        "count": len(rows),
        "total_balance": sum(r["balance"] for r in rows),
        "by_status": {
            "Collections": sum(1 for r in rows if r["status_code"] == 0),
            "Declined":    sum(1 for r in rows if r["status_code"] == 5),
        },
    }

def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")

def periods_with_data() -> list[str]:
    with get_db() as c:
        rs = c.execute("""
            SELECT DISTINCT revenue_period AS p FROM students
            WHERE revenue_period <> ''
            ORDER BY substr(p,2)||substr(p,1,1)
        """).fetchall()
    return [r["p"] for r in rs]

def _row_with_pct(r) -> dict:
    d = dict(r)
    expected = d.get("expected") or 0
    collected = d.get("collected") or 0
    d["outstanding"] = expected - collected
    d["pct"] = (collected / expected * 100) if expected else 0.0
    return d
