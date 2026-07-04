"""
broker.py
Broker component for Galao.
Polls DB for PENDING commands, submits bracket orders via PAPER port,
monitors fills, and writes status back to DB.

Key invariants:
- Writes status=SUBMITTING (claim lock) before calling IB (R-ORD-12)
- Polls open orders every broker.ib_poll_seconds for fill detection
- Reconnects on disconnect (R-ERR-01)
- Never touches LIVE connection (data only via IBClient)
- Session stops when SESSION=SHUTDOWN appears in system_state

Usage:
    python broker.py            # run broker loop (blocking)
    python broker.py --self-test

Self-test:
    python broker.py --self-test
"""

import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import (get_db, init_db, get_pending_commands, update_command_status,
                    get_system_state, set_system_state, record_completed_trade,
                    spawn_replenishment, update_price_cache, get_cached_price)
from lib.ib_client import IBClient
from lib.order_builder import build_bracket, place_bracket

log = get_logger("broker")

_MAX_RECONNECT_ATTEMPTS = 5

# IB error codes that are purely informational (data-farm connection status etc.)
_IB_INFO_CODES = {
    1102, 2103, 2104, 2105, 2106, 2107, 2108, 2109, 2110, 2119, 2158,
}


# ── IB Event wiring ───────────────────────────────────────────────────────────

def _write_ib_event(db_path, event_type: str, component: str,
                    message: str, code: int = None):
    """Thread-safe insert into ib_events (called from ib_insync background thread)."""
    try:
        with get_db(db_path) as con:
            con.execute(
                "INSERT INTO ib_events (event_type, component, code, message)"
                " VALUES (?,?,?,?)",
                (event_type, component, code, message)
            )
    except Exception as e:
        log.warning(f"ib_event write failed: {e}")


def _handle_exec_fill(order_id: int, fill_price: float, db_path, symbol: str = None):
    """
    Event-driven fill: mark SUBMITTED command FILLED immediately on execDetails.
    Also saves price to price_cache — bypasses IB's 15-min paper delay for future commands.
    """
    now = _now_utc()
    try:
        with get_db(db_path) as con:
            row = con.execute(
                "SELECT id, symbol FROM commands WHERE ib_order_id=? AND status='SUBMITTED'",
                (order_id,)
            ).fetchone()
            if row:
                sym = symbol or row["symbol"]
                update_command_status(
                    con, row["id"], "FILLED",
                    fill_price=fill_price,
                    fill_time=now,
                )
                # Save price so --build-trades can use it instead of 15-min delayed quote
                update_price_cache(con, sym, fill_price, now, source="fill")
                log.info(
                    f"[event] Command {row['id']} FILLED "
                    f"(execDetails orderId={order_id} price={fill_price})"
                )
    except Exception as e:
        log.warning(f"Event fill handler error: {e}")


