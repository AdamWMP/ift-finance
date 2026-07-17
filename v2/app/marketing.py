"""Ad-spend ingestion, campaign classifier, and rollup queries for the
Marketing tab on the finance dashboard.

Data sources:
  • Meta Marketing API — via META_ADS_ACCESS_TOKEN (long-lived Business token)
  • Google Ads API — via env-configured OAuth refresh token
  • Manual CSV import from Ads Manager / Google Ads export (always available)

All three land in the same `ad_spend` table, so the classifier + UI don't
care where a row came from.
"""
from __future__ import annotations
import csv, io, os
from datetime import date, datetime, timedelta
from typing import Iterable

import requests

from .db import get_db

META_API_VERSION   = "v20.0"
META_API_BASE      = f"https://graph.facebook.com/{META_API_VERSION}"
META_ACCESS_TOKEN  = os.environ.get("META_ADS_ACCESS_TOKEN", "")


# ---------------------------------------------------------------------------
# Campaign classifier
#
# Rule order matters — MOST SPECIFIC FIRST. A campaign named
# "Studio Derry - Pilates - Prospecting" should classify as "Derry Pilates",
# not "Pilates" or "Derry". The first matching rule wins.
#
# Case-insensitive substring match. Extend TOPIC_RULES to add more buckets;
# the UI picks up new topics automatically.
# ---------------------------------------------------------------------------
TOPIC_RULES: list[tuple[str, list[str]]] = [
    # (topic label, [keyword patterns — ANY match on lowercased name])
    #
    # Order matters: most-specific product/location first. Course-type buckets
    # (Pilates, PT) win over the generic-geo "Derry" fallback, so a campaign
    # named "IFT - Pilates - Derry" classifies as Pilates (the product) rather
    # than Derry (the audience).
    ("Derry Pilates",     ["derry pilates", "studio derry", "derrystudio", "derry - studio"]),
    ("Studio Swords",     ["studio swords", "swords studio", "the studio swords",
                           "the studio - swords", "swordsstudio", "swords - studio"]),
    ("Pilates",           ["pilates"]),
    ("Personal Training", ["personal training", "pt course", "pt-course", " pt ", "|pt|",
                           "pt-", "-pt", "pt|", "|pt"]),
    ("Derry",             ["derry"]),   # geo-only fallback for campaigns that
                                        # target Derry but aren't a specific
                                        # course/studio (rare — surface via UI
                                        # so Adam can rename if the classifier
                                        # gets it wrong)
]

DEFAULT_TOPIC = "Other"


def classify_campaign(name: str) -> str:
    """Bucket a campaign name into a marketing topic.

    Rules are ordered most-specific-first. First match wins. Falls back to
    DEFAULT_TOPIC when nothing hits — surfaced in the UI as an "Other" row
    that can be spot-checked and used to refine TOPIC_RULES.
    """
    n = (name or "").lower()
    if not n:
        return DEFAULT_TOPIC
    # Pad with spaces so " pt " boundary matches work even at start/end of string
    padded = f" {n} "
    for topic, keywords in TOPIC_RULES:
        for kw in keywords:
            # Pattern with explicit boundaries → look in the padded form
            probe = kw if kw.startswith(" ") or kw.startswith("|") else kw
            if probe in padded or kw in n:
                return topic
    return DEFAULT_TOPIC


