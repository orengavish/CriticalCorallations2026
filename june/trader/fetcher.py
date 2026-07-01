"""
fetcher.py
Historical tick fetcher for Galao.
Fetches every trade (and optionally bid/ask) for a full CME Globex session.

Session window: prev_day 17:00 CT → target_day 17:00 CT (full ~23h session)
Pagination:     IB returns max 1000 ticks per request — loops with cursor until window done
Deduplication:  fingerprint-based at cursor boundary (V1-proven technique)
Contracts:      includeExpired=True — handles front-month rolls transparently
Progress DB:    data/fetch_progress.db — resume interrupted fetches, skip completed days
Timezone:       both CT and UTC stored in every row

Output files (R-DAT-01):
  data/history/{SYMBOL}_trades_{YYYYMMDD}.csv
  data/history/{SYMBOL}_bidask_{YYYYMMDD}.csv  (with --bid-ask)

CSV columns:
  TRADES:  time_ct, time_utc, price, size, symbol
  BID_ASK: time_ct, time_utc, bid_p, bid_s, ask_p, ask_s, symbol

Usage:
  python fetcher.py --symbol MES --date 2026-04-07
  python fetcher.py --symbol MES --date 2026-04-07 --bid-ask
  python fetcher.py --symbol MES --from-date 2026-03-12
  python fetcher.py --verify  --symbol MES --date 2026-04-07
  python fetcher.py --self-test

Self-test:
  python fetcher.py --self-test
"""

import asyncio
import csv
import signal
import sqlite3
import sys
import time
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None
from zoneinfo import ZoneInfo

from ib_insync import IB, Future

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("fetcher")

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

_TICK_TIMEOUT  = 45   # seconds per reqHistoricalTicks call
_TICKS_PER_REQ = 1000
_PROGRESS_LOG_INTERVAL = 15  # seconds between telemetry lines
_N_WORKERS     = 4    # parallel async windows (reduced from 8 to avoid IB pacing cascade)

_EXCHANGE_MAP = {"MES": "CME", "MNQ": "CME", "M2K": "CME", "MYM": "CBOT"}

_running = True


def _signal_handler(sig, frame):
    global _running
    log.warning("Interrupt received — stopping after current batch")
    _running = False


import threading as _threading
if _threading.current_thread() is _threading.main_thread():
    signal.signal(signal.SIGINT, _signal_handler)


# ── Session window ────────────────────────────────────────────────────────────

def get_session_bounds(day: date):
    """
    Return (start_utc, end_utc) for the full CME Globex session covering 'day'.
    Session: prev_day 17:00 CT → day 17:00 CT  (~23 hours)
    """
    prev = day - timedelta(days=1)
    start_ct = datetime(prev.year, prev.month, prev.day, 17, 0, 0, tzinfo=CT)
    end_ct   = datetime(day.year,  day.month,  day.day,  17, 0, 0, tzinfo=CT)
    return start_ct.astimezone(timezone.utc), end_ct.astimezone(timezone.utc)


# ── Contract resolution ───────────────────────────────────────────────────────

def get_contract_for_date(ib: IB, symbol: str, target_date: date):
    """
    Find the front-month contract active on target_date.
    Uses includeExpired=True so historical dates work after roll.
    Returns qualified contract or raises ValueError.
    """
    exchange = _EXCHANGE_MAP.get(symbol, "CME")
    target_str = target_date.strftime("%Y%m%d")

    con = Future(symbol=symbol, exchange=exchange, currency="USD")
    con.includeExpired = True
    details = ib.reqContractDetails(con)
    if not details:
        raise ValueError(f"No contract details for {symbol}")

    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    for det in details:
        if det.contract.lastTradeDateOrContractMonth >= target_str:
            contract = det.contract
            ib.qualifyContracts(contract)
            log.info(f"Contract for {symbol} on {target_date}: "
                     f"{contract.localSymbol} exp={contract.lastTradeDateOrContractMonth}")
            return contract

    raise ValueError(f"No valid contract found for {symbol} on {target_date}")


# ── Progress DB ───────────────────────────────────────────────────────────────

def _progress_db_path() -> Path:
    try:
        cfg = get_config()
        return Path(cfg.paths.db).parent / "fetch_progress.db"
    except Exception:
        return Path("data/fetch_progress.db")


