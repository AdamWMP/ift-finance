"""Pull aggregate revenue cells from the IFT Sales Board via Apps Script.

Cell coordinates locked by the user (A1 notation). Each term tab follows the
same layout, so a new period (e.g. A26) just needs the cell positions
re-verified once.
"""
from __future__ import annotations
import os
from datetime import date

import requests
from .db import SALE_VALUE, get_db
from . import queries

URL   = os.environ.get("IFT_SALES_BOARD_URL", "")
TOKEN = os.environ.get("IFT_SALES_BOARD_TOKEN", "")

# A1 cell → (category, label). All amounts are in €.
CELLS = {
    "B20": ("sc_dublin",            "S&C Dublin"),
    "C20": ("online_pilates",       "Online Pilates"),
    "D20": ("pre_post_natal",       "Pre & Post Natal"),
    "F20": ("tesg_grants",          "TESG Grant Approvals"),
    "H19": ("iftg_global",          "IFTG Sales (Online Sales)"),
    "I19": ("nutricert",            "NutriCert Global"),
    "J19": ("advanced_programming", "Advanced Programming"),
    "H22": ("reformer",             "Reformer Pilates"),
    "I22": ("fba",                  "FBA"),
    "J22": ("brand_launch",         "Brand Launch"),
    "K22": ("ai_coaches",           "AI for Coaches"),
}

def a1_to_rc(a1: str) -> tuple[int, int]:
    """B20 → (19, 1) zero-indexed."""
    a1 = a1.strip().upper()
    i = 0
    while i < len(a1) and a1[i].isalpha(): i += 1
    col_letters, row_str = a1[:i], a1[i:]
    col = 0
    for ch in col_letters:
        col = col * 26 + (ord(ch) - ord('A') + 1)
    return int(row_str) - 1, col - 1

def _fetch_grid(period: str) -> list[list]:
    if not URL or not TOKEN:
        raise RuntimeError("IFT_SALES_BOARD_URL / IFT_SALES_BOARD_TOKEN missing")
    r = requests.get(URL, params={"token": TOKEN, "period": period}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"sales_board: {data['error']}")
    return data.get("grid", [])

def _to_float(v) -> float:
    if v is None or v == "": return 0.0
    s = str(v).replace("€","").replace(",","").strip()
    if s in ("#REF!", "#N/A", "#VALUE!", "PENDING", "COMP", "TBC"): return 0.0
    try: return float(s)
    except ValueError: return 0.0

def extract(period: str = "S26") -> list[dict]:
    grid = _fetch_grid(period)
    out = []
    for a1, (cat, label) in CELLS.items():
        r, c = a1_to_rc(a1)
        raw = grid[r][c] if r < len(grid) and c < len(grid[r]) else None
        out.append({"cell": a1, "category": cat, "label": label,
                    "raw": raw, "amount": _to_float(raw)})
    return out

def write_transactions(period: str = "S26") -> int:
    items = extract(period)
    today = date.today().isoformat()
    n = 0
    with get_db() as c:
        c.execute("DELETE FROM transactions WHERE source='sales_board' AND period=?", (period,))
        for it in items:
            if it["amount"] <= 0: continue
            c.execute("""
                INSERT INTO transactions
                  (date, period, direction, category, subcategory, amount,
                   contact_id, source, status, note)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (today, period, "in", it["category"], it["label"],
                  it["amount"], "", "sales_board", "paid",
                  f"sales_board {period} {it['cell']}"))
            n += 1
    return n

def push_live_money_in(period: str = "S26", cell: str = "L18") -> dict:
    """Compute total collected for the period (raw_students + sales_board
    transactions), divide by SALE_VALUE, and write the result to the Sales
    Board cell."""
    macro = queries.macro(period)
    extra = sum(it["amount"] for it in extract(period) if it["amount"] > 0)
    total_collected = (macro["collected"] or 0) + extra
    sales_eq = round(total_collected / SALE_VALUE, 4)
    if not URL or not TOKEN:
        raise RuntimeError("IFT_SALES_BOARD_URL / IFT_SALES_BOARD_TOKEN missing")
    r = requests.post(URL, json={
        "token": TOKEN, "period": period, "cell": cell, "value": sales_eq,
    }, timeout=30)
    r.raise_for_status()
    out = r.json()
    out["_input"] = {"collected": total_collected, "sale_value": SALE_VALUE,
                     "sales_eq": sales_eq, "cell": cell}
    return out

if __name__ == "__main__":
    items = extract("S26")
    print(f"\n{len(items)} cells:")
    for it in items:
        print(f"  {it['cell']:<4} {it['label']:<28} €{it['amount']:>10,.2f}   raw={it['raw']!r}")
    n = write_transactions("S26")
    print(f"\nwrote {n} non-zero rows to transactions")
    print("\npushing live money in to sales board L18 …")
    print(push_live_money_in("S26", "L18"))