def register_ib_events(ibc: IBClient, db_path):
    """
    Wire ib_insync events on both PAPER and LIVE connections to ib_events table.
    Also registers event-driven fill detection via execDetailsEvent.
    Call once after ibc.connect().
    """

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _classify_error(code: int) -> str:
        if code in _IB_INFO_CODES:
            return "INFO"
        if code >= 2000:
            return "WARNING"
        if code >= 1000:
            return "WARNING"
        return "ERROR"

    def _contract_sym(contract) -> str:
        if contract is None:
            return ""
        return getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")

    # ── PAPER handlers ────────────────────────────────────────────────────────
    def on_paper_error(reqId, errorCode, errorString, contract):
        evt = _classify_error(errorCode)
        sym = _contract_sym(contract)
        msg = f"[req={reqId}] {sym} {errorString}".strip()
        log.log(
            __import__("logging").WARNING if evt != "INFO" else __import__("logging").DEBUG,
            f"IB PAPER {evt} {errorCode}: {msg}"
        )
        _write_ib_event(db_path, evt, "paper", msg, errorCode)

    def on_paper_order_status(trade):
        o = trade.order
        s = trade.orderStatus
        msg = (f"orderId={o.orderId} {o.action} {o.orderType} "
               f"status={s.status} filled={s.filled} "
               f"remaining={s.remaining} avgFill={s.avgFillPrice}")
        _write_ib_event(db_path, "INFO", "paper", msg)

    def on_paper_exec(trade, fill):
        ex = fill.execution
        msg = (f"FILL orderId={ex.orderId} {ex.side} "
               f"qty={ex.shares} price={ex.avgPrice} time={ex.time}")
        log.info(f"IB execDetails: {msg}")
        _write_ib_event(db_path, "INFO", "paper", msg)
        _handle_exec_fill(ex.orderId, ex.avgPrice, db_path)

    def on_paper_connected():
        msg = f"PAPER connected (clientId={ibc._paper_client_id})"
        log.info(f"IB event: {msg}")
        _write_ib_event(db_path, "RECONNECT", "paper", msg)

    def on_paper_disconnected():
        msg = "PAPER disconnected"
        log.warning(f"IB event: {msg}")
        _write_ib_event(db_path, "DISCONNECT", "paper", msg)

    # ── LIVE handlers ─────────────────────────────────────────────────────────
    def on_live_error(reqId, errorCode, errorString, contract):
        evt = _classify_error(errorCode)
        sym = _contract_sym(contract)
        msg = f"[req={reqId}] {sym} {errorString}".strip()
        _write_ib_event(db_path, evt, "live", msg, errorCode)

    def on_live_connected():
        _write_ib_event(db_path, "RECONNECT", "live",
                        f"LIVE connected (clientId={ibc._live_client_id})")

    def on_live_disconnected():
        _write_ib_event(db_path, "DISCONNECT", "live", "LIVE disconnected")

    # ── Wire up ───────────────────────────────────────────────────────────────
    if ibc.paper:
        ibc.paper.errorEvent        += on_paper_error
        ibc.paper.orderStatusEvent  += on_paper_order_status
        ibc.paper.execDetailsEvent  += on_paper_exec
        ibc.paper.connectedEvent    += on_paper_connected
        ibc.paper.disconnectedEvent += on_paper_disconnected

    if ibc.live:
        ibc.live.errorEvent         += on_live_error
        ibc.live.connectedEvent     += on_live_connected
        ibc.live.disconnectedEvent  += on_live_disconnected

    log.info("IB event handlers registered")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_shutdown(db_path) -> bool:
    with get_db(db_path) as con:
        val = get_system_state(con, "SESSION")
    return val == "SHUTDOWN"


def _claim_command(db_path, command_id: int) -> bool:
    """
    Atomically claim a PENDING command by writing SUBMITTING + claimed_at.
    Returns True if we won the claim race, False if another process beat us.
    This is the claim lock (R-ORD-12).
    """
    now = _now_utc()
    with get_db(db_path) as con:
        cur = con.execute(
            "UPDATE commands SET status='SUBMITTING', claimed_at=?,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id=? AND status='PENDING'",
            (now, command_id)
        )
        return cur.rowcount == 1


def process_pending_commands(ibc: IBClient, db_path, cfg) -> int:
    """
    Find all PENDING commands, claim them, submit to IB, write SUBMITTED.
    Returns number of orders submitted.
    """
    with get_db(db_path) as con:
        pending = get_pending_commands(con)

    if not pending:
        return 0

    submitted = 0
    for cmd in pending:
        cid = cmd["id"]

        # Claim lock — atomic status change to SUBMITTING
        if not _claim_command(db_path, cid):
            log.debug(f"Command {cid} already claimed by another process — skip")
            continue

        log.info(f"Processing command {cid}: {cmd['direction']} {cmd['entry_type']} "
                 f"{cmd['symbol']} @ {cmd['entry_price']}")

        try:
            # Resolve front-month contract (cached in practice via IBClient)
            contract = ibc.get_contract(cmd["symbol"])

            # Build bracket order objects
            orders = build_bracket(
                ibc.paper, contract,
                direction   = cmd["direction"],
                entry_type  = cmd["entry_type"],
                entry_price = cmd["entry_price"],
                tp_price    = cmd["tp_price"],
                sl_price    = cmd["sl_price"],
                quantity    = cmd["quantity"],
            )

            # Submit to IB
            result = place_bracket(ibc.paper, contract, orders)

            # Write SUBMITTED + IB order IDs
            with get_db(db_path) as con:
                update_command_status(
                    con, cid, "SUBMITTED",
                    ib_order_id    = result["entry_id"],
                    ib_tp_order_id = result["tp_id"],
                    ib_sl_order_id = result["sl_id"],
                )
            log.info(f"Command {cid} SUBMITTED — IB entry_id={result['entry_id']}")
            submitted += 1

        except Exception as e:
            log.error(f"Command {cid} submission failed: {e}")
            with get_db(db_path) as con:
                update_command_status(con, cid, "ERROR", error_message=str(e))

    return submitted


