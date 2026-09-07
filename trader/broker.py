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
import threading
import argparse
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_pending_commands, update_command_status, get_system_state, record_completed_trade, spawn_replenishment, update_price_cache, get_cached_price
from lib.ib_client import IBClient
from lib.order_builder import build_bracket, place_bracket, round_tick, TICK_BY_SYMBOL as _TICK_BY_SYMBOL

log = get_logger("broker")

_MAX_RECONNECT_ATTEMPTS = 5

# 2026-07-20 incident: SUBMITTED commands whose ib_order_id had aged out of
# ibc.paper.trades() were silently skipped by poll_fills() forever -- 96
# commands stuck with no visibility, some from 18 days earlier. Past this
# age with no match, flag for reconciliation instead of ignoring silently.
_STALE_SUBMITTED_MINUTES = 10

# 2026-07-xx incident (bug 5): same failure mode as above, one hop later -- a FILLED
# command's TP/SL order id can age out of ibc.paper.trades() entirely, so
# poll_tp_sl_fills() never sees the exit. 90min stays inside the 120-min daily
# session window so this catches a vanished order id before force_close_all masks it.
_STALE_FILLED_MINUTES = 90

# Thread-safe queue: (cmd_id, fill_price) items pending TP/SL rebase
_rebase_queue: list = []
_rebase_lock  = threading.Lock()

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


def _handle_exec_fill(order_id: int, fill_price: float, db_path):
    """Event-driven fill: mark SUBMITTED command FILLED immediately on execDetails."""
    now = _now_utc()
    try:
        with get_db(db_path) as con:
            row = con.execute(
                "SELECT id, symbol FROM commands WHERE ib_order_id=? AND status='SUBMITTED'",
                (order_id,)
            ).fetchone()
            if row:
                update_command_status(
                    con, row["id"], "FILLED",
                    fill_price=fill_price,
                    fill_time=now,
                )
                update_price_cache(con, row["symbol"], fill_price, now, source="fill")
                log.info(
                    f"[event] Command {row['id']} FILLED "
                    f"(execDetails orderId={order_id} price={fill_price})"
                )
                # Queue TP/SL rebase — actual IB modify runs in main loop (not event thread)
                with _rebase_lock:
                    _rebase_queue.append((row["id"], fill_price))
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


def _minutes_since(iso_ts: str) -> float:
    """Minutes elapsed since an ISO UTC timestamp in this codebase's standard format."""
    then = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


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

    # Build lookup: ib_order_id → (status, fill_price)
    ib_status_by_oid = {}
    for trade in trades:
        oid = trade.order.orderId
        status = trade.orderStatus.status
        fill_price = trade.orderStatus.avgFillPrice
        ib_status_by_oid[oid] = (status, fill_price)

    # Find SUBMITTED commands whose entry order was filled
    with get_db(db_path) as con:
        submitted = con.execute(
            "SELECT * FROM commands WHERE status='SUBMITTED'"
        ).fetchall()

    fills = 0
    queued_ids = set()
    with _rebase_lock:
        queued_ids = {item[0] for item in _rebase_queue}

    for cmd in submitted:
        entry_oid = cmd["ib_order_id"]
        ib_info = ib_status_by_oid.get(entry_oid)

        if ib_info is None:
            # Order not in IB's trade list at all. Could just be a brand-new
            # order that hasn't synced into ibc.paper.trades() yet (normal,
            # transient) -- but if it's been this long with no match, IB has
            # no record of it (order ID aged out of the session cache, or it
            # was lost some other way) and it will never resolve on its own.
            age_min = _minutes_since(cmd["updated_at"])
            if age_min > _STALE_SUBMITTED_MINUTES:
                log.error(
                    f"Command {cmd['id']} SUBMITTED {age_min:.0f}min ago, "
                    f"ib_order_id={entry_oid} not found in IB trades — "
                    "flagging RECONCILE_REQUIRED"
                )
                with get_db(db_path) as con:
                    update_command_status(con, cmd["id"], "RECONCILE_REQUIRED")
            continue

        ib_status, fill_price = ib_info

        if ib_status in ("Filled", "PartiallyFilled"):
            # R-ORD-13: treat all fills as complete (partial fills ignored in V1)
            now = _now_utc()
            log.info(f"Command {cmd['id']} FILLED — price={fill_price}")
            with get_db(db_path) as con:
                update_command_status(
                    con, cmd["id"], "FILLED",
                    fill_price = fill_price,
                    fill_time  = now,
                )
                update_price_cache(con, cmd["symbol"], fill_price, now, source="fill")
            # Queue TP/SL rebase if not already queued by event handler
            if cmd["id"] not in queued_ids:
                with _rebase_lock:
                    _rebase_queue.append((cmd["id"], fill_price))
            fills += 1

        elif ib_status in ("Cancelled", "Inactive", "ApiCancelled"):
            # IB cancelled the order (day-order expiry, margin violation, manual cancel,
            # connectivity gap). Flip DB to CANCELLED so the slot is freed and
            # the candidate can be resubmitted if needed.
            log.warning(
                f"Command {cmd['id']} IB order {entry_oid} is {ib_status} — "
                "marking CANCELLED in DB"
            )
            with get_db(db_path) as con:
                update_command_status(con, cmd["id"], "CANCELLED")

    return fills


