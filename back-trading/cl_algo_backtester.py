"""
back-trading/cl_algo_backtester.py
CL Algo Backtest Engine.

For every ready (day, symbol) — days with TRADES + BID_ASK CSVs AND armed critical lines:
  For every Cartesian combo (algo_type × tp_ticks × sl_ticks × direction_filter × strength_max):
    Generate commands from critical lines (no DB insert)
    Simulate entry from BID_ASK (LMT) or TRADES (STP)
    Simulate exit via simulator.simulate_exit()
    INSERT OR IGNORE into cl_algo_sim_results

Fully resumable: UNIQUE constraint + INSERT OR IGNORE skip already-done rows.
Parallel-safe: symbol partitioning + WAL mode on the shared DB.

Usage:
    python back-trading/cl_algo_backtester.py                  # full run, all symbols
    python back-trading/cl_algo_backtester.py --symbol MES     # one symbol
    python back-trading/cl_algo_backtester.py --dry-run        # count only, no writes
    python back-trading/cl_algo_backtester.py --self-test
"""

import sys
import time
import json
import sqlite3
import argparse
import importlib.util
import tempfile
import os
import csv
from datetime import datetime, date as date_type, timedelta, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db, init_db
from lib.data_availability import get_ready_days
from lib.algo_engine import AlgoType, AlgoParams, ALGO_DESCRIPTIONS

CT  = __import__("zoneinfo").ZoneInfo("America/Chicago")
UTC = __import__("zoneinfo").ZoneInfo("UTC")

_TICK = 0.25
_RTH_OPEN  = (8,  30)   # CT hour, minute
_RTH_CLOSE = (15, 15)   # CT hour, minute

# Default coarse grid for the first exploration run
DEFAULT_TP_TICKS = [2, 4, 6, 8, 12]
DEFAULT_SL_TICKS = [2, 4, 6, 8, 12]
DEFAULT_ALGO_TYPES      = AlgoType.ALL
DEFAULT_DIRECTION_FILTERS = ["ALL", "BUY_ONLY", "SELL_ONLY"]
DEFAULT_STRENGTH_MAX    = [1, 2, 3]


# ── Simulator loader ──────────────────────────────────────────────────────────