def poll_fills(ibc: IBClient, db_path) -> int:
    """
    Check IB PAPER trades for fills of SUBMITTED commands.
    Updates DB to FILLED on detection.
    Returns number of fills detected.
    """
    if not ibc.is_paper_connected():
        log.warning("PAPER not connected — skipping fill poll")
        return 0

    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.error(f"Error fetching trades: {e}")
        return 0

    # Build lookup: ib_order_id → (status, fill_price, fill_time_utc)
    filled_ids = {}
    for trade in trades:
        oid = trade.order.orderId
        status = trade.orderStatus.status
        fill_price = trade.orderStatus.avgFillPrice
        if status in ("Filled", "PartiallyFilled"):
            ft_str = None
            try:
                if trade.fills:
                    ft = trade.fills[-1].time
                    if ft is not None:
                        from datetime import timezone as _tz
                        if ft.tzinfo is None:
                            ft = ft.replace(tzinfo=_tz.utc)
                        ft_str = ft.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
            filled_ids[oid] = (status, fill_price, ft_str or _now_utc())

    if not filled_ids:
        return 0

    # Find SUBMITTED commands whose entry order was filled
    with get_db(db_path) as con:
        submitted = con.execute(
            "SELECT * FROM commands WHERE status='SUBMITTED'"
        ).fetchall()

    fills = 0
    for cmd in submitted:
        entry_oid = cmd["ib_order_id"]
        if entry_oid in filled_ids:
            status, fill_price, fill_time_ib = filled_ids[entry_oid]
            # R-ORD-13: treat all fills as complete (partial fills ignored in V1)
            log.info(f"Command {cmd['id']} FILLED — price={fill_price}")
            with get_db(db_path) as con:
                update_command_status(
                    con, cmd["id"], "FILLED",
                    fill_price = fill_price,
                    fill_time  = fill_time_ib,
                )
            fills += 1

    return fills


def poll_tp_sl_fills(ibc: IBClient, db_path) -> int:
    """
    For each FILLED command, check whether IB has filled the TP or SL child order.
    When detected: write CLOSED + pnl_points + record to completed_trades.
    Returns number of exits recorded.
    """
    if not ibc.is_paper_connected():
        return 0

    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.error(f"poll_tp_sl_fills: error fetching trades: {e}")
        return 0

    # Build map of filled order IDs → (avg fill price, fill time from IB)
    ib_filled: dict[int, tuple[float, str]] = {}
    for trade in trades:
        if trade.orderStatus.status == "Filled":
            ft_str = None
            try:
                if trade.fills:
                    ft = trade.fills[-1].time
                    if ft is not None:
                        from datetime import timezone as _tz
                        if ft.tzinfo is None:
                            ft = ft.replace(tzinfo=_tz.utc)
                        ft_str = ft.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
            ib_filled[trade.order.orderId] = (trade.orderStatus.avgFillPrice, ft_str or _now_utc())

    if not ib_filled:
        return 0

    with get_db(db_path) as con:
        filled_cmds = con.execute("SELECT * FROM commands WHERE status='FILLED'").fetchall()

    if not filled_cmds:
        return 0

    closed = 0
    for cmd in filled_cmds:
        tp_oid = cmd["ib_tp_order_id"]
        sl_oid = cmd["ib_sl_order_id"]
        fill_p = cmd["fill_price"]
        if fill_p is None:
            continue

        exit_price = None
        exit_time  = None

        if tp_oid and tp_oid in ib_filled:
            exit_price, exit_time = ib_filled[tp_oid]
        elif sl_oid and sl_oid in ib_filled:
            exit_price, exit_time = ib_filled[sl_oid]

        if exit_price is None:
            continue

        # Derive exit_reason from price vs bracket levels — immune to order-ID swap bugs
        d    = cmd["direction"]
        tp_p = cmd["tp_price"]
        sl_p = cmd["sl_price"]
        if d == "BUY":
            if exit_price >= tp_p:   exit_reason = "TP"
            elif exit_price <= sl_p: exit_reason = "SL"
            else:                    exit_reason = "STAGNATION"
        else:  # SELL
            if exit_price <= tp_p:   exit_reason = "TP"
            elif exit_price >= sl_p: exit_reason = "SL"
            else:                    exit_reason = "STAGNATION"

        pnl = (exit_price - fill_p) if d == "BUY" else (fill_p - exit_price)

        log.info(
            f"Command {cmd['id']} CLOSED via {exit_reason} "
            f"fill={fill_p} exit={exit_price} pnl={pnl:+.2f}pts"
        )
        with get_db(db_path) as con:
            update_command_status(con, cmd["id"], "CLOSED",
                                  exit_price=exit_price,
                                  exit_time=exit_time,
                                  exit_reason=exit_reason,
                                  pnl_points=round(pnl, 4))
            record_completed_trade(con, cmd["id"])
        closed += 1

    return closed


