"""
trader/verify_data.py
Data correctness checker for fetched tick files.

Checks per file:
  1. File exists and has meaningful size
  2. Row count vs symbol-specific floor (warn if low, error if critical)
  3. Price range within historical norms
  4. No RTH gap > MAX_RTH_GAP_MINUTES (warn for illiquid symbols, error for liquid)
  5. Bid > ask inversion rate < threshold (bidask only)
  6. First tick near session open (17:00 CT ± 2h)
  7. Last  tick near session close (17:00 CT ± 2h)
  8. No price outlier jumps > OUTLIER_JUMP_POINTS between consecutive ticks (trades)

Cross-file check (run separately):
  9. TRADES vs BID_ASK count within 50% of each other

Returns structured (status, notes):
  status  'pass' | 'warn' | 'fail'
  notes   list[str] prefixed "ERROR:" or "WARN:" or "INFO:"

DB columns written:
  verified      INTEGER   1 when status != fail
  verify_status TEXT      'pass' | 'warn' | 'fail'
  verify_notes  TEXT      pipe-separated notes

Usage:
  python trader/verify_data.py --symbol MES --date 2026-06-02
  python trader/verify_data.py --symbol MES --date 2026-06-02 --dtype bidask
  python trader/verify_data.py --all-pending
  python trader/verify_data.py --cross-check --date 2026-06-02
"""

import csv
import sys
import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("verify_data")

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# RTH: 08:30–15:15 CT
_RTH_START = (8, 30)
_RTH_END   = (15, 15)

# Per-symbol RTH gap tolerance (minutes)
_MAX_RTH_GAP = {
    "MES": 3,
    "MNQ": 3,
    "MYM": 8,
    "M2K": 8,
}
_MAX_RTH_GAP_DEFAULT = 5

# Minimum TRADES row counts — below floor → warn, below critical → error
_MIN_ROWS_WARN = {"MES": 40_000, "MNQ": 20_000, "MYM":  6_000, "M2K":  6_000}
_MIN_ROWS_ERR  = {"MES":  5_000, "MNQ":  2_000, "MYM":    500, "M2K":    500}

# Minimum BID_ASK row counts
_MIN_BIDASK_WARN = {"MES": 30_000, "MNQ": 15_000, "MYM": 5_000, "M2K": 5_000}
_MIN_BIDASK_ERR  = {"MES":  3_000, "MNQ":  1_500, "MYM":   300, "M2K":   300}

# Expected price ranges (rough — update if market moves significantly)
_PRICE_RANGE = {
    "MES": (3_000,  8_000),
    "MNQ": (8_000, 32_000),
    "MYM": (20_000, 55_000),
    "M2K": (800,   4_500),
}

# Max single-tick price jump — larger is an outlier / bad data
_OUTLIER_JUMP = {"MES": 30, "MNQ": 150, "MYM": 300, "M2K": 20}
_OUTLIER_JUMP_DEFAULT = 50

_MAX_INVERSION_RATE = 0.01


# ── DB helpers ────────────────────────────────────────────────────────────────

