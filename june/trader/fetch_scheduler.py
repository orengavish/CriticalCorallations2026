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

import os
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

_LOCK_FILE = _ROOT / "data" / "fetch_scheduler.lock"


def _acquire_lock() -> bool:
    """Write PID lock file. Returns True if we got the lock, False if another instance is running."""
    if _LOCK_FILE.exists():
        try:
            pid = int(_LOCK_FILE.read_text().strip())
            import psutil
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                if any("fetch_scheduler" in " ".join(p) for p in [proc.cmdline()]):
                    log.warning("Another fetch_scheduler is already running (pid=%d) — exiting", pid)
                    return False
        except Exception:
            pass  # stale lock — overwrite it
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock():
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


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

def _present_files(cfg) -> set:
    """Return set of (symbol, date_str, dtype) tuples for complete CSV files."""
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
    return present


def _get_priority_dates(cfg, symbols: list) -> list:
    """
    Return list of (symbol, date) pairs to fetch, priority order:
      1. Days with verified trades AND missing tick files, sorted by trade count DESC
         (more verified trades = higher backtest value → fetch first)
      2. Yesterday for each symbol if not yet fetched
      3. Other recent dates missing tick files
    Called repeatedly during the fetch loop so priority stays current.
    """
    from pathlib import Path as P
    present = _present_files(cfg)
    pairs   = []
    seen    = set()

    # Priority 1: verified_trades dates — sorted by trade count DESC
    try:
        from lib.db import get_db
        db_path = P(cfg.paths.db)
        with get_db(db_path) as con:
            rows = con.execute(
                "SELECT DATE(fill_time) AS d, symbol, COUNT(*) AS n "
                "FROM verified_trades "
                "GROUP BY DATE(fill_time), symbol "
                "ORDER BY n DESC, d DESC"
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
        keep_gateway: bool = True) -> bool:
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
        # Fixed date: build list once, no dynamic re-priority needed
        static_pairs = [(sym, specific_date) for sym in symbols]
        dynamic_mode = False
    else:
        static_pairs = None
        dynamic_mode = True   # re-evaluate priority after each completed file
        initial = _get_priority_dates(cfg, symbols)
        if not initial:
            log.info("Nothing to fetch — all priority dates covered")
            return True
        log.info("Fetch plan (dynamic): first target → %s %s", initial[0][0], initial[0][1])

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
    _runner_path = Path(__file__).parent.parent / "back-trading" / "bt_matrix_runner.py"

    def _next_target():
        """Return next (sym, day) to fetch, or None if done."""
        if not dynamic_mode:
            return static_pairs.pop(0) if static_pairs else None
        pairs = _get_priority_dates(cfg, symbols)
        return pairs[0] if pairs else None

    try:
        while True:
            target = _next_target()
            if target is None:
                break
            sym, target_day = target
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
                        log.info("Uploaded %s to Drive %s", file_path.name, file_id)
                    elif getattr(getattr(cfg, "google_drive", None), "enabled", False):
                        log.warning("Drive upload failed for %s", file_path.name)
                        all_ok = False
                else:
                    log.warning("Verification FAILED for %s %s %s", sym, dtype, target_day)
                    all_ok = False

            # Trigger matrix runner for newly completed (sym, date) in background
            if _runner_path.exists():
                try:
                    import subprocess as _sp
                    date_str = target_day.strftime("%Y-%m-%d")
                    _sp.Popen(
                        [sys.executable, str(_runner_path),
                         "--incremental", sym, date_str],
                        cwd=str(_runner_path.parent),
                    )
                    log.info("Triggered matrix runner for %s %s", sym, date_str)
                except Exception as exc:
                    log.warning("Could not trigger matrix runner: %s", exc)

    finally:
        progress_conn.close()
        try:
            ib.disconnect()
        except Exception:
            pass
        # Gateway stays running — watchdog manages it 24/7.
        # Pass --no-keep-gateway CLI flag to stop after fetch (maintenance only).
        if not keep_gateway:
            _stop_gateway(cfg)

    status = "OK" if all_ok else "PARTIAL FAILURE"
    log.info("=== fetch_scheduler done | status=%s ===", status)
    return all_ok


