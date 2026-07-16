"""
lib/price_profile.py
Price-level market microstructure profile builder.

For each (symbol, date), reads tick CSV data and builds a per-price table:
  price, total_volume, visits,
  total_ask, total_bid, delta,      <- passive order flow (from bidask file, or NULL)
  price_up, price_down, price_change,   <- direction counts
  up_vol, down_vol, change_vol          <- volume at the triggering visit

Columns explained:
  visits      : number of trades that hit this exact price
  price_up    : how many of those visits were followed by a move up >= TICK_IGNORE ticks
  up_vol      : sum of trade size at those up-triggering visits (1 event w/ 7000 contracts
                counts far more than 1 event with 1 contract)
  price_down / down_vol : same for downward moves
  price_change / change_vol : price_up+price_down / up_vol+down_vol

DB knows if recreate is needed: if profile was built without bidask but the bidask
file now exists, profile_exists() returns False → next build() call fills in the gaps.

Usage:
    from lib.price_profile import ensure_profile, get_price_profile
    ensure_profile("MES", "2026-07-13")          # build if needed
    rows = get_price_profile("MES", "2026-07-13")

CLI:
    python -m lib.price_profile --build MES 2026-07-13
    python -m lib.price_profile --self-test
"""

import csv
import sys
import argparse
from pathlib import Path
from collections import defaultdict

TICK_IGNORE = 2   # moves <= this many ticks are noise; keep scanning for a bigger move
MAX_LOOKAHEAD = 2000  # cap forward scan per visit (inconclusive visits are not counted)

_TICK_SIZE = {
    "MES": 0.25,
    "MNQ": 0.25,
    "MYM": 1.0,
    "M2K": 0.10,
}

_DEFAULT_HIST = Path(r"C:\Projects\Galgo2026\june\trader\data\history")


def _tick(symbol: str) -> float:
    return _TICK_SIZE.get(symbol[:3].upper(), 0.25)


def _trades_path(hist_dir: Path, symbol: str, date_str: str) -> Path:
    return hist_dir / f"{symbol}_trades_{date_str.replace('-', '')}.csv"


def _bidask_path(hist_dir: Path, symbol: str, date_str: str) -> Path:
    return hist_dir / f"{symbol}_bidask_{date_str.replace('-', '')}.csv"


def profile_exists(
    symbol: str, date_str: str, db_path=None, hist_dir: Path = None
) -> bool:
    """
    True if profile is built and up-to-date.
    False if not built, OR if bidask file now exists but profile was built without it.
    """
    from lib.db import get_db
    hd = hist_dir or _DEFAULT_HIST
    with get_db(db_path) as con:
        row = con.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN total_ask IS NULL THEN 1 ELSE 0 END) AS no_ba"
            " FROM price_profile WHERE symbol=? AND date=?",
            (symbol, date_str),
        ).fetchone()
    if row["n"] == 0:
        return False
    if row["no_ba"] > 0 and _bidask_path(hd, symbol, date_str).exists():
        return False   # stale — bidask now available, rebuild
    return True


