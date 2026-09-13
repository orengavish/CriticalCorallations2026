"""
daily_paper_session.py
Orchestrates a 2-hour daily paper trading session for backtesting data collection.

Schedule (via Task Scheduler): 17:00 Israel time
Duration: 2 hours (fires at start, force-closes everything at +120 min)

Flow:
  1. Guard: skip weekends + US market holidays
  2. Clean up: reqGlobalCancel + wipe stale open DB rows
  3. Set SESSION=RUNNING, REPLENISH_ENABLED=1
  4. Start broker, position_manager, random_gen (~0.85 trades/min → ~100 in 2h)
  5. Sleep until end_time (start + SESSION_MINUTES)
  6. Force-close: reqGlobalCancel + MKT exit all FILLED positions
  7. Stop all subprocesses, set SESSION=SHUTDOWN

Usage:
    python daily_paper_session.py
    python daily_paper_session.py --dry-run      # no real IB orders
    python daily_paper_session.py --minutes 60   # shorter session
    python daily_paper_session.py --self-test

Self-test:
    python daily_paper_session.py --self-test
"""

import sys
import time
import signal
import argparse
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_system_state, set_system_state, update_command_status, record_completed_trade

log = get_logger("daily_paper_session")

SESSION_MINUTES   = 120
TRADES_TARGET     = 100
RATE_PER_MIN      = TRADES_TARGET / SESSION_MINUTES   # ~0.833

