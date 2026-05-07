"""
data_scan.py
Scan data/history/ on disk and report what files exist.

Output: list of dates with each file found, row count, and size.

Usage:
  python data_scan.py
  python data_scan.py --days 30
  python data_scan.py --symbol MES
  python data_scan.py --self-test
"""

import sys
import argparse
import re
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

_FILE_PAT = re.compile(r"^([A-Z0-9]+)_(trades|bidask)_(\d{8})\.csv$")

HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 7, 3),
    date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
}


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def _fmt_size(b: int) -> str:
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f}MB"
    if b >= 1024:
        return f"{b // 1024}KB"
    return f"{b}B"


def _count_rows(path: Path) -> int:
    """Count data rows (skip header)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return -1


def scan(history_dir: Path, days: int = None, symbol_filter: str = None) -> list[dict]:
    """Return list of dicts: date, symbol, file_type, rows, size_bytes, path."""
    results = []
    for p in sorted(history_dir.glob("*.csv")):
        m = _FILE_PAT.match(p.name)
        if not m:
            continue
        sym, ft, date_s = m.group(1), m.group(2), m.group(3)
        d = date(int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]))

        if symbol_filter and sym != symbol_filter.upper():
            continue

        if days:
            cutoff = date.today() - timedelta(days=days)
            if d < cutoff:
                continue

        results.append({
            "date":       d,
            "date_s":     d.isoformat(),
            "symbol":     sym,
            "file_type":  ft,
            "rows":       _count_rows(p),
            "size_bytes": p.stat().st_size,
            "path":       p,
        })

    results.sort(key=lambda r: (r["date"], r["symbol"], r["file_type"]))
    return results


def _print_report(results: list[dict]):
    if not results:
        print("No CSV files found.")
        return

    # Group by date
    by_date: dict[str, list] = {}
    for r in results:
        by_date.setdefault(r["date_s"], []).append(r)

    col_w = {"symbol": 6, "type": 7, "rows": 10, "size": 8}
    header = (f"  {'SYMBOL':<{col_w['symbol']}}  {'TYPE':<{col_w['type']}}"
              f"  {'ROWS':>{col_w['rows']}}  {'SIZE':>{col_w['size']}}")
    sep = "  " + "-" * (sum(col_w.values()) + 3 * len(col_w))

    total_files = 0
    total_rows = 0

    for date_s, rows in sorted(by_date.items()):
        d = date.fromisoformat(date_s)
        day_label = d.strftime("%Y-%m-%d  %a")
        print(f"\n{day_label}")
        print(sep)
        print(header)
        print(sep)
        for r in rows:
            rows_s = str(r["rows"]) if r["rows"] >= 0 else "ERR"
            size_s = _fmt_size(r["size_bytes"])
            print(f"  {r['symbol']:<{col_w['symbol']}}  {r['file_type']:<{col_w['type']}}"
                  f"  {rows_s:>{col_w['rows']}}  {size_s:>{col_w['size']}}")
            total_files += 1
            if r["rows"] >= 0:
                total_rows += r["rows"]

    print(f"\nTotal: {total_files} files, {total_rows:,} rows\n")


def run(days: int = None, symbol: str = None):
    history_dir = Path(__file__).parent / "data" / "history"
    if not history_dir.exists():
        print(f"History dir not found: {history_dir}")
        return
    results = scan(history_dir, days=days, symbol_filter=symbol)
    _print_report(results)


def _self_test():
    import tempfile, csv, os

    tmp = Path(tempfile.mkdtemp())
    # Write two test CSVs
    for name, rows in [
        ("MES_trades_20260101.csv", [["ts", "price", "qty"], ["2026-01-01T09:00:00", "5000", "1"]]),
        ("MES_bidask_20260101.csv", [["ts", "bid", "ask"], ["2026-01-01T09:00:00", "4999", "5001"]]),
    ]:
        with (tmp / name).open("w", newline="") as f:
            csv.writer(f).writerows(rows)

    results = scan(tmp)
    assert len(results) == 2, f"Expected 2, got {len(results)}"
    assert results[0]["rows"] == 1
    assert results[0]["symbol"] == "MES"
    assert results[0]["file_type"] == "bidask"

    # Clean up
    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()

    print("data_scan.py --self-test PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan data/history/ for CSV files")
    parser.add_argument("--days",      type=int,  default=None, help="Only show files within last N days")
    parser.add_argument("--symbol",    type=str,  default=None, help="Filter to one symbol (e.g. MES)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
    else:
        run(days=args.days, symbol=args.symbol)