def _open_progress_db(cfg) -> sqlite3.Connection:
    try:
        prog_db = Path(cfg.paths.db).parent / "fetch_progress.db"
    except Exception:
        prog_db = Path("data/fetch_progress.db")
    conn = sqlite3.connect(str(prog_db), timeout=30)
    conn.row_factory = sqlite3.Row
    for col, typedef in [
        ("verified",      "INTEGER DEFAULT 0"),
        ("verify_status", "TEXT"),
        ("verify_notes",  "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE fetch_progress ADD COLUMN {col} {typedef}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return conn


def _mark_verified(conn, symbol: str, date_str: str, dtype: str,
                   status: str, notes: list):
    notes_str = " | ".join(notes) if notes else ""
    conn.execute(
        "UPDATE fetch_progress SET verified=?, verify_status=?, verify_notes=? "
        "WHERE symbol=? AND date=? AND data_type=?",
        (1 if status != "fail" else 0, status, notes_str, symbol, date_str, dtype)
    )
    conn.commit()


# ── Fast last-rows reader ─────────────────────────────────────────────────────

def _read_last_lines(path: Path, n: int = 10) -> list:
    """Read last n non-empty lines from a file without loading it all."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        chunk_size = min(8192, size)
        f.seek(max(0, size - chunk_size))
        tail = f.read().decode("utf-8", errors="ignore")
    lines = [l for l in tail.splitlines() if l.strip()]
    return lines[-n:]


# ── Core checker ─────────────────────────────────────────────────────────────

def check_file(symbol: str, target_date: date,
               dtype: str = "trades", output_dir: Path = None,
               verbose: bool = True) -> tuple:
    """
    Run all correctness checks on one tick file.
    Returns (status: str, notes: list[str])
      status  'pass' | 'warn' | 'fail'
      notes   prefixed 'ERROR:' / 'WARN:' / 'INFO:'
    """
    date_compact = target_date.strftime("%Y%m%d")
    date_str     = target_date.strftime("%Y-%m-%d")

    if output_dir is None:
        try:
            output_dir = Path(get_config().paths.history)
        except Exception:
            output_dir = Path("data/history")

    path = output_dir / f"{symbol}_{dtype.replace('_', '')}_{date_compact}.csv"

    issues = []  # (severity, message)   severity: 'error' | 'warn' | 'info'

    def err(msg):
        issues.append(("error", msg))
        if verbose:
            print(f"  FAIL  {msg}")

    def warn(msg):
        issues.append(("warn", msg))
        if verbose:
            print(f"  WARN  {msg}")

    def info(msg):
        issues.append(("info", msg))
        if verbose:
            print(f"       {msg}")

    if verbose:
        print(f"\n--- Verify {symbol} {dtype} {date_str} ---")

    # ── 1. File existence ──
    if not path.exists():
        err("file not found")
        return "fail", _fmt_notes(issues)
    if path.stat().st_size < 200:
        err(f"file too small ({path.stat().st_size} bytes)")
        return "fail", _fmt_notes(issues)

    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    n = len(rows)
    info(f"rows: {n:,}")

    if n < 10:
        err(f"only {n} rows — fetch likely incomplete")
        return "fail", _fmt_notes(issues)

    # ── 2. Row count ──
    if dtype == "trades":
        floor_w = _MIN_ROWS_WARN.get(symbol, 5_000)
        floor_e = _MIN_ROWS_ERR.get(symbol, 500)
    else:
        floor_w = _MIN_BIDASK_WARN.get(symbol, 5_000)
        floor_e = _MIN_BIDASK_ERR.get(symbol, 300)

    if n < floor_e:
        err(f"row count {n:,} below critical floor {floor_e:,}")
    elif n < floor_w:
        warn(f"row count {n:,} below expected floor {floor_w:,} — short session?")

    # ── 3. Timestamps ──
    time_col = "time_utc"
    try:
        times = [datetime.fromisoformat(r[time_col]) for r in rows]
    except Exception as e:
        err(f"timestamp parse error: {e}")
        return "fail", _fmt_notes(issues)

    first_ct = times[0].astimezone(CT)
    last_ct  = times[-1].astimezone(CT)
    info(f"first: {first_ct.strftime('%H:%M:%S CT')}  last: {last_ct.strftime('%H:%M:%S CT')}")

    # Session open: prev-day 17:00 CT  (±2h tolerance → 15:00–19:00)
    if first_ct.hour < 15 or first_ct.hour > 19:
        warn(f"first tick at {first_ct.strftime('%H:%M CT')} — expected ~17:00 CT prev day")

    # Session close: CME last tick is 15:59:59 CT (not 16:00:00), so 15:59 is normal.
    # Only warn if last tick is before 15:59 (genuinely early) or outside normal range.
    if last_ct.hour < 15 or last_ct.hour > 19:
        warn(f"last tick at {last_ct.strftime('%H:%M CT')} — expected ~17:00 CT")
    elif last_ct.hour == 15 and last_ct.minute < 59:
        warn(f"last tick at {last_ct.strftime('%H:%M CT')} — session may be truncated")

    # ── 4. Price range ──
    lo, hi = _PRICE_RANGE.get(symbol, (0, 999_999))

    if dtype == "trades":
        try:
            prices = [float(r["price"]) for r in rows if r.get("price")]
        except Exception as e:
            err(f"price column parse error: {e}")
            return "fail", _fmt_notes(issues)
        if not prices:
            err("no valid price values")
            return "fail", _fmt_notes(issues)

        pmin, pmax = min(prices), max(prices)
        info(f"price range: {pmin:.2f} – {pmax:.2f}")

        if pmin < lo or pmax > hi:
            err(f"price {pmin:.2f}–{pmax:.2f} outside expected [{lo}, {hi}]")
        if any(p <= 0 for p in prices):
            err(f"{sum(1 for p in prices if p <= 0)} rows with price ≤ 0")

        # ── 8. Price outlier jumps ──
        jump_limit = _OUTLIER_JUMP.get(symbol, _OUTLIER_JUMP_DEFAULT)
        max_jump = 0.0
        for i in range(1, len(prices)):
            j = abs(prices[i] - prices[i - 1])
            if j > max_jump:
                max_jump = j
        if max_jump > jump_limit:
            warn(f"largest consecutive price jump: {max_jump:.2f} pts (limit {jump_limit})")
        else:
            info(f"max consecutive jump: {max_jump:.2f} pts")

    # ── 5. Bid/ask ──
    if dtype == "bidask":
        try:
            bids = [float(r["bid_p"]) for r in rows if r.get("bid_p")]
            asks = [float(r["ask_p"]) for r in rows if r.get("ask_p")]
        except Exception as e:
            err(f"bid/ask column parse error: {e}")
            return "fail", _fmt_notes(issues)
        if not bids or not asks:
            err("no valid bid/ask values")
            return "fail", _fmt_notes(issues)

        inv = sum(1 for b, a in zip(bids, asks) if b > a and b > 0 and a > 0)
        inv_rate = inv / len(bids)
        info(f"bid/ask inversions: {inv} ({inv_rate:.2%})")
        if inv_rate > _MAX_INVERSION_RATE:
            err(f"inversion rate {inv_rate:.2%} > limit {_MAX_INVERSION_RATE:.2%}")

        pmin, pmax = min(bids), max(bids)
        info(f"bid range: {pmin:.2f} – {pmax:.2f}")
        if pmin < lo or pmax > hi:
            err(f"bid price {pmin:.2f}–{pmax:.2f} outside expected [{lo}, {hi}]")

    # ── 6/7. RTH gap ──
    rth = [t for t in times
           if (t.astimezone(CT).hour, t.astimezone(CT).minute) >= _RTH_START
           and (t.astimezone(CT).hour, t.astimezone(CT).minute) < _RTH_END]

    if not rth:
        warn("no RTH ticks found — holiday or session problem?")
    else:
        max_gap = timedelta(0)
        for i in range(1, len(rth)):
            g = rth[i] - rth[i - 1]
            if g > max_gap:
                max_gap = g
        gap_min = max_gap.total_seconds() / 60
        limit = _MAX_RTH_GAP.get(symbol, _MAX_RTH_GAP_DEFAULT)
        info(f"RTH max gap: {gap_min:.1f} min (limit {limit} min)")
        if gap_min > limit:
            err(f"RTH gap of {gap_min:.1f} min > {limit} min limit")

    # ── Result ──
    has_error = any(s == "error" for s, _ in issues)
    has_warn  = any(s == "warn"  for s, _ in issues)
    status    = "fail" if has_error else ("warn" if has_warn else "pass")
    notes     = _fmt_notes(issues)

    if verbose:
        label = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[status]
        print(f"  {label}  {symbol} {dtype} {date_str}  ({n:,} rows)")

    return status, notes


def _fmt_notes(issues: list) -> list:
    prefix = {"error": "ERROR", "warn": "WARN", "info": "INFO"}
    return [f"{prefix.get(s, s)}: {m}" for s, m in issues]


# ── Cross-file check ──────────────────────────────────────────────────────────

def check_cross(symbol: str, target_date: date,
                output_dir: Path = None, verbose: bool = True) -> tuple:
    """
    Check TRADES vs BID_ASK count consistency for the same symbol/date.
    Returns (status, notes).
    """
    date_compact = target_date.strftime("%Y%m%d")
    date_str     = target_date.strftime("%Y-%m-%d")
    if output_dir is None:
        try:
            output_dir = Path(get_config().paths.history)
        except Exception:
            output_dir = Path("data/history")

    t_path = output_dir / f"{symbol}_trades_{date_compact}.csv"
    b_path = output_dir / f"{symbol}_bidask_{date_compact}.csv"

    issues = []
    if not t_path.exists() or not b_path.exists():
        return "skip", ["INFO: one or both files missing — skip cross-check"]

    def _count(p):
        with open(p, encoding="utf-8") as f:
            return sum(1 for _ in f) - 1  # subtract header

    tc = _count(t_path)
    bc = _count(b_path)

    ratio = bc / tc if tc else 0
    msg   = f"TRADES {tc:,}  BID_ASK {bc:,}  ratio {ratio:.2f}"
    if verbose:
        print(f"\n--- Cross-check {symbol} {date_str} ---")
        print(f"  {msg}")

    if ratio < 0.3 or ratio > 3.0:
        issues.append(("warn", f"count mismatch: {msg}"))
        return "warn", _fmt_notes(issues)

    if verbose:
        print("  PASS  counts consistent")
    return "pass", _fmt_notes(issues)


# ── Public entry point ────────────────────────────────────────────────────────

def check_and_mark(symbol: str, target_date: date, dtype: str,
                   output_dir: Path = None) -> bool:
    """Run check_file, write result to DB. Returns True unless status == 'fail'."""
    status, notes = check_file(symbol, target_date, dtype, output_dir)
    try:
        cfg  = get_config()
        conn = _open_progress_db(cfg)
        _mark_verified(conn, symbol, target_date.strftime("%Y-%m-%d"),
                       dtype.upper(), status, notes)
        conn.close()
    except Exception as e:
        log.warning(f"Could not write verify result to DB: {e}")
    return status != "fail"


# ── Read last price from CSV ──────────────────────────────────────────────────

def last_price(symbol: str, output_dir: Path = None) -> dict:
    """
    Read the last trade price from the most recent TRADES CSV for symbol.
    Returns dict with keys: price, time_ct, date, high, low  — or None.
    """
    if output_dir is None:
        try:
            output_dir = Path(get_config().paths.history)
        except Exception:
            output_dir = Path("data/history")

    files = sorted(output_dir.glob(f"{symbol}_trades_*.csv"), reverse=True)
    if not files:
        return None

    path = files[0]
    try:
        # Fast: read last few lines for last price
        tail_lines = _read_last_lines(path, n=20)
        with open(path, encoding="utf-8") as f:
            headers = f.readline().strip().split(",")

        last_row = None
        for line in reversed(tail_lines):
            parts = line.split(",")
            if len(parts) == len(headers):
                last_row = dict(zip(headers, parts))
                break

        if not last_row:
            return None

        last_p = float(last_row["price"])
        last_t = datetime.fromisoformat(last_row["time_utc"]).astimezone(CT)

        # High/low: scan last 2000 bytes efficiently for sample
        # (full scan for accuracy — ~50ms for 2MB file)
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            prices = [float(r["price"]) for r in reader if r.get("price")]

        src_date = path.stem.split("_")[2]  # YYYYMMDD

        return {
            "price":   last_p,
            "time_ct": last_t.strftime("%H:%M CT"),
            "date":    f"{src_date[:4]}-{src_date[4:6]}-{src_date[6:]}",
            "high":    max(prices) if prices else last_p,
            "low":     min(prices) if prices else last_p,
            "rows":    len(prices),
        }
    except Exception as e:
        log.debug(f"last_price {symbol}: {e}")
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galgo data correctness checker")
    parser.add_argument("--symbol",      default=None)
    parser.add_argument("--date",        default=None, help="YYYY-MM-DD")
    parser.add_argument("--dtype",       default="trades", choices=["trades", "bidask"])
    parser.add_argument("--all-pending", action="store_true",
                        help="Check all finished-but-unverified files")
    parser.add_argument("--cross-check", action="store_true",
                        help="Run TRADES vs BID_ASK cross-check")
    args = parser.parse_args()

    cfg        = get_config()
    output_dir = Path(cfg.paths.history)

    if args.all_pending:
        conn = _open_progress_db(cfg)
        rows = conn.execute(
            "SELECT symbol, date, data_type FROM fetch_progress "
            "WHERE finished=1 AND (verified IS NULL OR verified=0)"
        ).fetchall()
        conn.close()
        if not rows:
            print("No unverified finished files.")
            sys.exit(0)
        all_ok = True
        for r in rows:
            sym, date_s, dtype = r["symbol"], r["date"], r["data_type"].lower()
            if not check_and_mark(sym, date.fromisoformat(date_s), dtype, output_dir):
                all_ok = False
        sys.exit(0 if all_ok else 1)

    if not args.symbol or not args.date:
        parser.error("--symbol and --date required (or --all-pending)")

    sym    = args.symbol.upper()
    target = date.fromisoformat(args.date)

    if args.cross_check:
        status, notes = check_cross(sym, target, output_dir)
        print(f"\nCross-check result: {status}")
        for n in notes:
            print(f"  {n}")
        sys.exit(0 if status != "fail" else 1)

    ok = check_and_mark(sym, target, args.dtype, output_dir)
    sys.exit(0 if ok else 1)
