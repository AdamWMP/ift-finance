"""IFT Finance — entry point.
Run locally:
    cd v2/app
    pip install -r requirements.txt
    python -m app.ingest                    # one-time seed
    uvicorn app.main:app --reload
"""
from __future__ import annotations
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from threading import Thread

from fastapi import BackgroundTasks

from . import queries
from .auth import COOKIE, MAX_AGE, PASSPHRASE, is_authed, make_token
from .db import init_db, get_meta, set_meta

HERE = Path(__file__).resolve().parent
init_db()  # ensure all tables exist before serving traffic
app = FastAPI(title="IFT Finance")

@app.on_event("startup")
def _start_scheduler():
    from .scheduler import start_in_background
    start_in_background()
app.mount("/static", StaticFiles(directory=HERE/"static"), name="static")
templates = Jinja2Templates(directory=HERE/"templates")

PUBLIC_PATHS = {"/login", "/static", "/health"}

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p+"/") for p in PUBLIC_PATHS):
        return await call_next(request)
    if not is_authed(request):
        return RedirectResponse(url=f"/login?next={path}", status_code=302)
    return await call_next(request)

@app.get("/health")
def health(): return {"ok": True}

@app.get("/api/search")
def api_search(q: str = ""):
    return {"results": queries.search(q)}

@app.get("/api/today")
def api_today(period: str = "S26"):
    return queries.today_panel(period)

@app.post("/api/notes")
def api_notes_set(contact_id: str = Form(...), stream: str = Form(""), body: str = Form("")):
    queries.set_note(contact_id, stream, body)
    return {"ok": True}

@app.get("/api/notes")
def api_notes_get(contact_id: str, stream: str = ""):
    return {"body": queries.get_note(contact_id, stream)}

@app.get("/export.csv")
def export_csv(table: str, period: str = "S26", group_id: str | None = None,
               pathway: str | None = None):
    """CSV export. table = chase | admin_work | group | pathway | board_groups."""
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    if table == "chase":
        w.writerow(["name","contact_id","stream","location","status","balance","invoice_count","last_try","attempts"])
        for r in queries.chase_list(period):
            w.writerow([r["name"], r["contact_id"], r["stream"], r["location"], r["status"],
                        r["balance"], r["invoice_count"], r["last_recharge_date"], r["recharge_attempts"]])
    elif table == "admin_work":
        w.writerow(["task","name","contact_id","stream","detail","group_id"])
        for r in queries.admin_work(period):
            if r.get("dismissed"): continue
            w.writerow([r["task"], r["name"], r["contact_id"], r["stream"], r["detail"], r["group_id"]])
    elif table == "group" and group_id:
        g = queries.group_detail(group_id)
        if not g: return RedirectResponse("/board", status_code=302)
        w.writerow(["name","email","qualification","price","spent","outstanding","pct","status","payment_status","plan"])
        for s in g["students"]:
            w.writerow([s["name"], s["email"], s["qualification"], s["price"], s["spent"],
                        s["outstanding"], s["pct"], s["status"], s["payment_status"], s.get("payment_plan","")])
    elif table == "pathway" and pathway:
        p = queries.pathway_detail(pathway, period)
        w.writerow(["name","email","group","price","spent","pct","status","payment_status"])
        for s in p["students"]:
            w.writerow([s["name"], s["email"], s["group_label"], s["price"], s["spent"],
                        s["pct"], s["status"], s["payment_status"]])
    elif table == "board_groups":
        w.writerow(["group","start_date","students","collected","expected","pct"])
        for g in queries.class_groups(period):
            w.writerow([g["label"], g["start_date"], g["students"], g["collected"], g["expected"], g["pct"]])
    else:
        return RedirectResponse("/board", status_code=302)
    csv_bytes = buf.getvalue().encode("utf-8-sig")
    fname = f"ift-{table}-{period}.csv"
    from fastapi.responses import Response
    return Response(content=csv_bytes, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})

@app.get("/_meta/sync")
def meta_sync():
    val, _ = get_meta("last_sync_at")
    running, _ = get_meta("sync_running")
    return {"last_sync_at": val, "running": running == "1"}

def _run_sync_threaded():
    set_meta("sync_running", "1")
    try:
        from .sync import main as sync_main
        sync_main("S26")
    except Exception:
        import traceback; traceback.print_exc()
    finally:
        set_meta("sync_running", "0")

@app.post("/admin/sync")
def admin_sync_now(request: Request):
    val, _ = get_meta("sync_running")
    if val != "1":
        Thread(target=_run_sync_threaded, daemon=True).start()
    referer = request.headers.get("referer") or "/board"
    return RedirectResponse(url=referer, status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/board", error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": error})

@app.post("/login")
def login_submit(request: Request, passphrase: str = Form(...), next: str = Form("/board")):
    if passphrase != PASSPHRASE:
        return RedirectResponse(url=f"/login?next={next}&error=1", status_code=302)
    resp = RedirectResponse(url=next or "/board", status_code=302)
    resp.set_cookie(COOKIE, make_token(), max_age=MAX_AGE, httponly=True, samesite="lax")
    return resp

@app.get("/", include_in_schema=False)
def root(): return RedirectResponse(url="/board")

@app.get("/pathway", response_class=HTMLResponse)
def pathway_view(request: Request, name: str, period: str = "S26"):
    return templates.TemplateResponse("pathway.html", {
        "request": request,
        "p": queries.pathway_detail(name, period),
    })

@app.get("/method", response_class=HTMLResponse)
def method_view(request: Request, name: str, period: str = "S26"):
    return templates.TemplateResponse("filter.html", {
        "request": request,
        "f": queries.students_filtered(period, payment_method=name),
    })

