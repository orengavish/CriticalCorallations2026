"""
lib/data_availability.py
Scans the history directory for days that are ready for CL algo backtesting.

A day is "ready" when ALL of the following are true:
  1. TRADES CSV exists and is non-empty  ({SYMBOL}_trades_{YYYYMMDD}.csv)
  2. BID_ASK CSV exists and is non-empty ({SYMBOL}_bid_ask_{YYYYMMDD}.csv)
  3. At least one armed critical line exists in the DB for (symbol, date)

Usage:
    from lib.data_availability import get_ready_days, summarise
    days = get_ready_days(db_path, history_dir, symbols=["MES"])
    # returns list of {"symbol": "MES", "date": "2026-07-01",
    #                  "n_lines": 5, "trades_rows": 45000, "bidask_rows": 90000}

Self-test:
    python -m lib.data_availability --self-test
"""

import sys
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime, date as date_type


_MIN_TRADES_ROWS = 100   # fewer → probably a broken fetch
_MIN_BIDASK_ROWS = 100


def _count_csv_rows(path: Path) -> int:
    """Fast row count by counting newlines minus header."""
    if not path.exists() or path.stat().st_size < 50:
        return 0
    try:
        with open(path, "rb") as f:
            return f.read().count(b"\n") - 1   # subtract header
    except Exception:
        return 0


def _date_from_filename(name: str) -> str | None:
    """Extract YYYY-MM-DD from {SYM}_trades_{YYYYMMDD}.csv or {SYM}_bid_ask_{YYYYMMDD}.csv."""
    try:
        stem = Path(name).stem          # e.g. MES_trades_20260701
        compact = stem.rsplit("_", 1)[1]  # 20260701
        if len(compact) != 8 or not compact.isdigit():
            return None
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    except Exception:
        return None


def _get_lines_by_date(db_path: Path, symbols: list[str]) -> dict[tuple, int]:
    """
    Returns {(symbol, date): n_armed_lines} for all entries in critical_lines.
    Restricted to given symbols.
    """
    result = {}
    if not db_path.exists():
        return result
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in symbols)
        rows = con.execute(
            f"SELECT symbol, date, COUNT(*) as n FROM critical_lines"
            f" WHERE armed=1 AND symbol IN ({placeholders})"
            f" GROUP BY symbol, date",
            symbols
        ).fetchall()
        con.close()
        for r in rows:
            result[(r["symbol"], r["date"])] = r["n"]
    except Exception:
        pass
    return result


def get_ready_days(db_path: Path, history_dir: Path,
                   symbols: list[str] | None = None,
                   min_date: str | None = None,
                   max_date: str | None = None) -> list[dict]:
    """
    Return list of dicts for days ready for CL algo backtesting.
    Sorted by date ascending.
    """
    if symbols is None:
        symbols = ["MES", "MNQ", "MYM", "M2K"]

    history_dir = Path(history_dir)
    lines_map = _get_lines_by_date(db_path, symbols)

    # Build index of available files: {(symbol, date): {trades: path, bid_ask: path}}
    file_index: dict[tuple, dict] = {}
    if history_dir.exists():
        for f in history_dir.iterdir():
            name = f.name
            date_str = _date_from_filename(name)
            if date_str is None:
                continue
            if min_date and date_str < min_date:
                continue
            if max_date and date_str > max_date:
                continue
            for sym in symbols:
                if name.startswith(f"{sym}_trades_"):
                    key = (sym, date_str)
                    file_index.setdefault(key, {})["trades"] = f
                # Real fetcher output uses "bidask" (no underscore); accept both spellings.
                elif name.startswith(f"{sym}_bid_ask_") or name.startswith(f"{sym}_bidask_"):
                    key = (sym, date_str)
                    file_index.setdefault(key, {})["bid_ask"] = f

    ready = []
    for (sym, date_str), files in sorted(file_index.items()):
        if "trades" not in files or "bid_ask" not in files:
            continue
        n_lines = lines_map.get((sym, date_str), 0)
        if n_lines == 0:
            continue
        t_rows = _count_csv_rows(files["trades"])
        b_rows = _count_csv_rows(files["bid_ask"])
        if t_rows < _MIN_TRADES_ROWS or b_rows < _MIN_BIDASK_ROWS:
            continue
        ready.append({
            "symbol":       sym,
            "date":         date_str,
            "n_lines":      n_lines,
            "trades_rows":  t_rows,
            "bidask_rows":  b_rows,
            "trades_path":  str(files["trades"]),
            "bidask_path":  str(files["bid_ask"]),
        })

    return sorted(ready, key=lambda x: (x["date"], x["symbol"]))


