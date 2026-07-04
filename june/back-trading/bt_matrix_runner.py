"""
back-trading/bt_matrix_runner.py
Cartesian product matrix runner.

For each verified trade x each param_set:
  - Load tick CSV for (symbol, date) — cached in memory per session
  - Apply session_window + entry_delay + entry_offset adjustments
  - Call simulator.simulate_exit() with adjusted parameters
  - Write result to bt_matrix_results

The runner is fully resumable: skips (trade_id, param_set_id) pairs
already in bt_matrix_results.

Usage:
    python back-trading/bt_matrix_runner.py              # run all pending
    python back-trading/bt_matrix_runner.py --dry-run    # count pending only
    python back-trading/bt_matrix_runner.py --incremental MES 2026-06-30
    python back-trading/bt_matrix_runner.py --self-test
"""

import sys
import time
import sqlite3
import argparse
import tempfile
import os
from datetime import datetime, timedelta, timezone, date as date_type
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# Symbol point multipliers ($ per point)
MULTIPLIERS = {"MES": 5.0, "MNQ": 2.0, "MYM": 0.5, "M2K": 10.0}
_TICK = 0.25  # MES/MNQ/MYM/M2K all have 0.25-tick increments

# Session end times in CT (RTH close)
_SESSION_END_CT = {"hour": 15, "minute": 15}

# Session window CT boundaries (from bt_params.SESSION_WINDOWS)
_WIN_START = {"ALL": (8, 30), "MORNING": (8, 30), "MIDDAY": (11, 0), "AFTERNOON": (13, 30)}
_WIN_END   = {"ALL": (15, 15), "MORNING": (11, 0), "MIDDAY": (13, 30), "AFTERNOON": (15, 15)}


def _load_simulator():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "simulator", Path(__file__).parent / "simulator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bt_db():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bt_db", Path(__file__).parent / "bt_db.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bt_params():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bt_params", Path(__file__).parent / "bt_params.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── CSV loader ────────────────────────────────────────────────────────────────

def _load_trades_csv(csv_path: Path) -> pd.DataFrame | None:
    if not csv_path.exists() or csv_path.stat().st_size < 200:
        return None
    try:
        df = pd.read_csv(csv_path)
        df.columns = [c.strip().lower() for c in df.columns]
        # Prefer explicit time_utc column; fall back to first column containing "time"
        if "time_utc" in df.columns:
            ts_col = "time_utc"
        else:
            ts_col = next((c for c in df.columns if "time" in c), None)
        price_col = next((c for c in df.columns if "price" in c), None)
        if ts_col is None or price_col is None:
            return None
        # Keep only needed columns to avoid duplicate-rename collisions
        df = df[[ts_col, price_col]].rename(columns={ts_col: "time_utc", price_col: "price"})
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df = df.dropna(subset=["time_utc", "price"]).sort_values("time_utc").reset_index(drop=True)
        return df
    except Exception:
        return None


def _session_end_utc(trade_date: str) -> datetime:
    """Return session end (15:15 CT) as UTC datetime for the given date."""
    d = date_type.fromisoformat(trade_date)
    end_ct = datetime(d.year, d.month, d.day, 15, 15, 0, tzinfo=CT)
    return end_ct.astimezone(UTC)


def _apply_window(df: pd.DataFrame, window: str, trade_date: str) -> pd.DataFrame:
    """Filter trades_df to the session window time range."""
    if window == "ALL":
        return df
    d = date_type.fromisoformat(trade_date)
    sh, sm = _WIN_START[window]
    eh, em = _WIN_END[window]
    start_utc = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=CT).astimezone(UTC)
    end_utc   = datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=CT).astimezone(UTC)
    return df[(df["time_utc"] >= start_utc) & (df["time_utc"] < end_utc)]


# ── Verified trades reader ─────────────────────────────────────────────────────

