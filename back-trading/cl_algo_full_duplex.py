"""
back-trading/cl_algo_full_duplex.py
Full-duplex CL Algo backtester.

Entry: algo-driven from critical line (support + 1 tick BUY, resistance - 1 tick SELL).
Exit:  nearest critical line in trade direction (resistance for BUY, support for SELL),
       within two_hour_avg_move range and at least 5 ticks from entry.
       Fallback: entry +/- two_hour_avg_move (exact, no tick buffer).
SL:    nearest critical line in opposite direction (no range cap).
       Fallback: entry +/- two_hour_avg_move (exact).

Generates 2 commands per armed line: LMT and STP. Both simulated against tick data.
All results traceable: which line is TP, which is SL, why the fallback was used.

Idempotent: UNIQUE(date, symbol, entry_line_price, direction, entry_type) + INSERT OR IGNORE.
Parallel-safe: WAL mode on shared DB.

Usage:
    python back-trading/cl_algo_full_duplex.py --symbol MES
    python back-trading/cl_algo_full_duplex.py --dry-run
    python back-trading/cl_algo_full_duplex.py --self-test
"""

import sys
import time
import importlib.util
import argparse
import tempfile
import csv
import math
from datetime import datetime, date as date_type, timedelta, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db          import get_db, init_db
from lib.data_availability import get_ready_days
from lib.day_params  import get_day_params, _MIN_EXIT_TICKS

CT  = __import__("zoneinfo").ZoneInfo("America/Chicago")
UTC = __import__("zoneinfo").ZoneInfo("UTC")

_TICK          = 0.25
_RTH_OPEN      = (8,  30)
_RTH_CLOSE     = (15, 15)


# ── Simulator loader ───────────────────────────────────────────────────────────