def _load_simulator():
    spec = importlib.util.spec_from_file_location(
        "simulator", Path(__file__).parent / "simulator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── CSV loaders ───────────────────────────────────────────────────────────────

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
    """Load BID_ASK CSV. Expected columns: time_utc, bid_p, ask_p (+ optional others)."""
    if not path.exists() or path.stat().st_size < 100:
        return None
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        ts_col = "time_utc" if "time_utc" in df.columns else next(
            (c for c in df.columns if "time_utc" in c or c.endswith("_utc")), None)
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


# ── Entry simulation ──────────────────────────────────────────────────────────

def _session_window(trade_date: str) -> tuple[datetime, datetime]:
    """Return (RTH open UTC, RTH close UTC) for the given YYYY-MM-DD."""
    d = date_type.fromisoformat(trade_date)
    open_ct  = datetime(d.year, d.month, d.day, *_RTH_OPEN,  0, tzinfo=CT)
    close_ct = datetime(d.year, d.month, d.day, *_RTH_CLOSE, 0, tzinfo=CT)
    return open_ct.astimezone(UTC), close_ct.astimezone(UTC)


def _simulate_entry(direction: str, entry_type: str, entry_price: float,
                    signal_time: datetime, session_end: datetime,
                    bidask_df: pd.DataFrame | None,
                    trades_df: pd.DataFrame) -> tuple[float | None, datetime | None]:
    """
    Returns (fill_price, fill_time) or (None, None) if EXPIRED at entry.
    LMT: uses BID_ASK; falls back to TRADES if unavailable.
    STP: uses TRADES with 1-tick slippage.
    """
    if entry_type == "LMT":
        src = bidask_df if bidask_df is not None else None
        if src is not None:
            window = src[(src["time_utc"] >= signal_time) &
                         (src["time_utc"] < session_end)]
            if direction == "BUY":
                hits = window[window["ask_p"] <= entry_price]
            else:
                hits = window[window["bid_p"] >= entry_price]
            if not hits.empty:
                row = hits.iloc[0]
                return entry_price, row["time_utc"].to_pydatetime()
        # Fallback: TRADES touch
        window = trades_df[(trades_df["time_utc"] >= signal_time) &
                           (trades_df["time_utc"] < session_end)]
        if direction == "BUY":
            hits = window[window["price"] <= entry_price]
        else:
            hits = window[window["price"] >= entry_price]
        if not hits.empty:
            row = hits.iloc[0]
            return entry_price, row["time_utc"].to_pydatetime()
        return None, None

    else:  # STP
        window = trades_df[(trades_df["time_utc"] >= signal_time) &
                           (trades_df["time_utc"] < session_end)]
        if direction == "BUY":
            hits = window[window["price"] >= entry_price]
            fill = entry_price + _TICK
        else:
            hits = window[window["price"] <= entry_price]
            fill = entry_price - _TICK
        if not hits.empty:
            return fill, hits.iloc[0]["time_utc"].to_pydatetime()
        return None, None


# ── Combo builder ─────────────────────────────────────────────────────────────

def build_combos(tp_ticks: list[int] | None = None,
                 sl_ticks: list[int] | None = None,
                 algo_types: list[str] | None = None,
                 direction_filters: list[str] | None = None,
                 strength_max_vals: list[int] | None = None) -> list[dict]:
    """Return the full Cartesian product of combo params as a list of dicts."""
    tp_list  = tp_ticks or DEFAULT_TP_TICKS
    sl_list  = sl_ticks or DEFAULT_SL_TICKS
    at_list  = algo_types or DEFAULT_ALGO_TYPES
    df_list  = direction_filters or DEFAULT_DIRECTION_FILTERS
    sm_list  = strength_max_vals or DEFAULT_STRENGTH_MAX
    combos = []
    for at in at_list:
        for tp in tp_list:
            for sl in sl_list:
                for df in df_list:
                    for sm in sm_list:
                        combos.append({
                            "algo_type": at, "tp_ticks": tp, "sl_ticks": sl,
                            "direction_filter": df, "strength_max": sm,
                        })
    return combos


# ── Main runner ───────────────────────────────────────────────────────────────

def run(db_path: Path, history_dir: Path,
        symbols: list[str] | None = None,
        combos: list[dict] | None = None,
        dry_run: bool = False,
        verbose: bool = False,
        min_date: str | None = None,
        max_date: str | None = None,
        split: str = "train") -> dict:
    """
    Run the CL algo backtest for all ready (day, symbol) pairs.
    min_date/max_date scope the run to one train/validation/out_of_sample date
    range (see lib.data_availability.split_boundaries). split tags every row
    written with which split produced it.
    Returns summary dict.
    """
    sim = _load_simulator()
    init_db(db_path)

    syms = symbols or ["MES", "MNQ", "MYM", "M2K"]
    all_combos = combos or build_combos()
    ready_days = get_ready_days(db_path, history_dir, symbols=syms,
                                 min_date=min_date, max_date=max_date)

    if not ready_days:
        return {"ready_days": 0, "combos": len(all_combos), "written": 0,
                "skipped": 0, "errors": 0}

    if dry_run:
        # Estimate pending: total possible minus already done
        with get_db(db_path) as con:
            done = con.execute(
                "SELECT COUNT(*) FROM cl_algo_sim_results"
            ).fetchone()[0]
        # Each combo × each day × avg lines × 2 directions
        est_lines = sum(d["n_lines"] for d in ready_days)
        est_cmds  = sum(
            len(build_combos([c["tp_ticks"]], [c["sl_ticks"]],
                             [c["algo_type"]], [c["direction_filter"]],
                             [c["strength_max"]])) * d["n_lines"] * 2
            for c in all_combos for d in ready_days
        ) // len(all_combos)  # rough estimate
        return {"ready_days": len(ready_days), "combos": len(all_combos),
                "already_done": done, "dry_run": True}

    from lib.algo_engine import _build_cmds

    # Pre-load critical lines per (symbol, date) from DB
    lines_cache: dict[tuple, list[dict]] = {}
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT * FROM critical_lines WHERE armed=1 ORDER BY symbol, date, price"
        ).fetchall()
        for r in rows:
            key = (r["symbol"], r["date"])
            lines_cache.setdefault(key, []).append(dict(r))

    # CSV cache: (symbol, date) → (trades_df, bidask_df)
    csv_cache: dict[tuple, tuple] = {}

    written = skipped = errors = 0
    t0 = time.monotonic()

    for day in ready_days:
        sym      = day["symbol"]
        date_str = day["date"]
        lines    = lines_cache.get((sym, date_str), [])
        if not lines:
            continue

        cache_key = (sym, date_str)
        if cache_key not in csv_cache:
            t_df = _load_trades_csv(Path(day["trades_path"]))
            b_df = _load_bidask_csv(Path(day["bidask_path"]))
            csv_cache[cache_key] = (t_df, b_df)

        trades_df, bidask_df = csv_cache[cache_key]
        if trades_df is None:
            skipped += len(all_combos) * len(lines)
            continue

        signal_time, session_end = _session_window(date_str)

        # Trim CSVs to session window once
        t_session = trades_df[(trades_df["time_utc"] >= signal_time) &
                              (trades_df["time_utc"] < session_end)].reset_index(drop=True)
        b_session = None
        if bidask_df is not None:
            b_session = bidask_df[(bidask_df["time_utc"] >= signal_time) &
                                  (bidask_df["time_utc"] < session_end)].reset_index(drop=True)

        current_price = t_session.iloc[0]["price"] if not t_session.empty else lines[0]["price"]

        # ── OPTIMIZATION: pre-compute entry fills once per (line_price, direction, entry_type)
        # Entry price does NOT depend on tp/sl — same entry fill for all combos on same line+dir.
        entry_cache: dict[tuple, tuple] = {}  # (line_price, direction, entry_type) → (fill_p, fill_t, entry_p)
        for line in lines:
            lp = line["price"]
            for direction in ["BUY", "SELL"]:
                for entry_type in ["LMT", "STP"]:
                    if entry_type == "LMT":
                        ep = round(round(lp / _TICK) * _TICK, 10)
                    else:
                        ep = round(round((lp + _TICK if direction == "BUY" else lp - _TICK) / _TICK) * _TICK, 10)
                    fill_p, fill_t = _simulate_entry(
                        direction, entry_type, ep, signal_time, session_end,
                        b_session, t_session
                    )
                    entry_cache[(lp, direction, entry_type)] = (fill_p, fill_t, ep)

        # Fetch already-done unique keys for this (sym, date) to skip fast
        with get_db(db_path) as con:
            done_rows = con.execute(
                "SELECT algo_type, tp_ticks, sl_ticks, direction_filter,"
                " strength_max, line_price, direction"
                " FROM cl_algo_sim_results WHERE symbol=? AND date=?",
                (sym, date_str)
            ).fetchall()
        done_set = {(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in done_rows}

        batch_all = []
        for combo in all_combos:
            at      = combo["algo_type"]
            tp      = combo["tp_ticks"]
            sl_t    = combo["sl_ticks"]
            df_filt = combo["direction_filter"]
            sm      = combo["strength_max"]

            params = AlgoParams(algo_type=at, tp_ticks=tp, sl_ticks=sl_t,
                                direction_filter=df_filt, strength_max=sm)

            for line in lines:
                cmds = _build_cmds(line, params, current_price)
                for cmd in cmds:
                    key = (at, tp, sl_t, df_filt, sm, line["price"], cmd["direction"])
                    if key in done_set:
                        skipped += 1
                        continue

                    # Lookup pre-computed entry fill
                    fill_p, fill_t, entry_ep = entry_cache.get(
                        (line["price"], cmd["direction"], cmd["entry_type"]),
                        (None, None, cmd["entry_price"])
                    )

                    if fill_p is None:
                        batch_all.append((date_str, sym, at, tp, sl_t, df_filt, sm,
                                          line["price"], line["line_type"], line["strength"],
                                          cmd["direction"], cmd["entry_type"],
                                          cmd["entry_price"], cmd["tp_price"], cmd["sl_price"],
                                          None, None, "EXPIRED", None, None, None, split))
                        continue

                    # TP/SL relative to actual fill price (matters for STP where fill includes slippage)
                    if cmd["direction"] == "BUY":
                        tp_price = round(round((fill_p + tp   * _TICK) / _TICK) * _TICK, 10)
                        sl_price = round(round((fill_p - sl_t * _TICK) / _TICK) * _TICK, 10)
                    else:
                        tp_price = round(round((fill_p - tp   * _TICK) / _TICK) * _TICK, 10)
                        sl_price = round(round((fill_p + sl_t * _TICK) / _TICK) * _TICK, 10)

                    try:
                        result  = sim.simulate_exit(
                            fill_price=fill_p, fill_time=fill_t,
                            tp_price=tp_price, sl_price=sl_price,
                            direction=cmd["direction"],
                            trades_df=t_session,
                            session_end_utc=session_end,
                        )
                        exit_r  = result["exit_type"]
                        exit_fp = result["exit_fill_price"]
                        pnl, ticks_ex = None, None
                        if exit_fp is not None:
                            diff = (exit_fp - fill_p) if cmd["direction"] == "BUY" else (fill_p - exit_fp)
                            pnl  = round(diff / _TICK, 4)
                            exit_t = result["exit_fill_time"]
                            if exit_t:
                                ticks_ex = len(t_session[
                                    (t_session["time_utc"] > fill_t) &
                                    (t_session["time_utc"] <= exit_t)
                                ])
                        batch_all.append((date_str, sym, at, tp, sl_t, df_filt, sm,
                                          line["price"], line["line_type"], line["strength"],
                                          cmd["direction"], cmd["entry_type"],
                                          cmd["entry_price"], cmd["tp_price"], cmd["sl_price"],
                                          fill_p, fill_t.isoformat(),
                                          exit_r, exit_fp, pnl, ticks_ex, split))
                    except Exception:
                        errors += 1
                        batch_all.append((date_str, sym, at, tp, sl_t, df_filt, sm,
                                          line["price"], line["line_type"], line["strength"],
                                          cmd["direction"], cmd["entry_type"],
                                          cmd["entry_price"], cmd["tp_price"], cmd["sl_price"],
                                          fill_p, fill_t.isoformat(),
                                          "ERROR", None, None, None, split))

        # Flush entire day's batch at once (one DB write per day)
        if batch_all:
            with get_db(db_path) as con:
                con.executemany("""
                    INSERT OR IGNORE INTO cl_algo_sim_results
                        (date, symbol, algo_type, tp_ticks, sl_ticks,
                         direction_filter, strength_max,
                         line_price, line_type, line_strength,
                         direction, entry_type,
                         entry_price, tp_price, sl_price,
                         entry_fill_price, entry_fill_time,
                         exit_reason, exit_fill_price, pnl_ticks, ticks_to_exit,
                         split)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, batch_all)
            written += len(batch_all)
            if verbose:
                print(f"  {sym} {date_str}: {len(batch_all)} rows written")

    elapsed = round(time.monotonic() - t0, 1)
    return {"ready_days": len(ready_days), "combos": len(all_combos),
            "written": written, "skipped": skipped, "errors": errors,
            "elapsed_s": elapsed}


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    print("Running cl_algo_backtester self-test...")
    try:
        import sqlite3 as _sq

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p    = Path(tmp)
            hist_dir = tmp_p / "history"
            hist_dir.mkdir()
            db_path  = tmp_p / "galao.db"

            # 1. Seed DB: critical_lines for MES on 2026-06-30
            init_db(db_path)
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                    VALUES('MES','2026-06-30','SUPPORT',5500.0,1,1)
                """)
                con.execute("""
                    INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                    VALUES('MES','2026-06-30','RESISTANCE',5550.0,2,1)
                """)

            # 2. Write synthetic TRADES CSV: price oscillates around 5500-5550
            t_path = hist_dir / "MES_trades_20260630.csv"
            b_path = hist_dir / "MES_bid_ask_20260630.csv"

            base = datetime(2026, 6, 30, 13, 30, 0, tzinfo=UTC)  # 8:30 CT = 13:30 UTC
            # 200 ticks: price drifts down toward 5500 then back up past 5550
            import math
            prices = []
            for i in range(200):
                phase = i / 40.0  # slow oscillation
                prices.append(round(5525.0 + 30.0 * math.sin(phase), 2))

            with open(t_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "price", "size"])
                for i, p in enumerate(prices):
                    w.writerow([(base + timedelta(seconds=i*30)).isoformat(), p, 100])

            with open(b_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "bid_p", "bid_s", "ask_p", "ask_s"])
                for i, p in enumerate(prices):
                    w.writerow([(base + timedelta(seconds=i*30)).isoformat(),
                                p - 0.25, 10, p + 0.25, 10])

            # 3. Run with minimal combos: BOUNCE + BREAKOUT, tp=4 sl=4, ALL, strength 1-3
            combos = build_combos(
                tp_ticks=[4], sl_ticks=[4],
                algo_types=[AlgoType.BOUNCE, AlgoType.BREAKOUT],
                direction_filters=["ALL"],
                strength_max_vals=[3]
            )
            assert len(combos) == 2, f"Expected 2 combos, got {len(combos)}"

            result = run(db_path, hist_dir, symbols=["MES"], combos=combos)
            assert result["written"] > 0, f"Expected writes, got {result}"
            assert result["errors"] == 0, f"Errors: {result['errors']}"

            # 4. Re-run: all rows should be skipped (idempotent)
            result2 = run(db_path, hist_dir, symbols=["MES"], combos=combos)
            assert result2["written"] == 0, f"Re-run wrote {result2['written']}, expected 0"

            # 5. Check that some TP/SL exits exist (price did reach 5500 and 5550)
            with get_db(db_path) as con:
                tp_exits = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_sim_results WHERE exit_reason='TP'"
                ).fetchone()[0]
                sl_exits = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_sim_results WHERE exit_reason='SL'"
                ).fetchone()[0]
                total = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_sim_results"
                ).fetchone()[0]

            assert total > 0
            assert (tp_exits + sl_exits) > 0, "Expected some resolved exits"

            # 6. Dry-run doesn't write
            with get_db(db_path) as con:
                before = con.execute("SELECT COUNT(*) FROM cl_algo_sim_results").fetchone()[0]
            run(db_path, hist_dir, symbols=["MES"], combos=combos, dry_run=True)
            with get_db(db_path) as con:
                after = con.execute("SELECT COUNT(*) FROM cl_algo_sim_results").fetchone()[0]
            assert before == after, "dry_run wrote rows"

        print(f"PASS -- backtester: {total} rows, {tp_exits} TP + {sl_exits} SL exits")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CL Algo Backtester")
    parser.add_argument("--symbol", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--tp", nargs="*", type=int, default=None)
    parser.add_argument("--sl", nargs="*", type=int, default=None)
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg      = get_config()
    db_path  = Path(cfg.paths.db)
    hist_dir = db_path.parent / "history"

    combos = build_combos(
        tp_ticks=args.tp, sl_ticks=args.sl
    ) if (args.tp or args.sl) else None

    summary = run(db_path, hist_dir,
                  symbols=args.symbol,
                  combos=combos,
                  dry_run=args.dry_run,
                  verbose=args.verbose)
    print(summary)
