#!/usr/bin/env python3
"""
scripts/build_bars_diffs.py
Builds two tables, both with 3 rows per timestamp (one per symbol pair:
MES-MYM, MES-M2K, MYM-M2K), only at timestamps where all 3 symbols have data:

  bars_30m_diffs             close_a - close_b, RAW price units.
                              Note: not really a comparable number across
                              pairs (MES ~7000, MYM ~49000, M2K ~2600 --
                              subtracting raw closes mixes wildly different
                              scales), but this is literally "the diff of
                              the bars" as asked for -- bars_30m_diffs_normalized
                              below is the version that's actually meaningful
                              to compare pair-to-pair.

  bars_30m_diffs_normalized  close_norm_a - close_norm_b, from
                              bars_30m_normalized (each symbol already
                              scaled to its own [0,1] range) -- comparable
                              across pairs, in [-1, 1].

Run (after build_bars_normalized.py):
    python scripts/build_bars_diffs.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bars_common import open_db, PAIRS


def create_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_30m_diffs (
            ts       TEXT NOT NULL,
            pair     TEXT NOT NULL,
            sym_a    TEXT NOT NULL,
            sym_b    TEXT NOT NULL,
            close_a  REAL NOT NULL,
            close_b  REAL NOT NULL,
            diff     REAL NOT NULL,
            PRIMARY KEY (ts, pair)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_diffs_pair_ts ON bars_30m_diffs(pair, ts)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_30m_diffs_normalized (
            ts            TEXT NOT NULL,
            pair          TEXT NOT NULL,
            sym_a         TEXT NOT NULL,
            sym_b         TEXT NOT NULL,
            close_norm_a  REAL NOT NULL,
            close_norm_b  REAL NOT NULL,
            diff_norm     REAL NOT NULL,
            PRIMARY KEY (ts, pair)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_diffs_norm_pair_ts ON bars_30m_diffs_normalized(pair, ts)")
    con.commit()


def load_close(con, symbol: str) -> dict:
    rows = con.execute("SELECT ts, close FROM bars_30m WHERE symbol=?", (symbol,)).fetchall()
    return {r["ts"]: r["close"] for r in rows}


def load_close_norm(con, symbol: str) -> dict:
    rows = con.execute(
        "SELECT ts, close_norm FROM bars_30m_normalized WHERE symbol=?", (symbol,)
    ).fetchall()
    return {r["ts"]: r["close_norm"] for r in rows}


def build_raw_diffs(con) -> int:
    closes = {sym: load_close(con, sym) for sym in {s for pair in PAIRS for s in pair}}
    rows = []
    for a, b in PAIRS:
        common = sorted(set(closes[a]) & set(closes[b]))
        for ts in common:
            ca, cb = closes[a][ts], closes[b][ts]
            rows.append((ts, f"{a}-{b}", a, b, ca, cb, round(ca - cb, 6)))
    con.executemany(
        "INSERT OR REPLACE INTO bars_30m_diffs "
        "(ts, pair, sym_a, sym_b, close_a, close_b, diff) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    return len(rows)


def build_normalized_diffs(con) -> int:
    closes_norm = {sym: load_close_norm(con, sym) for sym in {s for pair in PAIRS for s in pair}}
    rows = []
    for a, b in PAIRS:
        common = sorted(set(closes_norm[a]) & set(closes_norm[b]))
        for ts in common:
            ca, cb = closes_norm[a][ts], closes_norm[b][ts]
            rows.append((ts, f"{a}-{b}", a, b, ca, cb, round(ca - cb, 6)))
    con.executemany(
        "INSERT OR REPLACE INTO bars_30m_diffs_normalized "
        "(ts, pair, sym_a, sym_b, close_norm_a, close_norm_b, diff_norm) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    return len(rows)


def main():
    con = open_db()
    create_tables(con)

    n_norm_meta = con.execute("SELECT COUNT(*) FROM bars_30m_normalized").fetchone()[0]
    if n_norm_meta == 0:
        print("bars_30m_normalized is empty -- run scripts/build_bars_normalized.py first")
        return

    n_raw = build_raw_diffs(con)
    print(f"bars_30m_diffs: {n_raw} rows ({n_raw // len(PAIRS)} timestamps x {len(PAIRS)} pairs)")

    n_normd = build_normalized_diffs(con)
    print(f"bars_30m_diffs_normalized: {n_normd} rows ({n_normd // len(PAIRS)} timestamps x {len(PAIRS)} pairs)")

    con.close()


if __name__ == "__main__":
    main()
