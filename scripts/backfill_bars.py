#!/usr/bin/env python3
"""
scripts/backfill_bars.py
Backfill OHLCV bars for MES, MNQ, MYM, M2K from IB into trader/data/bars.db.

Default bar size is 15 mins (table: bars_15m) -- aligned with Geva's AI-9 rule
("15-min bars to execute") and AI-35b (Spread's DIFF indicator is built on the
15-min chart), which this repo's bars_30m table did not match. lib/atr.py and
lib/spread_diff.py read bars_15m as of 2026-09-14; bars_30m is left in place
(correlation_lab.py's 7-year history still reads it) but no longer fed live
signals. --bar-size 30 is kept for that legacy table only.

Only ~60 days is fetched by default (15-min duration limits are tighter than
30-min, and every live consumer's lookback window is <=20 trading days --
a 1-year fetch was never needed here, just what earlier backfill_bars.py
attempts happened to ask for).

Run from the trader/ directory:
    python ../scripts/backfill_bars.py [--port 4001] [--symbols MES MNQ MYM M2K]
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
TRADER_DIR = SCRIPT_DIR.parent / "trader"
sys.path.insert(0, str(TRADER_DIR))

from ib_insync import IB, ContFuture

DB_PATH   = TRADER_DIR / "data" / "bars.db"
SYMBOLS   = ["MES", "MNQ", "MYM", "M2K"]
CLIENT_ID = 851   # dedicated — won't clash with broker (201+) or live (101+)
CURRENCY  = "USD"
EXCHANGE_MAP = {"MES": "CME", "MYM": "CBOT", "M2K": "CME", "MNQ": "CME"}


def table_for(bar_size: str) -> str:
    return "bars_15m" if bar_size == "15 mins" else "bars_30m"


def init_db(con: sqlite3.Connection, table: str) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol  TEXT NOT NULL,
            ts      TEXT NOT NULL,   -- ISO-8601 UTC
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  REAL,
            PRIMARY KEY (symbol, ts)
        )
    """)
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_sym_ts ON {table}(symbol, ts)")
    con.commit()


def existing_range(con: sqlite3.Connection, table: str, symbol: str) -> tuple[str | None, str | None]:
    row = con.execute(
        f"SELECT MIN(ts), MAX(ts) FROM {table} WHERE symbol=?", (symbol,)
    ).fetchone()
    return row[0], row[1]


def fetch_symbol(ib: IB, symbol: str, con: sqlite3.Connection,
                  bar_size: str, duration: str) -> int:
    table = table_for(bar_size)
    print(f"\n[{symbol}] Requesting {duration} of {bar_size} bars from IB...")
    exchange = EXCHANGE_MAP.get(symbol, "CME")
    contract = ContFuture(symbol=symbol, exchange=exchange, currency=CURRENCY)
    try:
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            print(f"[{symbol}] Could not qualify contract — skipping")
            return 0
        contract = qualified[0]
    except Exception as e:
        print(f"[{symbol}] qualifyContracts failed: {e} — using unqualified")

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=False,
        formatDate=1,
        timeout=90,
    )

    if not bars:
        print(f"[{symbol}] No bars returned — check IB connection and symbol")
        return 0

    rows = []
    for b in bars:
        # ib_insync returns b.date as a datetime object for intraday bars
        if isinstance(b.date, datetime):
            ts = b.date.astimezone(timezone.utc).isoformat()
        else:
            ts = str(b.date)
        rows.append((symbol, ts, b.open, b.high, b.low, b.close, b.volume))

    con.executemany(
        f"INSERT OR IGNORE INTO {table} (symbol, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()

    lo, hi = existing_range(con, table, symbol)
    total = con.execute(
        f"SELECT COUNT(*) FROM {table} WHERE symbol=?", (symbol,)
    ).fetchone()[0]
    print(f"[{symbol}] Inserted {len(rows)} bars  |  DB total: {total}  |  range: {lo[:10]} to {hi[:10]}")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill OHLCV bars from IB")
    parser.add_argument("--port",    type=int,   default=4001,     help="IB TWS/Gateway port (default 4001 LIVE)")
    parser.add_argument("--symbols", nargs="+",  default=SYMBOLS,  help="Symbols to fetch")
    parser.add_argument("--client-id", type=int, default=CLIENT_ID)
    parser.add_argument("--bar-size", choices=["15", "30"], default="15",
                         help="15 (default, table bars_15m) or 30 (legacy, table bars_30m)")
    parser.add_argument("--duration", default=None,
                         help="IB durationStr, e.g. '60 D'. Default: 60 D for 15-min, 1 Y for 30-min")
    args = parser.parse_args()

    bar_size = f"{args.bar_size} mins"
    duration = args.duration or ("60 D" if args.bar_size == "15" else "1 Y")
    table = table_for(bar_size)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    init_db(con, table)

    ib = IB()
    print(f"Connecting to IB port {args.port} clientId={args.client_id} (readonly)...")
    ib.connect("127.0.0.1", args.port, clientId=args.client_id, timeout=15, readonly=True)
    print("Connected.\n")

    total = 0
    errors = []
    for sym in args.symbols:
        try:
            total += fetch_symbol(ib, sym, con, bar_size, duration)
        except Exception as e:
            print(f"[{sym}] FATAL: {e}")
            errors.append(sym)

    ib.disconnect()
    con.close()

    print(f"\n{'='*50}")
    print(f"Done. Total bars inserted: {total}")
    if errors:
        print(f"Errors on: {', '.join(errors)}")


if __name__ == "__main__":
    main()
