"""
trader/fetch_scheduler.py
Scheduled daily tick fetcher for Galgo June 2026.

Replaces the old GalaoFetcherDaily + GalaoDailyPaperSession tasks.
Runs on THIS computer only. No other schedulers anywhere.

What it does each run:
  1. Start IBC gateway (paper) if not already up
  2. Determine which (symbol, date) pairs need fetching — priority first
  3. Fetch TRADES + BID_ASK for all 4 symbols
  4. Verify each file after fetch
  5. Upload verified files to Google Drive
  6. Stop IBC gateway when done (unless --keep-gateway flag)
  7. Write summary to logs/fetch_scheduler.log

Priority order:
  - Dates with verified_trades in DB but missing tick files (backfill priority)
  - Most-recent dates first for new fetches

Usage:
  python trader/fetch_scheduler.py             # normal scheduled run
  python trader/fetch_scheduler.py --run-now   # same, explicit
  python trader/fetch_scheduler.py --date 2026-06-02   # fetch specific date
  python trader/fetch_scheduler.py --backfill  # fetch ALL missing priority dates
  python trader/fetch_scheduler.py --keep-gateway   # don't stop IBC after run

Windows Task Scheduler:
  Script : scripts/run_fetcher.bat
  Trigger: daily at 23:30 UTC (= 17:30 CT standard, 18:30 CT daylight)
  Note   : adjust UTC time seasonally for CT offset
"""

import sys
import time
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.gdrive import GDriveClient
from trader.verify_data import check_and_mark

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

log = get_logger("fetch_scheduler")


# ── IBC helpers ───────────────────────────────────────────────────────────────