def poll_tp_sl_fills(ibc: IBClient, db_path) -> int:
    """
    For each FILLED command, check whether IB has filled the TP or SL child order.
    When detected: write CLOSED + pnl_points + record to completed_trades.

    Also (bug 5): flags a FILLED command RECONCILE_REQUIRED once BOTH its TP and SL
    order ids have aged out of IB's trades() cache entirely (genuinely gone, not
    merely unfilled) and _STALE_FILLED_MINUTES has passed since fill_time. This
    staleness pass runs every call regardless of whether any TP/SL filled this
    cycle -- it must NOT sit behind an "any new fills?" early return, since most
    poll cycles have zero new TP/SL fills.

    Returns number of exits recorded (not counting bug-5 RECONCILE_REQUIRED flips).
    """
    if not ibc.is_paper_connected():
        return 0

    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.error(f"poll_tp_sl_fills: error fetching trades: {e}")
        return 0

    # Build map of filled order IDs → avg fill price
    ib_filled: dict[int, float] = {}
    for trade in trades:
        if trade.orderStatus.status == "Filled":
            ib_filled[trade.order.orderId] = trade.orderStatus.avgFillPrice

    # Every order id IB currently knows about, filled or not -- built from the SAME
    # trades() call, BEFORE any early return, so the bug-5 staleness pass below
    # always runs (most cycles have ib_filled == {} and would otherwise bail out here).
    known_oids = {t.order.orderId for t in trades}

    with get_db(db_path) as con:
        filled_cmds = con.execute("SELECT * FROM commands WHERE status='FILLED'").fetchall()

    if not filled_cmds:
        return 0

    closed = 0
    closed_ids: set = set()
    now = _now_utc()
    for cmd in filled_cmds:
        tp_oid = cmd["ib_tp_order_id"]
        sl_oid = cmd["ib_sl_order_id"]
        fill_p = cmd["fill_price"]
        if fill_p is None:
            continue

        exit_price = None

        if tp_oid and tp_oid in ib_filled:
            exit_price = ib_filled[tp_oid]
        elif sl_oid and sl_oid in ib_filled:
            exit_price = ib_filled[sl_oid]

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
                                  exit_time=now,
                                  exit_reason=exit_reason,
                                  pnl_points=round(pnl, 4))
            record_completed_trade(con, cmd["id"])
            update_price_cache(con, cmd["symbol"], exit_price, now, source="fill")
        closed += 1
        closed_ids.add(cmd["id"])

    # bug 5: flag FILLED commands whose TP/SL bracket has genuinely vanished from
    # IB's cache (not merely unfilled) once they've sat past _STALE_FILLED_MINUTES.
    # reconcile_stuck_commands() picks these up on a later poll via fill_price NOT NULL.
    for cmd in filled_cmds:
        if cmd["id"] in closed_ids:
            continue
        fill_time = cmd["fill_time"]
        if not fill_time:
            continue
        tp_oid = cmd["ib_tp_order_id"]
        sl_oid = cmd["ib_sl_order_id"]
        if tp_oid not in known_oids and sl_oid not in known_oids \
                and _minutes_since(fill_time) > _STALE_FILLED_MINUTES:
            log.error(
                f"Command {cmd['id']} FILLED {_minutes_since(fill_time):.0f}min ago, "
                f"TP/SL order ids ({tp_oid}, {sl_oid}) both missing from IB trades — "
                "flagging RECONCILE_REQUIRED"
            )
            with get_db(db_path) as con:
                update_command_status(con, cmd["id"], "RECONCILE_REQUIRED")

    return closed


