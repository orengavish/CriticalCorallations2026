"""
fetch_priority.py
Show which dates have verified trades but missing tick files — fetch these first.

Usage:
  python fetch_priority.py
  python fetch_priority.py --all    # also show dates with tick files already present
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.db import get_db


def run(show_all: bool = False):
    cfg = get_config()
    try:
        db_path     = Path(cfg.paths.db)
        history_dir = Path(cfg.paths.history)
    except Exception:
        db_path     = Path("data/galao.db")
        history_dir = Path("data/history")

    # files already on disk
    present = set()
    if history_dir.exists():
        for f in history_dir.glob("*.csv"):
            parts = f.stem.split("_")
            if len(parts) < 3: continue
            sym, ftype, dc = parts[0], parts[1], parts[2]
            if f.stat().st_size > 100:
                date_s = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                present.add((sym, date_s, ftype))

    def has_trades(sym, date_s):
        return (sym, date_s, "trades") in present

    def has_bidask(sym, date_s):
        return (sym, date_s, "bidask") in present

    with get_db(db_path) as con:
        rows = con.execute("""
            SELECT DATE(fill_time) AS d, symbol,
                   COUNT(*)                                          AS n,
                   SUM(CASE WHEN exit_reason='TP'         THEN 1 ELSE 0 END) AS tp,
                   SUM(CASE WHEN exit_reason='SL'         THEN 1 ELSE 0 END) AS sl,
                   ROUND(SUM(pnl_points), 2)                         AS pnl
            FROM verified_trades
            GROUP BY d, symbol
            ORDER BY n DESC, d DESC, symbol
        """).fetchall()

    if not rows:
        print("No verified trades in DB.")
        return

    tier1   = [r for r in rows if has_trades(r["symbol"], r["d"]) and not has_bidask(r["symbol"], r["d"])]
    tier2   = [r for r in rows if not has_trades(r["symbol"], r["d"])]
    covered = [r for r in rows if has_trades(r["symbol"], r["d"]) and has_bidask(r["symbol"], r["d"])]

    def _print_rows(data):
        if not data:
            print("  (none)")
            return
        for r in data:
            t = "T" if has_trades(r["symbol"], r["d"]) else "-"
            b = "B" if has_bidask(r["symbol"], r["d"]) else "-"
            pnl_s = f"{r['pnl']:+.2f}"
            print(f"  {r['d']}  {r['symbol']:<5}  VT:{r['n']:>3}  "
                  f"TP:{r['tp']:>3}  SL:{r['sl']:>3}  PNL:{pnl_s:>8}  files:[{t}{b}]")

    print("\nFETCH PRIORITY — dates with verified trades, missing tick files")
    print("=" * 66)
    print(f"\n[TIER 1 — BID_ASK only, TRADES done] ({len(tier1)} dates, fetch order = count DESC)")
    _print_rows(tier1)
    print(f"\n[TIER 2 — full fetch needed]          ({len(tier2)} dates, fetch order = count DESC)")
    _print_rows(tier2)

    if show_all and covered:
        print(f"\n[COVERED — both TRADES+BID_ASK done]  ({len(covered)} dates)")
        _print_rows(covered)

    print(f"\nTier 1: {len(tier1)}  |  Tier 2: {len(tier2)}  |  Covered: {len(covered)}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch priority list for verified trades")
    parser.add_argument("--all", action="store_true", help="Also show already-covered dates")
    args = parser.parse_args()
    run(show_all=args.all)