# ---------------------------------------------------------------------------
# Upsert helper — used by every ingest path
# ---------------------------------------------------------------------------
def upsert_spend_rows(rows: Iterable[dict]) -> dict:
    """Insert or update ad_spend rows. Idempotent per (date, platform, campaign_id).

    Each row must have: date, platform, ad_account_id, campaign_id, campaign_name,
    spend. Optional: ad_account_label, impressions, clicks, leads.
    Topic is derived from campaign_name via classify_campaign.
    """
    n_ins, n_upd, n_skip = 0, 0, 0
    with get_db() as c:
        for r in rows:
            if not r.get("date") or not r.get("platform") or not r.get("campaign_id"):
                n_skip += 1
                continue
            topic = classify_campaign(r.get("campaign_name") or "")
            cur = c.execute("""
                INSERT INTO ad_spend
                    (date, platform, ad_account_id, ad_account_label,
                     campaign_id, campaign_name, spend, impressions, clicks,
                     leads, topic, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date, platform, campaign_id) DO UPDATE SET
                    ad_account_id    = excluded.ad_account_id,
                    ad_account_label = excluded.ad_account_label,
                    campaign_name    = excluded.campaign_name,
                    spend            = excluded.spend,
                    impressions      = excluded.impressions,
                    clicks           = excluded.clicks,
                    leads            = excluded.leads,
                    topic            = excluded.topic,
                    fetched_at       = datetime('now')
            """, (
                r["date"], r["platform"], r["ad_account_id"], r.get("ad_account_label"),
                r["campaign_id"], r.get("campaign_name") or "",
                float(r.get("spend") or 0),
                int(r["impressions"]) if r.get("impressions") is not None else None,
                int(r["clicks"])      if r.get("clicks")      is not None else None,
                int(r["leads"])       if r.get("leads")       is not None else None,
                topic,
            ))
            # SQLite doesn't distinguish insert vs update via rowcount reliably here
            # (both are 1). Track via cursor lastrowid: if it grew, it was an insert.
            if cur.rowcount:
                # Best effort: treat every affected row as ok
                n_ins += 1
    return {"upserted": n_ins, "skipped": n_skip}


# ---------------------------------------------------------------------------
# Meta Marketing API sync
# ---------------------------------------------------------------------------
def meta_fetch_spend(*, ad_account_id: str, ad_account_label: str,
                     since: str, until: str) -> list[dict]:
    """Pull daily campaign-level spend from Meta between two ISO dates.

    Returns raw rows ready for upsert_spend_rows. Requires META_ADS_ACCESS_TOKEN
    (a long-lived Business token with `ads_read` on the account).
    """
    if not META_ACCESS_TOKEN:
        raise RuntimeError("META_ADS_ACCESS_TOKEN missing — set env var to enable Meta sync")
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id.lstrip('act_')}"
    url = f"{META_API_BASE}/{ad_account_id}/insights"
    params = {
        "level": "campaign",
        "time_increment": 1,             # one row per day per campaign
        "time_range": '{"since":"' + since + '","until":"' + until + '"}',
        "fields": "campaign_id,campaign_name,spend,impressions,clicks,actions",
        "limit": 500,
        "access_token": META_ACCESS_TOKEN,
    }
    rows: list[dict] = []
    while True:
        r = requests.get(url, params=params, timeout=45)
        r.raise_for_status()
        data = r.json()
        for it in data.get("data", []):
            leads = 0
            for a in it.get("actions") or []:
                if a.get("action_type") in ("lead", "onsite_conversion.lead_grouped"):
                    try:
                        leads += int(float(a.get("value") or 0))
                    except (TypeError, ValueError):
                        pass
            rows.append({
                "date": it.get("date_start") or "",
                "platform": "meta",
                "ad_account_id": ad_account_id,
                "ad_account_label": ad_account_label,
                "campaign_id": it.get("campaign_id") or "",
                "campaign_name": it.get("campaign_name") or "",
                "spend": float(it.get("spend") or 0),
                "impressions": int(it.get("impressions") or 0),
                "clicks": int(it.get("clicks") or 0),
                "leads": leads,
            })
        # Pagination
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = nxt, None  # next contains the full URL already
    return rows


def sync_meta_ads(days_back: int = 7) -> dict:
    """Pull the last N days of spend for every enabled Meta ad account and
    upsert into ad_spend. Called by the scheduler + manual admin trigger."""
    since = (date.today() - timedelta(days=days_back)).isoformat()
    until = date.today().isoformat()
    with get_db() as c:
        accts = c.execute(
            "SELECT account_id, label FROM ad_accounts "
            "WHERE platform='meta' AND enabled=1"
        ).fetchall()
    total = 0
    per_acct = {}
    for a in accts:
        rows = meta_fetch_spend(ad_account_id=a["account_id"],
                                ad_account_label=a["label"],
                                since=since, until=until)
        res = upsert_spend_rows(rows)
        per_acct[a["label"]] = {"rows_fetched": len(rows), **res}
        total += res["upserted"]
    return {"since": since, "until": until, "accounts": per_acct, "total": total}