@app.get("/location", response_class=HTMLResponse)
def location_view(request: Request, name: str, period: str = "S26"):
    return templates.TemplateResponse("filter.html", {
        "request": request,
        "f": queries.students_filtered(period, location=name),
    })

@app.post("/group/cert-toggle")
def group_cert_toggle(contact_id: str = Form(...), stream: str = Form(...), group_id: str = Form(...)):
    from .db import get_db
    from datetime import datetime
    with get_db() as c:
        cur = c.execute(
            "SELECT COALESCE(cert_issued,0) AS v FROM students WHERE contact_id=? AND stream=?",
            (contact_id, stream)).fetchone()
        new = 0 if (cur and cur["v"]) else 1
        c.execute(
            "UPDATE students SET cert_issued=?, cert_issued_at=? WHERE contact_id=? AND stream=?",
            (new, datetime.now().isoformat(timespec="seconds") if new else None,
             contact_id, stream))
    from urllib.parse import quote
    return RedirectResponse(url=f"/group?id={quote(group_id, safe='')}", status_code=303)

@app.get("/group", response_class=HTMLResponse)
def group(request: Request, id: str):
    g = queries.group_detail(id)
    if not g:
        return HTMLResponse(f"<p>group not found: {id}</p>", status_code=404)
    chase = queries.chase_for_group(id)
    chase_total = sum(c["balance"] for c in chase)
    forecast = queries.cohort_forecast(id)
    notes = queries.all_notes_for_contacts([s["contact_id"] for s in g["students"]])
    for s in g["students"]:
        s["note"] = notes.get(s["contact_id"], "")
    return templates.TemplateResponse("group.html", {
        "request": request, "g": g, "chase": chase, "chase_total": chase_total,
        "forecast": forecast,
    })

@app.get("/admin/transactions", response_class=HTMLResponse)
def admin_transactions(request: Request, period: str = "S26",
                       contact: str | None = None, name: str | None = None):
    split = queries.per_student_payment_split(contact, period) if contact else None
    return templates.TemplateResponse("transactions.html", {
        "request": request, "period": period,
        "available": queries.periods_with_data() or [period],
        "rows": queries.manual_transactions(period),
        "summary": queries.manual_transactions_summary(period),
        "preselect_contact": contact,
        "preselect_name": name,
        "split": split,
    })

@app.post("/admin/transactions/add")
def admin_transactions_add(
    date: str = Form(...), period: str = Form(...), direction: str = Form(...),
    category: str = Form(...), amount: str = Form(...),
    note: str = Form(""), contact_id: str = Form(""),
    subcategory: str = Form(""), status: str = Form("paid"),
):
    from .db import get_db
    try: amt = float(amount.replace(",", "").replace("€", "").strip())
    except ValueError: amt = 0.0
    if amt <= 0:
        return RedirectResponse(url=f"/admin/transactions?period={period}", status_code=303)
    with get_db() as c:
        c.execute("""
            INSERT INTO transactions
              (date, period, direction, category, subcategory, amount,
               contact_id, source, status, note)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (date, period, direction, category, subcategory, amt,
              contact_id, "manual", status, note))
    return RedirectResponse(url=f"/admin/transactions?period={period}", status_code=303)

@app.post("/admin/transactions/delete")
def admin_transactions_delete(id: int = Form(...), period: str = Form("S26")):
    from .db import get_db
    with get_db() as c:
        c.execute("DELETE FROM transactions WHERE id=? AND source='manual'", (id,))
    return RedirectResponse(url=f"/admin/transactions?period={period}", status_code=303)

@app.get("/admin/work", response_class=HTMLResponse)
def admin_work_view(request: Request, period: str = "S26"):
    return templates.TemplateResponse("admin_work.html", {
        "request": request,
        "period": period,
        "available": queries.periods_with_data() or [period],
        "summary": queries.admin_work_summary(period),
        "rows": queries.admin_work(period),
    })

@app.post("/admin/work/dismiss")
def admin_work_dismiss(contact_id: str = Form(...), task: str = Form(...), period: str = Form("S26")):
    from .db import get_db
    from datetime import datetime
    with get_db() as c:
        c.execute(
            "INSERT OR REPLACE INTO admin_dismissals (contact_id, task, dismissed_at) VALUES (?,?,?)",
            (contact_id, task, datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse(url=f"/admin/work?period={period}", status_code=303)

@app.post("/admin/work/undo")
def admin_work_undo(contact_id: str = Form(...), task: str = Form(...), period: str = Form("S26")):
    from .db import get_db
    with get_db() as c:
        c.execute("DELETE FROM admin_dismissals WHERE contact_id=? AND task=?",
                  (contact_id, task))
    return RedirectResponse(url=f"/admin/work?period={period}", status_code=303)

@app.get("/admin/chase", response_class=HTMLResponse)
def admin_chase(request: Request, period: str = "S26"):
    return templates.TemplateResponse("chase.html", {
        "request": request,
        "period": period,
        "available": queries.periods_with_data() or [period],
        "summary": queries.chase_summary(period),
        "rows": queries.chase_list(period),
    })

@app.get("/board", response_class=HTMLResponse)
def board(request: Request, period: str = "S26"):
    available = queries.periods_with_data() or [period]
    return templates.TemplateResponse("board.html", {
        "request": request,
        "period": period,
        "available": available,
        "macro": queries.macro(period),
        "sales": queries.sales_summary(period),
        "streams": queries.by_stream(period),
        "pathways": queries.by_pathway(period),
        "locations": queries.by_location(period),
        "groups": queries.class_groups(period),
        "fees": queries.fees_summary(period),
        "money_in": queries.by_payment_method(period),
        "today": queries.today_panel(period),
        "deposit": queries.avg_deposit(period),
    })
