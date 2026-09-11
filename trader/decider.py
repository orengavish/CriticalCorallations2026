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
import uuid
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_filled_commands, get_system_state, set_system_state, update_command_status
from lib.order_builder import determine_entry_type, calc_bracket_prices, round_tick, get_tick_size
from lib.critical_lines import get_armed_lines
from lib.session_clock import (is_entry_cutoff, is_forced_exit_time, is_before_open,
                                seconds_until_open, is_before_trading_start,
                                seconds_until_trading_start, _FUTURES_SYMBOLS)

log = get_logger("decider")

# Control-group sources fan out to fewer brackets than real/treatment lines --
# see generate_commands()'s brackets_control.
_CONTROL_SOURCES = {"geva_manual_control", "research_random", "research_random_stock"}


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

    No new entries within 30 minutes of symbol's own market close (session_clock.py) --
    mirrors the backtest's entry-cutoff rule (backtest/simulate_trades.py). Each symbol
    is checked against its OWN close time/timezone (futures: CT; stocks: ET), not a
    single shared clock.
    """
    if is_entry_cutoff(symbol):
        log.info(f"{symbol}: within entry cutoff of close -- not generating new commands")
        return 0

    tick   = get_tick_size(symbol)
    qty    = cfg.orders.quantity
    brackets_real = cfg.orders.active_brackets
    # 2026-09-10: control-group lines fan out to 1 bracket size instead of all of them --
    # the real-vs-control comparison holds at the line level, this just cuts control's
    # order volume to a third with no loss of what's being tested (user decision).
    brackets_control = getattr(cfg.orders, "control_active_brackets", None) or brackets_real[:1]

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
        # Propagate the line's own source (e.g. geva_manual, geva_manual_control,
        # research_ce, research_random) into the commands it generates, instead of
        # flattening everything to 'critical_line' -- otherwise real-signal and
        # control-group trades become indistinguishable downstream (2026-09-09).
        line_source = line["source"] or "critical_line"
        brackets = brackets_control if line_source in _CONTROL_SOURCES else brackets_real

        for bracket_size in brackets:
            for direction in ("BUY", "SELL"):
                if (line["id"], direction, bracket_size) in in_flight:
                    skipped += 1
                    continue
                entry_type = determine_entry_type(direction, current_price, line_price)
                prices = calc_bracket_prices(
                    direction, entry_type, line_price, bracket_size, tick
                )
                logical_trade_id = str(uuid.uuid4())
                with get_db(db_path) as con:
                    con.execute("""
                        INSERT INTO commands
                            (symbol, line_price, line_type, line_strength,
                             direction, entry_type, entry_price, tp_price, sl_price,
                             bracket_size, source, critical_line_id, quantity,
                             logical_trade_id, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                    """, (
                        symbol, line_price, line_type, strength,
                        direction, entry_type,
                        prices["entry_price"], prices["tp_price"], prices["sl_price"],
                        bracket_size, line_source, line["id"], qty, logical_trade_id
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

    if is_entry_cutoff(symbol):
        log.debug(f"{symbol}: within entry cutoff of close -- replenishment disabled")
        return 0

    tick = get_tick_size(symbol)
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
                     bracket_size, source, quantity, logical_trade_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                cmd["symbol"], cmd["line_price"], cmd["line_type"], cmd["line_strength"],
                cmd["direction"], entry_type,
                prices["entry_price"], prices["tp_price"], prices["sl_price"],
                cmd["bracket_size"], cmd["source"] or "critical_line", qty, cmd["logical_trade_id"]
            ))

        log.info(
            f"Replenished command {cid}: {cmd['direction']} {entry_type} "
            f"line={cmd['line_price']} bracket={cmd['bracket_size']} "
            f"entry={prices['entry_price']}"
        )
        count += 1

    return count


def force_close_symbol(symbol: str, db_path, ibc) -> int:
    """
    Force-flatten THIS symbol only, at market -- called once a symbol enters its own
    5-minute forced-exit window (session_clock.py).

    Deliberately NOT reqGlobalCancel() (unlike daily_paper_session.py's force_close_all,
    which is fine to nuke every open order account-wide since it only ever runs at the
    very end of its own dedicated session): with multiple symbols now live at once and
    different asset classes closing at different times, an account-wide cancel here
    would also kill other symbols' still-active resting orders. Cancels only THIS
    symbol's own TP/SL legs before market-exiting -- same per-command mechanics as
    force_close_all, just symbol-scoped instead of account-wide.

    2026-09-10: rewritten after a live incident exposed two bugs in the original,
    per-command version: (1) it fired one naked MarketOrder per FILLED command every
    poll cycle, with nothing tying that order back to the command row -- broker.py's own
    fill-reconciliation only matches fills against a command's ib_tp_order_id/
    ib_sl_order_id, so these orders were invisible to it and commands stayed FILLED
    forever, meaning (2) the SAME commands got re-flattened on every single poll,
    forever -- normally harmless, but with two decider processes briefly running at once
    (a separate bug) this produced 124 duplicate stray MKT orders in under 3 minutes.
    Now: sizes exactly ONE order off the real net IB position (immune to double-counting
    stacked/partially-filled commands) and marks every FILLED command CLOSED as soon as
    the flatten is issued, so a repeat call the very next poll finds nothing left to do.
    """
    from ib_insync import MarketOrder, Order

    with get_db(db_path) as con:
        filled = con.execute(
            "SELECT * FROM commands WHERE symbol=? AND status='FILLED'", (symbol,)
        ).fetchall()
    if not filled:
        return 0

    if ibc.paper is None:
        # 2026-09-10: this exact gap (decider connected live-only, ibc.paper always None)
        # silently broke forced EOD flattening for at least 2 days -- each attempt logged
        # a per-command AttributeError and moved on, easy to miss in the noise. Fail loud
        # and up front instead so it can never again quietly do nothing for every symbol.
        log.error(f"[forced_eod] {symbol}: ibc.paper is None -- cannot force-flatten "
                   f"{len(filled)} position(s); decider's paper connection is down")
        return 0

    for cmd in filled:
        for oid in (cmd["ib_tp_order_id"], cmd["ib_sl_order_id"]):
            if oid:
                try:
                    o = Order(); o.orderId = oid
                    ibc.paper.cancelOrder(o)
                except Exception:
                    pass

    contract = ibc.get_contract(symbol)
    net = 0.0
    try:
        for p in ibc.get_positions():
            if p.contract.symbol == symbol:
                net += p.position
    except Exception as e:
        log.error(f"[forced_eod] {symbol}: could not read live position, "
                   f"skipping this poll: {e}")
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if net != 0:
        try:
            action = "SELL" if net > 0 else "BUY"
            mkt = MarketOrder(action, abs(net))
            ibc.paper.placeOrder(contract, mkt)
            log.info(f"[forced_eod] MKT exit placed for {symbol}: {action} {abs(net)}")
        except Exception as e:
            log.error(f"[forced_eod] {symbol}: MKT exit failed, leaving commands FILLED "
                       f"for retry next poll: {e}")
            return 0

    with get_db(db_path) as con:
        for cmd in filled:
            update_command_status(con, cmd["id"], "CLOSED",
                                  exit_time=now, exit_reason="FORCED_EOD")
    log.info(f"[forced_eod] {symbol}: force-flattened {len(filled)} command(s) "
             f"(net position was {net})")
    return len(filled)


def _generate_for_symbols(symbols, date_str, cfg, db_path, ibc):
    """Per-symbol session-start body: count armed lines, fetch price, generate commands.
    Shared by the futures (no-wait) and stock (gated) halves of run_session_start()."""
    for symbol in symbols:
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


def run_session_start(ibc, cfg, db_path, date_str: str = None):
    """
    Session start: read critical lines already in DB (entered via GUI),
    fetch price, generate all commands.
    Called once at the beginning of a trading session.
    """
    date_str = date_str or date.today().strftime("%Y-%m-%d")

    futures_symbols = [s for s in cfg.symbols if s in _FUTURES_SYMBOLS]
    stock_symbols   = [s for s in cfg.symbols if s not in _FUTURES_SYMBOLS]

    # 2026-09-11: futures markets are already open essentially the whole time decider
    # runs -- no reason to make them wait on the stock-session gate below. Generate for
    # them immediately; this closes the gap where decider restarts at 8AM IL and then
    # sits idle for ~9h doing nothing for futures until the shared 17:00 IL gate cleared.
    _generate_for_symbols(futures_symbols, date_str, cfg, db_path, ibc)

    # Stocks: don't generate a single command before the regular session actually opens --
    # pre-market prices are thin/unreliable and would seed brackets off a bad reference
    # price.
    #
    # 2026-09-10: on top of the open itself, trading intentionally starts
    # `trading_start_delay_minutes` after the open (30 min -> 17:00 IL, not the 16:30 IL
    # open) -- explicit user decision until pre-market trading is added; 0 restores the
    # old at-the-open behavior.
    #
    # Day Start panel's "Force All Symbols Now" button sets this system_state flag
    # (dated, so it only applies today) to skip this wait entirely on demand.
    with get_db(db_path) as con:
        force_all = get_system_state(con, "FORCE_ALL_SYMBOLS_DATE") == date_str

    if stock_symbols and not force_all:
        delay_min = getattr(cfg.session, "trading_start_delay_minutes", 0)
        wait_s = max((seconds_until_trading_start(s, delay_minutes=delay_min) for s in stock_symbols), default=0)
        if wait_s > 0:
            log.info(f"Not trading stocks yet -- waiting {wait_s:.0f}s (open + {delay_min}min delay)")
            while any(is_before_trading_start(s, delay_minutes=delay_min) for s in stock_symbols):
                time.sleep(min(30, max(1, seconds_until_trading_start(stock_symbols[0], delay_minutes=delay_min))))
    elif force_all:
        log.info("FORCE_ALL_SYMBOLS_DATE override set for today -- skipping stock-session wait")

    _generate_for_symbols(stock_symbols, date_str, cfg, db_path, ibc)

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

        # Reconnect if IB went down (e.g. IBC watchdog restart). Checks PAPER too now --
        # decider holds a paper connection solely for force_close_symbol()'s MKT exits,
        # and that side dropping silently would reproduce the exact "ibc.paper is None"
        # bug this paper=True wiring was added to fix.
        if ibc and (not ibc.is_live_connected() or not ibc.is_paper_connected()):
            log.warning("IB connection lost — attempting reconnect")
            try:
                ok = ibc.reconnect(live=True, paper=True, max_attempts=3)
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
            if is_forced_exit_time(symbol):
                force_close_symbol(symbol, db_path, ibc)  # logs its own outcome
                continue  # no replenishment once a symbol is being forced flat

            price = get_current_price(symbol, ibc)
            if price is None:
                log.warning(f"No price for {symbol} — skipping replenishment")
                continue
            n = replenish(symbol, date_str, price, cfg, db_path)
            if n:
                log.info(f"Replenished {n} command(s) for {symbol}")

        # ibc.live.sleep() instead of time.sleep(): services ib_insync's event loop
        # during this idle wait, which is what keeps get_price()'s persistent ticker
        # subscriptions (2026-09-11) actually updating in the background. Guarded --
        # this function tolerates ibc=None elsewhere (see the reconnect check above),
        # so this must too even though no current caller actually passes None.
        if ibc:
            ibc.live.sleep(poll_seconds)
        else:
            time.sleep(poll_seconds)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    mod = sys.modules[__name__]
    original_entry_cutoff = mod.is_entry_cutoff
    original_forced_exit  = mod.is_forced_exit_time
    try:
        from lib.logger import reset_loggers
        from lib.db import set_system_state, update_command_status

        cfg = get_config()
        tick = cfg.orders.tick_size

        # The existing fixture below uses a fixed test date but real wall-clock time --
        # without this, whatever moment this self-test happens to run at could
        # spuriously land inside MES's real entry-cutoff/forced-exit window and zero
        # out every assertion below. Disabled here; the cutoff/forced-exit logic itself
        # gets its own dedicated, time-controlled assertions further down.
        mod.is_entry_cutoff = lambda symbol, **kw: False
        mod.is_forced_exit_time = lambda symbol, **kw: False

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

            # 1c. logical_trade_id: each (line, bracket, direction) combo gets its
            # own distinct, non-null id -- they're independent slots, not siblings.
            ltids = [r["logical_trade_id"] for r in rows]
            assert all(ltids), "generate_commands left a NULL logical_trade_id"
            assert len(set(ltids)) == len(ltids), \
                f"generate_commands produced duplicate logical_trade_id values: {ltids}"

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

            # 1e. Futures/stock split (2026-09-11): run_session_start() no longer waits
            # on the stock-open gate for futures symbols -- confirms the split itself
            # puts each symbol in the right group, the smallest thing that would break
            # if _FUTURES_SYMBOLS membership or the list comprehensions were wrong.
            test_symbols = ["MES", "AAPL", "MNQ", "MSFT"]
            futures_split = [s for s in test_symbols if s in _FUTURES_SYMBOLS]
            stock_split   = [s for s in test_symbols if s not in _FUTURES_SYMBOLS]
            assert futures_split == ["MES", "MNQ"], f"Futures split wrong: {futures_split}"
            assert stock_split == ["AAPL", "MSFT"], f"Stock split wrong: {stock_split}"

            # 1d. Control-group lines fan out to control_active_brackets (1 bracket),
            # not active_brackets (3) -- cuts control volume without touching real lines.
            with get_db(db_path) as con:
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed, source)"
                    " VALUES ('MES', ?, 'SUPPORT', 6480.00, 1, 1, 'geva_manual_control')",
                    (today,)
                )
            n_control = generate_commands("MES", today, current_price, cfg, db_path)
            control_brackets = getattr(cfg.orders, "control_active_brackets", None) or brackets[:1]
            expected_control = len(control_brackets) * 2  # 1 line * N control brackets * 2 directions
            assert n_control == expected_control, \
                f"Expected {expected_control} control commands, got {n_control}"
            with get_db(db_path) as con:
                control_rows = con.execute(
                    "SELECT bracket_size FROM commands WHERE source='geva_manual_control'"
                ).fetchall()
            assert {r["bracket_size"] for r in control_rows} == set(control_brackets), \
                f"Control commands used wrong brackets: {sorted({r['bracket_size'] for r in control_rows})}"

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

            # 2b. logical_trade_id propagated unchanged onto the replenishment row --
            # same logical trade, not a new one. The replenishment row is the most
            # recently inserted command (autoincrement id).
            with get_db(db_path) as con:
                orig_ltid = con.execute(
                    "SELECT logical_trade_id FROM commands WHERE id=?", (cmd_id,)
                ).fetchone()["logical_trade_id"]
                repl_ltid = con.execute(
                    "SELECT logical_trade_id FROM commands ORDER BY id DESC LIMIT 1"
                ).fetchone()["logical_trade_id"]
            assert orig_ltid, "original command has no logical_trade_id"
            assert repl_ltid == orig_ltid, \
                f"replenish() did not propagate logical_trade_id: {orig_ltid!r} -> {repl_ltid!r}"

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

        # 4. Entry cutoff gate: generate_commands/replenish must respect it (0 commands
        # when "in cutoff", normal generation otherwise) -- session_clock.py's own time
        # math is tested there; this only checks decider.py wires the gate correctly.
        with tempfile.TemporaryDirectory() as tmp2:
            db_path2 = Path(tmp2) / "test2.db"
            init_db(db_path2)
            with get_db(db_path2) as con:
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                    " VALUES ('MES', '2026-04-07', 'SUPPORT', 6490.00, 2, 1)"
                )
            mod.is_entry_cutoff = lambda symbol, **kw: True
            n_cutoff = generate_commands("MES", "2026-04-07", 6500.0, cfg, db_path2)
            assert n_cutoff == 0, "must generate nothing once within the entry cutoff"
            mod.is_entry_cutoff = lambda symbol, **kw: False
            n_normal = generate_commands("MES", "2026-04-07", 6500.0, cfg, db_path2)
            assert n_normal > 0, "must generate normally once the cutoff lambda is lifted"

        # 5. force_close_symbol: sizes ONE MKT exit off the real net IB position, marks
        # every FILLED command CLOSED so a repeat call is a no-op, and leaves other
        # symbols alone. 2026-09-10 rewrite -- see the function's own docstring for why
        # (a live incident: the old per-command design re-fired an exit every poll
        # forever since nothing ever moved commands off FILLED).
        class _FakePos:
            def __init__(self, symbol, qty): self.contract = type("C", (), {"symbol": symbol}); self.position = qty

        class _FakePaper:
            def __init__(self): self.orders_placed = []; self.cancels = []
            def placeOrder(self, contract, order): self.orders_placed.append((contract, order))
            def cancelOrder(self, order): self.cancels.append(order)

        class _FakeIBC:
            def __init__(self, positions): self.paper = _FakePaper(); self._positions = positions
            def get_contract(self, symbol): return symbol  # identity stand-in
            def get_positions(self): return self._positions

        with tempfile.TemporaryDirectory() as tmp3:
            db_path3 = Path(tmp3) / "test3.db"
            init_db(db_path3)
            with get_db(db_path3) as con:
                con.execute(
                    "INSERT INTO commands (symbol, line_price, line_type, line_strength, direction,"
                    " entry_type, entry_price, tp_price, sl_price, bracket_size, source,"
                    " quantity, logical_trade_id, status) VALUES"
                    " ('MES', 6490, 'SUPPORT', 2, 'BUY', 'LMT', 6490, 6494, 6486, 4,"
                    " 'critical_line', 1, 'lt1', 'FILLED')"
                )
                con.execute(
                    "INSERT INTO commands (symbol, line_price, line_type, line_strength, direction,"
                    " entry_type, entry_price, tp_price, sl_price, bracket_size, source,"
                    " quantity, logical_trade_id, status) VALUES"
                    " ('AAPL', 220, 'SUPPORT', 2, 'BUY', 'LMT', 220, 222, 218, 4,"
                    " 'critical_line', 1, 'lt2', 'FILLED')"
                )
            fake_ibc = _FakeIBC([_FakePos("MES", 1), _FakePos("AAPL", 1)])
            n_closed = force_close_symbol("MES", db_path3, fake_ibc)
            assert n_closed == 1, f"expected exactly 1 MES command closed, got {n_closed}"
            assert len(fake_ibc.paper.orders_placed) == 1
            _, order = fake_ibc.paper.orders_placed[0]
            assert order.action == "SELL", "net long MES position must be flattened with a SELL"
            assert order.totalQuantity == 1
            with get_db(db_path3) as con:
                row = con.execute("SELECT status, exit_reason FROM commands WHERE logical_trade_id='lt1'").fetchone()
                assert row["status"] == "CLOSED" and row["exit_reason"] == "FORCED_EOD"
                aapl_row = con.execute("SELECT status FROM commands WHERE logical_trade_id='lt2'").fetchone()
                assert aapl_row["status"] == "FILLED", "AAPL must be untouched by an MES-scoped call"

            # 5b. Repeat call: the command is already CLOSED, so this must be a clean
            # no-op and must NOT place a second exit order -- this is the exact
            # idempotency gap that let two decider processes fire 124 duplicate MKT
            # orders for the same stale FILLED rows in the live incident.
            n_repeat = force_close_symbol("MES", db_path3, fake_ibc)
            assert n_repeat == 0, "already-closed MES must not be re-flattened"
            assert len(fake_ibc.paper.orders_placed) == 1, "must not place a duplicate exit order"

            n_noop = force_close_symbol("MYM", db_path3, fake_ibc)
            assert n_noop == 0, "no open MYM positions -- must be a no-op"

            # 5c. Net IB position is already flat (e.g. stacked BUY+SELL commands
            # cancelled out) -- must still close the stale FILLED rows, but must NOT
            # place a pointless zero-quantity market order.
            with get_db(db_path3) as con:
                con.execute(
                    "INSERT INTO commands (symbol, line_price, line_type, line_strength, direction,"
                    " entry_type, entry_price, tp_price, sl_price, bracket_size, source,"
                    " quantity, logical_trade_id, status) VALUES"
                    " ('QCOM', 180, 'SUPPORT', 2, 'BUY', 'LMT', 180, 182, 178, 4,"
                    " 'critical_line', 1, 'lt3', 'FILLED')"
                )
            flat_ibc = _FakeIBC([_FakePos("QCOM", 0)])
            n_flat = force_close_symbol("QCOM", db_path3, flat_ibc)
            assert n_flat == 1, "stale FILLED row must still be closed even if IB is already flat"
            assert len(flat_ibc.paper.orders_placed) == 0, "must not place an order when net position is 0"

            # 5d. ibc.paper is None (the real 2026-09-10 production bug: decider connected
            # live-only) must fail loud with 0 closed, never a bare AttributeError.
            with get_db(db_path3) as con:
                con.execute(
                    "INSERT INTO commands (symbol, line_price, line_type, line_strength, direction,"
                    " entry_type, entry_price, tp_price, sl_price, bracket_size, source,"
                    " quantity, logical_trade_id, status) VALUES"
                    " ('XOM', 164, 'SUPPORT', 2, 'BUY', 'LMT', 164, 166, 162, 4,"
                    " 'critical_line', 1, 'lt4', 'FILLED')"
                )
            class _FakeIBCNoPaper:
                paper = None
                def get_contract(self, symbol): return symbol
            n_none = force_close_symbol("XOM", db_path3, _FakeIBCNoPaper())
            assert n_none == 0, "ibc.paper is None must be a clean no-op, not a crash"
            with get_db(db_path3) as con:
                row = con.execute("SELECT status FROM commands WHERE logical_trade_id='lt4'").fetchone()
                assert row["status"] == "FILLED", "must stay FILLED for retry, not silently closed"

        # 5c. Regression guard: decider's own __main__ must connect with paper=True.
        # This exact line (paper=False, "decider only needs LIVE for price") is what
        # broke force_close_symbol's MKT exits for at least 2 days before being caught by
        # a live trade review -- nothing above exercises the REAL wiring, only fakes, so
        # this checks the actual source line directly.
        _this_source = Path(__file__).read_text()
        assert "ibc.connect(live=True, paper=True)" in _this_source, \
            "decider.py's __main__ must connect paper=True -- force_close_symbol needs ibc.paper"

        print("[self-test] decider: PASS")
        return True

    except Exception as e:
        print(f"[self-test] decider: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        mod.is_entry_cutoff = original_entry_cutoff
        mod.is_forced_exit_time = original_forced_exit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao decider")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--mode", choices=["session", "replenish"],
                        default="session",
                        help="session=full run, replenish=replenishment loop only")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    from lib.singleton_lock import acquire_singleton_lock
    if not acquire_singleton_lock("decider", Path(__file__).parent / "logs"):
        log.error("decider already running (another live process holds the lock) "
                   "— refusing to start a second instance")
        sys.exit(1)

    cfg = get_config()
    db_path = Path(cfg.paths.db)
    init_db(db_path)

    from lib.ib_client import IBClient
    ibc = IBClient(cfg)
    # 2026-09-10: paper=True too, not just live -- force_close_symbol() (the 5-minute
    # forced-flatten-at-close safety net) calls ibc.paper.placeOrder()/cancelOrder()
    # directly from THIS process's replenishment loop. With paper=False, ibc.paper is
    # None and every force-close attempt has been silently crashing with
    # "'NoneType' object has no attribute 'placeOrder'" since the feature was added --
    # confirmed live in decider's own logs on both 2026-09-09 and 2026-09-10, meaning no
    # stock position has ever actually been force-flattened at close. The self-test for
    # force_close_symbol used a fake ibc.paper that was always populated, which is
    # exactly why it never caught this.
    ibc.connect(live=True, paper=True)

    if args.mode == "session":
        run_session_start(ibc, cfg, db_path)
        run_replenishment_loop(ibc, cfg, db_path)
    else:
        run_replenishment_loop(ibc, cfg, db_path)

    ibc.disconnect()
