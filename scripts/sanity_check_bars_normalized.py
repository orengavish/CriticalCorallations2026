#!/usr/bin/env python3
"""
scripts/sanity_check_bars_normalized.py
Same 100 points as sanity_check_bars.py (same fixed seed), but checks
bars_30m_normalized instead: re-fetches a fresh year of bars from IB,
normalizes it using the EXACT basis recorded in bars_30m_normalize_meta
(not a freshly-recomputed min/max -- that would silently compare against a
different basis and invalidate the check), and compares.

Run:
    python scripts/sanity_check_bars_normalized.py [--symbols MES MYM M2K] [--n 100]
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
    parser = argparse.ArgumentParser(description="Sanity-check bars_30m_normalized against a fresh IB pull")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS)
    parser.add_argument("--n", type=int, default=N_POINTS)
    parser.add_argument("--client-id", type=int, default=862)
    args = parser.parse_args()

    con = open_db()

    meta = {}
    for sym in args.symbols:
        row = con.execute(
            "SELECT * FROM bars_30m_normalize_meta WHERE symbol=?", (sym,)
        ).fetchone()
        if row is None:
            print(f"ERROR: no bars_30m_normalize_meta row for {sym} -- "
                  f"run scripts/build_bars_normalized.py first")
            return
        meta[sym] = row

    points = pick_random_points(con, args.symbols, args.n)
    print(f"Selected {len(points)} timestamps (seed=42, same set as "
          f"sanity_check_bars.py): {points[0]} .. {points[-1]}")

    ib = connect_ib(args.client_id)
    fresh = {}
    try:
        for sym in args.symbols:
            print(f"\n[{sym}] fetching fresh 1Y of 30-min bars from IB...")
            fresh[sym] = fetch_fresh_bars(ib, sym)
            print(f"[{sym}] got {len(fresh[sym])} fresh bars")
    finally:
        ib.disconnect()

    def normalize(sym, bar):
        m = meta[sym]
        price_range = (m["max_high"] - m["min_low"]) or 1.0
        vol_range   = (m["max_vol"]  - m["min_vol"])  or 1.0
        return {
            "open_norm":   (bar.open   - m["min_low"]) / price_range,
            "high_norm":   (bar.high   - m["min_low"]) / price_range,
            "low_norm":    (bar.low    - m["min_low"]) / price_range,
            "close_norm":  (bar.close  - m["min_low"]) / price_range,
            "volume_norm": (bar.volume - m["min_vol"]) / vol_range,
        }

    fields = ["open_norm", "high_norm", "low_norm", "close_norm", "volume_norm"]
    checked, matched, mismatches, missing = 0, 0, [], []

    for ts in points:
        for sym in args.symbols:
            db_row = con.execute(
                "SELECT * FROM bars_30m_normalized WHERE symbol=? AND ts=?", (sym, ts)
            ).fetchone()
            fresh_bar = fresh.get(sym, {}).get(ts)

            if db_row is None or fresh_bar is None:
                missing.append((sym, ts, "db" if db_row is None else "ib"))
                continue

            checked += 1
            fresh_norm = normalize(sym, fresh_bar)
            diffs = []
            # Normalized values are small ([0,1]) so use a tighter absolute
            # tolerance than the raw-price check -- 0.0005 is ~5 hundredths
            # of a percent of the full range, well below meaningful noise.
            for f in fields:
                db_val = db_row[f]
                fresh_val = fresh_norm[f]
                if not close_enough(db_val, fresh_val, abs_tol=0.0005, rel_tol=1e-3):
                    diffs.append(f"{f}: db={db_val:.6f} fresh={fresh_val:.6f}")

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