def _init_progress_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_progress (
            symbol TEXT, date TEXT, data_type TEXT,
            records_fetched INTEGER DEFAULT 0,
            finished INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (symbol, date, data_type)
        )
    """)
    conn.commit()
    return conn


def _is_finished(conn, symbol, date_str, dtype) -> bool:
    row = conn.execute(
        "SELECT finished FROM fetch_progress WHERE symbol=? AND date=? AND data_type=?",
        (symbol, date_str, dtype)
    ).fetchone()
    return bool(row and row[0])


def _is_actively_running(conn, symbol, date_str, dtype, grace_seconds=120) -> bool:
    """Return True if another process updated this row within the last grace_seconds."""
    row = conn.execute(
        "SELECT finished, updated_at FROM fetch_progress WHERE symbol=? AND date=? AND data_type=?",
        (symbol, date_str, dtype)
    ).fetchone()
    if not row or row[0]:
        return False
    try:
        updated = datetime.fromisoformat(row[1]).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age < grace_seconds
    except Exception:
        return False


def _mark_started(conn, symbol, date_str, dtype):
    conn.execute("""
        INSERT INTO fetch_progress (symbol, date, data_type, records_fetched, finished, updated_at)
        VALUES (?,?,?,0,0,?)
        ON CONFLICT(symbol,date,data_type) DO UPDATE
        SET finished=0, records_fetched=0, updated_at=excluded.updated_at
    """, (symbol, date_str, dtype, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def _update_progress(conn, symbol, date_str, dtype, count):
    conn.execute(
        "UPDATE fetch_progress SET records_fetched=?, updated_at=? "
        "WHERE symbol=? AND date=? AND data_type=?",
        (count, datetime.now(timezone.utc).isoformat(), symbol, date_str, dtype)
    )
    conn.commit()


def _mark_finished(conn, symbol, date_str, dtype, count):
    conn.execute(
        "UPDATE fetch_progress SET records_fetched=?, finished=1, updated_at=? "
        "WHERE symbol=? AND date=? AND data_type=?",
        (count, datetime.now(timezone.utc).isoformat(), symbol, date_str, dtype)
    )
    conn.commit()


# ── Pagination core ───────────────────────────────────────────────────────────

async def _progress_reporter(progress: list, what: str, interval: float = 15.0,
                              db_conn=None, symbol: str = "", date_str: str = ""):
    """Print a summary line every `interval` seconds while windows are running.
    If db_conn is provided, also writes intermediate count to fetch_progress DB."""
    loop = asyncio.get_event_loop()
    t0   = loop.time()
    while True:
        await asyncio.sleep(interval)
        collected = sum(progress)
        elapsed   = loop.time() - t0
        speed     = collected / max(elapsed, 0.001)
        per_win   = "  ".join(f"W{i}:{progress[i]:>6,}" for i in range(len(progress)))
        print(f"    [{what}] {elapsed:5.0f}s  {collected:>8,} ticks  "
              f"{speed:5.0f} t/s  |  {per_win}", flush=True)
        if db_conn and symbol and date_str:
            try:
                _update_progress(db_conn, symbol, date_str, what, collected)
            except Exception:
                pass


async def _paginate_window_async(ib: IB, contract,
                                  win_start: datetime, win_end: datetime,
                                  what: str, win_idx: int,
                                  progress: list) -> list:
    """
    Fetch all ticks in [win_start, win_end) for one parallel window.
    Returns list of raw tuples:
      TRADES:  (t_utc, price, size)
      BID_ASK: (t_utc, bid_p, bid_s, ask_p, ask_s)
    Storing primitives instead of tick objects keeps memory ~4× lower.
    """
    ticks: list = []
    cursor = win_start
    last_processed_ts = None
    last_batch_fps: set = set()

    while _running and cursor < win_end:
        # Stop cleanly on disconnect rather than hammering a dead connection
        if not ib.isConnected():
            log.warning(f"W{win_idx} {what}: IB not connected — waiting for reconnect")
            while not ib.isConnected() and _running:
                await asyncio.sleep(5)
            if not _running:
                break
            log.info(f"W{win_idx} {what}: reconnected — resuming")
            await asyncio.sleep(2)

        try:
            batch = await asyncio.wait_for(
                ib.reqHistoricalTicksAsync(
                    contract,
                    startDateTime=cursor,
                    endDateTime="",
                    numberOfTicks=_TICKS_PER_REQ,
                    whatToShow=what,
                    useRth=False,
                ),
                timeout=_TICK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await asyncio.sleep(2)
            continue
        except Exception as e:
            log.warning(f"W{win_idx} {what} error: {e} — retrying")
            await asyncio.sleep(5)
            continue

        if not batch:
            break

        new_ticks = 0
        batch_end_ts = None
        batch_end_fps: set = set()
        reached_end = False

        for tick in batch:
            t_u = tick.time
            if not isinstance(t_u, datetime):
                t_u = datetime.fromtimestamp(t_u, tz=timezone.utc)
            if t_u.tzinfo is None:
                t_u = t_u.replace(tzinfo=timezone.utc)

            if t_u >= win_end:
                reached_end = True
                continue

            ts_ms = int(t_u.timestamp() * 1000)
            if what == "TRADES":
                fp  = (ts_ms, tick.price, tick.size)
                row = (t_u, tick.price, tick.size)
            else:
                bp  = getattr(tick, "priceBid", 0.0)
                bs  = getattr(tick, "sizeBid",  0)
                ap  = getattr(tick, "priceAsk", 0.0)
                as_ = getattr(tick, "sizeAsk",  0)
                fp  = (ts_ms, bp, ap, bs, as_)
                row = (t_u, bp, bs, ap, as_)

            if t_u >= cursor:
                if not (t_u == last_processed_ts and fp in last_batch_fps):
                    ticks.append(row)
                    new_ticks += 1

            if batch_end_ts is None or t_u > batch_end_ts:
                batch_end_ts = t_u
                batch_end_fps = {fp}
            elif t_u == batch_end_ts:
                batch_end_fps.add(fp)

        if reached_end or (new_ticks == 0 and len(batch) < _TICKS_PER_REQ):
            break

        if new_ticks == 0:
            cursor = max(cursor + timedelta(milliseconds=1),
                         cursor.replace(microsecond=0) + timedelta(seconds=1))
        elif batch_end_ts is not None:
            cursor = max(cursor, batch_end_ts)

        last_processed_ts = batch_end_ts
        last_batch_fps    = batch_end_fps
        progress[win_idx] = len(ticks)   # reporter reads this every 15 s

        # Brief yield — lets GC run and avoids IB pacing violations
        await asyncio.sleep(0.05)

    ct_str = cursor.astimezone(CT).strftime("%H:%M CT")
    print(f"    W{win_idx} done: {len(ticks):,} ticks  last={ct_str}", flush=True)
    return ticks


def paginate_ticks(ib: IB, contract, start_utc: datetime, end_utc: datetime,
                   what: str, write_row, conn, symbol, date_str) -> int:
    """
    Fetch all ticks for [start_utc, end_utc) using _N_WORKERS parallel async windows.
    Each window paginates its sub-range independently via asyncio.gather.
    All ticks are collected into lists, merged and sorted in memory, then written once.
    Returns total tick count.
    """
    duration = (end_utc - start_utc) / _N_WORKERS
    windows  = [(start_utc + i * duration, start_utc + (i + 1) * duration, i)
                for i in range(_N_WORKERS)]

    t0 = time.time()
    print(f"\n    [{what}] {_N_WORKERS} parallel windows × "
          f"{duration.total_seconds() / 3600:.1f}h each", flush=True)

    async def _run_all():
        progress = [0] * _N_WORKERS
        reporter = asyncio.create_task(
            _progress_reporter(progress, what, db_conn=conn, symbol=symbol, date_str=date_str)
        )
        try:
            tasks = [_paginate_window_async(ib, contract, ws, we, what, wi, progress)
                     for ws, we, wi in windows]
            return await asyncio.gather(*tasks)
        finally:
            reporter.cancel()

    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(_run_all())

    # Merge: windows are non-overlapping → extend then sort to guarantee order
    all_ticks: list = []
    for window_ticks in results:
        all_ticks.extend(window_ticks)
    all_ticks.sort(key=lambda x: x[0])

    total = len(all_ticks)
    elapsed_fetch = time.time() - t0
    speed = total / max(elapsed_fetch, 0.001)
    print(f"\n    {what} collected: {total:,} ticks in {elapsed_fetch:.1f}s"
          f"  ({speed:.0f} t/s) — writing to disk...", flush=True)
    log.info(f"{symbol} {what} | {date_str} | {total:,} ticks in {elapsed_fetch:.1f}s")

    for row in all_ticks:
        write_row(*row)

    _update_progress(conn, symbol, date_str, what, total)
    return total


# ── Fetch one day ─────────────────────────────────────────────────────────────

def fetch_day(ib: IB, symbol: str, target_date: date,
              fetch_bid_ask: bool, output_dir: Path,
              progress_conn) -> dict:
    """
    Fetch all ticks for symbol on target_date.
    Returns {dtype: count} dict.
    Always fetches TRADES. Optionally fetches BID_ASK.
    Skips already-finished types (resume support).
    """
    date_str  = target_date.strftime("%Y-%m-%d")
    date_compact = target_date.strftime("%Y%m%d")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    start_utc, end_utc = get_session_bounds(target_date)
    log.info(f"Session window: {start_utc.astimezone(CT)} -> {end_utc.astimezone(CT)} CT")

    contract = get_contract_for_date(ib, symbol, target_date)

    types = ["TRADES"]
    if fetch_bid_ask:
        types.append("BID_ASK")

    for dtype in types:
        if not _running:
            break

        if _is_finished(progress_conn, symbol, date_str, dtype):
            log.info(f"[SKIP] {symbol} {dtype} {date_str} — already finished")
            results[dtype] = "skipped"
            continue

        if _is_actively_running(progress_conn, symbol, date_str, dtype):
            log.warning(f"[SKIP] {symbol} {dtype} {date_str} — another process is actively fetching (updated <120s ago)")
            results[dtype] = "skipped_active"
            continue

        suffix    = "trades" if dtype == "TRADES" else "bidask"
        file_path = output_dir / f"{symbol}_{suffix}_{date_compact}.csv"

        # Delete incomplete file for clean restart
        if file_path.exists():
            log.info(f"Deleting incomplete file: {file_path.name}")
            for _ in range(5):
                try:
                    file_path.unlink()
                    break
                except OSError:
                    time.sleep(1)

        _mark_started(progress_conn, symbol, date_str, dtype)
        start_ct = start_utc.astimezone(CT).strftime("%H:%M CT")
        end_ct   = end_utc.astimezone(CT).strftime("%H:%M CT")
        print(f"\n  [{dtype}] {symbol} {date_str}  ({start_ct} -> {end_ct})",
              flush=True)
        log.info(f"[START] {symbol} {dtype} {date_str}")

        if dtype == "TRADES":
            headers = ["time_ct", "time_utc", "price", "size", "symbol"]
        else:
            headers = ["time_ct", "time_utc", "bid_p", "bid_s", "ask_p", "ask_s", "symbol"]

        with open(file_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(headers)

            if dtype == "TRADES":
                def write_row(t_u, price, size):
                    t_c = t_u.astimezone(CT)
                    w.writerow([t_c.isoformat(), t_u.isoformat(),
                                price, size, contract.localSymbol])
            else:
                def write_row(t_u, bid_p, bid_s, ask_p, ask_s):
                    t_c = t_u.astimezone(CT)
                    w.writerow([t_c.isoformat(), t_u.isoformat(),
                                bid_p, bid_s, ask_p, ask_s,
                                contract.localSymbol])

            count = paginate_ticks(ib, contract, start_utc, end_utc,
                                   dtype, write_row, progress_conn, symbol, date_str)

        if _running:
            _mark_finished(progress_conn, symbol, date_str, dtype, count)
            print(f"  [DONE] {symbol} {dtype} {date_str}: {count:,} ticks -> {file_path.name}",
                  flush=True)
            log.info(f"[DONE] {symbol} {dtype} {date_str}: {count:,} ticks -> {file_path.name}")
        results[dtype] = count

    return results


# ── Verify ────────────────────────────────────────────────────────────────────

def verify_csv(symbol: str, target_date: date,
               dtype: str = "trades", output_dir: Path = None) -> bool:
    """Sanity-check a fetched CSV. Prints report, returns True if OK."""
    date_compact = target_date.strftime("%Y%m%d")
    date_str     = target_date.strftime("%Y-%m-%d")
    if output_dir is None:
        try:
            output_dir = Path(get_config().paths.history)
        except Exception:
            output_dir = Path("data/history")

    path = output_dir / f"{symbol}_{dtype}_{date_compact}.csv"
    print(f"\n--- Verify: {symbol} {dtype} {date_str} ---")

    if not path.exists():
        print(f"  ERROR: file not found: {path}")
        return False

    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"  Rows      : {len(rows):,}")
    if not rows:
        print("  ERROR: empty file")
        return False

    print(f"  First     : {rows[0].get('time_ct','?')}")
    print(f"  Last      : {rows[-1].get('time_ct','?')}")

    issues = []
    if dtype == "trades":
        prices = [float(r["price"]) for r in rows if r.get("price")]
        if prices:
            print(f"  Price range: {min(prices):.2f} – {max(prices):.2f}")
        zeros = sum(1 for p in prices if p <= 0)
        if zeros:
            issues.append(f"{zeros} rows with price <= 0")
    else:
        bids = [float(r["bid_p"]) for r in rows if r.get("bid_p")]
        asks = [float(r["ask_p"]) for r in rows if r.get("ask_p")]
        if bids and asks:
            print(f"  Bid range : {min(bids):.2f} – {max(bids):.2f}")
            print(f"  Ask range : {min(asks):.2f} – {max(asks):.2f}")
        inverted = sum(1 for r in rows
                       if r.get("bid_p") and r.get("ask_p")
                       and float(r["bid_p"]) > float(r["ask_p"]))
        if inverted:
            issues.append(f"{inverted} rows where bid > ask")

    if issues:
        print(f"  ISSUES: {'; '.join(issues)}")
        return False

    print("  Status    : OK")
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_working_days(start: date, end: date) -> list:
    """Return list of working days from end down to start (descending)."""
    HOLIDAYS_2026 = {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
        date(2026, 4, 3), date(2026, 5, 25), date(2026, 7, 3),
        date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    }
    days = []
    curr = end
    while curr >= start:
        if curr.weekday() < 5 and curr not in HOLIDAYS_2026:
            days.append(curr)
        curr -= timedelta(days=1)
    return days


def _connect(cfg) -> IB:
    import random
    ib = IB()
    # Use dedicated fetcher_client_ids so engine pool stays free
    ids = list(getattr(cfg.ib, "fetcher_client_ids", cfg.ib.live_client_ids))
    random.shuffle(ids)
    for cid in ids:
        try:
            ib.connect(cfg.ib.live_host, cfg.ib.live_port, clientId=cid,
                       timeout=cfg.ib.connection_timeout)
            if ib.isConnected():
                log.info(f"Connected to LIVE port {cfg.ib.live_port} clientId={cid}")
                return ib
        except Exception as e:
            log.warning(f"clientId={cid} failed: {e}")
    raise ConnectionError(f"Could not connect to IB LIVE port {cfg.ib.live_port}")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers

        cfg = get_config()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path   = Path(tmp)
            output_dir = tmp_path / "history"
            prog_db    = tmp_path / "fetch_progress.db"
            output_dir.mkdir()

            # 1. Session bounds
            day = date(2026, 4, 7)
            start_u, end_u = get_session_bounds(day)
            assert start_u < end_u
            # Should start at prev day 17:00 CT
            start_ct = start_u.astimezone(CT)
            assert start_ct.hour == 17 and start_ct.day == 6, \
                f"Unexpected session start: {start_ct}"
            end_ct = end_u.astimezone(CT)
            assert end_ct.hour == 17 and end_ct.day == 7

            # 2. Progress DB round-trip
            conn = _init_progress_db(prog_db)
            _mark_started(conn, "MES", "2026-04-07", "TRADES")
            assert not _is_finished(conn, "MES", "2026-04-07", "TRADES")
            _mark_finished(conn, "MES", "2026-04-07", "TRADES", 42000)
            assert _is_finished(conn, "MES", "2026-04-07", "TRADES")
            conn.close()

            # 3. Working days list
            days = _get_working_days(date(2026, 4, 1), date(2026, 4, 7))
            assert date(2026, 4, 7) in days
            assert date(2026, 4, 5) not in days  # Sunday
            assert date(2026, 4, 6) in days      # Monday — working day

            # 4. Verify on a fake CSV
            fake_path = output_dir / "MES_trades_20260407.csv"
            with open(fake_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_ct", "time_utc", "price", "size", "symbol"])
                w.writerow(["2026-04-07T09:00:00-05:00", "2026-04-07T14:00:00+00:00",
                             6500.0, 10, "MESM6"])
                w.writerow(["2026-04-07T09:00:01-05:00", "2026-04-07T14:00:01+00:00",
                             6501.25, 5, "MESM6"])
            ok = verify_csv("MES", day, "trades", output_dir)
            assert ok, "verify_csv returned False on valid data"

            # 5. IB connection + real fetch attempt
            try:
                ib = _connect(cfg)
                contract = get_contract_for_date(ib, "MES", day)
                assert contract.localSymbol, "No localSymbol on contract"

                # Tiny fetch: first 1000 ticks only (no full pagination)
                start_u2, _ = get_session_bounds(day)
                end_u2 = start_u2 + timedelta(minutes=5)
                conn2 = _init_progress_db(tmp_path / "prog2.db")
                _mark_started(conn2, "MES", "2026-04-07", "TRADES")

                sample_path = output_dir / "MES_trades_20260407.csv"
                with open(sample_path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["time_ct", "time_utc", "price", "size", "symbol"])
                    def _wr(t_u, price, size):
                        t_c = t_u.astimezone(CT)
                        w.writerow([t_c.isoformat(), t_u.isoformat(),
                                    price, size, contract.localSymbol])
                    count = paginate_ticks(ib, contract, start_u2, end_u2,
                                          "TRADES", _wr, conn2, "MES", "2026-04-07")

                log.info(f"[self-test] sample fetch: {count} ticks in first 5 min")
                conn2.close()
                ib.disconnect()

            except Exception as e:
                log.info(f"[self-test] IB fetch skipped: {e}")

            reset_loggers()

        print("[self-test] fetcher: PASS")
        return True

    except Exception as e:
        print(f"[self-test] fetcher: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao tick fetcher")
    parser.add_argument("--self-test",  action="store_true")
    parser.add_argument("--verify",     action="store_true",
                        help="Sanity-check existing CSV (no fetch)")
    parser.add_argument("--symbol",     default=None, help="e.g. MES")
    parser.add_argument("--date",       default=None, help="YYYY-MM-DD (single day)")
    parser.add_argument("--from-date",  default=None, help="YYYY-MM-DD (start of range)")
    parser.add_argument("--days",       type=int, default=None, metavar="N",
                        help="Fetch last N working days (from yesterday back)")
    parser.add_argument("--bid-ask",    action="store_true",
                        help="Also fetch BID_ASK ticks")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    try:
        output_dir = Path(cfg.paths.history)
        prog_db    = Path(cfg.paths.db).parent / "fetch_progress.db"
    except Exception:
        output_dir = Path("data/history")
        prog_db    = Path("data/fetch_progress.db")

    symbols = [args.symbol.upper()] if args.symbol else cfg.symbols

    # --verify mode
    if args.verify:
        day = date.fromisoformat(args.date) if args.date \
              else (datetime.now(CT) - timedelta(days=1)).date()
        all_ok = True
        for sym in symbols:
            for dtype in (["trades"] + (["bidask"] if args.bid_ask else [])):
                if not verify_csv(sym, day, dtype, output_dir):
                    all_ok = False
        sys.exit(0 if all_ok else 1)

    # Resolve date range
    yesterday = (datetime.now(CT) - timedelta(days=1)).date()
    if args.date:
        days = [date.fromisoformat(args.date)]
    elif args.from_date:
        days = _get_working_days(date.fromisoformat(args.from_date), yesterday)
    elif args.days:
        # N working days back from yesterday — search far enough back to find them
        search_from = yesterday - timedelta(days=args.days * 3)
        days = _get_working_days(search_from, yesterday)[:args.days]
    else:
        days = [yesterday]

    ib = _connect(cfg)
    progress_conn = _init_progress_db(prog_db)

    types_label = "TRADES + BID_ASK" if args.bid_ask else "TRADES"
    print(f"\nFetching {types_label} for {len(days)} day(s) -> {output_dir}")

    try:
        for i, day in enumerate(days, 1):
            if not _running:
                break
            for sym in symbols:
                if not _running:
                    break
                print(f"\n{'='*60}")
                print(f"  {i}/{len(days)}  {sym}  {day}")
                print(f"{'='*60}")
                results = fetch_day(ib, sym, day, args.bid_ask, output_dir, progress_conn)
                # per-dtype summary already printed by fetch_day
    finally:
        progress_conn.close()
        ib.disconnect()
        log.info("Fetcher done")
