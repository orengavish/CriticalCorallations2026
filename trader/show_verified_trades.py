"""
show_verified_trades.py
Print all verified trades from the DB.

Usage:
  python show_verified_trades.py
  python show_verified_trades.py --days 30
  python show_verified_trades.py --symbol MES
  python show_verified_trades.py --source random_mkt
  python show_verified_trades.py --summary
  python show_verified_trades.py --self-test
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.db import get_db


def _get_db_path() -> Path:
    try:
        return Path(get_config().paths.db)
    except Exception:
        return Path(__file__).parent / "data" / "galao.db"


def _fmt_pnl(v) -> str:
    if v is None:
        return "?"
    s = f"{v:+.2f}"
    return s


def load_verified(db_path: Path, days: int = None,
                  symbol: str = None, source: str = None) -> list[dict]:
    clauses = []
    params: list = []

    if days:
        clauses.append("fill_time >= datetime('now', ?)")
        params.append(f"-{days} days")
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if source:
        clauses.append("source = ?")
        params.append(source)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT command_id, symbol, source, direction, entry_type,
               bracket_size, fill_price, fill_time,
               exit_price, exit_time, exit_reason, pnl_points,
               chain_depth, root_critical_line_id
        FROM verified_trades
        {where}
        ORDER BY fill_time ASC
    """
    with get_db(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def print_table(rows: list[dict]):
    if not rows:
        print("No verified trades found.")
        return

    # Column widths
    W = {
        "cmd":      5,
        "symbol":   5,
        "source":   12,
        "dir":      4,
        "type":     3,
        "brk":      4,
        "fill":     8,
        "fill_t":   19,
        "exit":     8,
        "exit_t":   19,
        "reason":   10,
        "pnl":      7,
        "depth":    5,
    }

    hdr = (
        f"{'ID':>{W['cmd']}}  "
        f"{'SYM':<{W['symbol']}}  "
        f"{'SOURCE':<{W['source']}}  "
        f"{'DIR':<{W['dir']}}  "
        f"{'TYP':<{W['type']}}  "
        f"{'BRK':>{W['brk']}}  "
        f"{'FILL':>{W['fill']}}  "
        f"{'FILL_TIME':<{W['fill_t']}}  "
        f"{'EXIT':>{W['exit']}}  "
        f"{'EXIT_TIME':<{W['exit_t']}}  "
        f"{'REASON':<{W['reason']}}  "
        f"{'PNL':>{W['pnl']}}  "
        f"{'DEPTH':>{W['depth']}}"
    )
    sep = "-" * len(hdr)

    print(sep)
    print(hdr)
    print(sep)

    total_pnl = 0.0
    wins = losses = 0

    for r in rows:
        pnl = r["pnl_points"] or 0.0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        print(
            f"{r['command_id']:>{W['cmd']}}  "
            f"{r['symbol']:<{W['symbol']}}  "
            f"{(r['source'] or ''):<{W['source']}}  "
            f"{r['direction']:<{W['dir']}}  "
            f"{r['entry_type']:<{W['type']}}  "
            f"{r['bracket_size']:>{W['brk']}.1f}  "
            f"{r['fill_price']:>{W['fill']}.2f}  "
            f"{r['fill_time']:<{W['fill_t']}}  "
            f"{r['exit_price']:>{W['exit']}.2f}  "
            f"{r['exit_time']:<{W['exit_t']}}  "
            f"{r['exit_reason']:<{W['reason']}}  "
            f"{_fmt_pnl(r['pnl_points']):>{W['pnl']}}  "
            f"{r['chain_depth']:>{W['depth']}}"
        )

    print(sep)
    total = len(rows)
    rate = f"{100 * wins / total:.1f}%" if total else "N/A"
    print(f"\nTotal: {total}  |  Wins: {wins}  Losses: {losses}  Win rate: {rate}  |  Total PnL: {_fmt_pnl(total_pnl)} pts\n")


def print_summary(rows: list[dict]):
    if not rows:
        print("No verified trades found.")
        return

    from collections import defaultdict
    import statistics

    by_source: dict[str, list] = defaultdict(list)
    by_symbol: dict[str, list] = defaultdict(list)
    by_reason: dict[str, int] = defaultdict(int)

    for r in rows:
        src = r["source"] or "unknown"
        by_source[src].append(r["pnl_points"] or 0.0)
        by_symbol[r["symbol"]].append(r["pnl_points"] or 0.0)
        by_reason[r["exit_reason"]] += 1

    print("\n=== BY SOURCE ===")
    for src, pnls in sorted(by_source.items()):
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        rate = f"{100 * wins / len(pnls):.1f}%"
        avg = statistics.mean(pnls)
        print(f"  {src:<20}  n={len(pnls):>4}  win={rate}  total={total_pnl:+.2f}  avg={avg:+.2f}")

    print("\n=== BY SYMBOL ===")
    for sym, pnls in sorted(by_symbol.items()):
        wins = sum(1 for p in pnls if p > 0)
        rate = f"{100 * wins / len(pnls):.1f}%"
        total_pnl = sum(pnls)
        print(f"  {sym:<8}  n={len(pnls):>4}  win={rate}  total={total_pnl:+.2f}")

    print("\n=== EXIT REASONS ===")
    for reason, cnt in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {reason:<14}  {cnt}")

    print()


def _self_test():
    db_path = _get_db_path()
    if not db_path.exists():
        print("show_verified_trades.py --self-test SKIP (no DB)")
        return
    rows = load_verified(db_path)
    # Just verify the query runs and returns a list
    assert isinstance(rows, list)
    if rows:
        r = rows[0]
        assert "command_id" in r
        assert "pnl_points" in r
        assert "exit_reason" in r
    print(f"show_verified_trades.py --self-test PASSED ({len(rows)} verified trades in DB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show verified trades")
    parser.add_argument("--days",      type=int,  default=None, help="Last N days only")
    parser.add_argument("--symbol",    type=str,  default=None, help="Filter by symbol")
    parser.add_argument("--source",    type=str,  default=None, help="Filter by source")
    parser.add_argument("--summary",   action="store_true",     help="Show stats summary instead of full table")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
    else:
        db_path = _get_db_path()
        rows = load_verified(db_path, days=args.days, symbol=args.symbol, source=args.source)
        if args.summary:
            print_table(rows)
            print_summary(rows)
        else:
            print_table(rows)
