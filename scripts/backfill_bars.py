#!/usr/bin/env python3
"""
scripts/backfill_bars.py
One-time backfill: fetch 1 year of 30-min OHLCV bars for MES, MYM, M2K from IB.
Saves to trader/data/bars.db  (table: bars_30m).

Run from the trader/ directory:
    python ../scripts/backfill_bars.py [--port 4001] [--symbols MES MYM M2K]
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
SYMBOLS   = ["MES", "MYM", "M2K"]
CLIENT_ID = 851   # dedicated — won't clash with broker (201+) or live (101+)
CURRENCY  = "USD"
EXCHANGE_MAP = {"MES": "CME", "MYM": "CBOT", "M2K": "CME", "MNQ": "CME"}


def init_db(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_30m (
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
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_sym_ts ON bars_30m(symbol, ts)")
    con.commit()


def existing_range(con: sqlite3.Connection, symbol: str) -> tuple[str | None, str | None]:
    row = con.execute(
        "SELECT MIN(ts), MAX(ts) FROM bars_30m WHERE symbol=?", (symbol,)
    ).fetchone()
    return row[0], row[1]


def fetch_symbol(ib: IB, symbol: str, con: sqlite3.Connection) -> int:
    print(f"\n[{symbol}] Requesting 1 Y of 30-min bars from IB...")
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
        durationStr="1 Y",
        barSizeSetting="30 mins",
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
        "INSERT OR IGNORE INTO bars_30m (symbol, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()

    lo, hi = existing_range(con, symbol)
    total = con.execute(
        "SELECT COUNT(*) FROM bars_30m WHERE symbol=?", (symbol,)
    ).fetchone()[0]
    print(f"[{symbol}] Inserted {len(rows)} bars  |  DB total: {total}  |  range: {lo[:10]} to {hi[:10]}")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill 30-min bars from IB")
    parser.add_argument("--port",    type=int,   default=4001,     help="IB TWS/Gateway port (default 4001 LIVE)")
    parser.add_argument("--symbols", nargs="+",  default=SYMBOLS,  help="Symbols to fetch")
    parser.add_argument("--client-id", type=int, default=CLIENT_ID)
    args = parser.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    init_db(con)

    ib = IB()
    print(f"Connecting to IB port {args.port} clientId={args.client_id} (readonly)...")
    ib.connect("127.0.0.1", args.port, clientId=args.client_id, timeout=15, readonly=True)
    print("Connected.\n")

    total = 0
    errors = []
    for sym in args.symbols:
        try:
            total += fetch_symbol(ib, sym, con)
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
