#!/usr/bin/env python3
"""
scripts/sanity_check_bars_diffs_normalized.py
Same 100 points as sanity_check_bars.py / sanity_check_bars_normalized.py
(same fixed seed). For each point and each of the 3 pairs, re-derives
close_norm_a - close_norm_b from a fresh IB pull (normalized with the exact
basis in bars_30m_normalize_meta, same as sanity_check_bars_normalized.py)
and compares against bars_30m_diffs_normalized.

Run (after build_bars_normalized.py and build_bars_diffs.py):
    python scripts/sanity_check_bars_diffs_normalized.py [--n 100]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bars_common import (
    open_db, pick_random_points, connect_ib, fetch_fresh_bars,
    close_enough, SYMBOLS, PAIRS, N_POINTS,
)


def main():
    parser = argparse.ArgumentParser(description="Sanity-check bars_30m_diffs_normalized against a fresh IB pull")
    parser.add_argument("--n", type=int, default=N_POINTS)
    parser.add_argument("--client-id", type=int, default=863)
    args = parser.parse_args()

    con = open_db()

    meta = {}
    for sym in SYMBOLS:
        row = con.execute("SELECT * FROM bars_30m_normalize_meta WHERE symbol=?", (sym,)).fetchone()
        if row is None:
            print(f"ERROR: no bars_30m_normalize_meta row for {sym} -- "
                  f"run scripts/build_bars_normalized.py first")
            return
        meta[sym] = row

    points = pick_random_points(con, SYMBOLS, args.n)
    print(f"Selected {len(points)} timestamps (seed=42, same set as the other "
          f"two sanity scripts): {points[0]} .. {points[-1]}")

    ib = connect_ib(args.client_id)
    fresh = {}
    try:
        for sym in SYMBOLS:
            print(f"\n[{sym}] fetching fresh 1Y of 30-min bars from IB...")
            fresh[sym] = fetch_fresh_bars(ib, sym)
            print(f"[{sym}] got {len(fresh[sym])} fresh bars")
    finally:
        ib.disconnect()

    def close_norm(sym, ts):
        bar = fresh.get(sym, {}).get(ts)
        if bar is None:
            return None
        m = meta[sym]
        price_range = (m["max_high"] - m["min_low"]) or 1.0
        return (bar.close - m["min_low"]) / price_range

    checked, matched, mismatches, missing = 0, 0, [], []

    for ts in points:
        for a, b in PAIRS:
            pair = f"{a}-{b}"
            db_row = con.execute(
                "SELECT * FROM bars_30m_diffs_normalized WHERE ts=? AND pair=?", (ts, pair)
            ).fetchone()
            ca, cb = close_norm(a, ts), close_norm(b, ts)

            if db_row is None or ca is None or cb is None:
                missing.append((pair, ts, "db" if db_row is None else "ib"))
                continue

            checked += 1
            fresh_diff = ca - cb
            db_diff = db_row["diff_norm"]
            if close_enough(db_diff, fresh_diff, abs_tol=0.001, rel_tol=1e-3):
                matched += 1
            else:
                mismatches.append((pair, ts, f"db={db_diff:.6f} fresh={fresh_diff:.6f}"))

    score = round(100 * matched / checked, 2) if checked else 0.0

    print(f"\n{'='*60}")
    print(f"Points requested: {len(points) * len(PAIRS)}  "
          f"(checked: {checked}, missing from one side: {len(missing)})")
    print(f"Matched exactly (within tolerance): {matched}/{checked}")
    print(f"SANITY SCORE: {score}/100")

    if missing:
        print(f"\n--- Missing ({len(missing)}) ---")
        for pair, ts, side in missing[:20]:
            print(f"  {pair} {ts}: absent from {side}")

    if mismatches:
        print(f"\n--- Mismatches ({len(mismatches)}) ---")
        for pair, ts, diff in mismatches[:20]:
            print(f"  {pair} {ts}: {diff}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches)-20} more")

    con.close()
    return score


if __name__ == "__main__":
    main()