def split_boundaries(ready_days: list[dict], symbol: str,
                     train_frac: float = 0.6, val_frac: float = 0.2) -> dict:
    """
    Chronological (never random -- this is time-series data, a random split would
    leak future information into training) train/validation/out_of_sample date
    boundaries for one symbol, given get_ready_days()'s full output.
    Returns {"train": (min_date, max_date), "validation": (min_date, max_date),
             "out_of_sample": (min_date, max_date)} -- any split with 0 days gets
    (None, None).
    """
    assert train_frac + val_frac < 1.0, "train_frac + val_frac must be < 1.0 (remainder is OOS)"

    dates = sorted(d["date"] for d in ready_days if d["symbol"] == symbol)
    n       = len(dates)
    n_train = int(n * train_frac)
    n_val   = int(n * val_frac)

    train_days = dates[:n_train]
    val_days   = dates[n_train:n_train + n_val]
    oos_days   = dates[n_train + n_val:]

    def _bounds(days: list[str]) -> tuple:
        return (days[0], days[-1]) if days else (None, None)

    return {
        "train":         _bounds(train_days),
        "validation":    _bounds(val_days),
        "out_of_sample": _bounds(oos_days),
    }


def summarise(ready_days: list[dict]) -> str:
    """Human-readable summary of ready days."""
    if not ready_days:
        return "No ready days found."
    by_sym: dict[str, list] = {}
    for d in ready_days:
        by_sym.setdefault(d["symbol"], []).append(d["date"])
    lines = [f"Ready days ({len(ready_days)} total):"]
    for sym, dates in sorted(by_sym.items()):
        lines.append(f"  {sym}: {len(dates)} days  [{dates[0]} to {dates[-1]}]")
    return "\n".join(lines)


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    import tempfile, os, csv

    print("Running data_availability self-test...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path  = Path(tmp)
            hist_dir  = tmp_path / "history"
            hist_dir.mkdir()
            db_path   = tmp_path / "galao.db"

            # 1. Create DB with critical_lines
            import sqlite3 as _sq
            con = _sq.connect(str(db_path))
            con.execute("""CREATE TABLE critical_lines (
                id INTEGER PRIMARY KEY, symbol TEXT, date TEXT,
                line_type TEXT, price REAL, strength INTEGER, armed INTEGER DEFAULT 1,
                created_at TEXT DEFAULT ''
            )""")
            con.executemany(
                "INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed) VALUES(?,?,?,?,?,?)",
                [
                    ("MES", "2026-06-30", "SUPPORT",    5500.0, 1, 1),
                    ("MES", "2026-06-30", "RESISTANCE", 5550.0, 2, 1),
                    ("MES", "2026-07-01", "SUPPORT",    5490.0, 1, 0),  # not armed
                    ("MES", "2026-07-01", "SUPPORT",    5480.0, 1, 0),  # not armed
                    ("MNQ", "2026-06-30", "SUPPORT",    19800.0, 1, 1),
                ]
            )
            con.commit(); con.close()

            def _write_csv(path: Path, rows: int):
                with open(path, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time_utc", "price", "size"])
                    for i in range(rows):
                        w.writerow([f"2026-06-30T14:{i%60:02d}:00Z", 5500.0 + i * 0.25, 10])

            # Day 2026-06-30: full data for MES (both files, enough rows) + MNQ (trades only)
            _write_csv(hist_dir / "MES_trades_20260630.csv",  200)
            _write_csv(hist_dir / "MES_bid_ask_20260630.csv", 200)
            _write_csv(hist_dir / "MNQ_trades_20260630.csv",  200)
            # MNQ missing bid_ask → not ready

            # Day 2026-07-01: MES has both files but only 1 armed line
            _write_csv(hist_dir / "MES_trades_20260701.csv",  200)
            _write_csv(hist_dir / "MES_bid_ask_20260701.csv", 200)

            # Day with too few rows → not ready
            _write_csv(hist_dir / "MES_trades_20260629.csv",  50)
            _write_csv(hist_dir / "MES_bid_ask_20260629.csv", 50)

            ready = get_ready_days(db_path, hist_dir, symbols=["MES", "MNQ"])

            # Assertions
            dates = [(r["symbol"], r["date"]) for r in ready]
            assert ("MES", "2026-06-30") in dates, f"MES 06-30 should be ready: {dates}"
            assert ("MNQ", "2026-06-30") not in dates, "MNQ 06-30 missing bid_ask, must NOT be ready"
            assert ("MES", "2026-07-01") not in dates, "MES 07-01 has 0 armed lines, must NOT be ready"
            assert ("MES", "2026-06-29") not in dates, "MES 06-29 too few rows, must NOT be ready"

            mes_jun30 = next(r for r in ready if r["symbol"] == "MES" and r["date"] == "2026-06-30")
            assert mes_jun30["n_lines"] == 2,     f"Expected 2 armed lines, got {mes_jun30['n_lines']}"
            assert mes_jun30["trades_rows"] >= 200

            # summarise
            s = summarise(ready)
            assert "MES" in s

            # split_boundaries: 10 synthetic ready days, chronological (not random) assignment
            synth = [{"symbol": "MES", "date": f"2026-08-{d:02d}"} for d in range(1, 11)]
            bounds = split_boundaries(synth, "MES", train_frac=0.6, val_frac=0.2)
            assert bounds["train"]         == ("2026-08-01", "2026-08-06"), bounds["train"]
            assert bounds["validation"]    == ("2026-08-07", "2026-08-08"), bounds["validation"]
            assert bounds["out_of_sample"] == ("2026-08-09", "2026-08-10"), bounds["out_of_sample"]

            # degenerate case: too few days -> some buckets get (None, None), never crash
            tiny = [{"symbol": "MES", "date": f"2026-08-{d:02d}"} for d in range(1, 4)]  # 3 days
            tiny_bounds = split_boundaries(tiny, "MES", train_frac=0.6, val_frac=0.2)
            assert tiny_bounds["validation"] == (None, None), \
                f"3 days * 0.2 frac should round to 0 days: {tiny_bounds}"
            assert tiny_bounds["train"] != (None, None)
            assert tiny_bounds["out_of_sample"] != (None, None)

            # symbol filtering: a different symbol's days must not leak into MNQ's split.
            # MNQ has only 1 ready day -> int(1*0.6)==0 train, int(1*0.2)==0 val, so
            # that lone day correctly falls through to out_of_sample, not train.
            mixed = synth + [{"symbol": "MNQ", "date": "2026-09-01"}]
            mnq_bounds = split_boundaries(mixed, "MNQ", train_frac=0.6, val_frac=0.2)
            assert mnq_bounds["out_of_sample"] == ("2026-09-01", "2026-09-01"), mnq_bounds
            assert mnq_bounds["train"] == (None, None), mnq_bounds

        print(f"PASS -- data_availability: {len(ready)} ready day(s) found correctly,"
              f" split_boundaries verified")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--db",   default=None, help="Path to galao.db")
    parser.add_argument("--hist", default=None, help="Path to history directory")
    parser.add_argument("--symbol", nargs="*", default=None)
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg = get_config()
    db_path  = Path(args.db)   if args.db   else Path(cfg.paths.db)
    hist_dir = Path(args.hist) if args.hist else Path(cfg.paths.db).parent / "history"
    syms     = args.symbol or ["MES", "MNQ", "MYM", "M2K"]

    days = get_ready_days(db_path, hist_dir, symbols=syms)
    print(summarise(days))
    for d in days:
        print(f"  {d['symbol']} {d['date']}  lines={d['n_lines']}"
              f"  trades={d['trades_rows']:,}  bidask={d['bidask_rows']:,}")
