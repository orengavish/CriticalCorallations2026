#!/usr/bin/env python3
"""
scripts/build_bars_normalized.py
Builds bars_30m_normalized from bars_30m: same shape, but every column is
min-max scaled to [0.0, 1.0] *per symbol*.

Normalization basis (per symbol):
  - open/high/low/close all share ONE basis: min(low) -> 0.0, max(high) -> 1.0
    over that symbol's full stored range in bars_30m. Sharing one basis
    across all four price columns preserves each bar's actual OHLC shape
    (e.g. close still sits between low_norm and high_norm proportionally).
  - volume gets its OWN basis: min(volume) -> 0.0, max(volume) -> 1.0. Volume
    isn't a price, so normalizing it against the price range would be
    meaningless -- it gets independently scaled.

The exact basis used (min_low, max_high, min_vol, max_vol per symbol) is
recorded in bars_30m_normalize_meta so a later sanity check can normalize a
fresh IB pull the *same way* and get a real apples-to-apples comparison,
rather than each script silently picking its own min/max.

Run:
    python scripts/build_bars_normalized.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bars_common import open_db, SYMBOLS


def create_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_30m_normalized (
            symbol      TEXT NOT NULL,
            ts          TEXT NOT NULL,
            open_norm   REAL,
            high_norm   REAL,
            low_norm    REAL,
            close_norm  REAL,
            volume_norm REAL,
            PRIMARY KEY (symbol, ts)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_norm_sym_ts ON bars_30m_normalized(symbol, ts)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_30m_normalize_meta (
            symbol    TEXT PRIMARY KEY,
            min_low   REAL NOT NULL,
            max_high  REAL NOT NULL,
            min_vol   REAL NOT NULL,
            max_vol   REAL NOT NULL,
            n_bars    INTEGER NOT NULL,
            built_at  TEXT NOT NULL
        )
    """)
    con.commit()


def build_symbol(con, symbol: str) -> int:
    stats = con.execute(
        "SELECT MIN(low) mn_low, MAX(high) mx_high, MIN(volume) mn_vol, MAX(volume) mx_vol, "
        "COUNT(*) n FROM bars_30m WHERE symbol=?", (symbol,)
    ).fetchone()
    if stats["n"] == 0:
        print(f"[{symbol}] no rows in bars_30m -- skipping")
        return 0

    mn_low, mx_high = stats["mn_low"], stats["mx_high"]
    mn_vol, mx_vol = stats["mn_vol"], stats["mx_vol"]
    price_range = (mx_high - mn_low) or 1.0   # guard div/0 on a degenerate range
    vol_range   = (mx_vol - mn_vol) or 1.0

    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM bars_30m WHERE symbol=? ORDER BY ts",
        (symbol,)
    ).fetchall()

    out = []
    for r in rows:
        out.append((
            symbol, r["ts"],
            (r["open"]  - mn_low) / price_range,
            (r["high"]  - mn_low) / price_range,
            (r["low"]   - mn_low) / price_range,
            (r["close"] - mn_low) / price_range,
            (r["volume"] - mn_vol) / vol_range,
        ))

    con.executemany(
        "INSERT OR REPLACE INTO bars_30m_normalized "
        "(symbol, ts, open_norm, high_norm, low_norm, close_norm, volume_norm) "
        "VALUES (?,?,?,?,?,?,?)",
        out,
    )
    con.execute(
        "INSERT OR REPLACE INTO bars_30m_normalize_meta "
        "(symbol, min_low, max_high, min_vol, max_vol, n_bars, built_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (symbol, mn_low, mx_high, mn_vol, mx_vol, len(out),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    print(f"[{symbol}] normalized {len(out)} bars  "
          f"| price basis [{mn_low}, {mx_high}]  | volume basis [{mn_vol}, {mx_vol}]")
    return len(out)


def main():
    con = open_db()
    create_tables(con)
    total = 0
    for sym in SYMBOLS:
        total += build_symbol(con, sym)
    print(f"\nDone. {total} rows in bars_30m_normalized across {len(SYMBOLS)} symbols.")
    con.close()


if __name__ == "__main__":
    main()
