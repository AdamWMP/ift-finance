"""End-to-end sync entry point. Runs every cron tick.

Steps:
  1. Re-ingest students from .op_live_s26.csv
  2. Pull invoices for current students → payment_status
  3. Pull aggregate revenue from the IFT Sales Board
  4. Push live-money-in (collected ÷ 2100) to the Sales Board's L18 cell

Each step is independent; an exception in one is caught and logged so the
others still run.
"""
from __future__ import annotations
import sys, traceback
from datetime import datetime

def _step(name, fn, *args, **kwargs):
    print(f"\n→ {name} ...", flush=True)
    try:
        out = fn(*args, **kwargs)
        print(f"✓ {name}", flush=True)
        return out
    except Exception as e:
        print(f"✗ {name} — {e}", flush=True)
        traceback.print_exc()
        return None

def main(period: str = "S26") -> int:
    print(f"=== IFT Finance sync · {datetime.now():%Y-%m-%d %H:%M:%S} · period={period} ===", flush=True)

    from . import ingest, ontraport, sales_board

    _step("refresh S26 contacts from ONtraport", ontraport.refresh_s26_csv)
    _step("ingest students from CSV", ingest.ingest_csv)
    _step("apply ONtraport tags (drop-off / deferral / grant)", ontraport.apply_tags)
    _step("ingest invoices from ONtraport", ontraport.ingest_invoices)
    _step("read sales-board categories", sales_board.write_transactions, period)
    _step("push live-money-in → L18", sales_board.push_live_money_in, period, "L18")

    from . import queries
    # backfill_followon_periods is a no-op while FOLLOWON_STREAMS is empty —
    # safe to leave it called for forward-compat with future re-enable.
    _step("backfill follow-on revenue_period from invoices",
          queries.backfill_followon_periods)
    _step("record daily snapshot", queries.record_snapshot, period)

    from .db import set_meta
    set_meta("last_sync_at", datetime.now().isoformat(timespec="seconds"))
    print(f"\n=== sync done · {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)
    return 0

if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "S26"
    sys.exit(main(period))