def _load_simulator():
    spec = importlib.util.spec_from_file_location(
        "simulator", Path(__file__).parent / "simulator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── CSV loaders (same as backtester) ──────────────────────────────────────────

def _load_trades_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists() or path.stat().st_size < 100:
        return None
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        ts_col    = "time_utc" if "time_utc" in df.columns else next(
            (c for c in df.columns if "time" in c), None)
        price_col = next((c for c in df.columns if "price" in c), None)
        if not ts_col or not price_col:
            return None
        df = df[[ts_col, price_col]].rename(
            columns={ts_col: "time_utc", price_col: "price"})
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df["price"]    = pd.to_numeric(df["price"], errors="coerce")
        return df.dropna().sort_values("time_utc").reset_index(drop=True)
    except Exception:
        return None


def _load_bidask_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists() or path.stat().st_size < 100:
        return None
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        ts_col  = "time_utc" if "time_utc" in df.columns else next(
            (c for c in df.columns if "time" in c), None)
        bid_col = next((c for c in df.columns if "bid_p" in c or c == "bid"), None)
        ask_col = next((c for c in df.columns if "ask_p" in c or c == "ask"), None)
        if not ts_col or not bid_col or not ask_col:
            return None
        df = df[[ts_col, bid_col, ask_col]].rename(
            columns={ts_col: "time_utc", bid_col: "bid_p", ask_col: "ask_p"})
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df["bid_p"]    = pd.to_numeric(df["bid_p"], errors="coerce")
        df["ask_p"]    = pd.to_numeric(df["ask_p"], errors="coerce")
        return df.dropna().sort_values("time_utc").reset_index(drop=True)
    except Exception:
        return None


# ── Session window ─────────────────────────────────────────────────────────────

def _session_window(trade_date: str) -> tuple[datetime, datetime]:
    d = date_type.fromisoformat(trade_date)
    open_ct  = datetime(d.year, d.month, d.day, *_RTH_OPEN,  0, tzinfo=CT)
    close_ct = datetime(d.year, d.month, d.day, *_RTH_CLOSE, 0, tzinfo=CT)
    return open_ct.astimezone(UTC), close_ct.astimezone(UTC)


# ── Entry simulation ───────────────────────────────────────────────────────────

def _simulate_entry(direction: str, entry_type: str, entry_price: float,
                    signal_time: datetime, session_end: datetime,
                    bidask_df: pd.DataFrame | None,
                    trades_df: pd.DataFrame) -> tuple[float | None, datetime | None]:
    if entry_type == "LMT":
        src = bidask_df
        if src is not None:
            w = src[(src["time_utc"] >= signal_time) & (src["time_utc"] < session_end)]
            hits = w[w["ask_p"] <= entry_price] if direction == "BUY" else w[w["bid_p"] >= entry_price]
            if not hits.empty:
                return entry_price, hits.iloc[0]["time_utc"].to_pydatetime()
        # Trades fallback
        w = trades_df[(trades_df["time_utc"] >= signal_time) & (trades_df["time_utc"] < session_end)]
        hits = w[w["price"] <= entry_price] if direction == "BUY" else w[w["price"] >= entry_price]
        if not hits.empty:
            return entry_price, hits.iloc[0]["time_utc"].to_pydatetime()
        return None, None
    else:  # STP
        w = trades_df[(trades_df["time_utc"] >= signal_time) & (trades_df["time_utc"] < session_end)]
        if direction == "BUY":
            hits = w[w["price"] >= entry_price]
            fill = entry_price + _TICK
        else:
            hits = w[w["price"] <= entry_price]
            fill = entry_price - _TICK
        if not hits.empty:
            return fill, hits.iloc[0]["time_utc"].to_pydatetime()
        return None, None


# ── Line finders ───────────────────────────────────────────────────────────────

def _rt(price: float) -> float:
    return round(round(price / _TICK) * _TICK, 10)


def _find_tp_line(entry_price: float, direction: str,
                  lines: list[dict], two_hour_avg_move: float) -> dict | None:
    """
    Find nearest critical line to use as TP.
    BUY: nearest RESISTANCE above entry, within two_hour_avg_move AND >= MIN_EXIT_TICKS.
    SELL: nearest SUPPORT below entry, same constraints.
    Returns the line dict, or None if no qualifying line found (use fallback).
    """
    min_dist = _MIN_EXIT_TICKS * _TICK
    if direction == "BUY":
        candidates = [l for l in lines
                      if l["line_type"] == "RESISTANCE"
                      and l["price"] >= entry_price + min_dist
                      and l["price"] <= entry_price + two_hour_avg_move]
        return min(candidates, key=lambda l: l["price"]) if candidates else None
    else:
        candidates = [l for l in lines
                      if l["line_type"] == "SUPPORT"
                      and l["price"] <= entry_price - min_dist
                      and l["price"] >= entry_price - two_hour_avg_move]
        return max(candidates, key=lambda l: l["price"]) if candidates else None


def _find_sl_line(line_price: float, direction: str, lines: list[dict]) -> dict | None:
    """
    Find nearest critical line to use as SL — no range cap.
    BUY: nearest SUPPORT strictly below the entry line.
    SELL: nearest RESISTANCE strictly above the entry line.
    """
    if direction == "BUY":
        candidates = [l for l in lines
                      if l["line_type"] == "SUPPORT" and l["price"] < line_price]
        return max(candidates, key=lambda l: l["price"]) if candidates else None
    else:
        candidates = [l for l in lines
                      if l["line_type"] == "RESISTANCE" and l["price"] > line_price]
        return min(candidates, key=lambda l: l["price"]) if candidates else None


# ── Main runner ────────────────────────────────────────────────────────────────

def run(db_path: Path, history_dir: Path,
        symbols: list[str] | None = None,
        dry_run: bool = False,
        verbose: bool = False) -> dict:
    """
    Simulate full-duplex commands for all ready (symbol, day) pairs.
    Returns {"written": N, "skipped": N, "errors": N, "elapsed_s": N}.
    """
    sim = _load_simulator()
    syms = symbols or ["MES", "MNQ", "MYM", "M2K"]

    ready_days = get_ready_days(db_path, history_dir, symbols=syms)
    if not ready_days:
        return {"written": 0, "skipped": 0, "errors": 0, "elapsed_s": 0.0}

    # Pre-load armed lines for all (symbol, date) pairs in ready_days
    needed = {(d["symbol"], d["date"]) for d in ready_days}
    lines_cache: dict[tuple, list[dict]] = {}
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT * FROM critical_lines WHERE armed=1"
        ).fetchall()
        for r in rows:
            key = (r["symbol"], r["date"])
            if key in needed:
                lines_cache.setdefault(key, []).append(dict(r))

    written = skipped = errors = 0
    t0 = time.monotonic()

    for day in ready_days:
        sym      = day["symbol"]
        date_str = day["date"]
        lines    = lines_cache.get((sym, date_str), [])
        if not lines:
            continue

        # Fetch done set for this (sym, date) — skip already-simulated rows
        with get_db(db_path) as con:
            done_rows = con.execute(
                "SELECT entry_line_price, direction, entry_type"
                " FROM cl_algo_fd_results WHERE symbol=? AND date=?",
                (sym, date_str)
            ).fetchall()
        done_set = {(r[0], r[1], r[2]) for r in done_rows}

        # Load day params (two_hour_avg_move, tick_buffer)
        params = get_day_params(db_path, sym, date_str, history_dir)
        avg_move    = params["two_hour_avg_move"]
        tick_buffer = params["tick_buffer"]
        buf_price   = _rt(tick_buffer * _TICK)

        # Load CSVs
        trades_df = _load_trades_csv(Path(day["trades_path"]))
        bidask_df = _load_bidask_csv(Path(day["bidask_path"]))
        if trades_df is None:
            skipped += len(lines) * 2
            continue

        signal_time, session_end = _session_window(date_str)
        t_session = trades_df[(trades_df["time_utc"] >= signal_time) &
                              (trades_df["time_utc"] < session_end)].reset_index(drop=True)
        b_session = None
        if bidask_df is not None:
            b_session = bidask_df[(bidask_df["time_utc"] >= signal_time) &
                                  (bidask_df["time_utc"] < session_end)].reset_index(drop=True)

        batch = []
        for line in lines:
            lp        = line["price"]
            line_type = line["line_type"]
            strength  = line["strength"]

            # Determine direction from line type
            direction = "BUY" if line_type == "SUPPORT" else "SELL"

            # Entry price: support + buf (BUY) or resistance - buf (SELL)
            entry_price = _rt(lp + buf_price) if direction == "BUY" else _rt(lp - buf_price)

            # Find TP line
            tp_line = _find_tp_line(entry_price, direction, lines, avg_move)
            if tp_line:
                tp_price  = (_rt(tp_line["price"] - buf_price) if direction == "BUY"
                             else _rt(tp_line["price"] + buf_price))
                tp_source = "critical_line"
                tp_line_p = tp_line["price"]
            else:
                tp_price  = (_rt(entry_price + avg_move) if direction == "BUY"
                             else _rt(entry_price - avg_move))
                tp_source = "2hr_avg_fallback"
                tp_line_p = None

            # Find SL line
            sl_line = _find_sl_line(lp, direction, lines)
            if sl_line:
                sl_price  = (_rt(sl_line["price"] - buf_price) if direction == "BUY"
                             else _rt(sl_line["price"] + buf_price))
                sl_source = "critical_line"
                sl_line_p = sl_line["price"]
            else:
                sl_price  = (_rt(entry_price - avg_move) if direction == "BUY"
                             else _rt(entry_price + avg_move))
                sl_source = "2hr_avg_fallback"
                sl_line_p = None

            if dry_run:
                written += 2
                continue

            for entry_type in ["LMT", "STP"]:
                if (lp, direction, entry_type) in done_set:
                    skipped += 1
                    continue

                fill_p, fill_t = _simulate_entry(
                    direction, entry_type, entry_price,
                    signal_time, session_end, b_session, t_session
                )

                if fill_p is None:
                    batch.append((date_str, sym,
                                  lp, line_type, strength,
                                  direction, entry_type, entry_price,
                                  tp_line_p, tp_price, tp_source,
                                  sl_line_p, sl_price, sl_source,
                                  avg_move, tick_buffer,
                                  None, None, "EXPIRED", None, None, None))
                    continue

                # TP/SL are anchored to line prices, not fill price — intentional for FD.
                # STP fills include 1-tick slippage; adjust TP/SL from fill_p for STP.
                if entry_type == "STP":
                    slip = _TICK if direction == "BUY" else -_TICK
                    adjusted_tp = _rt(tp_price + slip)
                    adjusted_sl = _rt(sl_price + slip)
                else:
                    adjusted_tp, adjusted_sl = tp_price, sl_price

                try:
                    result  = sim.simulate_exit(
                        fill_price=fill_p, fill_time=fill_t,
                        tp_price=adjusted_tp, sl_price=adjusted_sl,
                        direction=direction,
                        trades_df=t_session,
                        session_end_utc=session_end,
                    )
                    exit_r  = result["exit_type"]
                    exit_fp = result["exit_fill_price"]
                    pnl, ticks_ex = None, None
                    if exit_fp is not None:
                        diff = (exit_fp - fill_p) if direction == "BUY" else (fill_p - exit_fp)
                        pnl  = round(diff / _TICK, 4)
                        exit_t = result["exit_fill_time"]
                        if exit_t:
                            ticks_ex = len(t_session[
                                (t_session["time_utc"] > fill_t) &
                                (t_session["time_utc"] <= exit_t)
                            ])
                    batch.append((date_str, sym,
                                  lp, line_type, strength,
                                  direction, entry_type, entry_price,
                                  tp_line_p, tp_price, tp_source,
                                  sl_line_p, sl_price, sl_source,
                                  avg_move, tick_buffer,
                                  fill_p, fill_t.isoformat(),
                                  exit_r, exit_fp, pnl, ticks_ex))
                except Exception:
                    errors += 1
                    batch.append((date_str, sym,
                                  lp, line_type, strength,
                                  direction, entry_type, entry_price,
                                  tp_line_p, tp_price, tp_source,
                                  sl_line_p, sl_price, sl_source,
                                  avg_move, tick_buffer,
                                  fill_p, fill_t.isoformat(),
                                  "ERROR", None, None, None))

        if batch and not dry_run:
            with get_db(db_path) as con:
                con.executemany("""
                    INSERT OR IGNORE INTO cl_algo_fd_results
                        (date, symbol,
                         entry_line_price, entry_line_type, entry_line_strength,
                         direction, entry_type, entry_price,
                         tp_line_price, tp_price, tp_source,
                         sl_line_price, sl_price, sl_source,
                         two_hour_avg_move, tick_buffer,
                         entry_fill_price, entry_fill_time,
                         exit_reason, exit_fill_price, pnl_ticks, ticks_to_exit)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, batch)
            written += len(batch)
            if verbose:
                print(f"  [FD] {sym} {date_str}: {len(batch)} rows written")

    elapsed = round(time.monotonic() - t0, 1)
    return {"written": written, "skipped": skipped, "errors": errors, "elapsed_s": elapsed}


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    print("Running cl_algo_full_duplex self-test...")
    try:
        from zoneinfo import ZoneInfo
        _UTC = ZoneInfo("UTC")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p    = Path(tmp)
            hist_dir = tmp_p / "history"
            hist_dir.mkdir()
            db_path  = tmp_p / "galao.db"
            init_db(db_path)

            # Three lines: support 5490, support 5500, resistance 5510
            with get_db(db_path) as con:
                con.executemany(
                    "INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)"
                    " VALUES(?,?,?,?,?,?)", [
                        ("MES", "2026-07-04", "SUPPORT",    5490.0, 2, 1),
                        ("MES", "2026-07-04", "SUPPORT",    5500.0, 1, 1),
                        ("MES", "2026-07-04", "RESISTANCE", 5510.0, 1, 1),
                    ]
                )

            # Synthetic session: price moves 5495→5515 covering both lines
            base   = datetime(2026, 7, 4, 13, 30, 0, tzinfo=_UTC)  # 8:30 CT
            n_rows = 200
            prices = [round(5495.0 + 25.0 * math.sin(i / 35.0), 2) for i in range(n_rows)]

            t_path = hist_dir / "MES_trades_20260704.csv"
            b_path = hist_dir / "MES_bid_ask_20260704.csv"

            with open(t_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "price", "size"])
                for i, p in enumerate(prices):
                    w.writerow([(base + timedelta(seconds=i * 30)).isoformat(), p, 100])

            with open(b_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "bid_p", "bid_s", "ask_p", "ask_s"])
                for i, p in enumerate(prices):
                    w.writerow([(base + timedelta(seconds=i * 30)).isoformat(),
                                p - 0.25, 10, p + 0.25, 10])

            # Also write a prior-day CSV so day_params can compute avg_move
            prev_base  = datetime(2026, 7, 3, 13, 30, 0, tzinfo=_UTC)
            prev_prices = [round(5500.0 + 15.0 * math.sin(i / 30.0), 2) for i in range(n_rows)]
            with open(hist_dir / "MES_trades_20260703.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "price", "size"])
                for i, p in enumerate(prev_prices):
                    w.writerow([(prev_base + timedelta(seconds=i * 60)).isoformat(), p, 100])

            result = run(db_path, hist_dir, symbols=["MES"], verbose=False)
            assert result["written"] > 0,  f"Expected rows written, got {result}"
            assert result["errors"] == 0,  f"Unexpected errors: {result['errors']}"

            # Verify structure: 3 lines → up to 6 rows (LMT+STP per line)
            with get_db(db_path) as con:
                rows = con.execute("SELECT * FROM cl_algo_fd_results").fetchall()
            assert len(rows) > 0, "No fd_results rows"

            # Check traceability fields present
            r = dict(rows[0])
            assert "tp_source" in r and r["tp_source"] in ("critical_line", "2hr_avg_fallback")
            assert "sl_source" in r and r["sl_source"] in ("critical_line", "2hr_avg_fallback")
            assert r["tick_buffer"] == 1
            assert r["two_hour_avg_move"] > 0

            # Idempotency: re-run adds 0 rows
            result2 = run(db_path, hist_dir, symbols=["MES"])
            assert result2["written"] == 0, f"Re-run wrote {result2['written']} rows (not idempotent)"

            # Verify U-shape: support 5500 entry should be 5500.25, TP should be 5509.75
            support_lmt = next(
                (dict(r) for r in rows
                 if r["entry_line_price"] == 5500.0
                 and r["direction"] == "BUY"
                 and r["entry_type"] == "LMT"), None
            )
            if support_lmt:
                assert support_lmt["entry_price"] == 5500.25, \
                    f"Entry should be 5500.25, got {support_lmt['entry_price']}"
                if support_lmt["tp_source"] == "critical_line":
                    assert support_lmt["tp_price"] == 5509.75, \
                        f"TP should be 5509.75 (5510-1tick), got {support_lmt['tp_price']}"

        n = len(rows)
        print(f"PASS -- full_duplex: {result['written']} rows written, {n} total, idempotent re-run OK")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Full-Duplex CL Algo Backtester")
    parser.add_argument("--symbol",    nargs="*")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--verbose",   action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg      = get_config()
    db_path  = Path(cfg.paths.db)
    hist_dir = db_path.parent / "history"

    r = run(db_path, hist_dir, symbols=args.symbol,
            dry_run=args.dry_run, verbose=args.verbose)
    print(f"written={r['written']}  skipped={r['skipped']}"
          f"  errors={r['errors']}  elapsed={r['elapsed_s']}s")
