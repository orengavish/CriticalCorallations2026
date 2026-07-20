#!/usr/bin/env python3
"""
scripts/sanity_check_bars.py
Picks 100 random (symbol, ts) points that exist for all of MES/MYM/M2K in
trader/data/bars.db, re-fetches a full fresh year of 30-min bars for each
symbol straight from IB, and compares the two at exactly those 100 points.

Run from anywhere (paths are self-contained):
    python scripts/sanity_check_bars.py [--symbols MES MYM M2K] [--n 100]

Score = % of the 100 points where open/high/low/close/volume all match the
fresh IB pull within tolerance. Expect close to 100 -- these are closed
historical bars, not live/moving data, so DB and a fresh pull should agree
unless something is actually wrong (corruption, wrong contract month, a
stale/partial write, etc).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bars_common import (
    open_db, pick_random_points, connect_ib, fetch_fresh_bars,
    close_enough, SYMBOLS, N_POINTS,
)


def main():
    parser = argparse.ArgumentParser(description="Sanity-check bars_30m against a fresh IB pull")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS)
    parser.add_argument("--n", type=int, default=N_POINTS)
    parser.add_argument("--client-id", type=int, default=861)
    args = parser.parse_args()

    con = open_db()
    points = pick_random_points(con, args.symbols, args.n)
    print(f"Selected {len(points)} timestamps common to {args.symbols} "
          f"(seed=42, so reproducible): {points[0]} .. {points[-1]}")

    ib = connect_ib(args.client_id)
    fresh = {}
    try:
        for sym in args.symbols:
            print(f"\n[{sym}] fetching fresh 1Y of 30-min bars from IB...")
            fresh[sym] = fetch_fresh_bars(ib, sym)
            print(f"[{sym}] got {len(fresh[sym])} fresh bars")
    finally:
        ib.disconnect()

    fields = ["open", "high", "low", "close", "volume"]
    checked, matched, mismatches, missing = 0, 0, [], []

    for ts in points:
        for sym in args.symbols:
            db_row = con.execute(
                "SELECT * FROM bars_30m WHERE symbol=? AND ts=?", (sym, ts)
            ).fetchone()
            fresh_bar = fresh.get(sym, {}).get(ts)

            if db_row is None or fresh_bar is None:
                missing.append((sym, ts, "db" if db_row is None else "ib"))
                continue

            checked += 1
            diffs = []
            for f in fields:
                db_val = db_row[f]
                ib_val = getattr(fresh_bar, f)
                if not close_enough(db_val, ib_val):
                    diffs.append(f"{f}: db={db_val} ib={ib_val}")

            if diffs:
                mismatches.append((sym, ts, diffs))
            else:
                matched += 1

    score = round(100 * matched / checked, 2) if checked else 0.0

    print(f"\n{'='*60}")
    print(f"Points requested: {len(points) * len(args.symbols)}  "
          f"(checked: {checked}, missing from one side: {len(missing)})")
    print(f"Matched exactly (within tolerance): {matched}/{checked}")
    print(f"SANITY SCORE: {score}/100")

    if missing:
        print(f"\n--- Missing ({len(missing)}) ---")
        for sym, ts, side in missing[:20]:
            print(f"  {sym} {ts}: absent from {side}")
        if len(missing) > 20:
            print(f"  ... and {len(missing)-20} more")

    if mismatches:
        print(f"\n--- Mismatches ({len(mismatches)}) ---")
        for sym, ts, diffs in mismatches[:20]:
            print(f"  {sym} {ts}: {'; '.join(diffs)}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches)-20} more")

    con.close()
    return score


if __name__ == "__main__":
    main()
