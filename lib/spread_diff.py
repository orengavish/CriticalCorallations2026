"""
lib/spread_diff.py
DIFF indicator construction and entry-trigger detection for the spread
algorithm (Galgo2029/knowledge/Claims/spread-diff-trading.md, AI-35-AI-35c).

DIFF = a dollar-normalized price differential between two symbols: each leg's
price scaled by its own $-per-point multiplier, then differenced (AI-35b:
"Chart 1 = that instrument's own multiplier; Chart 2 = the paired instrument's
chart + its multiplier"). Uses lib.algo_pnl.SYMBOL_MULTIPLIERS -- already the
correct per-point dollar values for THIS system's actual micro contracts
(MES/MNQ/MYM/M2K), not AI-35b's own worked-example numbers (NQ-20, RTY-150,
etc.), which are for the mini contracts (ES/NQ/YM/RTY) the lessons were taught
on -- a ~10x larger dollar value per point. The ratio math is the same either
way; the multiplier just needs to match what this system actually trades.

Computed fresh from trader/data/bars.db's `bars_30m` (all 4 symbols have full
raw coverage there -- the existing `bars_30m_diffs` table only covers 3 of the
6 possible pairs and isn't dollar-scaled, so this reads bars_30m directly
rather than depending on it).

Entry trigger (AI-35c), deliberately simplified for v1 -- both conditions
required together:
  1. The DIFF has moved >= GAP_MULTIPLIER x its own recent average daily swing
     since the prior session's close (the numeric half of AI-35c).
  2. The DIFF is at a new local extreme over the recent lookback window -- a
     stand-in for "a strong S/R line on the DIFF itself" (the discretionary
     half). A real DIFF-specific S/R-line detector is a larger follow-up, not
     this v1's scope.

Self-test:
    python -m lib.spread_diff --self-test
"""

import os
import sys
import argparse
import sqlite3
from datetime import timedelta

from lib.algo_pnl import SYMBOL_MULTIPLIERS

# All 6 pairs across this system's 4 traded symbols.
ALL_PAIRS = [
    ("MES", "MNQ"), ("MES", "MYM"), ("MES", "M2K"),
    ("MNQ", "MYM"), ("MNQ", "M2K"), ("MYM", "M2K"),
]

GAP_MULTIPLIER = 1.75          # midpoint of AI-35c's "1.5-2x" range
SWING_WINDOW_DAYS = 20         # trailing window for the average daily swing
EXTREME_LOOKBACK_BARS = 40     # ~20h of 30-min bars, for the "new local extreme" check


def _read_closes(bars_db_path, symbol: str, limit_bars: int = None) -> list:
    """[(ts, close), ...] ascending. Empty if missing -- same contract as
    lib/correlation_lab.py's _read_closes/lib/atr.py's _daily_ohlc."""
    if not bars_db_path or not os.path.exists(str(bars_db_path)):
        return []
    con = sqlite3.connect(f"file:{bars_db_path}?mode=ro", uri=True)
    try:
        if limit_bars:
            rows = con.execute(
                "SELECT ts, close FROM bars_30m WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol, limit_bars)
            ).fetchall()
            rows.reverse()
        else:
            rows = con.execute(
                "SELECT ts, close FROM bars_30m WHERE symbol=? ORDER BY ts", (symbol,)
            ).fetchall()
        return rows
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def diff_series(bars_db_path, sym_a: str, sym_b: str, limit_bars: int = None) -> list:
    """
    [(ts, diff), ...] ascending, aligned by exact timestamp. diff = price_a *
    mult_a - price_b * mult_b, in dollars. Empty if either symbol lacks data.
    """
    rows_a = _read_closes(bars_db_path, sym_a, limit_bars)
    rows_b = _read_closes(bars_db_path, sym_b, limit_bars)
    if not rows_a or not rows_b:
        return []
    mult_a = SYMBOL_MULTIPLIERS.get(sym_a, 1.0)
    mult_b = SYMBOL_MULTIPLIERS.get(sym_b, 1.0)
    map_b = dict(rows_b)
    out = []
    for ts, close_a in rows_a:
        close_b = map_b.get(ts)
        if close_b is None:
            continue
        out.append((ts, close_a * mult_a - close_b * mult_b))
    return out


def average_daily_swing(bars_db_path, sym_a: str, sym_b: str,
                        window_days: int = SWING_WINDOW_DAYS) -> float | None:
    """
    AI-35c: "average daily gap swing" -- mean of each day's (max diff - min
    diff) over the trailing window. None if there's not enough data.
    """
    series = diff_series(bars_db_path, sym_a, sym_b)
    if not series:
        return None

    by_day: dict = {}
    for ts, diff in series:
        day = ts[:10]
        by_day.setdefault(day, []).append(diff)

    days = sorted(by_day)[-window_days:]
    if len(days) < 2:
        return None

    swings = [max(by_day[d]) - min(by_day[d]) for d in days]
    return round(sum(swings) / len(swings), 2)


