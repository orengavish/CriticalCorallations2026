"""
decider.py
Decider component for Galao.
Generates trading commands from critical lines at session start,
and replenishes commands after fills (re-evaluating toggle).

Responsibilities:
  - At session start: load critical lines, generate PENDING commands for all
    armed lines (both BUY and SELL directions, all active brackets)
  - Replenishment loop: poll DB for FILLED commands, write one new PENDING per fill
  - Replenishment is fully disabled when SESSION=SHUTDOWN (R-SHD-07)
  - Never submits orders — writes PENDING to DB only (R-DEV-04)
  - Toggle re-evaluated at every command generation (R-ORD-05)

Usage:
    python decider.py --mode session    # full session (start + replenishment loop)
    python decider.py --mode replenish  # replenishment loop only
    python decider.py --self-test

Self-test:
    python decider.py --self-test
"""

import sys
import time
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_filled_commands, get_system_state, set_system_state
from lib.order_builder import determine_entry_type, calc_bracket_prices, round_tick
from lib.critical_lines import get_armed_lines

log = get_logger("decider")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_shutdown(db_path) -> bool:
    with get_db(db_path) as con:
        val = get_system_state(con, "SESSION")
    return val == "SHUTDOWN"


def get_current_price(symbol: str, ibc=None) -> float | None:
    """
    Get current market price for toggle evaluation.
    If ibc is not provided, falls back to last known price in DB (or None).
    """
    if ibc:
        try:
            return ibc.get_price(symbol)
        except Exception as e:
            log.warning(f"Could not fetch live price for {symbol}: {e}")

    # Fallback: use fill_price of most recent FILLED command for this symbol
    return None


def generate_commands(symbol: str, date_str: str, current_price: float,
                      cfg, db_path) -> int:
    """
    Generate PENDING commands for all armed critical lines for symbol+date.
    Creates commands in BOTH directions (BUY + SELL) for each line,
    for each active bracket size.
    Returns number of commands inserted.
    """
    tick   = cfg.orders.tick_size
    qty    = cfg.orders.quantity
    brackets = cfg.orders.active_brackets

    with get_db(db_path) as con:
        lines = get_armed_lines(con, symbol, date_str)

    if not lines:
        log.warning(f"No armed lines for {symbol} {date_str} — nothing to generate")
        return 0

    # Dedup guard: skip (line, direction, bracket) combos that already have an
    # unresolved command in flight. Without this, every run_session_start() call
    # (e.g. each time a session is restarted) re-generates a full fresh batch on
    # top of whatever's still unfilled from the last one, with no cap — this is
    # exactly how MES accumulated 425 stale resting orders across repeated
    # restarts in one day (2026-07-17 incident). CLOSED/CANCELLED/ERROR/FILLED
    # commands don't block regeneration — only ones still actively working do.
    with get_db(db_path) as con:
        in_flight = {
            (r["critical_line_id"], r["direction"], r["bracket_size"])
            for r in con.execute(
                "SELECT critical_line_id, direction, bracket_size FROM commands"
                " WHERE symbol=? AND status IN ('PENDING','SUBMITTING','SUBMITTED')",
                (symbol,)
            ).fetchall()
        }

    count = 0
    skipped = 0
    for line in lines:
        line_price  = line["price"]
        line_type   = line["line_type"]
        strength    = line["strength"]

        for bracket_size in brackets:
            for direction in ("BUY", "SELL"):
                if (line["id"], direction, bracket_size) in in_flight:
                    skipped += 1
                    continue
                entry_type = determine_entry_type(direction, current_price, line_price)
                prices = calc_bracket_prices(
                    direction, entry_type, line_price, bracket_size, tick
                )
                with get_db(db_path) as con:
                    con.execute("""
                        INSERT INTO commands
                            (symbol, line_price, line_type, line_strength,
                             direction, entry_type, entry_price, tp_price, sl_price,
                             bracket_size, source, critical_line_id, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'critical_line', ?, ?, 'PENDING')
                    """, (
                        symbol, line_price, line_type, strength,
                        direction, entry_type,
                        prices["entry_price"], prices["tp_price"], prices["sl_price"],
                        bracket_size, line["id"], qty
                    ))
                count += 1
                log.debug(
                    f"Generated {direction} {entry_type} {symbol} "
                    f"line={line_price} bracket={bracket_size} "
                    f"entry={prices['entry_price']} TP={prices['tp_price']} SL={prices['sl_price']}"
                )

    log.info(f"Generated {count} commands for {symbol} {date_str} "
             f"({len(lines)} lines x {len(brackets)} brackets x 2 directions, "
             f"{skipped} skipped as already in flight)")
    return count