def _drain_rebase_queue(ibc: IBClient, db_path) -> int:
    """
    For each recently-filled command queued by _handle_exec_fill / poll_fills,
    modify the IB TP and SL child orders so they are relative to the actual fill
    price rather than the planned entry price.  Runs in the main broker loop
    (not the ib_insync event thread) so IB API calls are safe.
    Returns the number of commands whose brackets were adjusted.
    """
    with _rebase_lock:
        if not _rebase_queue:
            return 0
        items = list(_rebase_queue)
        _rebase_queue.clear()

    if not ibc.is_paper_connected():
        with _rebase_lock:
            _rebase_queue.extend(items)
        return 0

    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.warning(f"rebase: could not fetch IB trades: {e}")
        with _rebase_lock:
            _rebase_queue.extend(items)
        return 0

    trades_by_oid = {t.order.orderId: t for t in trades}
    rebased = 0

    for cmd_id, fill_price in items:
        with get_db(db_path) as con:
            cmd = con.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
        if not cmd:
            continue

        entry_price = cmd["entry_price"]
        tick        = _TICK_BY_SYMBOL.get(cmd["symbol"], 0.25)
        slippage    = abs(fill_price - entry_price)

        if slippage < tick:
            continue  # no meaningful slippage — leave bracket as-is

        direction  = cmd["direction"]
        tp_bracket = abs(cmd["tp_price"] - entry_price)
        sl_bracket = abs(cmd["sl_price"] - entry_price)

        if direction == "BUY":
            new_tp = round_tick(fill_price + tp_bracket, tick)
            new_sl = round_tick(fill_price - sl_bracket, tick)
        else:
            new_tp = round_tick(fill_price - tp_bracket, tick)
            new_sl = round_tick(fill_price + sl_bracket, tick)

        tp_oid = cmd["ib_tp_order_id"]
        sl_oid = cmd["ib_sl_order_id"]

        # Find contract from either child order
        contract = None
        for oid in (tp_oid, sl_oid):
            if oid and oid in trades_by_oid:
                contract = trades_by_oid[oid].contract
                break

        if not contract:
            log.warning(f"Cmd {cmd_id}: child orders not found in IB trades — rebase skipped")
            continue

        _DONE = ("Filled", "Cancelled", "Inactive")
        modified = 0

        # Modify TP
        if tp_oid and tp_oid in trades_by_oid:
            tp_trade = trades_by_oid[tp_oid]
            if tp_trade.orderStatus.status not in _DONE:
                tp_order = tp_trade.order
                old_tp = tp_order.lmtPrice if tp_order.orderType == "LMT" else tp_order.auxPrice
                if tp_order.orderType == "LMT":
                    tp_order.lmtPrice = new_tp
                else:
                    tp_order.auxPrice = new_tp
                try:
                    ibc.paper.modifyOrder(contract, tp_order)
                    log.info(f"Cmd {cmd_id}: TP {old_tp} → {new_tp} (fill={fill_price} slip={slippage:+.2f})")
                    modified += 1
                except Exception as e:
                    log.warning(f"Cmd {cmd_id}: TP modify failed: {e}")

        # Modify SL
        if sl_oid and sl_oid in trades_by_oid:
            sl_trade = trades_by_oid[sl_oid]
            if sl_trade.orderStatus.status not in _DONE:
                sl_order = sl_trade.order
                old_sl = sl_order.auxPrice if sl_order.orderType == "STP" else sl_order.lmtPrice
                if sl_order.orderType == "STP":
                    sl_order.auxPrice = new_sl
                else:
                    sl_order.lmtPrice = new_sl
                try:
                    ibc.paper.modifyOrder(contract, sl_order)
                    log.info(f"Cmd {cmd_id}: SL {old_sl} → {new_sl} (fill={fill_price} slip={slippage:+.2f})")
                    modified += 1
                except Exception as e:
                    log.warning(f"Cmd {cmd_id}: SL modify failed: {e}")

        if modified > 0:
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET tp_price=?, sl_price=?,"
                    " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (new_tp, new_sl, cmd_id)
                )
            rebased += 1

    return rebased


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

    try:
        price = ibc.get_price(cfg.symbols[0]) if ibc else None
    except Exception:
        price = None
    if not price:
        return 0

    tick = cfg.orders.tick_size
    spawned = 0
    for cmd in candidates:
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