def build_price_profile(
    symbol: str,
    date_str: str,
    db_path=None,
    hist_dir: Path = None,
    tick_ignore: int = TICK_IGNORE,
    force: bool = False,
) -> int | None:
    """
    Build and store price profile for (symbol, date).

    Returns:
        int  — number of price levels written (>0 = success)
        0    — skipped (already exists and up-to-date)
        None — trades CSV not found
    """
    from lib.db import get_db

    hd = hist_dir or _DEFAULT_HIST

    if not force and profile_exists(symbol, date_str, db_path, hd):
        return 0

    trades_p = _trades_path(hd, symbol, date_str)
    if not trades_p.exists():
        return None

    # --- Load trades: [(price, size), ...] in file order ---
    trades: list[tuple[float, float]] = []
    with open(trades_p, newline="") as f:
        for row in csv.DictReader(f):
            try:
                trades.append((float(row["price"]), float(row["size"])))
            except (ValueError, KeyError):
                continue

    if not trades:
        return None

    # --- Load bid/ask for passive order-flow columns (optional) ---
    bidask_p = _bidask_path(hd, symbol, date_str)
    has_bidask = bidask_p.exists()
    bid_by_price: dict[float, float] = defaultdict(float)
    ask_by_price: dict[float, float] = defaultdict(float)

    if has_bidask:
        with open(bidask_p, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    bid_by_price[float(row["bid_p"])] += float(row["bid_s"])
                    ask_by_price[float(row["ask_p"])] += float(row["ask_s"])
                except (ValueError, KeyError):
                    continue

    # --- Aggregate totals per price ---
    vol_by_price: dict[float, float] = defaultdict(float)
    cnt_by_price: dict[float, int]   = defaultdict(int)
    for price, size in trades:
        vol_by_price[price] += size
        cnt_by_price[price] += 1

    # --- Classify each visit: scan forward until move >= tick_ignore ticks ---
    # Option A: keep scanning past noise until a significant move is found.
    # Count both how many times (price_up/price_down) and the volume at the triggering
    # visit (up_vol/down_vol) — larger volume = stronger signal.
    prices_seq = [t[0] for t in trades]
    sizes_seq  = [t[1] for t in trades]
    n = len(prices_seq)
    min_move = _tick(symbol) * tick_ignore

    up_cnt:   dict[float, int]   = defaultdict(int)
    down_cnt: dict[float, int]   = defaultdict(int)
    up_vol:   dict[float, float] = defaultdict(float)
    down_vol: dict[float, float] = defaultdict(float)

    for i in range(n):
        p0   = prices_seq[i]
        vol0 = sizes_seq[i]
        limit = min(i + MAX_LOOKAHEAD + 1, n)
        for j in range(i + 1, limit):
            diff = prices_seq[j] - p0
            if abs(diff) >= min_move:
                if diff > 0:
                    up_cnt[p0]   += 1
                    up_vol[p0]   += vol0
                else:
                    down_cnt[p0] += 1
                    down_vol[p0] += vol0
                break

    # --- Build rows sorted by price ascending ---
    rows = []
    for price in sorted(vol_by_price):
        uc  = up_cnt[price]
        dc  = down_cnt[price]
        uv  = up_vol[price]
        dv  = down_vol[price]
        if has_bidask:
            ta  = ask_by_price.get(price) or None   # None if 0 / missing
            tb  = bid_by_price.get(price) or None
            # bid and ask are rarely at the same price; treat missing side as 0
            dlt = (ask_by_price[price]) - (bid_by_price[price])
        else:
            ta = tb = dlt = None
        rows.append((
            symbol, date_str, price,
            vol_by_price[price], cnt_by_price[price],
            ta, tb,
            uc, dc, uc + dc,
            uv, dv, uv + dv,
            dlt,
        ))

    with get_db(db_path) as con:
        con.execute(
            "DELETE FROM price_profile WHERE symbol=? AND date=?", (symbol, date_str)
        )
        con.executemany(
            "INSERT INTO price_profile"
            " (symbol, date, price, total_volume, visits,"
            "  total_ask, total_bid,"
            "  price_up, price_down, price_change,"
            "  up_vol, down_vol, change_vol,"
            "  delta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    return len(rows)


def get_price_profile(symbol: str, date_str: str, db_path=None) -> list[dict]:
    """Return all price levels for (symbol, date), ascending by price."""
    from lib.db import get_db
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT price, total_volume, visits,"
            " total_ask, total_bid, delta,"
            " price_up, price_down, price_change,"
            " up_vol, down_vol, change_vol"
            " FROM price_profile WHERE symbol=? AND date=? ORDER BY price",
            (symbol, date_str),
        ).fetchall()
    return [dict(r) for r in rows]


def ensure_profile(symbol: str, date_str: str, db_path=None) -> bool:
    """Build profile if needed. Returns True when profile is available after call."""
    if profile_exists(symbol, date_str, db_path):
        return True
    result = build_price_profile(symbol, date_str, db_path)
    return result is not None and result >= 0


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db_path  = tmp / "test.db"
            hist_dir = tmp / "history"
            hist_dir.mkdir()

            from lib.db import init_db
            init_db(db_path)

            # Trades: prices bounce between 100.00 and 99.50 (2 ticks = 0.50)
            trades_lines = [
                "time_ct,time_utc,price,size,symbol",
                "2026-07-13T09:00:00-05:00,2026-07-13T14:00:00+00:00,100.00,10.0,MESU6",
                "2026-07-13T09:00:01-05:00,2026-07-13T14:00:01+00:00,100.00,5.0,MESU6",
                "2026-07-13T09:00:02-05:00,2026-07-13T14:00:02+00:00,100.25,8.0,MESU6",  # +1 tick (noise)
                "2026-07-13T09:00:03-05:00,2026-07-13T14:00:03+00:00,100.50,3.0,MESU6",  # +2 ticks -> UP
                "2026-07-13T09:00:04-05:00,2026-07-13T14:00:04+00:00,100.00,12.0,MESU6",
                "2026-07-13T09:00:05-05:00,2026-07-13T14:00:05+00:00,99.50,7.0,MESU6",   # -2 ticks -> DOWN
                "2026-07-13T09:00:06-05:00,2026-07-13T14:00:06+00:00,100.00,4.0,MESU6",
            ]
            (hist_dir / "MES_trades_20260713.csv").write_text("\n".join(trades_lines))

            # Build (no bidask)
            n = build_price_profile("MES", "2026-07-13", db_path, hist_dir)
            assert n is not None and n > 0, f"Expected rows, got {n}"

            # Idempotent skip
            assert profile_exists("MES", "2026-07-13", db_path, hist_dir)
            assert build_price_profile("MES", "2026-07-13", db_path, hist_dir) == 0

            # Check data shape
            profile = get_price_profile("MES", "2026-07-13", db_path)
            assert len(profile) > 0
            prices = [r["price"] for r in profile]
            assert prices == sorted(prices), "Not sorted ascending"
            assert all(r["total_ask"] is None for r in profile), "Should be NULL without bidask"

            # Check volume weighting: 100.00 visits 1+2+4+5+7 = rows; at idx 0 (10.0) next sig = +2t up
            row_100 = next(r for r in profile if r["price"] == 100.00)
            assert row_100["price_up"] > 0,  "Expected price_up at 100.00"
            assert row_100["up_vol"] > 0,    "Expected up_vol at 100.00"
            assert row_100["price_down"] > 0, "Expected price_down at 100.00"

            # Bidask triggers stale rebuild
            ba_lines = [
                "time_ct,time_utc,bid_p,bid_s,ask_p,ask_s,symbol",
                "2026-07-13T09:00:00-05:00,2026-07-13T14:00:00+00:00,100.00,5.0,100.25,8.0,MESU6",
                "2026-07-13T09:00:01-05:00,2026-07-13T14:00:01+00:00,100.00,3.0,100.25,4.0,MESU6",
            ]
            (hist_dir / "MES_bidask_20260713.csv").write_text("\n".join(ba_lines))

            assert not profile_exists("MES", "2026-07-13", db_path, hist_dir), \
                "Should be stale when bidask file appears"

            n2 = build_price_profile("MES", "2026-07-13", db_path, hist_dir)
            assert n2 is not None and n2 > 0
            profile2 = get_price_profile("MES", "2026-07-13", db_path)
            assert any(r["total_ask"] is not None for r in profile2), "Expected ask data after rebuild"
            row2_100 = next(r for r in profile2 if r["price"] == 100.00)
            # bid=8.0 at 100.00, ask=0 → delta = -8.0
            assert row2_100["delta"] is not None, "Expected delta after rebuild"
            assert row2_100["delta"] < 0, f"Expected negative delta (more bid), got {row2_100['delta']}"

        print("[self-test] price_profile: PASS")
        return True
    except Exception as e:
        import traceback
        print(f"[self-test] price_profile: FAIL — {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--build", nargs=2, metavar=("SYMBOL", "DATE"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.build:
        from lib.db import init_db
        init_db()
        sym, dt = args.build
        result = build_price_profile(sym, dt, force=args.force)
        if result is None:
            print(f"No trades data for {sym} {dt}")
            sys.exit(1)
        elif result == 0:
            print("Already up-to-date (use --force to rebuild)")
        else:
            print(f"Built {result} price levels for {sym} {dt}")