# US market holidays 2026 (CME Globex closure or reduced liquidity)
_US_HOLIDAYS = {
    date(2026, 1,  1),   # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4,  3),   # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 7,  3),   # Independence Day (observed)
    date(2026, 9,  7),   # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

_procs: list[subprocess.Popen] = []
_stopping = False


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _US_HOLIDAYS


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup_stale_open_rows(db_path: Path, ibc=None):
    """
    Before starting a fresh session:
      1. reqGlobalCancel on PAPER (kills any leftover IB bracket orders)
      2. Mark stale SUBMITTED/FILLED commands as FORCE_CLOSED so the DB
         doesn't try to re-submit or re-fill them today.
      3. Delete leftover PENDING / SUBMITTING rows (never reached IB).
    Keeps completed_trades and CLOSED commands intact for backtesting history.
    """
    if ibc:
        try:
            if ibc.is_paper_connected():
                ibc.paper.reqGlobalCancel()
                log.info("reqGlobalCancel sent to PAPER")
                time.sleep(1)   # give IB a moment to process
        except Exception as e:
            log.warning(f"reqGlobalCancel failed (non-fatal): {e}")

    with get_db(db_path) as con:
        # Stale SUBMITTED: IB order was cancelled by reqGlobalCancel — record them as closed
        stale_submitted = con.execute(
            "SELECT * FROM commands WHERE status IN ('SUBMITTED', 'SUBMITTING')"
        ).fetchall()
        for cmd in stale_submitted:
            con.execute(
                "UPDATE commands SET status='CANCELLED', updated_at=? WHERE id=?",
                (_now_utc(), cmd["id"])
            )
        if stale_submitted:
            log.info(f"Cancelled {len(stale_submitted)} stale SUBMITTED/SUBMITTING commands")

        # Stale FILLED (open position with no exit) — force-close at last fill price
        stale_filled = con.execute(
            "SELECT * FROM commands WHERE status='FILLED'"
        ).fetchall()
        for cmd in stale_filled:
            exit_p = cmd["fill_price"] or cmd["entry_price"]
            pnl    = 0.0
            update_command_status(
                con, cmd["id"], "CLOSED",
                exit_price  = exit_p,
                exit_time   = _now_utc(),
                exit_reason = "SESSION_CLEANUP",
                pnl_points  = round(pnl, 4),
            )
            record_completed_trade(con, cmd["id"])
        if stale_filled:
            log.info(f"Force-closed {len(stale_filled)} stale FILLED positions at fill price")

        # Leftover PENDING — just delete them (never touched IB)
        n = con.execute(
            "DELETE FROM commands WHERE status='PENDING'"
        ).rowcount
        if n:
            log.info(f"Deleted {n} stale PENDING commands")

    log.info("Stale state cleaned up")


# ── Force close ───────────────────────────────────────────────────────────────

def force_close_all(db_path: Path, ibc, cfg):
    """
    End-of-session force close:
      1. reqGlobalCancel — kills all open IB orders (TP/SL legs, pending limits)
      2. For each FILLED command: place MKT exit on PAPER
      3. Wait up to 30s for MKT fills to come back via trades()
      4. Any remaining FILLED: record at last traded price as FORCE_CLOSE
    """
    log.info("Force-close: starting")

    # Step 1 — cancel everything in IB
    try:
        if ibc.is_paper_connected():
            ibc.paper.reqGlobalCancel()
            log.info("reqGlobalCancel sent")
            time.sleep(2)
    except Exception as e:
        log.warning(f"reqGlobalCancel error: {e}")

    # Step 2 — MKT exit every open position
    with get_db(db_path) as con:
        filled = con.execute("SELECT * FROM commands WHERE status='FILLED'").fetchall()

    if not filled:
        log.info("Force-close: no open positions")
        return

    log.info(f"Force-close: {len(filled)} open position(s) — placing MKT exits")
    from ib_insync import MarketOrder, Order

    for cmd in filled:
        try:
            contract    = ibc.get_contract(cmd["symbol"])
            exit_action = "SELL" if cmd["direction"] == "BUY" else "BUY"
            # Cancel remaining bracket legs first
            for oid in (cmd["ib_tp_order_id"], cmd["ib_sl_order_id"]):
                if oid:
                    try:
                        o = Order(); o.orderId = oid
                        ibc.paper.cancelOrder(o)
                    except Exception:
                        pass
            mkt = MarketOrder(exit_action, cmd["quantity"])
            ibc.paper.placeOrder(contract, mkt)
            log.info(f"MKT exit placed for cmd {cmd['id']} ({exit_action} {cmd['symbol']})")
        except Exception as e:
            log.error(f"MKT exit failed for cmd {cmd['id']}: {e}")

    # Step 3 — wait up to 30 s for fills to land
    log.info("Force-close: waiting up to 30s for MKT fills…")
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(2)
        try:
            trades = ibc.paper.trades()
            ib_filled = {t.order.orderId: t.orderStatus.avgFillPrice
                         for t in trades if t.orderStatus.status == "Filled"}
        except Exception:
            ib_filled = {}

        with get_db(db_path) as con:
            still_open = con.execute(
                "SELECT COUNT(*) FROM commands WHERE status='FILLED'"
            ).fetchone()[0]
        if still_open == 0:
            break

        # Close any that now show filled in IB
        with get_db(db_path) as con:
            open_cmds = con.execute(
                "SELECT * FROM commands WHERE status='FILLED'"
            ).fetchall()
        now = _now_utc()
        for cmd in open_cmds:
            exit_p = None
            # Look for a filled MKT order placed for this cmd's contract/direction
            # (we match by symbol + opposing action since we don't track the MKT oid)
            for trade in (ibc.paper.trades() if ibc.is_paper_connected() else []):
                t_sym = getattr(trade.contract, "symbol", "")
                t_act = trade.order.action
                t_qty = trade.order.totalQuantity
                if (t_sym == cmd["symbol"]
                        and t_act == ("SELL" if cmd["direction"] == "BUY" else "BUY")
                        and t_qty == cmd["quantity"]
                        and trade.orderStatus.status == "Filled"):
                    exit_p = trade.orderStatus.avgFillPrice
                    break

            if exit_p:
                d   = cmd["direction"]
                pnl = (exit_p - cmd["fill_price"]) if d == "BUY" else (cmd["fill_price"] - exit_p)
                with get_db(db_path) as con:
                    update_command_status(con, cmd["id"], "CLOSED",
                                          exit_price=exit_p, exit_time=now,
                                          exit_reason="FORCE_CLOSE",
                                          pnl_points=round(pnl, 4))
                    record_completed_trade(con, cmd["id"])
                log.info(f"Force-closed cmd {cmd['id']} @ {exit_p}  pnl={pnl:+.2f}")

    # Step 4 — anything still FILLED: close at fill_price (worst case)
    with get_db(db_path) as con:
        remaining = con.execute(
            "SELECT * FROM commands WHERE status='FILLED'"
        ).fetchall()
    if remaining:
        log.warning(f"Force-close: {len(remaining)} position(s) did not confirm fill "
                    f"— recording at fill_price with pnl=0")
        now = _now_utc()
        for cmd in remaining:
            exit_p = cmd["fill_price"] or cmd["entry_price"]
            with get_db(db_path) as con:
                update_command_status(con, cmd["id"], "CLOSED",
                                      exit_price=exit_p, exit_time=now,
                                      exit_reason="FORCE_CLOSE",
                                      pnl_points=0.0)
                record_completed_trade(con, cmd["id"])

    # Cancel any remaining SUBMITTED (their TP/SL legs were nuked by reqGlobalCancel)
    with get_db(db_path) as con:
        con.execute(
            "UPDATE commands SET status='CANCELLED', updated_at=? "
            "WHERE status IN ('SUBMITTED','SUBMITTING','PENDING')",
            (_now_utc(),)
        )
    log.info("Force-close complete")


# ── Component management ──────────────────────────────────────────────────────

def _start(name: str, cmd: list) -> subprocess.Popen:
    log.info(f"Starting {name}: {' '.join(cmd)}")
    p = subprocess.Popen(cmd, cwd=str(Path(__file__).parent))
    _procs.append(p)
    return p


def _stop_all():
    global _stopping
    _stopping = True
    log.info("Stopping all components…")
    for p in _procs:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    deadline = time.time() + 10
    for p in _procs:
        remaining = max(0, deadline - time.time())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.kill()
    log.info("All components stopped")


# ── Main session ──────────────────────────────────────────────────────────────

def run_session(duration_minutes: int = SESSION_MINUTES,
                rate: float = RATE_PER_MIN,
                dry_run: bool = False):
    today = date.today()
    cfg   = get_config()
    db_path = Path(cfg.paths.db)

    # ── Guard: trading day check ─────────────────────────────────────────────
    if not _is_trading_day(today):
        log.info(f"Not a trading day ({today.strftime('%A %Y-%m-%d')}) — skipping session")
        print(f"[daily_paper_session] Not a trading day — skipping")
        return

    end_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)

    print(f"\n{'='*60}")
    print(f"  DAILY PAPER SESSION  —  {today}")
    print(f"  Start  : {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"  End    : {end_time.strftime('%H:%M UTC')}  (+{duration_minutes} min)")
    print(f"  Target : {TRADES_TARGET} trades @ {rate:.2f}/min")
    print(f"  DB     : {db_path}")
    if dry_run:
        print(f"  *** DRY-RUN — no real IB orders ***")
    print(f"{'='*60}\n")

    init_db(db_path)

    # ── Connect IB ───────────────────────────────────────────────────────────
    from lib.ib_client import IBClient
    ibc = IBClient(cfg)
    if not dry_run:
        try:
            ibc.connect(live=True, paper=True)
            log.info("IB connected")
        except ConnectionError as e:
            log.error(f"IB connection failed: {e}")
            sys.exit(1)

    # ── Cleanup stale state ──────────────────────────────────────────────────
    cleanup_stale_open_rows(db_path, ibc if not dry_run else None)

    # ── Set session state ────────────────────────────────────────────────────
    # 2026-09-13: REPLENISH_ENABLED default flipped to "0" -- disabled by policy
    # decision (see docs/replenishment.md), not a bug. Replenishment (both
    # decider.py's own immediate re-arm-on-fill and broker.py's CLOSED-based
    # baseline-only path) manufactures extra competitors for a capacity-capped
    # admission queue that's already supposed to run every currently-armed line
    # through the same priority sort -- re-enable only via that doc's own
    # instructions, not by flipping this line back.
    with get_db(db_path) as con:
        set_system_state(con, "SESSION",           "RUNNING")
        set_system_state(con, "REPLENISH_ENABLED", "0")
    log.info("SESSION=RUNNING  REPLENISH_ENABLED=0 (disabled by policy, see docs/replenishment.md)")

    # ── Start components ─────────────────────────────────────────────────────
    broker_cmd = [sys.executable, "broker.py"]
    if dry_run:
        broker_cmd.append("--dry-run")

    gen_cmd = [
        sys.executable, "random_gen.py",
        "--rate", str(round(rate, 4)),
    ]
    if dry_run:
        gen_cmd.append("--dry-run")

    components = [
        ("broker",           broker_cmd),
        ("position_manager", [sys.executable, "position_manager.py"]),
        ("random_gen",       gen_cmd),
    ]
    for name, cmd in components:
        _start(name, cmd)
        time.sleep(1)

    log.info(f"Session running — will force-close at {end_time.strftime('%H:%M UTC')}")

    # ── Wait until end_time ──────────────────────────────────────────────────
    try:
        while True:
            remaining_s = (end_time - datetime.now(timezone.utc)).total_seconds()
            if remaining_s <= 0:
                break
            # Heartbeat every 5 min
            wake = min(remaining_s, 300)
            time.sleep(wake)
            still = (end_time - datetime.now(timezone.utc)).total_seconds()
            if still > 0:
                with get_db(db_path) as con:
                    n_closed = con.execute(
                        "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
                    ).fetchone()[0]
                    n_open = con.execute(
                        "SELECT COUNT(*) FROM commands WHERE status='FILLED'"
                    ).fetchone()[0]
                log.info(f"Heartbeat — {still/60:.0f} min remaining | "
                         f"closed={n_closed} open={n_open}")
    except KeyboardInterrupt:
        log.warning("Interrupted by user — proceeding to force-close")

    # ── Force close ──────────────────────────────────────────────────────────
    log.info("Session end — initiating force-close")

    # Tell random_gen to stop generating
    with get_db(db_path) as con:
        set_system_state(con, "SESSION", "SHUTDOWN")

    if not dry_run and ibc.is_paper_connected():
        force_close_all(db_path, ibc, cfg)
    else:
        # dry-run: just mark remaining FILLED closed at fill price
        with get_db(db_path) as con:
            remaining = con.execute(
                "SELECT * FROM commands WHERE status='FILLED'"
            ).fetchall()
        for cmd in remaining:
            exit_p = cmd["fill_price"] or cmd["entry_price"]
            with get_db(db_path) as con:
                update_command_status(con, cmd["id"], "CLOSED",
                                      exit_price=exit_p, exit_time=_now_utc(),
                                      exit_reason="FORCE_CLOSE", pnl_points=0.0)
                record_completed_trade(con, cmd["id"])

    # ── Stop components ───────────────────────────────────────────────────────
    _stop_all()

    if not dry_run:
        ibc.disconnect()

    # ── Summary ───────────────────────────────────────────────────────────────
    with get_db(db_path) as con:
        n_total  = con.execute("SELECT COUNT(*) FROM completed_trades").fetchone()[0]
        n_today  = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE DATE(recorded_at)=?",
            (today.isoformat(),)
        ).fetchone()[0]
        tp_today = con.execute(
            "SELECT COUNT(*) FROM completed_trades ct "
            "JOIN commands c ON ct.command_id=c.id "
            "WHERE DATE(ct.recorded_at)=? AND c.exit_reason='TP'",
            (today.isoformat(),)
        ).fetchone()[0]
        sl_today = con.execute(
            "SELECT COUNT(*) FROM completed_trades ct "
            "JOIN commands c ON ct.command_id=c.id "
            "WHERE DATE(ct.recorded_at)=? AND c.exit_reason='SL'",
            (today.isoformat(),)
        ).fetchone()[0]
        pnl_today = con.execute(
            "SELECT COALESCE(SUM(c.pnl_points),0) FROM completed_trades ct "
            "JOIN commands c ON ct.command_id=c.id "
            "WHERE DATE(ct.recorded_at)=?",
            (today.isoformat(),)
        ).fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  SESSION COMPLETE  —  {today}")
    print(f"  Today's trades : {n_today}  (TP={tp_today} SL={sl_today})")
    print(f"  Today's P&L    : {pnl_today:+.2f} pts")
    print(f"  Total in DB    : {n_total} completed trades")
    print(f"{'='*60}\n")

    log.info(f"Daily paper session done. today={n_today} trades  pnl={pnl_today:+.2f}")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers

        # 1. Trading day guard
        assert _is_trading_day(date(2026, 4, 21))          # Monday
        assert not _is_trading_day(date(2026, 4, 25))       # Saturday
        assert not _is_trading_day(date(2026, 1,  1))       # New Year

        # 2. Cleanup + force-close logic (no IB, no real DB)
        cfg = get_config()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            with get_db(db_path) as con:
                # Insert stale FILLED command
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, source, quantity, status, fill_price, fill_time)
                    VALUES ('MES', 6500.0, 'SUPPORT', 2,
                            'BUY', 'LMT', 6500.0, 6502.0, 6498.0,
                            2.0, 'random_lmt', 1, 'FILLED', 6500.0, ?)
                """, (_now_utc(),))
                # Insert stale PENDING
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, source, quantity, status)
                    VALUES ('MES', 6500.0, 'SUPPORT', 2,
                            'SELL', 'STP', 6500.0, 6498.0, 6502.0,
                            2.0, 'random_stp', 1, 'PENDING')
                """)

            cleanup_stale_open_rows(db_path, ibc=None)

            with get_db(db_path) as con:
                # FILLED should become CLOSED
                row = con.execute(
                    "SELECT status, exit_reason FROM commands WHERE status='CLOSED'"
                ).fetchone()
                assert row, "Stale FILLED not cleaned up"
                assert row["exit_reason"] == "SESSION_CLEANUP"
                # PENDING should be deleted
                n_pending = con.execute(
                    "SELECT COUNT(*) FROM commands WHERE status='PENDING'"
                ).fetchone()[0]
                assert n_pending == 0, f"Stale PENDING not deleted: {n_pending}"

            # 3. Rate calculation sanity
            assert abs(RATE_PER_MIN - TRADES_TARGET / SESSION_MINUTES) < 0.01

            reset_loggers()

        print("[self-test] daily_paper_session: PASS")
        return True

    except Exception as e:
        print(f"[self-test] daily_paper_session: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Daily 2-hour paper trading session for backtesting data collection"
    )
    parser.add_argument("--self-test",  action="store_true")
    parser.add_argument("--dry-run",    action="store_true",
                        help="No real IB orders — simulates full lifecycle offline")
    parser.add_argument("--minutes",    type=int, default=SESSION_MINUTES,
                        help=f"Session duration in minutes (default {SESSION_MINUTES})")
    parser.add_argument("--rate",       type=float, default=RATE_PER_MIN,
                        help=f"Trades per minute (default {RATE_PER_MIN:.3f} ≈ 100 in 2h)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    signal.signal(signal.SIGINT,  lambda s, f: None)   # let main loop handle Ctrl+C
    signal.signal(signal.SIGTERM, lambda s, f: _stop_all() or sys.exit(0))

    run_session(duration_minutes=args.minutes, rate=args.rate, dry_run=args.dry_run)