# ---------------------------------------------------------------------------
# CSV import (fallback for Google Ads until API is wired, and general escape
# hatch for either platform)
# ---------------------------------------------------------------------------
CSV_ALIASES = {
    "date":          ["date", "day", "reporting_starts", "reporting starts"],
    "campaign_id":   ["campaign_id", "campaign id"],
    "campaign_name": ["campaign_name", "campaign", "campaign name"],
    "spend":         ["spend", "amount spent (eur)", "amount_spent_eur", "cost",
                      "cost (eur)", "amount spent"],
    "impressions":   ["impressions"],
    "clicks":        ["clicks", "link clicks", "link_clicks"],
    "leads":         ["leads", "results", "conversions", "lead form submissions",
                      "on-facebook leads"],
}


def _pick(row: dict, canonical: str) -> str | None:
    aliases = CSV_ALIASES.get(canonical, [canonical])
    keys = {k.strip().lower(): k for k in row.keys()}
    for a in aliases:
        if a in keys:
            return row[keys[a]]
    return None


def import_spend_csv(text: str, *, platform: str, ad_account_id: str,
                     ad_account_label: str) -> dict:
    """Parse an Ads-Manager or Google-Ads CSV and upsert its rows.

    Accepts flexible column headers via CSV_ALIASES — tolerant of the standard
    export from either platform. If `campaign_id` is missing (Meta rarely
    exports it in the report UI CSV), we hash campaign_name+date as a
    stable synthetic id so the uniqueness constraint still works.
    """
    import hashlib
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        d = _pick(r, "date") or ""
        # Meta CSVs sometimes ship dates as "2 Jun 2026" — normalise to ISO
        try:
            d = datetime.strptime(d.strip(), "%Y-%m-%d").date().isoformat()
        except (ValueError, TypeError):
            for fmt in ("%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%d/%m/%Y"):
                try:
                    d = datetime.strptime(d.strip(), fmt).date().isoformat()
                    break
                except ValueError:
                    continue
        name = _pick(r, "campaign_name") or ""
        cid = _pick(r, "campaign_id") or ""
        if not cid and name:
            cid = "syn_" + hashlib.md5(name.encode()).hexdigest()[:12]
        try:
            spend = float((_pick(r, "spend") or "0").replace(",", "").replace("€", "").strip() or 0)
        except (TypeError, ValueError):
            spend = 0.0
        def _int(v):
            try: return int(float((v or "0").replace(",", "") or 0))
            except (TypeError, ValueError, AttributeError): return 0
        rows.append({
            "date": d,
            "platform": platform,
            "ad_account_id": ad_account_id,
            "ad_account_label": ad_account_label,
            "campaign_id": cid,
            "campaign_name": name,
            "spend": spend,
            "impressions": _int(_pick(r, "impressions")),
            "clicks":      _int(_pick(r, "clicks")),
            "leads":       _int(_pick(r, "leads")),
        })
    return {"parsed": len(rows), **upsert_spend_rows(rows)}


# ---------------------------------------------------------------------------
# Ad account management
# ---------------------------------------------------------------------------
def list_accounts() -> list[dict]:
    with get_db() as c:
        rs = c.execute("SELECT id, platform, account_id, label, enabled "
                       "FROM ad_accounts ORDER BY platform, label").fetchall()
    return [dict(r) for r in rs]


def upsert_account(platform: str, account_id: str, label: str,
                   enabled: bool = True) -> None:
    with get_db() as c:
        c.execute("""
            INSERT INTO ad_accounts (platform, account_id, label, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, account_id) DO UPDATE SET
                label=excluded.label, enabled=excluded.enabled
        """, (platform, account_id, label, 1 if enabled else 0))
