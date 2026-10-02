"""Build v2/app/a25_seed.json from the A25 Finance Report workbook.

Reads the Google-Sheets export (File → Download → .xlsx) and emits a seed
that /admin/import-a25 loads. Everything the DASHBOARD tab derives is
rebuilt from the per-student rows so the import reproduces the sheet's
headline to the cent — the build asserts this and refuses to write the
seed if any cohort tab's Σ(Fees Due / Paid) drifts from its R2/R3 cells.

Usage:
    cd v2
    python -m scripts.build_a25_seed "../A25 Finance Report.xlsx"

What's captured
    students         every row of the 7 PT cohort tabs + Pilates, with
                     location / timetable / start date so cohorts split up
                     the same way S26 / A26 do
    is_deferral      the 14 rows on 'A25 Deferals ' — their money belongs to
                     the origin term (parsed from the '<term> … Sale' tag);
                     revenue_period is left blank when no origin tag exists
                     so the normal invoice-based backfill can fill it
    is_dropoff       the DASHBOARD drop-off block (D37:I75)
    method_overrides CASH PAYMENTS / BANK TRANSFER / Skillnet / Revolut tabs
    sales_board      the other-streams cells the DASHBOARD pulls by
                     IMPORTRANGE (Reformer / FBA / NutriCert / PPN / S&C /
                     sponsored / grant)
    snapshots        the 'What & When Has Money Come In' monthly points

Sheet quirks handled
    • B4 'TOTAL FEES OWED' double-adds NutriCert+PPN+S&C+grant; B6 'COMBO
      FEES DUE' points at Wexford!R3. Adam opted for the corrected figures
      (2 Oct 2026) — the seed carries due/paid only and owed is derived.
    • Two deferral rows have a date in the Location column; the cohort-tab
      row is authoritative for location, the deferral tab only for the flag.
"""
from __future__ import annotations
import json, re, sys
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is required — `pip install openpyxl` in your venv first")

PT_COHORT_SHEETS = ["Swords", "Tallaght", "Cork", "Galway", "Limerick", "Wexford", "Belfast  Online"]
PILATES_SHEET    = "Pilates"
DEFERRAL_SHEET   = "A25 Deferals "          # sic — trailing space in the workbook
DASHBOARD_SHEET  = "DASHBOARD"
METHOD_SHEETS    = {"CASH PAYMENTS": "Cash", "BANK TRANSFER": "Bank Transfer",
                    "Skillnet": "Skillnet", "Revolut": "Revolut"}
# DASHBOARD cells → sales_board category (keys match db.CATEGORY_STREAM)
SB_CELLS = {
    "H27": ("reformer",       "Reformer Pilates"),
    "H28": ("fba",            "FBA"),
    "H29": ("nutricert",      "NutriCert"),
    "H30": ("pre_post_natal", "PPN"),
    "H31": ("sc_dublin",      "S&C"),
    "H18": ("sponsored",      "Sponsored (LIFE / K67 Pillo)"),
    "H32": ("tesg_grants",    "Grant paid"),      # excluded from headline, shown separately
}
SNAPSHOT_YEAR = 2026   # A25 ran Sep 2025 → Jul 2026; the monthly points are Jan–Jul 2026


def _num(v) -> float:
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0

def _iso(v) -> str:
    if v in (None, ""): return ""
    if isinstance(v, datetime): return v.date().isoformat()
    s = str(v).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try: return datetime.strptime(s, fmt).date().isoformat()
        except ValueError: pass
    return ""

def _phone(v) -> str:
    if v in (None, ""): return ""
    s = str(v).strip()
    if s.endswith(".0"): s = s[:-2]
    return "".join(ch for ch in s if ch.isdigit())

def _cid(v) -> str:
    if isinstance(v, float) and v.is_integer(): return str(int(v))
    return str(v).strip()

def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts: return "", ""
    if len(parts) == 1: return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]

_TERM = re.compile(r"\b([SA]\d{2})\b")
def _origin_from_tags(tags: str, exclude: str) -> str:
    """Latest '<term> … Sale' tag that isn't the seeded term."""
    found = []
    for t in (tags or "").split(","):
        t = t.strip()
        if "Sale" not in t or "Defer" in t: continue
        m = _TERM.search(t)
        if m and m.group(1) != exclude:
            yy = int(m.group(1)[1:]); found.append(((yy, 0 if m.group(1)[0] == "S" else 1), m.group(1)))
    return max(found)[1] if found else ""


