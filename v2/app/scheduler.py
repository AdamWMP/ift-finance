"""In-process scheduler. Runs the sync every 10 minutes and the daily digest
on weekday mornings, all inside the web process. Avoids needing separate
Render cron services that can't share the same disk as the web service.
"""
from __future__ import annotations
import os
import threading
import time
import traceback
from datetime import datetime, timezone

# Toggle off in tests / one-off scripts
ENABLED = os.environ.get("IFT_SCHEDULER", "1") == "1"

# Schedule config. UTC times for the digest (Dublin = UTC in winter, +1 BST in summer).
# IFT_DIGEST_HOUR_UTC accepts "HH" or "HH:MM" (the minute is optional).
SYNC_INTERVAL_SEC   = int(os.environ.get("IFT_SYNC_INTERVAL_SEC", "600"))  # 10 min
def _parse_hour(raw: str) -> tuple[int, int]:
    raw = (raw or "9").strip()
    if ":" in raw:
        h, m = raw.split(":", 1)
        try: return int(h), int(m)
        except ValueError: return 9, 0
    try: return int(raw), 0
    except ValueError: return 9, 0
DIGEST_HOUR_UTC, DIGEST_MIN_UTC = _parse_hour(os.environ.get("IFT_DIGEST_HOUR_UTC", "9"))
DIGEST_WEEKDAYS_ONLY = os.environ.get("IFT_DIGEST_WEEKDAYS_ONLY", "1") == "1"

_started = False
_last_digest_date: str | None = None

def _run_sync():
    print("[scheduler] running sync …", flush=True)
    try:
        from .sync import main as sync_main
        sync_main("S26")
    except Exception:
        print("[scheduler] sync failed", flush=True)
        traceback.print_exc()

def _maybe_run_digest():
    global _last_digest_date
    now = datetime.now(timezone.utc)
    if DIGEST_WEEKDAYS_ONLY and now.weekday() >= 5: return
    # Match HH:MM with a 1-minute tolerance window (loop ticks every 60s)
    if not (now.hour == DIGEST_HOUR_UTC and now.minute == DIGEST_MIN_UTC): return
    today_iso = now.date().isoformat()
    if _last_digest_date == today_iso: return  # already sent today
    print("[scheduler] running daily digest …", flush=True)
    try:
        from .digest import send_digest
        send_digest("S26")
        _last_digest_date = today_iso
    except Exception:
        print("[scheduler] digest failed", flush=True)
        traceback.print_exc()

def _loop():
    last_sync = 0.0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_sync >= SYNC_INTERVAL_SEC:
                _run_sync()
                last_sync = now_ts
            _maybe_run_digest()
        except Exception:
            print("[scheduler] loop error", flush=True)
            traceback.print_exc()
        time.sleep(60)  # check every minute

def start_in_background() -> None:
    global _started
    if not ENABLED or _started: return
    _started = True
    t = threading.Thread(target=_loop, daemon=True, name="ift-scheduler")
    t.start()
    print("[scheduler] background thread started "
          f"(sync every {SYNC_INTERVAL_SEC}s, digest at "
          f"{DIGEST_HOUR_UTC:02d}:{DIGEST_MIN_UTC:02d} UTC)", flush=True)