def check_spread_entry(bars_db_path, sym_a: str, sym_b: str,
                       gap_multiplier: float = GAP_MULTIPLIER,
                       lookback_bars: int = EXTREME_LOOKBACK_BARS) -> dict | None:
    """
    AI-35c's entry trigger. Returns a dict describing the signal if both
    conditions hold, else None.

    {"pair": (sym_a, sym_b), "diff": <current>, "swing": <today's gap>,
     "avg_swing": <trailing average>, "direction": "A_OVER" | "B_OVER"}

    direction: which leg moved MORE than its expected share -- AI-35a: the
    instrument that moved more -> LONG (expect it to correct back down, i.e.
    SHORT the differential); the one that moved less -> SHORT it. "A_OVER"
    means sym_a is the one that ran ahead (diff pushed toward its extreme in
    sym_a's favor) -- spread_manager.py turns this into the actual long/short
    leg assignment.
    """
    series = diff_series(bars_db_path, sym_a, sym_b, limit_bars=lookback_bars)
    if len(series) < 5:
        return None

    avg_swing = average_daily_swing(bars_db_path, sym_a, sym_b)
    if avg_swing is None or avg_swing == 0:
        return None

    today = series[-1][0][:10]
    today_diffs = [d for ts, d in series if ts[:10] == today]
    if len(today_diffs) < 2:
        return None

    session_open = today_diffs[0]
    current = today_diffs[-1]
    today_swing = max(today_diffs) - min(today_diffs)

    # Condition 1: gap has opened wide enough today.
    if today_swing < gap_multiplier * avg_swing:
        return None

    # Condition 2 (v1 stand-in for "at a strong DIFF S/R line"): current diff
    # is a new extreme over the lookback window, not merely mid-range noise.
    all_vals = [d for _, d in series]
    is_new_high = current >= max(all_vals)
    is_new_low = current <= min(all_vals)
    if not (is_new_high or is_new_low):
        return None

    direction = "A_OVER" if current > session_open else "B_OVER"
    return {
        "pair": (sym_a, sym_b),
        "diff": round(current, 2),
        "swing": round(today_swing, 2),
        "avg_swing": avg_swing,
        "direction": direction,
    }


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

            # 19 quiet days (small, constant swing) then 1 wide-gap day, for MES/MNQ.
            for day in range(20):
                d = f"2026-06-{day+1:02d}"
                is_last = (day == 19)
                mes_vals = [5500.0, 5505.0] if not is_last else [5500.0, 5560.0]  # wide move
                mnq_vals = [20000.0, 20000.0] if not is_last else [20000.0, 20000.0]  # MNQ flat
                for i, (mp, np_) in enumerate(zip(mes_vals, mnq_vals)):
                    ts = f"{d}T{14+i:02d}:00:00Z"
                    con.execute("INSERT INTO bars_30m VALUES ('MES', ?, ?,?,?,?,?)",
                               (ts, mp, mp, mp, mp, 100))
                    con.execute("INSERT INTO bars_30m VALUES ('MNQ', ?, ?,?,?,?,?)",
                               (ts, np_, np_, np_, np_, 100))
            con.commit()
            con.close()

            # diff_series: dollar-scaled, MES mult=5.0, MNQ mult=2.0
            series = diff_series(db_path, "MES", "MNQ")
            assert len(series) == 40, f"Expected 40 aligned points, got {len(series)}"
            first_diff = series[0][1]
            assert abs(first_diff - (5500.0 * 5.0 - 20000.0 * 2.0)) < 0.01

            # average_daily_swing: quiet days have swing = (5505-5500)*5 = 25 (MNQ flat, adds 0)
            avg = average_daily_swing(db_path, "MES", "MNQ", window_days=20)
            assert avg is not None
            # 19 quiet days at 25, 1 wide day at (5560-5500)*5=300 -- included in the trailing
            # average itself since window_days=20 covers all of it.
            expected_avg = (19 * 25.0 + 300.0) / 20
            assert abs(avg - expected_avg) < 1.0, f"Expected ~{expected_avg}, got {avg}"

            # check_spread_entry: the wide-gap day should trigger (big swing vs
            # average, AND a new extreme over the lookback window).
            signal = check_spread_entry(db_path, "MES", "MNQ")
            assert signal is not None, "Expected a signal on the wide-gap day"
            assert signal["pair"] == ("MES", "MNQ")
            assert signal["direction"] == "A_OVER", \
                f"MES ran ahead (diff went up) -- expected A_OVER, got {signal['direction']}"

            # A quiet pair (MYM/M2K, no wide day at all) must not trigger.
            for day in range(20):
                d = f"2026-06-{day+1:02d}"
                for i in range(2):
                    ts = f"{d}T{14+i:02d}:00:00Z"
                    con2 = sqlite3.connect(db_path)
                    con2.execute("INSERT INTO bars_30m VALUES ('MYM', ?, 42000,42000,42000,42000,100)", (ts,))
                    con2.execute("INSERT INTO bars_30m VALUES ('M2K', ?, 2200,2200,2200,2200,100)", (ts,))
                    con2.commit()
                    con2.close()
            no_signal = check_spread_entry(db_path, "MYM", "M2K")
            assert no_signal is None, "A flat pair with no gap must not trigger"

            # Missing symbol -> empty series, no crash.
            assert diff_series(db_path, "MES", "NOPE") == []
            assert check_spread_entry(db_path, "MES", "NOPE") is None

            # Nonexistent bars.db -> empty, not an exception.
            assert diff_series("Z:/nonexistent/bars.db", "MES", "MNQ") == []

        print("[self-test] spread_diff: PASS")
        return True

    except Exception as e:
        print(f"[self-test] spread_diff: FAIL — {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("spread_diff — run --self-test to verify logic")
