#!/usr/bin/env python3
"""
scripts/import_7year_bars.py
One-off ingest: merges the 7-year 30-min bar history CSVs (data/bars_7years_30m_*.csv,
sourced from Databento) into trader/data/bars.db's bars_30m table, extending its
coverage from ~1 year back to 2019-05-05.

Uses INSERT OR IGNORE, not REPLACE: bars_30m's most recent rows come from live IB
backfill (scripts/backfill_bars.py) and may be fresher than the CSV snapshot (e.g.
MNQ in bars.db already reaches 2026-07-21T07:00, past the CSV's cutoff). This script
only fills in (symbol, ts) pairs that don't already exist, so it can never regress
already-live data -- it just adds 6+ years of history underneath it.

Run:
    python scripts/import_7year_bars.py
Then rebuild derived tables:
    python scripts/build_bars_normalized.py
    python scripts/build_bars_diffs.py
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bars_common import open_db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

CSV_FILES = {
    "MES": DATA_DIR / "bars_7years_30m_MES.csv",
    "MNQ": DATA_DIR / "bars_7years_30m_MNQ.csv",
    "MYM": DATA_DIR / "bars_7years_30m_MYM.csv",
    "M2K": DATA_DIR / "bars_7years_30m_M2K.csv",
}


def import_symbol(con, symbol: str, csv_path: Path) -> tuple:
    before = con.execute("SELECT COUNT(*) FROM bars_30m WHERE symbol=?", (symbol,)).fetchone()[0]

    rows = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append((
                r["symbol"], r["ts"],
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                float(r["volume"]),
            ))

    con.executemany(
        "INSERT OR IGNORE INTO bars_30m (symbol, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()

    after = con.execute("SELECT COUNT(*) FROM bars_30m WHERE symbol=?", (symbol,)).fetchone()[0]
    span = con.execute(
        "SELECT MIN(ts), MAX(ts) FROM bars_30m WHERE symbol=?", (symbol,)
    ).fetchone()
    return before, after, len(rows), span


def main():
    con = open_db()
    print(f"{'symbol':6} {'csv_rows':>9} {'before':>9} {'after':>9} {'added':>9}   span")
    for symbol, csv_path in CSV_FILES.items():
        if not csv_path.exists():
            print(f"[{symbol}] MISSING: {csv_path}")
            continue
        before, after, csv_rows, span = import_symbol(con, symbol, csv_path)
        added = after - before
        print(f"{symbol:6} {csv_rows:9d} {before:9d} {after:9d} {added:9d}   {span[0]} -> {span[1]}")
    con.close()


if __name__ == "__main__":
    main()
