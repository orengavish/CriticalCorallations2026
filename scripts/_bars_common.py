"""
scripts/_bars_common.py
Shared helpers for the bars.db build/sanity-check script family:
  - build_bars_normalized.py   -> bars_30m_normalized
  - build_bars_diffs.py        -> bars_30m_diffs, bars_30m_diffs_normalized
  - sanity_check_bars.py
  - sanity_check_bars_normalized.py
  - sanity_check_bars_diffs_normalized.py

Not a standalone script -- imported by the others.
"""

import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TRADER_DIR = SCRIPT_DIR.parent / "trader"
DB_PATH    = TRADER_DIR / "data" / "bars.db"

SYMBOLS      = ["MES", "MYM", "M2K"]
EXCHANGE_MAP = {"MES": "CME", "MYM": "CBOT", "M2K": "CME", "MNQ": "CME"}
CURRENCY     = "USD"

# Only port 4002 is reachable in this environment (single paper gateway
# serves both LIVE-data and PAPER-order roles -- see trader/config.yaml).
# backfill_bars.py's own --port default of 4001 is not reachable here.
IB_PORT = 4002

# Fixed seed so every sanity script that calls pick_random_points() with the
# same (symbols, n) gets the literal same 100 points -- "same 100 points"
# across the raw/normalized/diffs-normalized sanity checks is a deliberate
# requirement, not a coincidence of luck.
RANDOM_SEED = 42
N_POINTS    = 100

PAIRS = [("MES", "MYM"), ("MES", "M2K"), ("MYM", "M2K")]


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def pick_random_points(con: sqlite3.Connection, symbols=SYMBOLS,
                       n: int = N_POINTS, seed: int = RANDOM_SEED) -> list[str]:
    """
    Deterministically pick n timestamps that exist in bars_30m for ALL given
    symbols (so any cross-symbol point, e.g. a diff row, always has a full
    row to compare). Same seed -> same points every call.
    """
    placeholders = ",".join("?" * len(symbols))
    rows = con.execute(f"""
        SELECT ts FROM bars_30m WHERE symbol IN ({placeholders})
        GROUP BY ts HAVING COUNT(DISTINCT symbol) = ?
        ORDER BY ts
    """, (*symbols, len(symbols))).fetchall()
    common_ts = [r["ts"] for r in rows]
    if len(common_ts) < n:
        raise ValueError(f"Only {len(common_ts)} timestamps common to all of {symbols}, need {n}")
    rng = random.Random(seed)
    return sorted(rng.sample(common_ts, n))


def connect_ib(client_id: int, port: int = IB_PORT):
    from ib_insync import IB
    ib = IB()
    print(f"Connecting to IB port {port} clientId={client_id} (readonly)...")
    ib.connect("127.0.0.1", port, clientId=client_id, timeout=15, readonly=True)
    print("Connected.")
    return ib


def fetch_fresh_bars(ib, symbol: str, timeout: int = 90, retries: int = 3) -> dict:
    """
    Fetch a fresh 1-year 30-min bar series for `symbol` straight from IB --
    same request shape as scripts/backfill_bars.py, so the comparison is
    apples-to-apples against how bars_30m itself was populated.
    Returns {ts_iso: BarData}.

    IB's historical-data pacing limit intermittently times out one of the
    3 sequential per-symbol requests in a run (observed on both the 2nd and
    3rd symbol across repeated runs -- not a fixed position). Retries with
    growing backoff instead of a single flat pre-sleep, since a flat sleep
    alone did not fully eliminate the timeouts.
    """
    from ib_insync import ContFuture
    exchange = EXCHANGE_MAP.get(symbol, "CME")
    contract = ContFuture(symbol=symbol, exchange=exchange, currency=CURRENCY)
    qualified = ib.qualifyContracts(contract)
    if qualified:
        contract = qualified[0]

    bars = []
    for attempt in range(1, retries + 1):
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="1 Y", barSizeSetting="30 mins",
            whatToShow="TRADES", useRTH=False, formatDate=1, timeout=timeout,
        )
        if bars:
            break
        backoff = 10 * attempt
        print(f"[{symbol}] got 0 bars on attempt {attempt}/{retries}, "
              f"retrying after {backoff}s...")
        ib.sleep(backoff)
    ib.sleep(6)
    out = {}
    for b in bars:
        if isinstance(b.date, datetime):
            ts = b.date.astimezone(timezone.utc).isoformat()
        else:
            ts = str(b.date)
        out[ts] = b
    return out


def close_enough(a: float, b: float, abs_tol: float = 0.01, rel_tol: float = 1e-4) -> bool:
    """Two floats are 'the same value' allowing for tiny float/round-trip noise."""
    if a is None or b is None:
        return a == b
    diff = abs(a - b)
    return diff <= abs_tol or diff <= rel_tol * max(abs(a), abs(b), 1.0)
