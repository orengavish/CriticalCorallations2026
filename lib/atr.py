"""
lib/atr.py
Average True Range (daily), computed from trader/data/bars.db's `bars_30m` table --
the only bar granularity this repo has (confirmed by repo-wide search: no ATR
calculation existed anywhere before this). Shared by the correlation algorithm
(laggard selection by $-distance-to-target) and the spread algorithm (dollar-ATR
position sizing).

bars_30m is stale in practice (backfill can lag by weeks) and this repo has no
finer granularity -- ATR here is therefore a recent-volatility ESTIMATE, not a
tick-fresh one. That's fine for both callers: neither needs minute-fresh ATR,
just a reasonable read on "how much does this symbol typically move in a day."

Usage:
    from lib.atr import atr20_points
    atr = atr20_points(bars_db_path, "MES")   # points, not $ -- multiply by
                                               # SYMBOL_MULTIPLIERS[symbol] for $

Self-test:
    python -m lib.atr --self-test
"""

import os
import sys
import argparse
import sqlite3


def _daily_ohlc(bars_db_path, symbol: str, lookback_days: int = None) -> list:
    """
    Aggregate bars_30m into daily (date, high, low, close), oldest first.
    Empty list if the symbol has no data or bars.db is missing.
    """
    if not bars_db_path or not os.path.exists(str(bars_db_path)):
        return []
    con = sqlite3.connect(f"file:{bars_db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("""
            SELECT substr(ts, 1, 10) AS d,
                   MAX(high) AS h, MIN(low) AS l,
                   MAX(ts) AS last_ts
            FROM bars_30m WHERE symbol=?
            GROUP BY d ORDER BY d
        """, (symbol,)).fetchall()
        if not rows:
            return []
        # close = the close of the last bar of each day -- needs a second pass
        # since SQLite has no simple "value at max(ts)" aggregate.
        closes = dict(con.execute(
            "SELECT ts, close FROM bars_30m WHERE symbol=?", (symbol,)
        ).fetchall())
        out = [{"date": d, "high": h, "low": l, "close": closes.get(last_ts)}
               for d, h, l, last_ts in rows]
        out = [d for d in out if d["close"] is not None]
        return out[-lookback_days:] if lookback_days else out
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def has_data(bars_db_path, symbol: str) -> bool:
    return bool(_daily_ohlc(bars_db_path, symbol, lookback_days=2))


def atr20_points(bars_db_path, symbol: str, window: int = 20) -> float | None:
    """
    Average True Range over the most recent `window` trading days, in points
    (not dollars -- multiply by a per-symbol $ multiplier for that).
    Returns None if there's fewer than 2 days of data (need a prior close for
    the first day's true range).
    """
    days = _daily_ohlc(bars_db_path, symbol, lookback_days=window + 1)
    if len(days) < 2:
        return None

    true_ranges = []
    for i in range(1, len(days)):
        h, l = days[i]["high"], days[i]["low"]
        prev_close = days[i - 1]["close"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        true_ranges.append(tr)

    if not true_ranges:
        return None
    return round(sum(true_ranges) / len(true_ranges), 4)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    from pathlib import Path
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bars_test.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE bars_30m (symbol TEXT, ts TEXT, "
                        "open REAL, high REAL, low REAL, close REAL, volume REAL)")

            # 21 days of MES bars: each day ranges [base, base+10] (constant
            # 10pt true range once rolling, since close sits mid-range and the
            # next day's high/low straddle it by 5 either way) -- easiest way
            # to build a KNOWN ATR to assert against exactly.
            base = 5500.0
            for day in range(21):
                d = f"2026-06-{day+1:02d}" if day < 30 else f"2026-07-{day-29:02d}"
                # two 30-min bars per day: one sets the low, one sets the high
                con.execute("INSERT INTO bars_30m VALUES ('MES', ?, ?,?,?,?,?)",
                           (f"{d}T14:00:00Z", base, base, base - 5, base, 100))
                con.execute("INSERT INTO bars_30m VALUES ('MES', ?, ?,?,?,?,?)",
                           (f"{d}T14:30:00Z", base, base + 5, base, base, 100))
                # close = base each day -> next day's TR = max(H-L, |H-pc|, |L-pc|)
                # = max(10, 5, 5) = 10 exactly
            con.commit()
            con.close()

            atr = atr20_points(db_path, "MES", window=20)
            assert atr is not None
            assert abs(atr - 10.0) < 0.01, f"Expected ATR=10.0, got {atr}"

            # Missing symbol -> None, not a crash
            assert not has_data(db_path, "MNQ")
            assert atr20_points(db_path, "MNQ") is None

            # Nonexistent bars.db -> None, not an exception
            assert atr20_points("Z:/nonexistent/bars.db", "MES") is None

            # Single day of data -> None (needs a prior close for true range)
            db2 = Path(tmp) / "bars_test2.db"
            con2 = sqlite3.connect(db2)
            con2.execute("CREATE TABLE bars_30m (symbol TEXT, ts TEXT, "
                         "open REAL, high REAL, low REAL, close REAL, volume REAL)")
            con2.execute("INSERT INTO bars_30m VALUES ('MES','2026-06-01T14:00:00Z',"
                        "5500,5505,5495,5500,100)")
            con2.commit()
            con2.close()
            assert atr20_points(db2, "MES") is None

        print("[self-test] atr: PASS")
        return True

    except Exception as e:
        print(f"[self-test] atr: FAIL — {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("atr — run --self-test to verify logic")