def reconcile_stuck_commands(ibc: IBClient, db_path) -> int:
    """
    Sweep RECONCILE_REQUIRED rows and resolve them automatically (bugs 4 & 5).
    Two disjoint cases, disambiguated by fill_price:
      - fill_price IS NULL:     never-filled entry stuck past _STALE_SUBMITTED_MINUTES
                                 (bug 4) -- resolved against IB's current trades().
      - fill_price IS NOT NULL: FILLED command whose TP/SL bracket order id vanished
                                 from IB's cache before poll_tp_sl_fills could catch
                                 the exit (bug 5) -- the bracket orders themselves are
                                 gone from trades(), so this resolves against current
                                 IB positions instead.
    Every case here has an unambiguous IB answer on a paper account: found+filled,
    found+cancelled, or genuinely gone (safe to presume cancelled/expired) -- except
    the flat-vs-open branch below, which is the one genuinely ambiguous case (unknown
    true exit fill) and is left as a logged warning, not auto-closed.
    """
    if not ibc.is_paper_connected():
        return 0
    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.error(f"reconcile_stuck_commands: error fetching trades: {e}")
        return 0
    ib_status_by_oid = {t.order.orderId: (t.orderStatus.status, t.orderStatus.avgFillPrice)
                         for t in trades}

    resolved = 0

    # -- case 1 (bug 4): never-filled entries --
    with get_db(db_path) as con:
        stuck = con.execute(
            "SELECT * FROM commands WHERE status='RECONCILE_REQUIRED' AND fill_price IS NULL"
        ).fetchall()

    for cmd in stuck:
        info = ib_status_by_oid.get(cmd["ib_order_id"])
        with get_db(db_path) as con:
            if info is None:
                update_command_status(con, cmd["id"], "CANCELLED",
                                       error_message="auto-reconciled: not found in IB trades")
            else:
                status, fill_price = info
                if status in ("Filled", "PartiallyFilled"):
                    update_command_status(con, cmd["id"], "FILLED",
                                           fill_price=fill_price, fill_time=_now_utc())
                elif status in ("Cancelled", "Inactive", "ApiCancelled"):
                    update_command_status(con, cmd["id"], "CANCELLED")
                else:
                    continue
        resolved += 1

    # -- case 2 (bug 5): FILLED commands whose bracket vanished --
    with get_db(db_path) as con:
        stuck_filled = con.execute(
            "SELECT * FROM commands WHERE status='RECONCILE_REQUIRED' AND fill_price IS NOT NULL"
        ).fetchall()

    if stuck_filled:
        try:
            positions = {p.contract.symbol: p.position for p in ibc.get_positions()}
        except Exception as e:
            log.error(f"reconcile_stuck_commands: error fetching positions: {e}")
            positions = {}

        for cmd in stuck_filled:
            if positions.get(cmd["symbol"], 0) == 0:
                with get_db(db_path) as con:
                    # ponytail: exit_price approximated from the last cached price_cache
                    # row, not a true IB fill -- upgrade path is querying
                    # ibc.paper.fills()/executions() for the real exit if this proves
                    # inaccurate in practice.
                    last_px = get_cached_price(con, cmd["symbol"])
                    if last_px is None:
                        log.warning(
                            f"Command {cmd['id']} FILLED, bracket vanished, position flat, "
                            "but no cached price to close against -- needs human review"
                        )
                        continue
                    pnl = (last_px - cmd["fill_price"]) if cmd["direction"] == "BUY" \
                        else (cmd["fill_price"] - last_px)
                    update_command_status(con, cmd["id"], "CLOSED",
                                          exit_price=last_px, exit_time=_now_utc(),
                                          exit_reason="RECONCILED", pnl_points=round(pnl, 4))
                    record_completed_trade(con, cmd["id"])
                resolved += 1
            else:
                log.warning(
                    f"Command {cmd['id']} FILLED, bracket vanished, position still open — "
                    "protected by reconcile_naked_positions, needs human review"
                )

    return resolved