def _get_verified_trades(galao_db: Path) -> list[dict]:
    """
    Read verified trades from completed_trades (or verified_trades VIEW if present).
    Returns list of dicts with keys: id, symbol, direction, fill_price, fill_time,
    exit_price, exit_time, exit_reason, pnl_points, bracket_size.
    """
    if not galao_db.exists():
        return []
    conn = sqlite3.connect(str(galao_db))
    conn.row_factory = sqlite3.Row
    try:
        # Try verified_trades VIEW first — order DESC so dates with CSVs run first
        try:
            rows = conn.execute(
                "SELECT id, symbol, direction, fill_price, fill_time, "
                "exit_price, exit_time, exit_reason, pnl_points, bracket_size "
                "FROM verified_trades ORDER BY fill_time DESC"
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT id, symbol, direction, fill_price, fill_time, "
                "exit_price, exit_time, exit_reason, pnl_points, bracket_size "
                "FROM completed_trades "
                "WHERE fill_price IS NOT NULL AND exit_price IS NOT NULL "
                "ORDER BY fill_time DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Main runner ───────────────────────────────────────────────────────────────

def run(bt_db_path: Path, galao_db: Path, history_dir: Path,
        symbol_filter: str | None = None,
        date_filter: str | None = None,
        dry_run: bool = False) -> dict:
    """
    Run the Cartesian matrix for all pending (trade, param_set) pairs.
    Returns summary dict.
    """
    sim = _load_simulator()
    bt_db_mod = _load_bt_db()
    bt_params_mod = _load_bt_params()

    bt_conn = bt_db_mod.init_bt_db(bt_db_path)
    trades  = _get_verified_trades(galao_db)

    if symbol_filter:
        trades = [t for t in trades if t["symbol"] == symbol_filter]
    if date_filter:
        trades = [t for t in trades if t["fill_time"][:10] == date_filter]

    if not trades:
        bt_conn.close()
        return {"trades": 0, "written": 0, "skipped": 0, "errors": 0}

    param_sets = bt_params_mod.get_active_param_sets(bt_conn)
    if not param_sets:
        bt_conn.close()
        return {"trades": len(trades), "written": 0, "skipped": 0, "errors": 0}

    # Count already-done pairs for progress
    already_done = bt_conn.execute(
        "SELECT COUNT(*) FROM bt_matrix_results"
    ).fetchone()[0]
    total_possible = len(trades) * len(param_sets)
    pending = total_possible - already_done

    if dry_run:
        bt_conn.close()
        return {"trades": len(trades), "param_sets": len(param_sets),
                "pending": pending, "already_done": already_done}

    # Cache of loaded DataFrames: (symbol, date) → df
    csv_cache: dict[tuple, pd.DataFrame | None] = {}

    written = skipped = errors = 0
    t0 = time.monotonic()

    for trade in trades:
        trade_id   = trade["id"]
        symbol     = trade["symbol"]
        fill_time  = datetime.fromisoformat(trade["fill_time"].replace("Z", "+00:00"))
        if fill_time.tzinfo is None:
            fill_time = fill_time.replace(tzinfo=UTC)
        fill_price = float(trade["fill_price"])
        direction  = trade["direction"]
        trade_date = fill_time.strftime("%Y-%m-%d")
        bracket_sz = float(trade.get("bracket_size") or 4.0)  # ticks default
        session_end = _session_end_utc(trade_date)

        # Load tick CSV (cached)
        cache_key = (symbol, trade_date)
        if cache_key not in csv_cache:
            date_compact = trade_date.replace("-", "")
            csv_path = history_dir / f"{symbol}_trades_{date_compact}.csv"
            csv_cache[cache_key] = _load_trades_csv(csv_path)

        trades_df = csv_cache[cache_key]
        if trades_df is None:
            # CSV not available yet — leave unprocessed so a future run picks it up
            skipped += len(param_sets)
            continue

        tick = _TICK

        # Get set of already-computed param_set_ids for this trade
        done_ps = {r[0] for r in bt_conn.execute(
            "SELECT param_set_id FROM bt_matrix_results WHERE trade_id=?",
            (trade_id,)
        ).fetchall()}

        batch = []
        for ps in param_sets:
            if isinstance(ps, dict):
                ps_id = ps["id"]
                tp_t, sl_t = ps["tp_ticks"], ps["sl_ticks"]
                delay_s    = ps["entry_delay_s"]
                offset_t   = ps["entry_offset_t"]
                confirm_t  = ps["tp_confirm_t"]
                window     = ps["session_window"]
            else:
                ps_id, tp_t, sl_t, delay_s, offset_t, confirm_t, window = \
                    ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6]

            if ps_id in done_ps:
                skipped += 1
                continue

            # Adjust fill time by entry_delay_s
            adj_fill_time = fill_time + timedelta(seconds=delay_s)
            if adj_fill_time >= session_end:
                batch.append((trade_id, ps_id, symbol, trade_date,
                              direction, "EXPIRED", None, None, None))
                continue

            # Adjust entry price by entry_offset_t (positive = worse fill for direction)
            if direction == "BUY":
                adj_fill_price = fill_price + offset_t * tick
                tp_price = adj_fill_price + tp_t * tick
                sl_price = adj_fill_price - sl_t * tick
            else:
                adj_fill_price = fill_price - offset_t * tick
                tp_price = adj_fill_price - tp_t * tick
                sl_price = adj_fill_price + sl_t * tick

            # Apply session window filter
            win_df = _apply_window(trades_df, window, trade_date)
            if win_df.empty:
                batch.append((trade_id, ps_id, symbol, trade_date,
                              direction, "EXPIRED", None, None, None))
                continue

            # Simulate exit
            try:
                ts_sim = time.monotonic()
                result = sim.simulate_exit(
                    fill_price=adj_fill_price,
                    fill_time=adj_fill_time,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    direction=direction,
                    trades_df=win_df,
                    session_end_utc=session_end,
                    tp_confirm_ticks=confirm_t,
                )
                ms = round((time.monotonic() - ts_sim) * 1000)

                exit_reason = result["exit_type"]
                pnl_ticks   = None
                ticks_exit  = None
                if result["exit_fill_price"] is not None:
                    diff = (result["exit_fill_price"] - adj_fill_price)
                    if direction != "BUY":
                        diff = -diff
                    pnl_ticks = round(diff / tick, 4)
                    # ticks consumed: rows between fill and exit
                    if result["exit_fill_time"] is not None:
                        ticks_exit = len(win_df[
                            (win_df["time_utc"] > adj_fill_time) &
                            (win_df["time_utc"] <= result["exit_fill_time"])
                        ])

                batch.append((trade_id, ps_id, symbol, trade_date,
                              direction, exit_reason, pnl_ticks, ticks_exit, ms))
            except Exception:
                errors += 1
                batch.append((trade_id, ps_id, symbol, trade_date,
                              direction, "ERROR", None, None, None))

        # Flush batch to DB
        if batch:
            bt_conn.executemany(
                "INSERT OR IGNORE INTO bt_matrix_results "
                "(trade_id, param_set_id, symbol, trade_date, direction, "
                "exit_reason, pnl_ticks, ticks_to_exit, ms_to_exit) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                batch
            )
            bt_conn.commit()
            written += len(batch)

    elapsed = round(time.monotonic() - t0, 1)
    bt_conn.close()
    return {
        "trades": len(trades),
        "param_sets": len(param_sets),
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "elapsed_s": elapsed,
    }


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    from datetime import timedelta

    print("Running bt_matrix_runner self-test...")
    sim = _load_simulator()
    bt_db_mod = _load_bt_db()
    bt_params_mod = _load_bt_params()

    tmp_bt = tempfile.mktemp(suffix="_bt.db")
    tmp_ga = tempfile.mktemp(suffix="_ga.db")
    tmp_dir = tempfile.mkdtemp()

    try:
        # ── 1. Setup bt.db ──────────────────────────────────────────────────
        bt_conn = bt_db_mod.init_bt_db(Path(tmp_bt))
        n_inserted = bt_params_mod.seed_param_sets(bt_conn)
        assert n_inserted == 10800, f"Expected 10800 param sets, got {n_inserted}"

        # ── 2. Setup galao.db with 3 synthetic completed_trades ─────────────
        ga_conn = sqlite3.connect(tmp_ga)
        ga_conn.execute("""
            CREATE TABLE completed_trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT, direction TEXT,
                fill_price REAL, fill_time TEXT,
                exit_price REAL, exit_time TEXT,
                exit_reason TEXT, pnl_points REAL, bracket_size REAL,
                entry_type TEXT, source TEXT
            )
        """)
        base_dt = datetime(2026, 6, 30, 14, 0, 0, tzinfo=UTC)
        trades_data = [
            (1, "MES", "BUY",  5600.0, (base_dt).isoformat(),
             5601.0, (base_dt + timedelta(minutes=5)).isoformat(), "TP",  5.0, 4.0),
            (2, "MES", "SELL", 5601.0, (base_dt + timedelta(minutes=10)).isoformat(),
             5600.0, (base_dt + timedelta(minutes=15)).isoformat(), "TP",  5.0, 4.0),
            (3, "MES", "BUY",  5599.0, (base_dt + timedelta(minutes=20)).isoformat(),
             5597.0, (base_dt + timedelta(minutes=25)).isoformat(), "SL", -10.0, 4.0),
        ]
        ga_conn.executemany(
            "INSERT INTO completed_trades VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
            trades_data
        )
        ga_conn.commit()
        ga_conn.close()

        # ── 3. Setup galao.db with fills INSIDE the CSV time range ──────────
        # Recalculate so fill times land within the CSV window below
        ga_conn2 = sqlite3.connect(tmp_ga)
        ga_conn2.execute("DELETE FROM completed_trades")
        # CSV will span 13:30–13:58 UTC; put fills at 13:32, 13:42, 13:47 UTC
        t1 = datetime(2026, 6, 30, 13, 32, 0, tzinfo=UTC)
        t2 = datetime(2026, 6, 30, 13, 42, 0, tzinfo=UTC)
        t3 = datetime(2026, 6, 30, 13, 47, 0, tzinfo=UTC)
        trades_data2 = [
            # BUY at 5599.0 → TP=5600.0 hit at 13:34
            (1, "MES", "BUY",  5599.0, t1.isoformat(),
             5600.0, (t1 + timedelta(minutes=2)).isoformat(), "TP",  5.0, 4.0),
            # SELL at 5601.0 → TP=5600.0 hit at 13:46
            (2, "MES", "SELL", 5601.0, t2.isoformat(),
             5600.0, (t2 + timedelta(minutes=4)).isoformat(), "TP",  5.0, 4.0),
            # BUY at 5599.0 → SL=5598.0 hit at 13:52
            (3, "MES", "BUY",  5599.0, t3.isoformat(),
             5597.0, (t3 + timedelta(minutes=5)).isoformat(), "SL", -10.0, 4.0),
        ]
        ga_conn2.executemany(
            "INSERT INTO completed_trades VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
            trades_data2
        )
        ga_conn2.commit()
        ga_conn2.close()

        # ── 4. Create synthetic TRADES CSV for 2026-06-30 ───────────────────
        # Spans 13:30–13:58 UTC (covers all fill times above)
        hist_dir = Path(tmp_dir)
        csv_path = hist_dir / "MES_trades_20260630.csv"
        rows_csv = []
        cur = datetime(2026, 6, 30, 13, 30, 0, tzinfo=UTC)
        prices = [5598.0, 5599.0, 5600.0, 5601.0, 5600.5, 5600.0,
                  5601.0, 5601.5, 5600.0, 5599.0, 5598.5, 5597.0,
                  5598.0, 5599.0, 5600.0]
        for i, p in enumerate(prices):
            rows_csv.append(f"{(cur + timedelta(minutes=i*2)).isoformat()},{p},10")
        csv_path.write_text("time_utc,price,size\n" + "\n".join(rows_csv))

        # ── 5. Run matrix runner ─────────────────────────────────────────────
        t0 = time.monotonic()
        summary = run(
            bt_db_path=Path(tmp_bt),
            galao_db=Path(tmp_ga),
            history_dir=hist_dir,
        )
        elapsed = time.monotonic() - t0

        expected_results = 3 * 10800
        total_in_db = bt_conn.execute(
            "SELECT COUNT(*) FROM bt_matrix_results"
        ).fetchone()[0]

        # Reload connection after run() closed it
        bt_conn2 = sqlite3.connect(tmp_bt)
        bt_conn2.row_factory = sqlite3.Row
        total_in_db = bt_conn2.execute(
            "SELECT COUNT(*) FROM bt_matrix_results"
        ).fetchone()[0]

        assert total_in_db == expected_results, \
            f"Expected {expected_results} results, got {total_in_db}"

        # Re-run should skip all (idempotent)
        summary2 = run(
            bt_db_path=Path(tmp_bt),
            galao_db=Path(tmp_ga),
            history_dir=hist_dir,
        )
        total_after_rerun = bt_conn2.execute(
            "SELECT COUNT(*) FROM bt_matrix_results"
        ).fetchone()[0]
        assert total_after_rerun == expected_results, "Re-run must not add rows"
        assert summary2["written"] == 0, "Re-run written must be 0"

        # Check some results are non-EXPIRED
        real_exits = bt_conn2.execute(
            "SELECT COUNT(*) FROM bt_matrix_results WHERE exit_reason IN ('TP','SL')"
        ).fetchone()[0]
        assert real_exits > 0, "Expected some TP/SL exits"

        bt_conn2.close()

        print(f"PASS -- 3 trades x 10,800 param sets = {total_in_db:,} results "
              f"in {elapsed:.1f}s ({real_exits:,} TP/SL exits)")
        return True

    except Exception as e:
        print(f"FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        for p in [tmp_bt, tmp_ga]:
            try: os.unlink(p)
            except Exception: pass
        try:
            import shutil; shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception: pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cartesian matrix runner")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--incremental", nargs=2, metavar=("SYMBOL", "DATE"),
                        help="Run only for SYMBOL DATE (YYYY-MM-DD)")
    parser.add_argument("--self-test",   action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    # Resolve real paths
    cfg_root = _ROOT / "trader"
    bt_db_path  = cfg_root / "data" / "bt.db"
    galao_db    = cfg_root / "data" / "galao.db"
    history_dir = cfg_root / "data" / "history"

    sym = args.incremental[0] if args.incremental else None
    dt  = args.incremental[1] if args.incremental else None

    summary = run(bt_db_path, galao_db, history_dir,
                  symbol_filter=sym, date_filter=dt, dry_run=args.dry_run)
    print(summary)