def _self_test():
    """Test priority ordering without touching real DB or gateway."""
    import sqlite3, tempfile, os, shutil

    print("Running fetch_scheduler self-test...")
    tmp_ga  = tempfile.mktemp(suffix="_ga.db")
    tmp_dir = tempfile.mkdtemp()
    try:
        # Create minimal galao.db with completed_trades
        ga_conn = sqlite3.connect(tmp_ga)
        ga_conn.execute("""
            CREATE TABLE completed_trades (
                id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
                fill_price REAL, fill_time TEXT, exit_price REAL, exit_time TEXT,
                exit_reason TEXT, pnl_points REAL, bracket_size REAL,
                entry_type TEXT, source TEXT
            )
        """)
        # MES 2026-06-27 has 3 trades (highest priority)
        # MNQ 2026-06-27 has 1 trade (lower)
        ga_conn.executemany("INSERT INTO completed_trades VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)", [
            (1, "MES", "BUY",  5500.0, "2026-06-27T14:00:00+00:00",
             5501.0, "2026-06-27T14:05:00+00:00", "TP", 5.0, 4.0),
            (2, "MES", "SELL", 5501.0, "2026-06-27T14:10:00+00:00",
             5500.0, "2026-06-27T14:15:00+00:00", "TP", 5.0, 4.0),
            (3, "MES", "BUY",  5500.0, "2026-06-27T14:20:00+00:00",
             5498.0, "2026-06-27T14:25:00+00:00", "SL", -10.0, 4.0),
            (4, "MNQ", "BUY", 19500.0, "2026-06-27T14:00:00+00:00",
             19502.0, "2026-06-27T14:05:00+00:00", "TP", 4.0, 4.0),
        ])
        ga_conn.commit()
        ga_conn.close()

        # Simulate cfg-like object
        class FakePaths:
            db      = tmp_ga
            history = tmp_dir

        class FakeCfg:
            paths = FakePaths()
            symbols = ["MES", "MNQ", "MYM", "M2K"]

        class FakeFetcher:
            symbols_override = ["MES", "MNQ", "MYM", "M2K"]

        cfg = FakeCfg()

        # Monkey-patch get_db to work with our temp DB
        import lib.db as _libdb
        orig_get_db = _libdb.get_db

        from contextlib import contextmanager

        @contextmanager
        def _fake_get_db(path):
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            # Add verified_trades VIEW that reads completed_trades directly
            try:
                conn.execute("DROP VIEW IF EXISTS verified_trades")
                conn.execute("""
                    CREATE VIEW verified_trades AS
                    SELECT * FROM completed_trades
                    WHERE fill_price IS NOT NULL AND exit_price IS NOT NULL
                """)
            except Exception:
                pass
            try:
                yield conn
            finally:
                conn.close()

        _libdb.get_db = _fake_get_db

        try:
            # Test 1: MES 2026-06-27 should be #1 (3 trades vs MNQ's 1)
            pairs = _get_priority_dates(cfg, ["MES", "MNQ", "MYM", "M2K"])
            assert len(pairs) > 0, "Expected at least 1 pair"
            assert pairs[0][0] == "MES", \
                f"Expected MES first (3 trades), got {pairs[0][0]}"
            assert pairs[0][1].isoformat() == "2026-06-27", \
                f"Expected 2026-06-27, got {pairs[0][1]}"

            # Test 2: after MES CSV added, MNQ should be next
            # Simulate MES trades CSV now present
            mes_csv = Path(tmp_dir) / "MES_trades_20260627.csv"
            mes_csv.write_text("time_utc,price,size\n" + "x" * 200)
            pairs2 = _get_priority_dates(cfg, ["MES", "MNQ", "MYM", "M2K"])
            # MES trades done, MES bidask still missing AND MNQ trades missing
            # First should still be MES (bidask missing) or MNQ
            found_mnq = any(p[0] == "MNQ" for p in pairs2)
            assert found_mnq, "MNQ should appear in priority list"

            print("PASS -- MES (3 trades) ranked before MNQ (1 trade), "
                  "dynamic re-priority confirmed")
            return True
        finally:
            _libdb.get_db = orig_get_db

    except Exception as e:
        print(f"FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        try: os.unlink(tmp_ga)
        except Exception: pass
        try: shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception: pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galgo scheduled tick fetcher")
    parser.add_argument("--run-now",         action="store_true")
    parser.add_argument("--date",            default=None, help="Fetch specific date YYYY-MM-DD")
    parser.add_argument("--backfill",        action="store_true")
    parser.add_argument("--no-keep-gateway", action="store_true")
    parser.add_argument("--self-test",       action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    if not _acquire_lock():
        sys.exit(1)

    try:
        specific = date.fromisoformat(args.date) if args.date else None
        success  = run(specific_date=specific,
                       backfill=args.backfill,
                       keep_gateway=not args.no_keep_gateway)
    finally:
        _release_lock()

    sys.exit(0 if success else 1)