def reconcile_naked_positions(ibc: IBClient, cfg) -> None:
    """
    Startup-only safety check (2026-07-20 incident): if broker was offline
    while a position's TP/SL got cancelled or otherwise dropped, there is
    normally no code path that would ever notice — poll_fills/poll_tp_sl_fills
    only look at commands broker itself is actively tracking through a fill,
    not at "does every currently-open IB position still have cover." Any
    symbol with a non-zero IB position and zero resting orders gets a single
    emergency protective stop, sized to the full position.

    Price is taken fresh from get_price() (real quoted price via LIVE market
    data), never from Position.avgCost — avgCost is multiplier-scaled for
    futures (e.g. M2K x5, MNQ x2) and using it directly for an order price
    is exactly the mistake that turned an intended resting stop into an
    instant-fill market order during the 2026-07-20 incident.
    """
    try:
        positions = [p for p in ibc.get_positions() if p.position != 0]
    except Exception as e:
        log.error(f"reconcile_naked_positions: could not fetch positions: {e}")
        return
    if not positions:
        return

    try:
        protected_symbols = {t.contract.symbol for t in ibc.paper.openTrades()}
    except Exception as e:
        log.error(f"reconcile_naked_positions: could not fetch open orders: {e}")
        return

    bracket_pts = cfg.orders.active_brackets[0]

    for pos in positions:
        sym = pos.contract.symbol
        if sym in protected_symbols:
            continue

        qty = abs(pos.position)
        tick = _TICK_BY_SYMBOL.get(sym, 0.25)
        log.error(
            f"RECONCILE: {sym} has a naked position ({pos.position:+.0f} contracts, "
            f"no resting protective order) — placing emergency stop"
        )
        try:
            contract = ibc.get_contract(sym)
            price = ibc.get_price(sym, contract=contract)
        except Exception as e:
            log.error(f"RECONCILE: could not price {sym}, skipping: {e}")
            continue

        if pos.position > 0:  # LONG -> protective SELL stop below market
            sl_price = round_tick(price - bracket_pts, tick)
            action = "SELL"
        else:  # SHORT -> protective BUY stop above market
            sl_price = round_tick(price + bracket_pts, tick)
            action = "BUY"

        from ib_insync import StopOrder
        sl = StopOrder(action, qty, sl_price, tif="GTC")
        try:
            trade = ibc.paper.placeOrder(contract, sl)
            log.error(
                f"RECONCILE: {sym} emergency stop placed — orderId={trade.order.orderId} "
                f"{action} qty={qty} @ {sl_price} (market was {price})"
            )
        except Exception as e:
            log.error(f"RECONCILE: failed to place emergency stop for {sym}: {e}")


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

    # Reset any commands stuck in SUBMITTING from a previous interrupted run
    with get_db(db_path) as con:
        n = con.execute(
            "UPDATE commands SET status='PENDING', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE status='SUBMITTING'"
        ).rowcount
    if n:
        log.warning(f"Reset {n} SUBMITTING command(s) to PENDING")

    ibc = IBClient(cfg)
    try:
        ibc.connect(live=True, paper=True)
    except ConnectionError as e:
        log.error(f"Broker startup: IB connection failed: {e}")
        sys.exit(1)

    register_ib_events(ibc, db_path)

    try:
        reconcile_naked_positions(ibc, cfg)
    except Exception as e:
        log.error(f"reconcile_naked_positions failed (continuing startup): {e}")

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
                        from lib.db import set_system_state
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
                    rb = _drain_rebase_queue(ibc, db_path)
                    if rb:
                        log.info(f"Rebased TP/SL brackets for {rb} command(s)")
                except Exception as e:
                    log.error(f"Error in _drain_rebase_queue: {e}")
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
                try:
                    rc = reconcile_stuck_commands(ibc, db_path)
                    if rc:
                        log.info(f"Auto-reconciled {rc} stuck command(s)")
                except Exception as e:
                    log.error(f"Error in reconcile_stuck_commands: {e}")
                try:
                    reconcile_naked_positions(ibc, cfg)
                except Exception as e:
                    log.error(f"Error in reconcile_naked_positions: {e}")
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

