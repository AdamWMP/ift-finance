"""Pulls the IFT Sales Board via an Apps Script web app.

Why Apps Script: the Workspace org policy blocks service-account key creation,
so we can't use gspread directly. The Apps Script runs as adam@imageft.ie and
exposes the sheet over a simple authenticated HTTPS endpoint.

Required env vars (set in ~/.zshrc):
    IFT_SALES_BOARD_URL    https://script.google.com/macros/s/.../exec
    IFT_SALES_BOARD_TOKEN  matches SECRET in sales_board.gs
"""
from __future__ import annotations
import os
from datetime import date

import requests

from .db import get_db, parse_date

URL   = os.environ.get("IFT_SALES_BOARD_URL", "")
TOKEN = os.environ.get("IFT_SALES_BOARD_TOKEN", "")
CATEGORIES = ("Reformer", "FBA", "Nutricert", "PPN", "S&C")

def _fetch(period: str) -> list[dict]:
    if not URL or not TOKEN:
        raise RuntimeError(
            "IFT_SALES_BOARD_URL / IFT_SALES_BOARD_TOKEN not set. "
            "See v2/SETUP_SHEETS.md."
        )
    r = requests.get(URL, params={"token": TOKEN, "period": period}, timeout=20,
                     allow_redirects=True)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"sales-board: {data['error']} (period={period})")
    return data.get("rows", [])

def pull_term_tab(period: str = "S26") -> int:
    rows = _fetch(period)
    if not rows:
        print(f"sales board: {period} tab empty"); return 0

    inserted = 0
    with get_db() as c:
        c.execute("DELETE FROM transactions WHERE source='sales_board' AND period=?", (period,))
        for r in rows:
            cat   = _resolve_category(r)
            amt   = _resolve_amount(r)
            d_iso = _resolve_date(r) or date.today().isoformat()
            cid   = _resolve_contact_id(r)
            note  = _resolve_note(r)
            if not cat or not amt:
                continue
            c.execute("""
                INSERT INTO transactions
                  (date, period, direction, category, subcategory, amount,
                   contact_id, source, status, note)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (d_iso, period, "in", cat.lower(), cat, amt,
                  cid, "sales_board", "paid", note))
            inserted += 1
    print(f"sales board: ingested {inserted} {period} rows")
    return inserted

# ---- header resolvers ------------------------------------------------------
def _first(row: dict, *names: str) -> str:
    for n in names:
        for k in row:
            if k and str(k).strip().lower() == n.lower():
                v = row[k]
                if v is None: continue
                s = str(v).strip()
                if s: return s
    return ""

def _resolve_category(row) -> str:
    cat = _first(row, "Category", "Course", "Product", "Type")
    if cat:
        for c in CATEGORIES:
            if c.lower() in cat.lower(): return c
    for c in CATEGORIES:
        if _to_float(_first(row, c)): return c
    return ""

def _resolve_amount(row) -> float:
    for n in ("Amount", "Amount Received", "Paid", "Total", "€"):
        s = _first(row, n)
        if s: return _to_float(s)
    for c in CATEGORIES:
        v = _to_float(_first(row, c))
        if v: return v
    return 0.0

def _resolve_date(row) -> str | None:
    for n in ("Date", "Payment Date", "Date Received", "When"):
        s = _first(row, n)
        if s:
            d = parse_date(s)
            if d: return d.isoformat()
    return None

def _resolve_contact_id(row) -> str:
    return _first(row, "Contact ID", "ContactID", "ONtraport ID", "Student ID", "ID")

def _resolve_note(row) -> str:
    n = _first(row, "Name", "Student", "Customer")
    note = _first(row, "Note", "Notes", "Description")
    return " · ".join(x for x in (n, note) if x)[:240]

def _to_float(v) -> float:
    if not v: return 0.0
    s = str(v).replace("€","").replace(",","").replace(" ","").strip()
    try: return float(s)
    except ValueError: return 0.0

if __name__ == "__main__":
    pull_term_tab("S26")
