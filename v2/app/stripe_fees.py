"""Pull Stripe balance transactions (gross / fee / net per charge) so the
board can show Simon real money-to-bank rather than gross collected.

Needs STRIPE_API_KEY in the environment — a *restricted* key with only
"Balance transactions: Read" is enough (Stripe Dashboard → Developers →
API keys → Create restricted key). If the key is absent the step is a
no-op and the panel says so. Plain REST via requests; no stripe package.

Idempotent: balance transaction ids are the primary key, so re-pulls just
upsert. Each sync pulls everything created since the newest row we hold
(minus 2 days of overlap), first run pulls from 2025-01-01.
"""
from __future__ import annotations
import os, time
from datetime import datetime, timedelta, timezone
import requests
from .db import get_db

API = "https://api.stripe.com/v1/balance_transactions"


def pull(since: str | None = None) -> dict:
    key = os.environ.get("STRIPE_API_KEY", "").strip()
    if not key:
        print("  STRIPE_API_KEY not set — skipping Stripe fees pull", flush=True)
        return {"ok": False, "reason": "no-key"}
    with get_db() as c:
        newest = c.execute("SELECT MAX(created) FROM stripe_balance_tx").fetchone()[0]
    if since:
        start = datetime.fromisoformat(since)
    elif newest:
        start = datetime.fromisoformat(newest[:19]) - timedelta(days=2)
    else:
        start = datetime(2025, 1, 1)
    params = {"limit": 100, "created[gte]": int(start.replace(tzinfo=timezone.utc).timestamp()),
              "expand[]": "data.source"}
    n = 0; starting_after = None
    while True:
        if starting_after: params["starting_after"] = starting_after
        for attempt in range(5):
            r = requests.get(API, params=params, auth=(key, ""), timeout=30)
            if r.status_code == 429: time.sleep(2 ** attempt); continue
            break
        if r.status_code != 200:
            print(f"  Stripe {r.status_code}: {r.text[:200]}", flush=True)
            return {"ok": False, "reason": f"http-{r.status_code}", "pulled": n}
        j = r.json(); data = j.get("data", [])
        with get_db() as c:
            for t in data:
                src = t.get("source")
                desc = t.get("description") or ""
                if isinstance(src, dict):
                    desc = desc or (src.get("description") or "")
                    email = (src.get("billing_details") or {}).get("email") or (src.get("receipt_email") or "")
                    if email: desc = f"{desc} <{email}>".strip()
                    src_id = src.get("id", "")
                else:
                    src_id = src or ""
                c.execute("""
                    INSERT INTO stripe_balance_tx (id, created, type, currency, amount, fee, net, description, source_id)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET created=excluded.created, type=excluded.type,
                        currency=excluded.currency, amount=excluded.amount, fee=excluded.fee,
                        net=excluded.net, description=excluded.description, source_id=excluded.source_id
                """, (t["id"], datetime.fromtimestamp(t["created"], tz=timezone.utc).isoformat(timespec="seconds"),
                      t.get("type"), (t.get("currency") or "").upper(),
                      (t.get("amount") or 0) / 100.0, (t.get("fee") or 0) / 100.0, (t.get("net") or 0) / 100.0,
                      desc[:200], src_id))
                n += 1
        if not j.get("has_more") or not data: break
        starting_after = data[-1]["id"]
        time.sleep(0.2)
    print(f"  Stripe: {n} balance transactions upserted (since {start.date()})", flush=True)
    return {"ok": True, "pulled": n, "since": start.date().isoformat()}