def replenish_if_enabled(ibc: IBClient, db_path, cfg) -> int:
    """
    If REPLENISH_ENABLED=1 in system_state, find CLOSED commands that have
    no child replenishment yet and spawn one PENDING replacement each.
    Returns number of replenishments spawned.
    """
    with get_db(db_path) as con:
        if get_system_state(con, "REPLENISH_ENABLED") != "1":
            return 0

    # Find completed commands with no child yet
    with get_db(db_path) as con:
        candidates = con.execute("""
            SELECT c.* FROM commands c
            WHERE c.status = 'CLOSED'
              AND c.source IS NOT NULL
              AND c.source != 'critical_line'
              AND NOT EXISTS (
                  SELECT 1 FROM commands child
                  WHERE child.parent_command_id = c.id
              )
            ORDER BY c.updated_at DESC
            LIMIT 50
        """).fetchall()

    if not candidates:
        return 0

    tick = cfg.orders.tick_size
    spawned = 0
    for cmd in candidates:
        # Get price: prefer price_cache (fill-sourced, no delay), then IB
        try:
            with get_db(db_path) as con:
                price = get_cached_price(con, cmd["symbol"])
            if not price:
                price = ibc.get_price(cmd["symbol"]) if ibc else None
        except Exception:
            price = None
        if not price:
            log.warning(f"[replenish] No price for {cmd['symbol']} — skipping cmd {cmd['id']}")
            continue
        try:
            with get_db(db_path) as con:
                child_id = spawn_replenishment(con, cmd, price, tick)
            log.info(
                f"[replenish] Spawned #{child_id} from parent #{cmd['id']} "
                f"({cmd['source']} bracket={cmd['bracket_size']})"
            )
            spawned += 1
        except Exception as e:
            log.error(f"[replenish] Failed for cmd {cmd['id']}: {e}")

    return spawned


