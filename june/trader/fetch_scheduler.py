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
    """Atomically write PID lock file using O_CREAT|O_EXCL. Returns True if we got the lock."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # First: if lock exists, check if owning process is actually alive
    if _LOCK_FILE.exists():
        try:
            pid = int(_LOCK_FILE.read_text().strip())
            import psutil
            try:
                proc = psutil.Process(pid)
                if proc.status() not in ("zombie", "dead") and \
                   "fetch_scheduler" in " ".join(proc.cmdline()):
                    log.warning("Another fetch_scheduler is already running (pid=%d) — exiting", pid)
                    return False
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                pass
            # Stale lock (process dead, zombie, or not fetch_scheduler) — remove it
            _LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            _LOCK_FILE.unlink(missing_ok=True)
    # Atomic create: fails if another process just created it between our check and now
    try:
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        log.warning("Lost lock race — another fetch_scheduler grabbed it first")
        return False


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


MIN_TRADES_FOR_PRIORITY = 20  # dates with more verified trades than this go first

def _ensure_progress_db(cfg):
    """Create fetch_progress.db and its table if they don't exist yet."""
    import sqlite3 as _sq3
    from pathlib import Path as P
    pdb = P(cfg.paths.db).parent / "fetch_progress.db"
    pdb.parent.mkdir(parents=True, exist_ok=True)
    with _sq3.connect(str(pdb)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS fetch_progress (
                symbol TEXT, date TEXT, data_type TEXT,
                records_fetched INTEGER DEFAULT 0,
                finished INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (symbol, date, data_type)
            )
        """)


def _get_priority_dates(cfg, symbols: list) -> list:
    """
    Return list of (symbol, date) pairs to fetch, priority order:
      P1. CRITICAL  — dates in verified_trades (ANY symbol), missing CSV for each symbol.
                      All symbols are fetched for each critical date (needed for correlation).
      P2. RESUME    — partial files in fetch_progress (finished=0, records>0) not covered by P1.
      P3. STANDARD  — backfill last backfill_days trading days, most recent first.
    Only considers BID_ASK as missing if fetch_bid_ask is enabled in config.
    Called repeatedly during the fetch loop so priority stays current.
    """
    import sqlite3 as _sq3
    from pathlib import Path as P
    _ensure_progress_db(cfg)   # guarantee table exists before querying
    present    = _present_files(cfg)
    do_bid_ask = bool(getattr(cfg.fetcher, "fetch_bid_ask", True))
    pairs      = []
    seen       = set()

    def _is_missing(sym, d):
        if (sym, d, "trades") not in present:
            return True
        if do_bid_ask and (sym, d, "bidask") not in present:
            return True
        return False

    # ── P1: CRITICAL — verified trade dates, all symbols ─────────────────────
    # Ordered by verified trade count DESC so high-value dates are fetched first.
    # P1a: TRADES already done, only BID_ASK missing — cheap, complete these first.
    # P1b: TRADES missing — full fetch needed, ordered by count DESC within tier.
    vt_dates = []
    try:
        from lib.db import get_db
        db_path = P(cfg.paths.db)
        with get_db(db_path) as con:
            rows = con.execute(
                "SELECT DATE(fill_time) AS d, COUNT(*) AS n "
                "FROM verified_trades GROUP BY DATE(fill_time) ORDER BY n DESC"
            ).fetchall()
        vt_dates = [(r["d"], r["n"]) for r in rows]
    except Exception as e:
        log.warning("Could not query verified_trades: %s", e)

    # Partial files (finished=0, records>0) — needed to promote CRITICAL partials to P1
    partial_keys: set = set()
    try:
        pdb = P(cfg.paths.db).parent / "fetch_progress.db"
        with _sq3.connect(str(pdb)) as pcon:
            pcon.row_factory = _sq3.Row
            partial_rows = pcon.execute(
                "SELECT symbol, date FROM fetch_progress "
                "WHERE finished=0 AND records_fetched > 0 ORDER BY updated_at DESC"
            ).fetchall()
        for r in partial_rows:
            if r["symbol"] in symbols:
                partial_keys.add((r["symbol"], r["date"]))
    except Exception as e:
        log.warning("Could not query partial files: %s", e)

    p1a = []  # tier 1: TRADES done, only BID_ASK missing
    p1b = []  # tier 2: TRADES missing, full fetch needed
    for d_str, _count in vt_dates:
        for sym in symbols:
            # CRITICAL if CSV missing -OR- CSV exists but fetch is unfinished
            is_unfinished = (sym, d_str) in partial_keys
            if _is_missing(sym, d_str) or is_unfinished:
                key = (sym, d_str)
                if key not in seen:
                    seen.add(key)
                    if (sym, d_str, "trades") in present:
                        p1a.append((sym, date.fromisoformat(d_str)))
                    else:
                        p1b.append((sym, date.fromisoformat(d_str)))
    pairs.extend(p1a)
    pairs.extend(p1b)

    # ── P2: RESUME — partial files not already covered by P1 ─────────────────
    for sym, d_str in sorted(partial_keys):
        key = (sym, d_str)
        if key not in seen:
            seen.add(key)
            pairs.append((sym, date.fromisoformat(d_str)))

    # ── P3: STANDARD — backfill recent trading days, most recent first ──────────
    # P3a: trades CSV already present, only bidask missing — fast path, completes
    #      the day without re-fetching trades (fetcher resumes from last tick instantly).
    # P3b: trades missing — full fetch needed.
    # P3a before P3b maximises number of fully complete days produced per unit time.
    yesterday     = (datetime.now(CT) - timedelta(days=1)).date()
    backfill_days = int(getattr(cfg.fetcher, "backfill_days", 180))
    scan_date     = yesterday
    days_checked  = 0
    p3a = []  # bidask-only (trades done)
    p3b = []  # full fetch  (trades missing)
    while days_checked < backfill_days:
        if scan_date.weekday() < 5:  # Mon-Fri only
            d_str = scan_date.strftime("%Y-%m-%d")
            for sym in symbols:
                if _is_missing(sym, d_str):
                    key = (sym, d_str)
                    if key not in seen:
                        seen.add(key)
                        if (sym, d_str, "trades") in present:
                            p3a.append((sym, scan_date))
                        else:
                            p3b.append((sym, scan_date))
            days_checked += 1
        scan_date -= timedelta(days=1)

    pairs.extend(p3a)
    pairs.extend(p3b)
    return pairs


# ── Main run ──────────────────────────────────────────────────────────────────

def run(specific_date: date = None, backfill: bool = False,
        keep_gateway: bool = True) -> bool:
    """
    Execute one full fetch cycle.
    Returns True if all fetches succeeded and verified.
    """
    cfg        = get_config()
    symbols    = list(cfg.fetcher.symbols_override or cfg.symbols)
    do_bid_ask = bool(getattr(cfg.fetcher, "fetch_bid_ask", True))
    gdrive     = GDriveClient(cfg)

    log.info("=== fetch_scheduler start | symbols=%s bid_ask=%s ===", symbols, do_bid_ask)

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
        log.info("Fetch plan (dynamic): first target -> %s %s", initial[0][0], initial[0][1])

    # ── Start gateway ──
    if not _start_gateway(cfg):
        log.error("Cannot start IBC gateway — aborting")
        return False

    # ── Import fetcher (needs IB available) ──
    from trader.fetcher import fetch_day, _init_progress_db, _connect

    output_dir   = Path(cfg.paths.history)
    prog_db_path = Path(cfg.paths.db).parent / "fetch_progress.db"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Ensure fetch_progress table exists before first _get_priority_dates call.
    _ensure_progress_db(cfg)

    ib            = _connect(cfg)
    progress_conn = _init_progress_db(prog_db_path)

    all_ok = True
    _runner_path = Path(__file__).parent.parent / "back-trading" / "bt_matrix_runner.py"

    # Cooldown map: (sym, date_str) → unix timestamp after which the target may be retried.
    # Used when fetch_day returns skipped_active — we move on to other targets instead of
    # spinning, and come back once the grace period (_is_actively_running = 120 s) expires.
    _active_cooldowns: dict = {}

    def _next_target():
        """Return next (sym, day) to fetch, skipping targets in active cooldown."""
        if not dynamic_mode:
            return static_pairs.pop(0) if static_pairs else None
        pairs = _get_priority_dates(cfg, symbols)
        now = time.time()
        # Evict expired cooldowns so they become eligible again
        for k in [k for k, v in _active_cooldowns.items() if v <= now]:
            del _active_cooldowns[k]
        # Return first pair not in cooldown
        for pair in pairs:
            if (pair[0], str(pair[1])) not in _active_cooldowns:
                return pair
        # All current targets are in cooldown — signal caller to wait
        return None

    try:
        while True:
            target = _next_target()

            if target is None:
                # Truly done (no pairs left) or all pairs in cooldown
                if not _active_cooldowns:
                    break   # nothing left to fetch
                # Wait until the nearest cooldown expires, then re-check
                wait = max(min(_active_cooldowns.values()) - time.time(), 1)
                log.info("All %d remaining target(s) in cooldown — waiting %.0fs",
                         len(_active_cooldowns), wait)
                time.sleep(wait)
                continue

            sym, target_day = target
            log.info("--- %s  %s ---", sym, target_day)
            try:
                results = fetch_day(ib, sym, target_day,
                                    fetch_bid_ask=do_bid_ask,
                                    output_dir=output_dir,
                                    progress_conn=progress_conn)
                log.info("fetch_day results: %s", results)

                # Any dtype came back as skipped_active → another fetch is in progress
                # (or _mark_started just touched the timestamp on this process's first attempt).
                # Put the whole (sym, date) into cooldown so we don't spin.
                if results and any(v == "skipped_active" for v in results.values()):
                    key = (sym, str(target_day))
                    _active_cooldowns[key] = time.time() + 120
                    log.warning("%s %s — skipped_active on ≥1 dtype; "
                                "cooldown 120s, moving to next target", sym, target_day)
                    continue   # skip verification — nothing was written

            except Exception as e:
                log.error("fetch_day %s %s failed: %s", sym, target_day, e)
                all_ok = False
                # Add a short cooldown so we don't immediately retry the same target.
                # _mark_started may have refreshed updated_at which would otherwise cause
                # an infinite skipped_active loop on the very next iteration.
                key = (sym, str(target_day))
                _active_cooldowns[key] = time.time() + 60
                err_str = str(e).lower()
                if "not connected" in err_str or "connection" in err_str or "disconnect" in err_str:
                    log.warning("IB disconnected — trying to reconnect in 60 s...")
                    time.sleep(60)
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    try:
                        ib = _connect(cfg)
                        log.info("Reconnected to IB")
                    except Exception as re:
                        log.error("Reconnect failed: %s — will retry in 120 s", re)
                        time.sleep(120)
                continue

            # ── Verify + upload only dtypes that were actually fetched this pass ─
            # results keys are uppercase ("TRADES", "BID_ASK"); values are int counts
            # when fetched, or the strings "skipped" / "skipped_active" otherwise.
            dtype_map = {"trades": "TRADES", "bidask": "BID_ASK"}
            newly_fetched = {lc for lc, uc in dtype_map.items()
                             if isinstance(results.get(uc), int)}

            date_compact = target_day.strftime("%Y%m%d")
            for dtype in ("trades", "bidask"):
                if dtype not in newly_fetched:
                    continue
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

            # Trigger matrix runner only when at least one file was newly completed
            if newly_fetched and _runner_path.exists():
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
        # MES 2026-06-20 has 25 trades  -> P1 (n > MIN_TRADES_FOR_PRIORITY=20)
        # MES 2026-06-27 has  3 trades  -> P3 (n <= 20, after yesterday)
        # MNQ 2026-06-27 has  1 trade   -> P3
        ga_conn = sqlite3.connect(tmp_ga)
        ga_conn.execute("""
            CREATE TABLE completed_trades (
                id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
                fill_price REAL, fill_time TEXT, exit_price REAL, exit_time TEXT,
                exit_reason TEXT, pnl_points REAL, bracket_size REAL,
                entry_type TEXT, source TEXT
            )
        """)
        rows = []
        for i in range(25):
            rows.append((100+i, "MES", "BUY", 5500.0,
                         f"2026-06-20T{14+i//4:02d}:{(i%4)*15:02d}:00+00:00",
                         5501.0, f"2026-06-20T{14+i//4:02d}:{(i%4)*15+5:02d}:00+00:00",
                         "TP", 5.0, 4.0))
        rows += [
            (1, "MES", "BUY",  5500.0, "2026-06-27T14:00:00+00:00",
             5501.0, "2026-06-27T14:05:00+00:00", "TP", 5.0, 4.0),
            (2, "MES", "SELL", 5501.0, "2026-06-27T14:10:00+00:00",
             5500.0, "2026-06-27T14:15:00+00:00", "TP", 5.0, 4.0),
            (3, "MES", "BUY",  5500.0, "2026-06-27T14:20:00+00:00",
             5498.0, "2026-06-27T14:25:00+00:00", "SL", -10.0, 4.0),
            (4, "MNQ", "BUY", 19500.0, "2026-06-27T14:00:00+00:00",
             19502.0, "2026-06-27T14:05:00+00:00", "TP", 4.0, 4.0),
        ]
        ga_conn.executemany("INSERT INTO completed_trades VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)", rows)
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
            fetch_bid_ask    = False

        cfg         = FakeCfg()
        cfg.fetcher = FakeFetcher()

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
            # Test 1: MES 2026-06-20 (P1 critical) must be the first pair
            pairs = _get_priority_dates(cfg, ["MES", "MNQ", "MYM", "M2K"])
            assert len(pairs) > 0, "Expected at least 1 pair"
            assert pairs[0][0] == "MES", \
                f"Expected MES first (oldest P1 date), got {pairs[0][0]}"
            assert pairs[0][1].isoformat() == "2026-06-20", \
                f"Expected 2026-06-20 (P1), got {pairs[0][1]}"

            # Test 2: ALL verified-trade dates are P1 regardless of trade count.
            # Both 2026-06-20 (25 trades) and 2026-06-27 (3 trades) must come
            # BEFORE any standard backfill (yesterday / recent) entries.
            yesterday_str = (datetime.now(CT) - timedelta(days=1)).date().isoformat()
            idx_jun20 = next(i for i,p in enumerate(pairs) if p[1].isoformat()=="2026-06-20")
            idx_jun27 = next((i for i,p in enumerate(pairs) if p[1].isoformat()=="2026-06-27"), None)
            idx_yd    = next((i for i,p in enumerate(pairs) if p[1].isoformat()==yesterday_str), None)
            assert idx_jun20 == 0, f"2026-06-20 should be first, got index {idx_jun20}"
            if idx_jun27 is not None and idx_yd is not None:
                assert idx_jun27 < idx_yd, \
                    f"P1 date 2026-06-27 (idx {idx_jun27}) should come before yesterday P3 (idx {idx_yd})"

            # Test 3: after MES Jun-20 CSV added, MNQ should still appear (same date still missing)
            mes_csv = Path(tmp_dir) / "MES_trades_20260620.csv"
            mes_csv.write_text("time_utc,price,size\n" + "x" * 200)
            pairs2 = _get_priority_dates(cfg, ["MES", "MNQ", "MYM", "M2K"])
            found_mnq = any(p[0] == "MNQ" for p in pairs2)
            assert found_mnq, "MNQ should appear in priority list after MES CSV added"

            print("PASS -- P1 (all verified-trade dates) before P3 (standard backfill), "
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
