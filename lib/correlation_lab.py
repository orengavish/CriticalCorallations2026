"""
lib/correlation_lab.py
Rolling pairwise correlation across the 4 traded micro futures (MES/MNQ/MYM/M2K)
-- built to give the "Correlation" dashboard tab something real to show. There
is currently no correlation-analysis code anywhere in this repo (grepped the
whole tree for "correlat" -- only hits are old planning-doc prose); S/R lines
are the only signal source today, and none of them account for how the 4
symbols move together or apart. This module doesn't trade anything -- it's a
read-only exploration tool over bars.db, meant to surface correlation ideas
that could later become a third algo type.

Uses trader/data/bars.db's `bars_30m` table (30-min OHLCV, built by
scripts/backfill_bars.py plus scripts/import_7year_bars.py -- see that script
for the Databento-sourced 7-year backfill merged in 2026-07-21, extending
coverage for all 4 symbols back to 2019-05-05). Correlation is computed on
log-returns of the close, not raw price or the existing bars_30m_normalized
table -- normalized-to-window-start price levels aren't comparable across
arbitrary correlation windows, but returns are.

Every function here still treats a symbol with no bars.db coverage as
"missing" and returns None/empty rather than raising (rather than assuming
all 4 symbols always have data), so the Correlation tab degrades gracefully
if a symbol's backfill ever falls behind instead of erroring the whole page
out.

Usage:
    from lib.correlation_lab import correlation_matrix, rolling_correlation_series
    matrix = correlation_matrix(bars_db_path, ["MES","MNQ","MYM","M2K"], window=50)
    series = rolling_correlation_series(bars_db_path, "MES", "MYM", window=50)

Self-test:
    python -m lib.correlation_lab --self-test
"""

import sys
import os
import argparse
import sqlite3
from itertools import combinations

import numpy as np


def _read_closes(bars_db_path, symbol: str, limit_bars: int = None) -> list:
    """Return [(ts, close), ...] ordered by ts ascending. Empty list if the
    symbol has no rows (e.g. MNQ, not yet backfilled) or bars.db is missing."""
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


def has_data(bars_db_path, symbol: str) -> bool:
    return bool(_read_closes(bars_db_path, symbol, limit_bars=2))


def _log_returns(closes: list) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    if len(arr) < 2:
        return np.array([])
    return np.diff(np.log(arr))


def _aligned_returns(bars_db_path, symbol_a: str, symbol_b: str, limit_bars: int = None):
    """Inner-join two symbols' bars by timestamp, return (ts_of_return, ret_a, ret_b).
    Empty arrays if either symbol has no data or too little overlap."""
    rows_a = _read_closes(bars_db_path, symbol_a, limit_bars)
    rows_b = _read_closes(bars_db_path, symbol_b, limit_bars)
    if not rows_a or not rows_b:
        return [], np.array([]), np.array([])

    map_a = dict(rows_a)
    map_b = dict(rows_b)
    common_ts = sorted(set(map_a) & set(map_b))
    if len(common_ts) < 3:
        return [], np.array([]), np.array([])

    ret_a = _log_returns([map_a[t] for t in common_ts])
    ret_b = _log_returns([map_b[t] for t in common_ts])
    ts_ret = common_ts[1:]
    return ts_ret, ret_a, ret_b


def pair_correlation(bars_db_path, symbol_a: str, symbol_b: str,
                     window: int = 50, limit_bars: int = None):
    """
    Single Pearson correlation over the most recent `window` aligned
    log-returns. Returns None if there isn't enough overlapping data.

    limit_bars, if given, is applied AFTER alignment (to the common-timestamp
    series), not per-symbol before it -- two symbols backfilled at different
    times (e.g. one refreshed today, one stale from last week) can have their
    most-recent N raw rows not overlap in time at all, which would silently
    yield "not enough data" even though there's plenty of overlapping history
    further back. Aligning on the full history first and trimming after is
    the only way to get this right regardless of per-symbol data freshness.
    """
    _ts, ret_a, ret_b = _aligned_returns(bars_db_path, symbol_a, symbol_b, limit_bars=None)
    if limit_bars:
        ret_a, ret_b = ret_a[-limit_bars:], ret_b[-limit_bars:]
    if len(ret_a) < window:
        return None
    a, b = ret_a[-window:], ret_b[-window:]
    if np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def correlation_matrix(bars_db_path, symbols: list, window: int = 50) -> dict:
    """
    Full pairwise correlation matrix for the given symbols.
    Returns {"symbols": [...], "window": window,
             "matrix": {sym: {sym2: corr_or_None}}, "missing": [symbols w/ no data]}
    """
    missing = [s for s in symbols if not has_data(bars_db_path, s)]
    matrix = {s: {s2: (1.0 if s == s2 else None) for s2 in symbols} for s in symbols}

    for a, b in combinations(symbols, 2):
        if a in missing or b in missing:
            continue
        corr = pair_correlation(bars_db_path, a, b, window)
        matrix[a][b] = corr
        matrix[b][a] = corr

    return {"symbols": symbols, "window": window, "matrix": matrix, "missing": missing}


