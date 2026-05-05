"""Daily summary email — sends a digest of admin work + chase items + new sales.
Runs as its own cron entry. Reuses the v1 Gmail SMTP setup.
"""
from __future__ import annotations
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import queries

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("IFT_SMTP_USER", "adam@imageft.ie")
SMTP_PASS = os.environ.get("IFT_SMTP_PASS", "")
TO_EMAIL  = os.environ.get("IFT_DIGEST_TO", "adam@imageft.ie")
DASHBOARD_URL = os.environ.get("IFT_DASHBOARD_URL", "https://finance.imageft.ie")

CSS = """
  body{font:14px/1.45 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:#0f172a;max-width:640px;margin:0 auto;padding:20px}
  h1{font-size:18px;margin:0 0 4px}.muted{color:#64748b}
  h2{font-size:13px;margin:24px 0 8px;text-transform:uppercase;letter-spacing:.6px;color:#10b981}
  table{width:100%;border-collapse:collapse;margin:6px 0 12px}
  td,th{padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:left;font-size:13px}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#64748b}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;background:#f1f5f9;color:#0f172a;margin-right:4px}
  .pill.bad{background:#fee2e2;color:#b91c1c}.pill.warn{background:#fef3c7;color:#92400e}.pill.ok{background:#dcfce7;color:#166534}
  a.btn{display:inline-block;padding:8px 14px;background:#10b981;color:#000;text-decoration:none;border-radius:6px;font-weight:600;margin-top:6px}
  hr{border:0;border-top:1px solid #e2e8f0;margin:18px 0}
"""

def build_digest(period: str = "S26") -> tuple[str, str]:
    """Returns (subject, html_body)."""
    fees     = queries.fees_summary(period)
    sales    = queries.sales_summary(period)
    today    = queries.today_panel(period)
    chase    = queries.chase_summary(period)
    admin    = queries.admin_work_summary(period)

    today_iso = datetime.now().strftime("%-d %b %Y")

    parts = [f"<style>{CSS}</style>",
             f"<h1>IFT Finance · {today_iso}</h1>",
             f"<div class='muted'>Daily digest for {period}</div>",
             f"<a class='btn' href='{DASHBOARD_URL}/board?period={period}'>Open dashboard →</a>"]

    # ── Money pulse
    parts += [
        "<h2>Money pulse</h2>",
        "<table>",
        f"<tr><td>Total fees paid</td><td class='num'><b>€{fees['total']['paid']:,.0f}</b></td></tr>",
        f"<tr><td>Outstanding</td><td class='num'>€{fees['total']['owed']:,.0f}</td></tr>",
        f"<tr><td>Collection rate</td><td class='num'>{(fees['total']['paid']/fees['total']['due']*100 if fees['total']['due'] else 0):.1f}%</td></tr>",
        f"<tr><td>Sales-equivalent</td><td class='num'>{sales['sales']:.1f} of {sales['next_target']} ({sales['to_next_target']:.1f} away)</td></tr>",
        "</table>",
    ]

    # ── Today
    t = today["today"]; w = today["week"]
    parts += [
        "<h2>Today</h2>",
        "<table>",
        f"<tr><td>New sales</td><td class='num'>{t['new_sales']} (€{t['new_sales_amt']:,.0f})</td></tr>",
        f"<tr><td>Payments closed</td><td class='num'>{t['new_payments']} (€{t['new_payments_amt']:,.0f})</td></tr>",
        f"<tr><td>Certs issued</td><td class='num'>{t['certs_issued']}</td></tr>",
        "</table>",
        "<h2>Last 7 days</h2>",
        "<table>",
        f"<tr><td>New sales</td><td class='num'>{w['new_sales']} (€{w['new_sales_amt']:,.0f})</td></tr>",
        f"<tr><td>Money collected</td><td class='num'>€{w['new_payments_amt']:,.0f} ({w['new_payments']} payments)</td></tr>",
        f"<tr><td>Failures</td><td class='num'>{w['failures']}</td></tr>",
        "</table>",
    ]

    # ── Recent new sales
    if today.get("recent_sales"):
        parts.append("<h2>Recent new sales</h2><table><tr><th>Date</th><th>Name</th><th>Stream</th><th>Loc</th><th class='num'>€</th></tr>")
        for s in today["recent_sales"]:
            parts.append(f"<tr><td>{s['date']}</td><td>{s['name']}</td><td>{s['stream']}</td><td>{s['location']}</td><td class='num'>€{s['total']:,.0f}</td></tr>")
        parts.append("</table>")

    # ── Chase + admin
    parts += [
        "<h2>Action queue</h2>",
        f"<p><span class='pill bad'>{chase['count']} chase items</span> · €{chase['total_balance']:,.0f} outstanding · "
        f"<span class='pill warn'>{admin['count']} admin tasks</span></p>",
    ]
    if admin.get("by_task"):
        parts.append("<table>")
        for task, n in admin["by_task"].items():
            parts.append(f"<tr><td>{task}</td><td class='num'>{n}</td></tr>")
        parts.append("</table>")
    parts += [
        f"<a class='btn' href='{DASHBOARD_URL}/admin/work?period={period}'>Admin Work →</a> ",
        f"<a class='btn' href='{DASHBOARD_URL}/admin/chase?period={period}'>Debt Collection →</a>",
    ]

    parts.append("<hr><p class='muted'>Sent automatically by the IFT Finance dashboard.</p>")

    subject = f"IFT Finance · {today_iso} · {sales['sales']:.0f} sales · €{fees['total']['paid']/1000:.0f}k collected"
    return subject, "".join(parts)


def send_digest(period: str = "S26") -> bool:
    if not SMTP_PASS:
        print("⚠️  IFT_SMTP_PASS not set — skipping send")
        subject, html = build_digest(period)
        print("subject:", subject)
        return False
    subject, html = build_digest(period)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText("Open the HTML version of this email.", "plain"))
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"✓ digest sent → {TO_EMAIL}")
    return True


if __name__ == "__main__":
    import sys
    period = sys.argv[1] if len(sys.argv) > 1 else "S26"
    if "--preview" in sys.argv:
        from pathlib import Path
        subject, html = build_digest(period)
        out = Path("/tmp/ift-digest-preview.html")
        out.write_text(html)
        print(f"subject: {subject}")
        print(f"preview: file://{out}")
    else:
        send_digest(period)