def replenish(symbol: str, date_str: str, current_price: float,
              cfg, db_path) -> int:
    """
    For each FILLED command (replenishment_issued=0), generate one replacement
    PENDING command (same line, re-evaluate toggle).
    Marks original as replenishment_issued=1 to prevent double-replenishment (R-ORD-10).
    Fully disabled when SESSION=SHUTDOWN (R-SHD-07).
    Returns number of commands replenished.
    """
    if _is_shutdown(db_path):
        log.debug("Replenishment disabled — SESSION=SHUTDOWN")
        return 0

    tick = cfg.orders.tick_size
    qty  = cfg.orders.quantity

    with get_db(db_path) as con:
        filled = get_filled_commands(con, symbol)

    if not filled:
        return 0

    count = 0
    for cmd in filled:
        cid = cmd["id"]

        # Mark as replenishment_issued atomically before generating replacement
        with get_db(db_path) as con:
            cur = con.execute(
                "UPDATE commands SET replenishment_issued=1,"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                " WHERE id=? AND replenishment_issued=0",
                (cid,)
            )
            if cur.rowcount == 0:
                log.debug(f"Command {cid} already replenished — skip")
                continue

        # Check armed status (may have been disarmed by SL cool-down)
        with get_db(db_path) as con:
            line_row = con.execute(
                "SELECT * FROM critical_lines WHERE symbol=? AND date=?"
                " AND price=? AND armed=1",
                (cmd["symbol"], date_str, cmd["line_price"])
            ).fetchone()

        if not line_row:
            log.info(f"Command {cid}: line {cmd['line_price']} is disarmed — no replenishment")
            continue

        # Re-evaluate toggle with current price
        entry_type = determine_entry_type(cmd["direction"], current_price, cmd["line_price"])
        prices = calc_bracket_prices(
            cmd["direction"], entry_type,
            cmd["line_price"], cmd["bracket_size"], tick
        )

        with get_db(db_path) as con:
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength,
                     direction, entry_type, entry_price, tp_price, sl_price,
                     bracket_size, source, quantity, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'critical_line', ?, 'PENDING')
            """, (
                cmd["symbol"], cmd["line_price"], cmd["line_type"], cmd["line_strength"],
                cmd["direction"], entry_type,
                prices["entry_price"], prices["tp_price"], prices["sl_price"],
                cmd["bracket_size"], qty
            ))

        log.info(
            f"Replenished command {cid}: {cmd['direction']} {entry_type} "
            f"line={cmd['line_price']} bracket={cmd['bracket_size']} "
            f"entry={prices['entry_price']}"
        )
        count += 1

    return count


def run_session_start(ibc, cfg, db_path, date_str: str = None):
    """
    Session start: read critical lines already in DB (entered via GUI),
    fetch price, generate all commands.
    Called once at the beginning of a trading session.
    """
    date_str = date_str or date.today().strftime("%Y-%m-%d")

    for symbol in cfg.symbols:
        # Lines come from DB (entered via /lines GUI) — just count them
        with get_db(db_path) as con:
            n = con.execute(
                "SELECT COUNT(*) FROM critical_lines WHERE symbol=? AND date=? AND armed=1",
                (symbol, date_str)
            ).fetchone()[0]
        log.info(f"Found {n} armed critical lines in DB for {symbol} {date_str}")

        # Get current price
        price = get_current_price(symbol, ibc)
        if price is None:
            raise ValueError(f"Cannot get current price for {symbol} — abort session start")

        # Generate commands
        count = generate_commands(symbol, date_str, price, cfg, db_path)
        log.info(f"Session start: {count} commands generated for {symbol}")

    with get_db(db_path) as con:
        set_system_state(con, "SESSION", "RUNNING")
    log.info("Session state set to RUNNING")


def run_replenishment_loop(ibc, cfg, db_path, date_str: str = None):
    """
    Replenishment loop: polls for filled commands and replenishes.
    Runs until SESSION=SHUTDOWN.
    """
    date_str = date_str or date.today().strftime("%Y-%m-%d")
    poll_seconds = cfg.decider.replenishment_poll_seconds
    log.info(f"Replenishment loop started — polling every {poll_seconds}s")

    while True:
        if _is_shutdown(db_path):
            log.info("SESSION=SHUTDOWN — replenishment loop exiting")
            break

        # Reconnect if IB went down (e.g. IBC watchdog restart)
        if ibc and not ibc.is_live_connected():
            log.warning("LIVE connection lost — attempting reconnect")
            try:
                ok = ibc.reconnect(live=True, paper=False, max_attempts=3)
                if ok:
                    log.info("Reconnected to LIVE")
                else:
                    log.warning("Reconnect failed — will retry next poll")
                    time.sleep(poll_seconds)
                    continue
            except Exception as e:
                log.warning(f"Reconnect error: {e} — will retry next poll")
                time.sleep(poll_seconds)
                continue

        for symbol in cfg.symbols:
            price = get_current_price(symbol, ibc)
            if price is None:
                log.warning(f"No price for {symbol} — skipping replenishment")
                continue
            n = replenish(symbol, date_str, price, cfg, db_path)
            if n:
                log.info(f"Replenished {n} command(s) for {symbol}")

        time.sleep(poll_seconds)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import set_system_state, update_command_status

        cfg = get_config()
        tick = cfg.orders.tick_size

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            # Insert critical lines directly into DB (simulates GUI entry)
            today = "2026-04-07"
            with get_db(db_path) as con:
                for line_type, price, strength in [
                    ("SUPPORT",    6490.00, 2),
                    ("RESISTANCE", 6510.00, 1),
                ]:
                    con.execute(
                        "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                        " VALUES ('MES', ?, ?, ?, ?, 1)",
                        (today, line_type, price, strength)
                    )

            # Simulate current price at 6500 (between lines)
            current_price = 6500.0
            brackets = cfg.orders.active_brackets  # [2, 4]

            # 1. Generate commands
            n = generate_commands("MES", today, current_price, cfg, db_path)
            expected = 2 * len(brackets) * 2  # 2 lines * N brackets * 2 directions
            assert n == expected, f"Expected {expected} commands, got {n}"

            with get_db(db_path) as con:
                rows = con.execute("SELECT * FROM commands WHERE status='PENDING'").fetchall()
            assert len(rows) == expected

            # Verify toggle: price=6500 ABOVE line 6490 → BUY=LMT, SELL=STP
            buy_rows  = [r for r in rows if r["direction"] == "BUY"  and r["line_price"] == 6490.0]
            sell_rows = [r for r in rows if r["direction"] == "SELL" and r["line_price"] == 6490.0]
            assert any(r["entry_type"] == "LMT" for r in buy_rows),  "6490 BUY should be LMT"
            assert any(r["entry_type"] == "STP" for r in sell_rows), "6490 SELL should be STP"

            # Verify toggle: price=6500 BELOW line 6510 → BUY=STP, SELL=LMT
            buy_rows2  = [r for r in rows if r["direction"] == "BUY"  and r["line_price"] == 6510.0]
            sell_rows2 = [r for r in rows if r["direction"] == "SELL" and r["line_price"] == 6510.0]
            assert any(r["entry_type"] == "STP" for r in buy_rows2),  "6510 BUY should be STP"
            assert any(r["entry_type"] == "LMT" for r in sell_rows2), "6510 SELL should be LMT"

            # 1b. Dedup guard: calling generate_commands again (e.g. a session
            # restart) with the same still-unresolved commands must not create
            # duplicates -- this is the fix for the 2026-07-17 incident where
            # repeated restarts piled up 425 stale MES orders with no cap.
            n_again = generate_commands("MES", today, current_price, cfg, db_path)
            assert n_again == 0, f"Expected 0 new commands (all in flight), got {n_again}"
            with get_db(db_path) as con:
                total_after = con.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            assert total_after == expected, \
                f"Dedup guard failed -- expected {expected} total commands, got {total_after}"

            # 2. Replenishment test
            # Mark one command as FILLED
            cmd_id = rows[0]["id"]
            with get_db(db_path) as con:
                update_command_status(
                    con, cmd_id, "FILLED",
                    fill_price = current_price,
                    fill_time  = _now_utc(),
                )

            n_replenished = replenish("MES", today, current_price, cfg, db_path)
            assert n_replenished == 1, f"Expected 1 replenishment, got {n_replenished}"

            # No double-replenishment
            n_replenished2 = replenish("MES", today, current_price, cfg, db_path)
            assert n_replenished2 == 0, "Double replenishment detected"

            # 3. Replenishment disabled on SHUTDOWN
            with get_db(db_path) as con:
                update_command_status(con, rows[1]["id"], "FILLED",
                                      fill_price=current_price, fill_time=_now_utc())
                set_system_state(con, "SESSION", "SHUTDOWN")
            n_shutdown = replenish("MES", today, current_price, cfg, db_path)
            assert n_shutdown == 0, "Replenishment should be disabled on SHUTDOWN"

            reset_loggers()

        print("[self-test] decider: PASS")
        return True

    except Exception as e:
        print(f"[self-test] decider: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao decider")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--mode", choices=["session", "replenish"],
                        default="session",
                        help="session=full run, replenish=replenishment loop only")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    db_path = Path(cfg.paths.db)
    init_db(db_path)

    from lib.ib_client import IBClient
    ibc = IBClient(cfg)
    ibc.connect(live=True, paper=False)  # Decider only needs LIVE for price

    if args.mode == "session":
        run_session_start(ibc, cfg, db_path)
        run_replenishment_loop(ibc, cfg, db_path)
    else:
        run_replenishment_loop(ibc, cfg, db_path)

    ibc.disconnect()
