"""
lib/day_params.py
Computes and caches per-day parameters for the full-duplex CL algo system.

Key output: two_hour_avg_move — avg (max-min) across 2-hour session windows
from the prior trading day's TRADES CSV. Used as:
  - TP search range cap (max distance to look for an exit critical line)
  - Fallback TP/SL distance when no critical line is found in range

Usage:
    from lib.day_params import get_day_params
    p = get_day_params(db_path, "MES", "2026-07-04", history_dir)
    # p = {"two_hour_avg_move": 5.25, "source_date": "2026-07-03",
    #       "tick_buffer": 1, "from_cache": False}

Self-test:
    python -m lib.day_params --self-test
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta, date as date_type
from zoneinfo import ZoneInfo

_CT  = ZoneInfo("America/Chicago")
_UTC = ZoneInfo("UTC")

_TICK                       = 0.25
_SESSION_START_CT           = (8,  30)
_SESSION_END_CT             = (15, 15)
_WINDOW_HOURS               = 2
_DEFAULT_TICK_BUFFER        = 1       # ticks (configurable, start at 1)
_MIN_EXIT_TICKS             = 5       # ignore exit lines closer than this
_DEFAULT_TWO_HOUR_AVG       = 10.0    # fallback when no prior data (10 points)


def _date_from_filename(name: str) -> str | None:
    try:
        stem = Path(name).stem
        compact = stem.rsplit("_", 1)[1]
        if len(compact) != 8 or not compact.isdigit():
            return None
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    except Exception:
        return None


def _compute_two_hour_avg(trades_path: Path, date_str: str) -> float | None:
    """
    Compute avg(max-min) across 2-hour RTH windows from a TRADES CSV.
    Returns None if data is insufficient.
    """
    try:
        import pandas as pd
        df = pd.read_csv(trades_path)
        df.columns = [c.strip().lower() for c in df.columns]
        ts_col    = "time_utc" if "time_utc" in df.columns else next(
            (c for c in df.columns if "time" in c), None)
        price_col = next((c for c in df.columns if "price" in c), None)
        if not ts_col or not price_col:
            return None
        df = df[[ts_col, price_col]].rename(columns={ts_col: "time_utc", price_col: "price"})
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df["price"]    = pd.to_numeric(df["price"], errors="coerce")
        df = df.dropna()
        if df.empty:
            return None

        y, m, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:])
        session_start = datetime(y, m, d, *_SESSION_START_CT, 0, tzinfo=_CT).astimezone(_UTC)
        session_end   = datetime(y, m, d, *_SESSION_END_CT,   0, tzinfo=_CT).astimezone(_UTC)

        df = df[(df["time_utc"] >= session_start) & (df["time_utc"] < session_end)]
        if len(df) < 10:
            return None

        ranges = []
        current = session_start
        while current < session_end:
            window_end = min(current + timedelta(hours=_WINDOW_HOURS), session_end)
            w = df[(df["time_utc"] >= current) & (df["time_utc"] < window_end)]
            if len(w) >= 5:
                ranges.append(float(w["price"].max() - w["price"].min()))
            current = window_end

        return round(sum(ranges) / len(ranges), 4) if ranges else None
    except Exception:
        return None


def get_day_params(db_path: Path, symbol: str, date_str: str,
                   history_dir: Path,
                   tick_buffer: int = _DEFAULT_TICK_BUFFER) -> dict:
    """
    Return day params for (symbol, date). Reads from DB cache first;
    computes and caches if missing.

    Returns dict with keys:
        two_hour_avg_move  — float, price range cap
        source_date        — YYYY-MM-DD of prior day used, or 'default'
        tick_buffer        — int ticks (stored so each run is traceable)
        from_cache         — bool
    """
    from lib.db import get_db

    with get_db(db_path) as con:
        row = con.execute(
            "SELECT two_hour_avg_move, source_date, tick_buffer"
            " FROM cl_algo_day_params WHERE symbol=? AND date=?",
            (symbol, date_str)
        ).fetchone()
        if row:
            return {
                "two_hour_avg_move": row[0],
                "source_date":       row[1],
                "tick_buffer":       row[2],
                "from_cache":        True,
            }

    # Find most recent prior-day trades CSV for this symbol
    avg_move    = None
    source_date = None
    history_dir = Path(history_dir)

    if history_dir.exists():
        candidates = sorted(
            [f for f in history_dir.glob(f"{symbol}_trades_*.csv")
             if (_date_from_filename(f.name) or "") < date_str],
            reverse=True
        )
        for f in candidates:
            d = _date_from_filename(f.name)
            if d is None:
                continue
            result = _compute_two_hour_avg(f, d)
            if result and result > 0:
                avg_move    = result
                source_date = d
                break

    if avg_move is None:
        avg_move    = _DEFAULT_TWO_HOUR_AVG
        source_date = "default"

    with get_db(db_path) as con:
        con.execute("""
            INSERT OR REPLACE INTO cl_algo_day_params
                (symbol, date, two_hour_avg_move, source_date, tick_buffer)
            VALUES (?, ?, ?, ?, ?)
        """, (symbol, date_str, avg_move, source_date, tick_buffer))

    return {
        "two_hour_avg_move": avg_move,
        "source_date":       source_date,
        "tick_buffer":       tick_buffer,
        "from_cache":        False,
    }


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    import tempfile, csv, math
    from datetime import timezone
    print("Running day_params self-test...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p    = Path(tmp)
            hist_dir = tmp_p / "history"
            hist_dir.mkdir()

            from lib.db import init_db
            db_path = tmp_p / "galao.db"
            init_db(db_path)

            # Write a prior-day trades CSV (2026-07-03) with clear 2hr range
            base  = datetime(2026, 7, 3, 13, 30, 0, tzinfo=timezone.utc)  # 8:30 CT
            rows  = 200
            prices = [5500.0 + 20.0 * math.sin(i / 30.0) for i in range(rows)]
            p = hist_dir / "MES_trades_20260703.csv"
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "price", "size"])
                for i, pr in enumerate(prices):
                    w.writerow([(base + timedelta(seconds=i * 60)).isoformat(), round(pr, 2), 100])

            # Compute for 2026-07-04 → should use 2026-07-03 as source
            result = get_day_params(db_path, "MES", "2026-07-04", hist_dir)
            assert result["source_date"] == "2026-07-03", f"source_date: {result['source_date']}"
            assert result["two_hour_avg_move"] > 0,       "avg_move should be > 0"
            assert result["two_hour_avg_move"] <= 40.0,   f"avg_move implausibly large: {result['two_hour_avg_move']}"
            assert not result["from_cache"],               "should not be from cache on first call"

            # Second call: should hit cache
            result2 = get_day_params(db_path, "MES", "2026-07-04", hist_dir)
            assert result2["from_cache"],  "second call should be from cache"
            assert result2["two_hour_avg_move"] == result["two_hour_avg_move"]

            # No prior data → default fallback
            result3 = get_day_params(db_path, "MES", "2026-06-01", hist_dir)
            assert result3["source_date"] == "default",              "no prior data → 'default'"
            assert result3["two_hour_avg_move"] == _DEFAULT_TWO_HOUR_AVG

        print(f"PASS -- day_params: avg_move={result['two_hour_avg_move']:.2f}, "
              f"source={result['source_date']}, default fallback works")
        return True
    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--symbol",   default="MES")
    parser.add_argument("--date",     default=None)
    parser.add_argument("--hist",     default=None)
    parser.add_argument("--db",       default=None)
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg      = get_config()
    db_path  = Path(args.db)   if args.db   else Path(cfg.paths.db)
    hist_dir = Path(args.hist) if args.hist else db_path.parent / "history"
    date_str = args.date or date_type.today().isoformat()
    p        = get_day_params(db_path, args.symbol, date_str, hist_dir)
    print(f"{args.symbol} {date_str}: two_hour_avg_move={p['two_hour_avg_move']:.4f}"
          f"  source={p['source_date']}  cached={p['from_cache']}")