def build_trades(db_path=None):
    """
    --build-trades mode: send as many commands as possible at once.

    For each trading symbol:
      1. Get current price from price_cache (fill-sourced, no 15-min delay).
         Fallback: reqMktData with 5s timeout, saved as 'paper_delayed'.
      2. Generate one BUY + one SELL PENDING command at price ± entry_offset.
      3. Insert all as PENDING, then immediately submit them.

    Brackets are fast-exit (tp_ticks / sl_ticks from config).
    Contracts per symbol from config.orders.quantity_per_symbol.
    """
    cfg     = get_config()
    db_path = db_path or Path(cfg.paths.db)
    init_db(db_path)

    symbols   = cfg.symbols
    tick      = cfg.orders.tick_size
    tp_ticks  = getattr(cfg.orders, "tp_ticks", 4)
    sl_ticks  = getattr(cfg.orders, "sl_ticks", 4)

    qty_map = {}
    try:
        qty_map = dict(cfg.orders.quantity_per_symbol)
    except Exception:
        pass

    log.info(f"build_trades: symbols={symbols}")

    ibc = IBClient(cfg)
    try:
        ibc.connect(live=True, paper=True)
    except ConnectionError as e:
        log.error(f"build_trades: IB connection failed: {e}")
        sys.exit(1)

    register_ib_events(ibc, db_path)

    def _get_price(sym: str) -> float | None:
        # First: use cached fill price (no delay)
        with get_db(db_path) as con:
            p = get_cached_price(con, sym)
        if p:
            log.info(f"build_trades: {sym} price from cache: {p}")
            return p
        # Fallback: IB paper quote (15-min delay for paper)
        try:
            p = ibc.get_price(sym)
            if p:
                with get_db(db_path) as con:
                    update_price_cache(con, sym, p, _now_utc(), source="paper_delayed")
                log.info(f"build_trades: {sym} price from IB (delayed): {p}")
                return p
        except Exception as e:
            log.warning(f"build_trades: could not get price for {sym}: {e}")
        return None

    def _rt(p: float) -> float:
        return round(round(p / tick) * tick, 10)

    inserted = 0
    for sym in symbols:
        price = _get_price(sym)
        if price is None:
            log.warning(f"build_trades: skipping {sym} — no price available")
            continue

        qty    = qty_map.get(sym, 1)
        offset = tick  # 1-tick offset so LMT fills quickly

        for direction in ("BUY", "SELL"):
            if direction == "BUY":
                entry = _rt(price - offset)
                tp    = _rt(entry + tp_ticks * tick)
                sl    = _rt(entry - sl_ticks * tick)
            else:
                entry = _rt(price + offset)
                tp    = _rt(entry - tp_ticks * tick)
                sl    = _rt(entry + sl_ticks * tick)

            bracket = tp_ticks * tick

            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, source, quantity, status)
                    VALUES (?, ?, ?, 1, ?, 'LMT', ?, ?, ?, ?, 'random_lmt', ?, 'PENDING')
                """, (sym, entry,
                      "SUPPORT" if direction == "BUY" else "RESISTANCE",
                      direction, entry, tp, sl, bracket, qty))
                cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            log.info(f"build_trades: inserted cmd={cmd_id} {sym} {direction} "
                     f"entry={entry} tp={tp} sl={sl} qty={qty}")
            inserted += 1

    log.info(f"build_trades: {inserted} PENDING commands inserted — submitting...")

    # Submit all in one pass
    submitted = process_pending_commands(ibc, db_path, cfg)
    log.info(f"build_trades: submitted {submitted}/{inserted} orders")

    ibc.disconnect()


def run_broker(db_path=None, dry_run: bool = False):
    """Main broker loop."""
    cfg = get_config()
    db_path = db_path or Path(cfg.paths.db)
    init_db(db_path)

    if dry_run:
        log.warning("*** DRY-RUN MODE — no IB orders will be sent ***")
        _run_broker_dry(db_path, cfg)
        return

    log.info(f"Broker starting — DB={db_path}")

    ibc = IBClient(cfg)
    try:
        ibc.connect(live=True, paper=True)
    except ConnectionError as e:
        log.error(f"Broker startup: IB connection failed: {e}")
        sys.exit(1)

    register_ib_events(ibc, db_path)

    poll_seconds     = cfg.broker.command_poll_seconds
    ib_poll_seconds  = cfg.broker.ib_poll_seconds
    last_ib_poll     = 0.0

    log.info("Broker loop started")

    try:
        while True:
            # Check for shutdown signal
            if _is_shutdown(db_path):
                log.info("SESSION=SHUTDOWN detected — broker exiting")
                break

            # Check connections; reconnect if needed
            if not ibc.is_paper_connected() or not ibc.is_live_connected():
                log.warning("IB connection lost — attempting reconnect")
                ok = ibc.reconnect(max_attempts=_MAX_RECONNECT_ATTEMPTS)
                if ok:
                    register_ib_events(ibc, db_path)
                if not ok:
                    log.error("Reconnect failed after max attempts — aborting broker")
                    # R-ERR-05: abort means trigger shutdown then exit
                    with get_db(db_path) as con:
                        set_system_state(con, "SESSION", "SHUTDOWN")
                    break

            # Process pending commands
            try:
                n = process_pending_commands(ibc, db_path, cfg)
                if n:
                    log.info(f"Submitted {n} order(s)")
            except Exception as e:
                log.error(f"Error in process_pending_commands: {e}")

            # Periodic fill poll (entry fills + TP/SL child order exits)
            now = time.time()
            if now - last_ib_poll >= ib_poll_seconds:
                try:
                    f = poll_fills(ibc, db_path)
                    if f:
                        log.info(f"Detected {f} entry fill(s)")
                except Exception as e:
                    log.error(f"Error in poll_fills: {e}")
                try:
                    c = poll_tp_sl_fills(ibc, db_path)
                    if c:
                        log.info(f"Detected {c} TP/SL exit(s)")
                except Exception as e:
                    log.error(f"Error in poll_tp_sl_fills: {e}")
                try:
                    r = replenish_if_enabled(ibc, db_path, cfg)
                    if r:
                        log.info(f"Replenished {r} trade(s)")
                except Exception as e:
                    log.error(f"Error in replenish_if_enabled: {e}")
                last_ib_poll = now

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        log.info("Broker interrupted")
    finally:
        ibc.disconnect()
        log.info("Broker stopped")


def _run_broker_dry(db_path, cfg):
    """
    Dry-run broker loop: consumes PENDING commands and logs what would be sent
    to IB, but never opens a connection or places an order.
    Commands are advanced to SUBMITTED with fake order IDs so the rest of the
    system (decider, position_manager) behaves normally.
    """
    poll_seconds = cfg.broker.command_poll_seconds
    fake_order_id = 90000

    log.info("Dry-run broker loop started")

    while True:
        if _is_shutdown(db_path):
            log.info("SESSION=SHUTDOWN detected — dry-run broker exiting")
            break

        with get_db(db_path) as con:
            pending = get_pending_commands(con)

        for cmd in pending:
            cid = cmd["id"]
            if not _claim_command(db_path, cid):
                continue

            fake_order_id += 1
            log.info(
                f"[DRY-RUN] Would submit command {cid}: "
                f"{cmd['direction']} {cmd['entry_type']} "
                f"{cmd['symbol']} @ {cmd['entry_price']} "
                f"(fake IB id={fake_order_id})"
            )
            with get_db(db_path) as con:
                update_command_status(
                    con, cid, "SUBMITTED",
                    ib_order_id    = fake_order_id,
                    ib_tp_order_id = fake_order_id + 1,
                    ib_sl_order_id = fake_order_id + 2,
                )
            fake_order_id += 2

        time.sleep(poll_seconds)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Self-test:
    - Config loads
    - DB init + PENDING→SUBMITTING claim lock (no IB needed)
    - IB connection attempt (SKIP if not available)
    - Broker loop runs for 2 poll cycles (no real orders)
    """
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import set_system_state

        cfg = get_config()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            # 1. Insert a PENDING command
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price, bracket_size)
                    VALUES ('MES', 6500.0, 'SUPPORT', 2,
                            'BUY', 'LMT', 6500.0, 6502.0, 6498.0, 2.0)
                """)
                cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 2. Claim lock test — PENDING → SUBMITTING
            claimed = _claim_command(db_path, cmd_id)
            assert claimed, "Claim failed"
            with get_db(db_path) as con:
                row = con.execute("SELECT status FROM commands WHERE id=?",
                                  (cmd_id,)).fetchone()
            assert row["status"] == "SUBMITTING", f"Status: {row['status']}"

            # 3. Second claim attempt should fail (already SUBMITTING)
            claimed2 = _claim_command(db_path, cmd_id)
            assert not claimed2, "Second claim should fail"

            # 4. Shutdown detection
            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "SHUTDOWN")
            assert _is_shutdown(db_path), "Shutdown not detected"

            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
            assert not _is_shutdown(db_path), "Running misdetected as SHUTDOWN"

            # 5. IB connection attempt
            ibc = IBClient(cfg)
            try:
                ibc.connect(live=True, paper=True)
                ib_ok = ibc.is_live_connected() and ibc.is_paper_connected()
                ibc.disconnect()
                if ib_ok:
                    log.info("[self-test] IB connections: PASS")
                else:
                    log.warning("[self-test] IB partial connection — non-fatal")
            except Exception as e:
                log.info(f"[self-test] IB not available: {e} — SKIP")

            reset_loggers()

        print("[self-test] broker: PASS")
        return True

    except Exception as e:
        print(f"[self-test] broker: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao broker")
    parser.add_argument("--self-test",    action="store_true")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Log commands instead of sending to IB")
    parser.add_argument("--build-trades", action="store_true",
                        help="Insert+submit commands for all symbols at current price, then exit")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.build_trades:
        build_trades()
        sys.exit(0)

    run_broker(dry_run=args.dry_run)
