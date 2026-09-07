"""
random_gen.py
Random trade generator for Galao paper-trading baseline.

Generates random bracket orders (MKT / LMT / STP) at a configurable rate and
writes them as PENDING commands to the DB. The existing broker + position_manager
handle submission, fills, and exits — so every trade flows through the full
tracked lifecycle and appears in the P&L report.

Sources written to commands.source:
  random_mkt   Market orders  — fill immediately at current price
  random_lmt   Limit orders   — entry offset 1-8 ticks away from market
  random_stp   Stop orders    — entry offset 1-8 ticks away from market

Usage:
    python random_gen.py                          # live mode, 6 trades/min
    python random_gen.py --rate 12                # 12 trades/min
    python random_gen.py --dry-run                # simulate full lifecycle offline
    python random_gen.py --dry-run --rate 60      # stress test
    python random_gen.py --self-test
"""

import sys
import time
import random
import argparse
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_system_state, update_command_status

log = get_logger("random_gen")

_DEFAULT_RATE      = 6    # trades per minute
_MKT_WEIGHT        = 0.50
_LMT_WEIGHT        = 0.25
_STP_WEIGHT        = 0.25
_DEFAULT_MAX_OFFSET_TICKS = 2


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_shutdown(db_path) -> bool:
    with get_db(db_path) as con:
        val = get_system_state(con, "SESSION")
    return val == "SHUTDOWN"