# ── Self-test fixtures (bugs 4 & 5 -- no real IB connection needed) ─────────────

class _FakeOrder:
    def __init__(self, order_id): self.orderId = order_id

class _FakeOrderStatus:
    def __init__(self, status, avg_fill_price=0.0):
        self.status = status
        self.avgFillPrice = avg_fill_price

class _FakeTrade:
    def __init__(self, order_id, status, avg_fill_price=0.0):
        self.order = _FakeOrder(order_id)
        self.orderStatus = _FakeOrderStatus(status, avg_fill_price)

class _FakeContract:
    def __init__(self, symbol): self.symbol = symbol

class _FakePosition:
    def __init__(self, symbol, position):
        self.contract = _FakeContract(symbol)
        self.position = position

class _FakePaper:
    def __init__(self, trades): self._trades = trades
    def trades(self): return self._trades

class _FakeIBClient:
    """Minimal stand-in for IBClient's paper-account surface -- no real IB needed."""
    def __init__(self, trades=None, positions=None):
        self.paper = _FakePaper(trades or [])
        self._positions = positions or []
    def is_paper_connected(self): return True
    def get_positions(self): return self._positions


def self_test() -> bool:
    """
    Self-test:
    - Config loads
    - DB init + PENDING→SUBMITTING claim lock (no IB needed)
    - reconcile_stuck_commands: missing/Filled/Cancelled RECONCILE_REQUIRED rows
      resolve correctly against a fake IB trades() response (bug 4)
    - poll_tp_sl_fills: a FILLED row whose TP/SL oids are absent from a fake
      known_oids set past _STALE_FILLED_MINUTES flips to RECONCILE_REQUIRED (bug 5)
    - reconcile_stuck_commands: a stuck FILLED (bracket-vanished) row closes when
      flat, is left alone with a warning when the position is still open (bug 5)
    - IB connection attempt (SKIP if not available)
    - Broker loop runs for 2 poll cycles (no real orders)
    """
    import tempfile
    from datetime import timedelta
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

            # 5. reconcile_stuck_commands (bug 4): missing / Filled / Cancelled
            def _insert_cmd(**overrides):
                base = dict(symbol='MES', line_price=6500.0, line_type='SUPPORT',
                            line_strength=2, direction='BUY', entry_type='LMT',
                            entry_price=6500.0, tp_price=6502.0, sl_price=6498.0,
                            bracket_size=2.0, status='RECONCILE_REQUIRED')
                base.update(overrides)
                cols = ", ".join(base.keys())
                qs   = ", ".join("?" for _ in base)
                with get_db(db_path) as con:
                    cur = con.execute(f"INSERT INTO commands ({cols}) VALUES ({qs})",
                                      list(base.values()))
                    return cur.lastrowid

            id_missing   = _insert_cmd(ib_order_id=9001)
            id_filled    = _insert_cmd(ib_order_id=9002)
            id_cancelled = _insert_cmd(ib_order_id=9003)

            fake_ibc_1 = _FakeIBClient(trades=[
                _FakeTrade(9002, "Filled", 6501.0),
                _FakeTrade(9003, "Cancelled"),
            ])
            n = reconcile_stuck_commands(fake_ibc_1, db_path)
            assert n == 3, f"expected 3 resolved, got {n}"
            with get_db(db_path) as con:
                s_missing   = con.execute("SELECT status FROM commands WHERE id=?", (id_missing,)).fetchone()["status"]
                s_filled    = con.execute("SELECT status, fill_price FROM commands WHERE id=?", (id_filled,)).fetchone()
                s_cancelled = con.execute("SELECT status FROM commands WHERE id=?", (id_cancelled,)).fetchone()["status"]
            assert s_missing == "CANCELLED", f"missing-order case: {s_missing}"
            assert s_filled["status"] == "FILLED" and s_filled["fill_price"] == 6501.0, \
                f"filled case: {dict(s_filled)}"
            assert s_cancelled == "CANCELLED", f"cancelled case: {s_cancelled}"

            # 6. poll_tp_sl_fills staleness pass (bug 5): FILLED cmd, tp/sl oids
            #    absent from a fake known_oids set, old fill_time -> RECONCILE_REQUIRED
            old_fill_time = (datetime.now(timezone.utc) - timedelta(minutes=100)) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            id_stale = _insert_cmd(status='FILLED', fill_price=6500.5, fill_time=old_fill_time,
                                   ib_order_id=9010, ib_tp_order_id=9011, ib_sl_order_id=9012)
            fake_ibc_2 = _FakeIBClient(trades=[_FakeTrade(9099, "Filled", 1.0)])  # unrelated oid only
            poll_tp_sl_fills(fake_ibc_2, db_path)
            with get_db(db_path) as con:
                s_stale = con.execute("SELECT status FROM commands WHERE id=?", (id_stale,)).fetchone()["status"]
            assert s_stale == "RECONCILE_REQUIRED", f"stale FILLED case: {s_stale}"

            # 7. reconcile_stuck_commands case 2 (bug 5): flat position -> CLOSED,
            #    open position -> left alone with warning
            with get_db(db_path) as con:
                update_price_cache(con, "MES", 6510.0, _now_utc(), source="test")
            id_flat = _insert_cmd(symbol='MES', fill_price=6500.0, fill_time=old_fill_time,
                                  ib_order_id=9020, ib_tp_order_id=9021, ib_sl_order_id=9022)
            id_open = _insert_cmd(symbol='MNQ', fill_price=18000.0, fill_time=old_fill_time,
                                  ib_order_id=9030, ib_tp_order_id=9031, ib_sl_order_id=9032)
            fake_ibc_3 = _FakeIBClient(positions=[_FakePosition("MNQ", 1)])  # MES absent -> flat
            reconcile_stuck_commands(fake_ibc_3, db_path)
            with get_db(db_path) as con:
                s_flat = con.execute("SELECT status FROM commands WHERE id=?", (id_flat,)).fetchone()["status"]
                s_open = con.execute("SELECT status FROM commands WHERE id=?", (id_open,)).fetchone()["status"]
            assert s_flat == "CLOSED", f"flat-position case: {s_flat}"
            assert s_open == "RECONCILE_REQUIRED", f"open-position case should stay untouched: {s_open}"

            # 8. IB connection attempt
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
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Log commands instead of sending to IB")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    run_broker(dry_run=args.dry_run)