def _gateway_up(cfg) -> bool:
    """Return True if IB gateway is responding on the configured port."""
    import socket
    try:
        s = socket.create_connection((cfg.ib.live_host, cfg.ib.live_port), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def _start_gateway(cfg) -> bool:
    """Launch IBC gateway and wait for it to be ready. Returns True on success."""
    from lib.ibc_launcher import try_start_gateway
    if _gateway_up(cfg):
        log.info("Gateway already up on port %s", cfg.ib.live_port)
        return True
    log.info("Starting IBC gateway (paper mode)…")
    try:
        try_start_gateway(cfg)
    except Exception as e:
        log.warning("ibc_launcher raised: %s — will wait and check port", e)

    for attempt in range(1, 13):  # wait up to 60s
        time.sleep(5)
        if _gateway_up(cfg):
            log.info("Gateway ready (attempt %d)", attempt)
            return True
        log.debug("Waiting for gateway… attempt %d/12", attempt)

    log.error("Gateway did not come up after 60s")
    return False


def _stop_gateway(cfg):
    """Stop the IBC gateway process if it's running."""
    try:
        import subprocess
        # Kill any IBC/Gateway java processes — safe on paper-only setup
        subprocess.run(
            ["taskkill", "/F", "/IM", "java.exe"],
            capture_output=True
        )
        log.info("Gateway stopped (java.exe killed)")
    except Exception as e:
        log.warning("Could not stop gateway: %s", e)


# ── Priority list ─────────────────────────────────────────────────────────────

def _get_priority_dates(cfg, symbols: list) -> list:
    """
    Return list of (symbol, date) pairs to fetch, priority order:
      1. Dates with verified_trades in DB but no tick files (backfill)
      2. Yesterday if not yet fetched
    """
    from pathlib import Path as P
    history_dir = P(cfg.paths.history)
    present = set()
    if history_dir.exists():
        for f in history_dir.glob("*.csv"):
            parts = f.stem.split("_")
            if len(parts) >= 3 and f.stat().st_size > 100:
                sym, ftype, dc = parts[0], parts[1], parts[2]
                date_s = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                present.add((sym, date_s, ftype))

    pairs = []
    seen  = set()

    # Priority 1: verified_trades dates missing tick files
    try:
        from lib.db import get_db
        db_path = P(cfg.paths.db)
        with get_db(db_path) as con:
            rows = con.execute(
                "SELECT DISTINCT DATE(fill_time) AS d, symbol "
                "FROM verified_trades ORDER BY d DESC"
            ).fetchall()
        for r in rows:
            sym, d = r["symbol"], r["d"]
            if sym not in symbols:
                continue
            missing_trades = (sym, d, "trades") not in present
            missing_bidask = (sym, d, "bidask") not in present
            if missing_trades or missing_bidask:
                key = (sym, d)
                if key not in seen:
                    seen.add(key)
                    pairs.append((sym, date.fromisoformat(d)))
    except Exception as e:
        log.warning("Could not query verified_trades: %s", e)

    # Priority 2: yesterday for each symbol if not present
    yesterday = (datetime.now(CT) - timedelta(days=1)).date()
    yd_str = yesterday.strftime("%Y-%m-%d")
    for sym in symbols:
        missing_trades = (sym, yd_str, "trades") not in present
        missing_bidask = (sym, yd_str, "bidask") not in present
        if missing_trades or missing_bidask:
            key = (sym, yd_str)
            if key not in seen:
                seen.add(key)
                pairs.append((sym, yesterday))

    return pairs


# ── Main run ──────────────────────────────────────────────────────────────────

def run(specific_date: date = None, backfill: bool = False,
        keep_gateway: bool = False) -> bool:
    """
    Execute one full fetch cycle.
    Returns True if all fetches succeeded and verified.
    """
    cfg     = get_config()
    symbols = list(cfg.fetcher.symbols_override or cfg.symbols)
    gdrive  = GDriveClient(cfg)

    log.info("=== fetch_scheduler start | symbols=%s ===", symbols)

    # ── Determine what to fetch ──
    if specific_date:
        pairs = [(sym, specific_date) for sym in symbols]
    elif backfill:
        pairs = _get_priority_dates(cfg, symbols)
        if not pairs:
            log.info("Nothing to backfill — all priority dates covered")
            return True
    else:
        # Normal daily run: yesterday + any priority backfill
        pairs = _get_priority_dates(cfg, symbols)
        if not pairs:
            log.info("All symbols up to date — nothing to fetch")
            return True

    log.info("Fetch plan: %d (symbol, date) pairs", len(pairs))
    for sym, d in pairs:
        log.info("  → %s  %s", sym, d)

    # ── Start gateway ──
    if not _start_gateway(cfg):
        log.error("Cannot start IBC gateway — aborting")
        return False

    # ── Import fetcher (needs IB available) ──
    from trader.fetcher import fetch_day, _init_progress_db, _connect

    output_dir   = Path(cfg.paths.history)
    prog_db_path = Path(cfg.paths.db).parent / "fetch_progress.db"
    output_dir.mkdir(parents=True, exist_ok=True)

    ib            = _connect(cfg)
    progress_conn = _init_progress_db(prog_db_path)

    all_ok = True
    try:
        for sym, target_day in pairs:
            log.info("--- %s  %s ---", sym, target_day)
            try:
                results = fetch_day(ib, sym, target_day,
                                    fetch_bid_ask=True,
                                    output_dir=output_dir,
                                    progress_conn=progress_conn)
                log.info("fetch_day results: %s", results)
            except Exception as e:
                log.error("fetch_day %s %s failed: %s", sym, target_day, e)
                all_ok = False
                continue

            # Verify + upload each completed file
            date_compact = target_day.strftime("%Y%m%d")
            for dtype in ("trades", "bidask"):
                file_path = output_dir / f"{sym}_{dtype}_{date_compact}.csv"
                if not file_path.exists():
                    continue

                ok = check_and_mark(sym, target_day, dtype, output_dir)
                if ok:
                    file_id = gdrive.upload_file(file_path)
                    if file_id:
                        log.info("Uploaded %s → Drive %s", file_path.name, file_id)
                    elif getattr(getattr(cfg, "google_drive", None), "enabled", False):
                        log.warning("Drive upload failed for %s", file_path.name)
                        all_ok = False
                else:
                    log.warning("Verification FAILED for %s %s %s", sym, dtype, target_day)
                    all_ok = False

    finally:
        progress_conn.close()
        try:
            ib.disconnect()
        except Exception:
            pass
        if not keep_gateway:
            _stop_gateway(cfg)

    status = "OK" if all_ok else "PARTIAL FAILURE"
    log.info("=== fetch_scheduler done | status=%s ===", status)
    return all_ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galgo scheduled tick fetcher")
    parser.add_argument("--run-now",      action="store_true", help="Run immediately (default)")
    parser.add_argument("--date",         default=None, help="Fetch specific date YYYY-MM-DD")
    parser.add_argument("--backfill",     action="store_true", help="Fetch all missing priority dates")
    parser.add_argument("--keep-gateway", action="store_true", help="Don't stop IBC after run")
    args = parser.parse_args()

    specific = date.fromisoformat(args.date) if args.date else None
    success  = run(specific_date=specific,
                   backfill=args.backfill,
                   keep_gateway=args.keep_gateway)
    sys.exit(0 if success else 1)