def _rt(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def _pick_entry_type() -> str:
    return random.choices(
        ["MKT", "LMT", "STP"],
        weights=[_MKT_WEIGHT, _LMT_WEIGHT, _STP_WEIGHT]
    )[0]


def _build_trade(symbol: str, price: float, cfg,
                 bracket_override: float = None,
                 max_offset_ticks: int = _DEFAULT_MAX_OFFSET_TICKS) -> dict:
    """Return a dict of command fields for one random bracket trade."""
    tick         = cfg.orders.tick_size
    bracket_size = bracket_override if bracket_override else random.choice(cfg.orders.active_brackets)
    direction    = random.choice(["BUY", "SELL"])
    entry_type   = _pick_entry_type()
    offset       = random.randint(1, max(1, max_offset_ticks)) * tick

    if entry_type == "MKT":
        entry_price = _rt(price, tick)
    elif entry_type == "LMT":
        # Limit resting below market for BUY, above for SELL
        entry_price = _rt(price - offset, tick) if direction == "BUY" else _rt(price + offset, tick)
    else:  # STP
        # Stop triggered above market for BUY, below for SELL
        entry_price = _rt(price + offset, tick) if direction == "BUY" else _rt(price - offset, tick)

    if direction == "BUY":
        tp_price = _rt(entry_price + bracket_size, tick)
        sl_price = _rt(entry_price - bracket_size, tick)
    else:
        tp_price = _rt(entry_price - bracket_size, tick)
        sl_price = _rt(entry_price + bracket_size, tick)

    return {
        "symbol":       symbol,
        "line_price":   entry_price,
        "line_type":    "SUPPORT" if direction == "BUY" else "RESISTANCE",
        "line_strength": random.randint(1, 3),
        "direction":    direction,
        "entry_type":   entry_type,
        "entry_price":  entry_price,
        "tp_price":     tp_price,
        "sl_price":     sl_price,
        "bracket_size": bracket_size,
        "source":       f"random_{entry_type.lower()}",
        "quantity":     cfg.orders.quantity,
    }


def _insert_pending(db_path, trade: dict) -> int:
    with get_db(db_path) as con:
        con.execute("""
            INSERT INTO commands
                (symbol, line_price, line_type, line_strength,
                 direction, entry_type, entry_price, tp_price, sl_price,
                 bracket_size, source, quantity, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """, (
            trade["symbol"], trade["line_price"], trade["line_type"], trade["line_strength"],
            trade["direction"], trade["entry_type"],
            trade["entry_price"], trade["tp_price"], trade["sl_price"],
            trade["bracket_size"], trade["source"], trade["quantity"],
        ))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _simulate_lifecycle(db_path, cmd_id: int, trade: dict, tick: float):
    """
    Advance a PENDING command through the full lifecycle without IB.
    Used only in --dry-run mode for offline testing.
    """
    now = _now_utc()
    fake_oid = cmd_id * 10

    with get_db(db_path) as con:
        update_command_status(con, cmd_id, "SUBMITTED",
                              ib_order_id=fake_oid,
                              ib_tp_order_id=fake_oid + 1,
                              ib_sl_order_id=fake_oid + 2,
                              claimed_at=now)

    # Simulate fill at entry price ± small slippage
    slip = random.randint(0, 1) * tick * (1 if random.random() < 0.5 else -1)
    fill_price = _rt(trade["entry_price"] + slip, tick)

    with get_db(db_path) as con:
        update_command_status(con, cmd_id, "FILLED",
                              fill_price=fill_price, fill_time=now)

    # Randomly decide TP / SL / STAGNATION exit
    exit_choice = random.choices(
        ["TP", "SL", "STAGNATION"],
        weights=[0.45, 0.45, 0.10]
    )[0]

    d = trade["direction"]
    bs = trade["bracket_size"]

    if exit_choice == "TP":
        exit_price = _rt(fill_price + bs, tick) if d == "BUY" else _rt(fill_price - bs, tick)
    elif exit_choice == "SL":
        exit_price = _rt(fill_price - bs, tick) if d == "BUY" else _rt(fill_price + bs, tick)
    else:
        exit_price = fill_price

    pnl = (exit_price - fill_price) if d == "BUY" else (fill_price - exit_price)

    with get_db(db_path) as con:
        update_command_status(con, cmd_id, "CLOSED",
                              exit_price=exit_price,
                              exit_time=now,
                              exit_reason=exit_choice,
                              pnl_points=round(pnl, 4))


def run_gen(db_path, cfg, rate_per_min: float = _DEFAULT_RATE,
            dry_run: bool = False, symbol: str = None,
            bracket_override: float = None,
            max_offset_ticks: int = _DEFAULT_MAX_OFFSET_TICKS,
            count: int | None = None):
    """
    Main generator loop.
    dry_run: simulate fill+close in-process (no IB needed).
    count: if set, generate exactly this many commands then exit instead of
        running forever -- lets this be triggered as a scheduled burst (e.g.
        matching GevaExtract's 3x/day cadence on a comparison symbol) rather
        than only as an always-on background process. None (default) = unchanged
        continuous behavior.
    """
    symbol      = symbol or cfg.symbols[0]
    tick        = cfg.orders.tick_size
    sleep_secs  = 60.0 / max(rate_per_min, 0.1)

    log.info(
        f"random_gen starting — symbol={symbol} rate={rate_per_min}/min "
        f"sleep={sleep_secs:.1f}s bracket={bracket_override or 'random'} "
        f"max_offset={max_offset_ticks}ticks dry_run={dry_run}"
    )

    if not dry_run:
        from lib.ib_client import IBClient
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=False)
        log.info("IB LIVE connection established for price feed")
    else:
        ibc = None
        log.warning("DRY-RUN mode — no IB connection, using random-walk price")

    # Random-walk price for dry-run
    sim_price = 5500.0

    generated = 0
    try:
        while True:
            if count is not None and generated >= count:
                log.info(f"random_gen: generated {generated}/{count} — burst complete, exiting")
                break
            if _is_shutdown(db_path):
                log.info("SESSION=SHUTDOWN — random_gen exiting")
                break

            # Get current price
            if ibc:
                try:
                    price = ibc.get_price(symbol)
                except Exception as e:
                    log.warning(f"Price fetch failed: {e} — skipping this tick")
                    time.sleep(5)
                    continue
                if price is None:
                    log.warning("Price is None — skipping this tick")
                    time.sleep(5)
                    continue
            else:
                sim_price += random.gauss(0, 0.5)
                price = round(sim_price, 2)

            trade = _build_trade(symbol, price, cfg,
                                 bracket_override=bracket_override,
                                 max_offset_ticks=max_offset_ticks)
            cmd_id = _insert_pending(db_path, trade)

            log.info(
                f"Inserted #{cmd_id} {trade['source']} {trade['direction']} "
                f"{trade['entry_type']} entry={trade['entry_price']} "
                f"TP={trade['tp_price']} SL={trade['sl_price']}"
            )

            if dry_run:
                _simulate_lifecycle(db_path, cmd_id, trade, tick)
                log.debug(f"  Simulated lifecycle for #{cmd_id}")

            generated += 1
            if count is not None and generated >= count:
                log.info(f"random_gen: generated {generated}/{count} — burst complete, exiting")
                break

            time.sleep(sleep_secs)

    except KeyboardInterrupt:
        log.info("random_gen interrupted")
    finally:
        if ibc:
            ibc.disconnect()
        log.info("random_gen stopped")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import set_system_state

        cfg = get_config()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")

            price = 5500.0
            trade = _build_trade("MES", price, cfg)

            assert trade["source"].startswith("random_"), f"Bad source: {trade['source']}"
            assert trade["direction"] in ("BUY", "SELL")
            assert trade["entry_type"] in ("MKT", "LMT", "STP")
            assert trade["tp_price"] != trade["sl_price"]

            cmd_id = _insert_pending(db_path, trade)
            assert cmd_id > 0

            with get_db(db_path) as con:
                row = con.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
            assert row["status"] == "PENDING"
            assert row["source"] == trade["source"]

            _simulate_lifecycle(db_path, cmd_id, trade, cfg.orders.tick_size)

            with get_db(db_path) as con:
                row = con.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
            assert row["status"] == "CLOSED", f"Expected CLOSED, got {row['status']}"
            assert row["pnl_points"] is not None, "pnl_points not set after simulation"
            assert row["exit_reason"] in ("TP", "SL", "STAGNATION")

            # Verify multiple trades accumulate correctly
            for _ in range(9):
                t = _build_trade("MES", price + random.gauss(0, 1), cfg)
                cid = _insert_pending(db_path, t)
                _simulate_lifecycle(db_path, cid, t, cfg.orders.tick_size)

            with get_db(db_path) as con:
                count = con.execute(
                    "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
                ).fetchone()[0]
            assert count == 10, f"Expected 10 CLOSED, got {count}"

            reset_loggers()

        print("[self-test] random_gen: PASS")
        return True

    except Exception as e:
        print(f"[self-test] random_gen: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao random trade generator")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate full lifecycle without IB")
    parser.add_argument("--rate", type=float, default=_DEFAULT_RATE,
                        metavar="N", help="Trades per minute (default 6)")
    parser.add_argument("--bracket", type=float, default=None,
                        metavar="PTS", help="Fixed bracket size in points (default: random from config)")
    parser.add_argument("--max-offset", type=int, default=_DEFAULT_MAX_OFFSET_TICKS,
                        metavar="T", help="Max entry offset from live price in ticks (default 8; use 1-2 for near-market fills)")
    parser.add_argument("--symbol", default=None, help="Override symbol from config")
    parser.add_argument("--count", type=int, default=None, metavar="N",
                        help="Generate exactly N commands then exit (burst mode, for "
                             "scheduled runs) instead of running forever")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg     = get_config()
    db_path = Path(cfg.paths.db)
    init_db(db_path)

    run_gen(db_path, cfg,
            rate_per_min=args.rate,
            dry_run=args.dry_run,
            symbol=args.symbol,
            bracket_override=args.bracket,
            max_offset_ticks=args.max_offset,
            count=args.count)
