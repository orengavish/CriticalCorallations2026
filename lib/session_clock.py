"""
lib/session_clock.py
Market-close awareness for the entry-cutoff / forced-flatten window, generalized across
both asset classes now live in this system: the 4 futures (CME/CBOT, close ~16:00 CT --
matches CriticalExtraction's own trades-based backtest convention, which used each day's
actual last trade print as "close", empirically ~15:59:57 CT) and the ~100-stock research
universe (regular US equity session, close 16:00 America/New_York).

Two windows, matching the backtest design this mirrors (backtest/simulate_trades.py):
  - entry cutoff: stop searching for NEW entries this many minutes before close.
  - forced-exit:  force-flatten anything still open this many minutes before close.

Usage:
    from lib.session_clock import is_entry_cutoff, is_forced_exit_time, seconds_until_close

Self-test:
    python -m lib.session_clock --self-test
"""

import sys
import argparse
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_FUTURES_SYMBOLS = {"MES", "MNQ", "MYM", "M2K"}

_FUTURES_TZ = ZoneInfo("America/Chicago")
_FUTURES_CLOSE = dtime(16, 0)   # matches simulate_trades.py's empirical ~16:00 CT close
_FUTURES_OPEN = dtime(8, 30)    # CME equity-index RTH open, matches cash-market 9:30 ET open

_STOCK_TZ = ZoneInfo("America/New_York")
_STOCK_CLOSE = dtime(16, 0)     # regular US equity session close
_STOCK_OPEN = dtime(9, 30)      # regular US equity session open


def _close_today(symbol: str, now: datetime) -> datetime:
    """Today's close datetime for symbol's asset class, in that class's own timezone,
    converted to `now`'s timezone for a direct comparison."""
    if symbol in _FUTURES_SYMBOLS:
        tz, close_t = _FUTURES_TZ, _FUTURES_CLOSE
    else:
        tz, close_t = _STOCK_TZ, _STOCK_CLOSE
    local_now = now.astimezone(tz)
    close_local = local_now.replace(hour=close_t.hour, minute=close_t.minute,
                                     second=0, microsecond=0)
    return close_local.astimezone(now.tzinfo or ZoneInfo("UTC"))


def _open_today(symbol: str, now: datetime) -> datetime:
    """Today's regular-session open datetime for symbol's asset class, same convention
    as _close_today."""
    if symbol in _FUTURES_SYMBOLS:
        tz, open_t = _FUTURES_TZ, _FUTURES_OPEN
    else:
        tz, open_t = _STOCK_TZ, _STOCK_OPEN
    local_now = now.astimezone(tz)
    open_local = local_now.replace(hour=open_t.hour, minute=open_t.minute,
                                    second=0, microsecond=0)
    return open_local.astimezone(now.tzinfo or ZoneInfo("UTC"))


def seconds_until_open(symbol: str, now: datetime | None = None) -> float:
    """Seconds until symbol's market opens today. Negative if already past today's open."""
    now = now or datetime.now(ZoneInfo("UTC"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return (_open_today(symbol, now) - now).total_seconds()


def is_before_open(symbol: str, now: datetime | None = None) -> bool:
    """True if symbol's regular session has not opened yet today -- no new entries should
    be generated or submitted (pre-market/after-hours prices are thin and unreliable)."""
    return seconds_until_open(symbol, now) > 0


def seconds_until_close(symbol: str, now: datetime | None = None) -> float:
    """Seconds remaining until symbol's market closes today. Negative if already past
    close (a holiday or an after-hours run -- callers treat negative the same as
    "inside every cutoff window", not as an error)."""
    now = now or datetime.now(ZoneInfo("UTC"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return (_close_today(symbol, now) - now).total_seconds()


def is_entry_cutoff(symbol: str, now: datetime | None = None, cutoff_minutes: float = 30) -> bool:
    """True once within cutoff_minutes of close (or past it) -- stop searching for new
    entries for this symbol."""
    return seconds_until_close(symbol, now) <= cutoff_minutes * 60


def is_forced_exit_time(symbol: str, now: datetime | None = None, cutoff_minutes: float = 5) -> bool:
    """True once within cutoff_minutes of close (or past it) -- force-flatten anything
    still open for this symbol."""
    return seconds_until_close(symbol, now) <= cutoff_minutes * 60


def self_test() -> bool:
    try:
        utc = ZoneInfo("UTC")

        # MES (CT, close 16:00): 15:45 CT -> 15 min to close -> inside 30-min entry
        # cutoff, NOT yet inside the 5-min forced-exit window.
        t = datetime(2026, 9, 8, 15, 45, tzinfo=_FUTURES_TZ).astimezone(utc)
        assert is_entry_cutoff("MES", t, 30) is True
        assert is_forced_exit_time("MES", t, 5) is False

        # 15:57 CT -> 3 min to close -> inside BOTH windows.
        t2 = datetime(2026, 9, 8, 15, 57, tzinfo=_FUTURES_TZ).astimezone(utc)
        assert is_entry_cutoff("MES", t2, 30) is True
        assert is_forced_exit_time("MES", t2, 5) is True

        # 10:00 CT -> mid-morning, hours from close -> neither window.
        t3 = datetime(2026, 9, 8, 10, 0, tzinfo=_FUTURES_TZ).astimezone(utc)
        assert is_entry_cutoff("MES", t3, 30) is False
        assert is_forced_exit_time("MES", t3, 5) is False

        # Same wall-clock moment, but AAPL (ET close) is on a DIFFERENT clock than MES
        # (CT close) -- 15:45 CT == 16:45 ET, already an hour past AAPL's close.
        assert is_entry_cutoff("AAPL", t, 30) is True
        assert is_forced_exit_time("AAPL", t, 5) is True
        assert seconds_until_close("AAPL", t) < 0

        # AAPL mid-morning (10:00 CT == 11:00 ET): 5 hours from its own 16:00 ET close.
        assert is_entry_cutoff("AAPL", t3, 30) is False

        # Pre-market: 6:00 CT is before both MES's 8:30 CT open and AAPL's 9:30 ET open.
        t4 = datetime(2026, 9, 8, 6, 0, tzinfo=_FUTURES_TZ).astimezone(utc)
        assert is_before_open("MES", t4) is True
        assert is_before_open("AAPL", t4) is True

        # Mid-morning (10:00 CT == 11:00 ET): both already open.
        assert is_before_open("MES", t3) is False
        assert is_before_open("AAPL", t3) is False

        print("[self-test] session_clock: PASS")
        return True
    except Exception as e:
        print(f"[self-test] session_clock: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("session_clock — run --self-test to verify logic")