def rolling_correlation_series(bars_db_path, symbol_a: str, symbol_b: str,
                               window: int = 50, max_points: int = 500) -> list:
    """
    Rolling correlation over time for one pair, for a "correlation over time"
    line chart. Returns [{"ts": ..., "corr": float}, ...], oldest first.
    Empty list if either symbol lacks data or there's not enough history.
    """
    ts_ret, ret_a, ret_b = _aligned_returns(bars_db_path, symbol_a, symbol_b)
    n = len(ret_a)
    if n < window:
        return []

    out = []
    for i in range(window, n + 1):
        a, b = ret_a[i - window:i], ret_b[i - window:i]
        corr = 0.0 if (np.std(a) == 0 or np.std(b) == 0) else float(np.corrcoef(a, b)[0, 1])
        out.append({"ts": ts_ret[i - 1], "corr": round(corr, 4)})

    return out[-max_points:] if len(out) > max_points else out


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    from pathlib import Path
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bars_test.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE bars_30m (symbol TEXT, ts TEXT, close REAL)")

            n = 120
            base = [6500.0 + 2.0 * np.sin(i / 5.0) + i * 0.1 for i in range(n)]
            ts = [f"2026-04-{(i // 20) + 1:02d}T{(i % 20):02d}:00:00Z" for i in range(n)]

            # MES: base series. MYM: same shape scaled x6.5 (perfectly correlated).
            # M2K: inverted shape (perfectly anti-correlated). MNQ: no rows at all.
            for i in range(n):
                con.execute("INSERT INTO bars_30m VALUES ('MES', ?, ?)", (ts[i], base[i]))
                con.execute("INSERT INTO bars_30m VALUES ('MYM', ?, ?)", (ts[i], base[i] * 6.5))
                con.execute("INSERT INTO bars_30m VALUES ('M2K', ?, ?)",
                           (ts[i], 3000.0 - (base[i] - base[0])))
            con.commit()
            con.close()

            # 1. Perfectly correlated pair -> ~1.0
            c_mes_mym = pair_correlation(db_path, "MES", "MYM", window=50)
            assert c_mes_mym is not None and c_mes_mym > 0.999, f"MES/MYM corr: {c_mes_mym}"

            # 2. Perfectly anti-correlated pair -> ~-1.0
            c_mes_m2k = pair_correlation(db_path, "MES", "M2K", window=50)
            assert c_mes_m2k is not None and c_mes_m2k < -0.999, f"MES/M2K corr: {c_mes_m2k}"

            # 3. Missing symbol handled gracefully, not raised
            assert not has_data(db_path, "MNQ")
            c_missing = pair_correlation(db_path, "MES", "MNQ", window=50)
            assert c_missing is None

            # 4. Not enough data for the window -> None, no crash
            c_short = pair_correlation(db_path, "MES", "MYM", window=10_000)
            assert c_short is None

            # 5. Full matrix: MNQ flagged missing, others populated, self=1.0
            matrix = correlation_matrix(db_path, ["MES", "MNQ", "MYM", "M2K"], window=50)
            assert matrix["missing"] == ["MNQ"]
            assert matrix["matrix"]["MES"]["MES"] == 1.0
            assert matrix["matrix"]["MES"]["MYM"] > 0.999
            assert matrix["matrix"]["MNQ"]["MES"] is None

            # 6. Nonexistent bars.db file -> empty results, not an exception
            assert correlation_matrix("Z:/nonexistent/bars.db", ["MES", "MYM"], 50)["missing"] == ["MES", "MYM"]

            # 7. Two symbols whose bars.db coverage ends on different dates
            # (e.g. one backfilled today, one stale from days ago) must still
            # correlate correctly over their overlapping history -- this is
            # the exact bug hit when MNQ was freshly backfilled while
            # MES/MYM/M2K were last refreshed earlier: naively taking each
            # symbol's own most-recent N rows before aligning can miss all
            # overlap entirely (if the newer-only rows outnumber the window)
            # and wrongly report "not enough data".
            con2 = sqlite3.connect(db_path)
            # NQF: same shape as base (perfectly correlated with MES/MYM), so a
            # correct alignment-first computation has real overlapping signal.
            for i in range(n):
                con2.execute("INSERT INTO bars_30m VALUES ('NQF', ?, ?)", (ts[i], base[i] * 4.0))
            # ...plus 80 *newer*, MES-less rows -- more than the window size,
            # so a naive "take my own last `window` rows, then align" approach
            # would land entirely in this non-overlapping region and find 0 overlap.
            extra_ts = [f"2026-05-{(k // 20) + 1:02d}T{(k % 20):02d}:00:00Z" for k in range(80)]
            for k, t in enumerate(extra_ts):
                con2.execute("INSERT INTO bars_30m VALUES ('NQF', ?, ?)", (t, 99999.0 + k))
            con2.commit()
            con2.close()
            c_stale_fresh = pair_correlation(db_path, "MES", "NQF", window=50)
            assert c_stale_fresh is not None and c_stale_fresh > 0.999, \
                f"Should find the real overlapping correlation despite NQF having 80 newer MES-less bars, got {c_stale_fresh}"

            # 8. Rolling series: right length, oldest-first, all in valid range
            series = rolling_correlation_series(db_path, "MES", "MYM", window=50)
            assert len(series) == (n - 1) - 50 + 1, f"series length: {len(series)}"
            assert all(-1.0001 <= p["corr"] <= 1.0001 for p in series)
            assert series[0]["ts"] < series[-1]["ts"]

        print("[self-test] correlation_lab: PASS")
        return True

    except Exception as e:
        print(f"[self-test] correlation_lab: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("correlation_lab — run --self-test to verify logic")