def build(xlsx: Path) -> dict:
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    period = "A25"
    students: dict[str, dict] = {}
    tab_check = []

    def take(ws, tab, stream, cols):
        """cols: dict of field → 1-based column index for this tab layout."""
        due = paid = 0.0; n = 0
        for r in range(2, ws.max_row + 1):
            raw = ws.cell(r, 1).value
            if raw in (None, ""): continue
            cid = _cid(raw)
            price = _num(ws.cell(r, cols["price"]).value)
            spent = _num(ws.cell(r, cols["paid"]).value)
            first, last = _split_name(ws.cell(r, 2).value)
            row = dict(
                contact_id=cid, first_name=first, last_name=last,
                email=(ws.cell(r, 3).value or "").strip(), phone=_phone(ws.cell(r, 4).value),
                stream=stream,
                qualification=(ws.cell(r, cols["qual"]).value or "").strip() if cols.get("qual") else "",
                location=(ws.cell(r, cols["loc"]).value or "").strip(),
                timetable=(ws.cell(r, cols["tt"]).value or "").strip() if cols.get("tt") else "",
                start_date=_iso(ws.cell(r, cols["start"]).value),
                price=price, spent=spent,
                payment_plan=str(ws.cell(r, cols["plan"]).value or "").strip(),
                payment_method=(ws.cell(r, cols["method"]).value or "").strip(),
                tags=(ws.cell(r, cols["tags"]).value or "").strip(),
                is_deferral=0, revenue_period=period, is_dropoff=0, cohort_tab=tab,
            )
            key = f"{cid}|{stream}"
            if key in students:
                print(f"  ! duplicate {key} ({tab}) — keeping first", file=sys.stderr)
                continue
            students[key] = row; due += price; paid += spent; n += 1
        r2, r3 = _num(ws["R2"].value), _num(ws["R3"].value)
        ok = abs(due - r2) < 0.01 and abs(paid - r3) < 0.01
        tab_check.append((tab, n, due, paid, r2, r3, ok))
        if not ok:
            sys.exit(f"ABORT: {tab} Σrows due/paid {due:,.2f}/{paid:,.2f} ≠ R2/R3 {r2:,.2f}/{r3:,.2f}")

    pt_cols = dict(qual=5, loc=6, tt=7, start=8, price=9, paid=10, plan=12, method=14, tags=15)
    for tab in PT_COHORT_SHEETS:
        take(wb[tab], tab, "PT", pt_cols)
    take(wb[PILATES_SHEET], PILATES_SHEET, "Pilates",
         dict(loc=5, start=6, price=7, paid=8, plan=10, method=11, tags=12))

    # Deferrals — flag + origin term; money + location come from the cohort row.
    ws = wb[DEFERRAL_SHEET]; deferrals = []
    for r in range(2, ws.max_row + 1):
        raw = ws.cell(r, 1).value
        if raw in (None, ""): continue
        cid = _cid(raw); tags = (ws.cell(r, 15).value or "")
        origin = _origin_from_tags(tags, period)
        hit = [k for k in students if k.startswith(cid + "|")]
        for k in hit:
            s = students[k]
            s["is_deferral"] = 1
            s["revenue_period"] = origin           # '' → backfill_deferral_periods fills from invoices
            # cohort row is zeroed in the sheet; carry the true money from the deferral tab
            s["price"] = _num(ws.cell(r, 9).value) or s["price"]
            s["spent"] = _num(ws.cell(r, 10).value)
        deferrals.append(dict(contact_id=cid, name=ws.cell(r, 2).value, origin=origin,
                              in_cohort=bool(hit)))

    # Drop-offs — DASHBOARD block D37:I75
    D = wb[DASHBOARD_SHEET]; drops = []
    for r in range(37, 80):
        raw = D.cell(r, 4).value
        if raw in (None, ""): continue
        cid = _cid(raw)
        hit = [k for k in students if k.startswith(cid + "|")]
        for k in hit: students[k]["is_dropoff"] = 1
        drops.append(dict(contact_id=cid, name=D.cell(r, 5).value,
                          potential=_num(D.cell(r, 7).value), spent=_num(D.cell(r, 8).value),
                          lost=_num(D.cell(r, 9).value), in_cohort=bool(hit)))

    # Method overrides
    overrides = []
    for sheet, method in METHOD_SHEETS.items():
        ws = wb[sheet]
        for r in range(2, ws.max_row + 1):
            raw = ws.cell(r, 1).value
            if raw in (None, ""): continue
            amt = _num(ws.cell(r, 10).value)
            if amt <= 0: continue
            overrides.append(dict(contact_id=_cid(raw), amount=amt, method=method,
                                  date="2025-09-01", note=f"Imported from A25 Finance Report · {method}"))

    # Other streams (cached IMPORTRANGE values on the DASHBOARD)
    sales_board = [dict(category=cat, label=label, amount=_num(D[cell].value))
                   for cell, (cat, label) in SB_CELLS.items() if _num(D[cell].value) > 0]
    grant_potential = _num(D["F32"].value)

    # Monthly snapshots — I11:K17, labels like '29th January'
    snaps = []
    for r in range(11, 18):
        lab = D.cell(r, 9).value
        if not lab: continue
        m = re.match(r"(\d+)\w*\s+(\w+)", str(lab))
        if not m: continue
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {SNAPSHOT_YEAR}", "%d %B %Y").date()
        except ValueError: continue
        sales = _num(re.sub(r"[^\d.]", "", str(D.cell(r, 10).value or "")))
        snaps.append(dict(date=d.isoformat(), sales=int(round(sales)), revenue=_num(D.cell(r, 11).value)))

    rows = sorted(students.values(), key=lambda s: (s["stream"], s["location"], s["start_date"], s["last_name"]))
    sb_headline = sum(x["amount"] for x in sales_board if x["category"] != "tesg_grants")
    due_all  = sum(s["price"] for s in rows if not s["is_deferral"])
    paid_all = sum(s["spent"] for s in rows if not s["is_deferral"])
    drop_pot = sum(s["price"] for s in rows if s["is_dropoff"] and not s["is_deferral"])
    stats = dict(
        student_count=len(rows), deferral_count=sum(s["is_deferral"] for s in rows),
        dropoff_count=sum(s["is_dropoff"] for s in rows),
        method_override_count=len(overrides),
        sheet_total_due=round(_num(D["B2"].value), 2), sheet_total_paid=round(_num(D["B3"].value), 2),
        rebuilt_total_due=round(due_all + sb_headline, 2), rebuilt_total_paid=round(paid_all + sb_headline, 2),
        rebuilt_owed=round(due_all - paid_all, 2),
        rebuilt_due_excl_dropoffs=round(due_all - drop_pot + sb_headline, 2),
        dropoff_potential=round(drop_pot, 2), dropoff_lost=round(sum(x["lost"] for x in drops), 2),
        grant_potential=grant_potential,
        tab_check=[dict(tab=t, n=n, due=d, paid=p, ok=ok) for t, n, d, p, _, _, ok in tab_check],
    )
    return dict(period=period, source=str(xlsx.name), built_at=datetime.now().isoformat(timespec="seconds"),
                students=rows, method_overrides=overrides, sales_board=sales_board,
                snapshots=snaps, deferrals=deferrals, drop_offs=drops, stats=stats)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    xlsx = Path(sys.argv[1]).expanduser()
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "app" / "a25_seed.json"
    seed = build(xlsx)
    st = seed["stats"]
    for t in st["tab_check"]:
        print(f"  {t['tab']:16s} n={t['n']:3d} due={t['due']:>11,.2f} paid={t['paid']:>11,.2f} {'✓' if t['ok'] else '✗'}")
    print(f"  students={st['student_count']} deferrals={st['deferral_count']} dropoffs={st['dropoff_count']} overrides={st['method_override_count']}")
    print(f"  TOTAL DUE  sheet {st['sheet_total_due']:,.2f}  rebuilt {st['rebuilt_total_due']:,.2f}  {'✓' if abs(st['sheet_total_due']-st['rebuilt_total_due'])<0.01 else '✗'}")
    print(f"  TOTAL PAID sheet {st['sheet_total_paid']:,.2f}  rebuilt {st['rebuilt_total_paid']:,.2f}  {'✓' if abs(st['sheet_total_paid']-st['rebuilt_total_paid'])<0.01 else '✗'}")
    print(f"  owed (due−paid) {st['rebuilt_owed']:,.2f} · headline excl. drop-offs {st['rebuilt_due_excl_dropoffs']:,.2f} (drop-off potential {st['dropoff_potential']:,.2f})")
    if abs(st['sheet_total_due']-st['rebuilt_total_due']) >= 0.01 or abs(st['sheet_total_paid']-st['rebuilt_total_paid']) >= 0.01:
        sys.exit("ABORT: headline does not reproduce — seed not written")
    out.write_text(json.dumps(seed, indent=1, ensure_ascii=False))
    print(f"  → wrote {out} ({out.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
