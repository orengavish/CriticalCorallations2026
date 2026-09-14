"""
trader/spread_manager.py
Spread algorithm (Part 3 of the comparison-engine build cycle): Geva's DIFF
method (Galgo2029/knowledge/Claims/spread-diff-trading.md, AI-35-AI-35g).
Genuinely new execution infrastructure -- the one part of this build with no
existing code to build on: two simultaneous legs (1 long + 1 short), hedged,
with NO per-leg stop-loss (AI-35: the hedge itself bounds risk), closed only
by the strategy's own DIFF pattern logic, never a resting order.

Leg direction (AI-35a) is genuinely ambiguous from the extracted claim text
alone ("the instrument that moved MORE -> LONG it, expect the excess to
correct back" reads backwards against plain mean-reversion logic on its face)
-- 2026-09-11 decision: run BOTH readings simultaneously, tagged separately
(source='spread' = literal AI-35a text, source='spread_control' = the
reversed/mean-reversion reading), same real-vs-control pattern this system
already uses for Algo 1-5, and let Part 1's comparison engine judge which one
is actually right instead of guessing.

Position sizing (AI-35d): dollar-ATR contract ratio via lib/atr.py +
lib/algo_pnl.SYMBOL_MULTIPLIERS, simplified to a small whole-number ratio
(Python's own fractions.Fraction.limit_denominator -- stdlib, no custom
ratio-simplification code needed).

Per-leg stop-loss (2026-09-14, explicit user instruction overriding AI-35's
"no per-leg stop" design -- see docs/spread_bracket_stop.md for the full
deviation note): each leg gets a real resting STP order, sized like every
other algorithm family's bracket ("a few brackets, as we do with all other
types" -- cfg.spread.bracket_size, same convention as generator.bracket_sizes,
not a literal 4). This is a control-group build, not a redesign of AI-35 --
the DIFF-pattern exit logic below is unchanged and still the PRIMARY close
path; the per-leg stop is a safety net for the case that primary logic is
slow to react or a leg gaps hard. If a leg's stop fires first,
check_spread_exit() detects it, cancels the other leg's still-resting stop,
and flattens the other leg at market (the hedge is broken the instant one
leg is stopped out -- an unhedged single leg is exactly what
reconcile_naked_positions() still won't protect for source='spread'/
'spread_control', so this system's OWN exit logic must not leave one
resting).

Exit (AI-35e), deliberately simplified for v1: the full "1-2-3" pattern is a
discretionary, multi-point pattern; this implements its core risk-control
intent -- track the furthest the DIFF moves in the OPENING (against-position)
direction, detect the first reversal back toward closing (point 1), and:
  - GAP_CLOSED: diff makes it back to the entry-time level -- take the
    convergence profit.
  - ADVERSE_BREAK: diff breaks back past point 1 in the OPENING direction
    after having reversed -- momentum isn't with the position, cut it (AI-35e:
    "exit the moment it breaks point 1 against the closing direction, before
    the gap widens further"). A fuller point-2/point-3 classifier is a larger
    follow-up, not this v1's scope.

Also runs a portfolio kill-switch (2026-09-11 decision, operational safety
net around the method, not a change to it): sums current unrealized $ PnL
across an open spread_group_id's two legs each check, flattens both if it
crosses cfg.spread.kill_switch_usd.

CRITICAL: broker.py's reconcile_naked_positions() is explicitly told (see its
own docstring) to leave source='spread'/'spread_control' positions
unprotected -- that's by design, but it means a partial fill (one leg fills,
the other's submission fails) would leave a genuinely naked, unprotected
position if not caught immediately. open_spread_position() below checks both
legs filled and flattens the first leg immediately if the second fails.

Usage (called from the broker's main loop):
    from trader.spread_manager import (check_spread_signals, check_spread_exit,
                                        check_portfolio_kill_switch)
    check_spread_signals(ibc, db_path, cfg)
    check_spread_exit(ibc, db_path)
    check_portfolio_kill_switch(ibc, db_path, cfg)

Self-test:
    python trader/spread_manager.py --self-test
"""

import sys
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ib_insync import Order, StopOrder

from lib.db import get_db, init_db
from lib.order_builder import get_tick_size, round_tick
from lib.order_builder_spread import build_spread_leg, place_spread_leg
from lib.algo_pnl import SYMBOL_MULTIPLIERS
from lib.atr import atr20_points
from lib.spread_diff import ALL_PAIRS, check_spread_entry, diff_series
from lib.logger import get_logger

log = get_logger("spread_manager")

DEFAULT_BRACKET_SIZE = 8.0   # mid-point of generator.bracket_sizes' [2,4,8,16] --
                              # a coarse safety-net stop, not a tight profit bracket;
                              # see cfg.spread.bracket_size


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Direction & sizing (pure, no IB dependency) ────────────────────────────────

def resolve_leg_directions(diff_direction: str, literal: bool) -> tuple[str, str]:
    """
    diff_direction: "A_OVER" (sym_a ran ahead) or "B_OVER" (sym_b ran ahead),
    from lib.spread_diff.check_spread_entry().
    literal=True: AI-35a's literal text -- LONG the over-mover, SHORT the
    under-mover. literal=False: the reversed/mean-reversion reading -- SHORT
    the over-mover, LONG the under-mover.
    Returns (action_a, action_b), each "BUY" or "SELL".
    """
    a_over = diff_direction == "A_OVER"
    if literal:
        return ("BUY", "SELL") if a_over else ("SELL", "BUY")
    else:
        return ("SELL", "BUY") if a_over else ("BUY", "SELL")


def contract_ratio(bars_db_path, sym_a: str, sym_b: str,
                   max_denominator: int = 6) -> tuple[int, int] | None:
    """
    AI-35d: dollar-ATR contract ratio, simplified to a small whole-number
    ratio. Worked example from the source doc: ES $ATR=$4,100, NASDAQ
    $ATR=$7,780, ratio~1.9 -> "roughly 2 ES : 1 NASDAQ" (more contracts of the
    smaller-$ATR leg to size the two legs dollar-equivalent). Returns
    (qty_a, qty_b), or None if ATR data isn't available for either symbol.
    """
    atr_a = atr20_points(bars_db_path, sym_a)
    atr_b = atr20_points(bars_db_path, sym_b)
    if atr_a is None or atr_b is None:
        return None
    dollar_a = atr_a * SYMBOL_MULTIPLIERS.get(sym_a, 1.0)
    dollar_b = atr_b * SYMBOL_MULTIPLIERS.get(sym_b, 1.0)
    if dollar_a <= 0 or dollar_b <= 0:
        return None
    frac = Fraction(dollar_b / dollar_a).limit_denominator(max_denominator)
    return (max(frac.numerator, 1), max(frac.denominator, 1))


# ── Exit state machine (pure core, IB wrapper below) ───────────────────────────

def _advance_exit_state(pos: dict, current_diff: float, confirm_dist: float) -> dict:
    """
    Pure decision step for one open spread position given the latest diff.
    Returns a dict of fields to persist: {extreme_diff, point1_diff, close}
    where close is None, "GAP_CLOSED", or "ADVERSE_BREAK".
    """
    # closing_down: whether this position profits as the diff FALLS (True) or
    # RISES (False) -- captured once, at open time, as pos["closing_down"]
    # (open_spread_position() sets it from action_a, which by then already
    # reflects whichever variant, literal or control, this position is).
    closing_down = pos["closing_down"]

    extreme = pos["extreme_diff"]
    point1 = pos["point1_diff"]
    entry = pos["entry_diff"]

    if closing_down:
        # Profits as diff falls. "Opening" = diff rising further.
        if current_diff > extreme:
            return {"extreme_diff": current_diff, "point1_diff": None, "close": None}
        if point1 is None and current_diff <= extreme - confirm_dist:
            return {"extreme_diff": extreme, "point1_diff": current_diff, "close": None}
        if point1 is not None:
            if current_diff <= entry:
                return {"extreme_diff": extreme, "point1_diff": point1, "close": "GAP_CLOSED"}
            if current_diff >= point1 + confirm_dist:
                return {"extreme_diff": extreme, "point1_diff": point1, "close": "ADVERSE_BREAK"}
        return {"extreme_diff": extreme, "point1_diff": point1, "close": None}
    else:
        # Profits as diff rises. "Opening" = diff falling further.
        if current_diff < extreme:
            return {"extreme_diff": current_diff, "point1_diff": None, "close": None}
        if point1 is None and current_diff >= extreme + confirm_dist:
            return {"extreme_diff": extreme, "point1_diff": current_diff, "close": None}
        if point1 is not None:
            if current_diff >= entry:
                return {"extreme_diff": extreme, "point1_diff": point1, "close": "GAP_CLOSED"}
            if current_diff <= point1 - confirm_dist:
                return {"extreme_diff": extreme, "point1_diff": point1, "close": "ADVERSE_BREAK"}
        return {"extreme_diff": extreme, "point1_diff": point1, "close": None}


# ── IB-interaction wrappers ─────────────────────────────────────────────────────

def _place_leg_stop(ibc, sym: str, action: str, qty: int, bracket_size: float):
    """
    Best-effort resting STP for one already-filled leg, bracket_size points off
    the current market price (2026-09-14 addition, see module docstring). Returns
    (stop_price, order_id) or (None, None) if a live price isn't available --
    that leg is left unprotected exactly as before this change, not a crash.
    """
    try:
        price = ibc.get_price(sym)
    except Exception as e:
        log.warning(f"Spread leg stop for {sym}: no live price ({e}) -- leg left unprotected")
        return None, None

    tick = get_tick_size(sym)
    stop_action = "SELL" if action == "BUY" else "BUY"
    stop_price = round_tick(price - bracket_size if action == "BUY" else price + bracket_size, tick)

    try:
        order = StopOrder(stop_action, qty, stop_price)
        order.tif = "GTC"
        trade = ibc.paper.placeOrder(ibc.get_contract(sym), order)
        return stop_price, trade.order.orderId
    except Exception as e:
        log.warning(f"Spread leg stop for {sym}: submission failed ({e}) -- leg left unprotected")
        return None, None


def open_spread_position(ibc, db_path, sym_a: str, sym_b: str,
                         qty_a: int, qty_b: int, action_a: str, action_b: str,
                         source: str, entry_diff: str,
                         bracket_size: float = DEFAULT_BRACKET_SIZE) -> str | None:
    """
    Submit both legs (MKT) and, only if both fill, write the tracking rows.
    If leg B's submission fails after leg A already went out, immediately
    flattens leg A with a reverse MKT order -- a lone unhedged fill left
    standing is exactly the naked-and-unprotected state
    reconcile_naked_positions() has been told to ignore for this source.

    Then (2026-09-14) gives each leg its own resting STP order, bracket_size
    points off the current price -- see module docstring for why this
    deviates from AI-35's original "hedge bounds risk, no per-leg stop"
    design. Best-effort: a leg with no live price available is left
    unprotected (logged loudly) rather than blocking the whole position.

    Returns the new spread_group_id, or None if the pair could not be
    safely established.
    """
    group_id = str(uuid.uuid4())
    now = _now()

    contract_a = ibc.get_contract(sym_a)
    order_a = build_spread_leg(action_a, qty_a)
    try:
        result_a = place_spread_leg(ibc.paper, contract_a, order_a)
    except Exception as e:
        log.error(f"Spread open {sym_a}/{sym_b}: leg A submission failed: {e}")
        return None

    contract_b = ibc.get_contract(sym_b)
    order_b = build_spread_leg(action_b, qty_b)
    try:
        result_b = place_spread_leg(ibc.paper, contract_b, order_b)
    except Exception as e:
        log.error(f"Spread open {sym_a}/{sym_b}: leg B submission failed ({e}) -- "
                  f"flattening leg A ({sym_a}) to avoid a naked fill")
        flatten_action = "SELL" if action_a == "BUY" else "BUY"
        try:
            flatten_order = build_spread_leg(flatten_action, qty_a)
            place_spread_leg(ibc.paper, contract_a, flatten_order)
        except Exception as e2:
            log.error(f"Spread open {sym_a}/{sym_b}: FAILED TO FLATTEN leg A after "
                      f"leg B failure -- needs immediate human attention: {e2}")
        return None

    sl_price_a, sl_order_id_a = _place_leg_stop(ibc, sym_a, action_a, qty_a, bracket_size)
    sl_price_b, sl_order_id_b = _place_leg_stop(ibc, sym_b, action_b, qty_b, bracket_size)

    tick_a, tick_b = get_tick_size(sym_a), get_tick_size(sym_b)
    with get_db(db_path) as con:
        for sym, action, qty, ib_id, tick in (
            (sym_a, action_a, qty_a, result_a["entry_id"], tick_a),
            (sym_b, action_b, qty_b, result_b["entry_id"], tick_b),
        ):
            # tp_price/sl_price = entry_price is a deliberate inert sentinel
            # (zero-distance bracket) on the commands row itself -- the real
            # per-leg stop (2026-09-14) is tracked on spread_positions instead
            # (sl_price_a/b, sl_order_id_a/b), not here, since this row's own
            # tp/sl columns are shared with every other algo family's very
            # different single-order-per-command bracket shape.
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength, direction,
                     entry_type, entry_price, tp_price, sl_price, bracket_size,
                     source, quantity, logical_trade_id, status,
                     ib_order_id, spread_group_id)
                VALUES (?, 0, 'SUPPORT', 1, ?, 'MKT', 0, 0, 0, 0,
                        ?, ?, ?, 'FILLED', ?, ?)
            """, (sym, action, source, qty, group_id, ib_id, group_id))

        closing_down = action_a == "SELL"  # see _advance_exit_state's own note
        con.execute("""
            INSERT INTO spread_positions
                (spread_group_id, sym_a, sym_b, qty_a, qty_b, action_a, action_b,
                 source, entry_diff, extreme_diff, status, opened_at, updated_at,
                 bracket_size, sl_price_a, sl_price_b, sl_order_id_a, sl_order_id_b)
            VALUES (?,?,?,?,?,?,?,?,?,?, 'OPEN', ?, ?, ?,?,?,?,?)
        """, (group_id, sym_a, sym_b, qty_a, qty_b, action_a, action_b,
              source, entry_diff, entry_diff, now, now,
              bracket_size, sl_price_a, sl_price_b, sl_order_id_a, sl_order_id_b))

    log.info(f"Spread opened ({source}): {action_a} {qty_a} {sym_a} (stop={sl_price_a}) / "
             f"{action_b} {qty_b} {sym_b} (stop={sl_price_b}), group={group_id}, "
             f"entry_diff={entry_diff}, bracket_size={bracket_size}")
    return group_id


def _cancel_leg_stop(ibc, order_id) -> None:
    """Best-effort cancel of one leg's resting STP by order id (2026-09-14 addition).
    Same fire-and-forget pattern as decider.py's forced_eod cancels -- a stop that's
    already filled/gone just no-ops here, not an error."""
    if not order_id:
        return
    try:
        o = Order()
        o.orderId = order_id
        ibc.paper.cancelOrder(o)
    except Exception as e:
        log.warning(f"Spread leg stop cancel (order {order_id}) failed: {e}")


def _close_spread_position(ibc, db_path, pos: dict, reason: str) -> bool:
    """Flatten both legs with reverse MKT orders, mark commands CLOSED and the
    spread_positions row CLOSED. Best-effort on each leg independently -- a
    failure on one leg is logged loudly but doesn't block closing the other.
    Cancels each leg's resting stop first (2026-09-14) -- otherwise it would be
    left resting against a position that's about to be flat, an orphaned order
    that could fire later against whatever happens to be in that symbol next."""
    now = _now()
    _cancel_leg_stop(ibc, pos.get("sl_order_id_a"))
    _cancel_leg_stop(ibc, pos.get("sl_order_id_b"))
    ok = True
    for sym, action, qty in (
        (pos["sym_a"], pos["action_a"], pos["qty_a"]),
        (pos["sym_b"], pos["action_b"], pos["qty_b"]),
    ):
        flatten_action = "SELL" if action == "BUY" else "BUY"
        try:
            contract = ibc.get_contract(sym)
            order = build_spread_leg(flatten_action, qty)
            place_spread_leg(ibc.paper, contract, order)
        except Exception as e:
            ok = False
            log.error(f"Spread close {pos['spread_group_id']}: FAILED to flatten "
                      f"{sym} leg -- needs immediate human attention: {e}")

    with get_db(db_path) as con:
        con.execute(
            "UPDATE commands SET status='CLOSED', exit_time=?, exit_reason=? "
            "WHERE spread_group_id=?", (now, reason, pos["spread_group_id"])
        )
        con.execute(
            "UPDATE spread_positions SET status='CLOSED', closed_at=?, close_reason=?, "
            "updated_at=? WHERE spread_group_id=?",
            (now, reason, now, pos["spread_group_id"])
        )
    log.info(f"Spread closed ({reason}): group={pos['spread_group_id']}")
    return ok


def _close_spread_position_after_leg_stop(ibc, db_path, pos: dict, hit_leg: str) -> bool:
    """
    One leg's own resting STP already filled at IB (detected by
    check_spread_exit()'s _leg_stop_hit() scan) -- the hedge is now broken.
    Cancel the OTHER leg's still-resting stop and flatten it at market;
    the hit leg needs no action, IB already closed it. hit_leg: "a" or "b".
    """
    now = _now()
    other = "b" if hit_leg == "a" else "a"
    _cancel_leg_stop(ibc, pos.get(f"sl_order_id_{other}"))

    sym, action, qty = pos[f"sym_{other}"], pos[f"action_{other}"], pos[f"qty_{other}"]
    flatten_action = "SELL" if action == "BUY" else "BUY"
    ok = True
    try:
        contract = ibc.get_contract(sym)
        order = build_spread_leg(flatten_action, qty)
        place_spread_leg(ibc.paper, contract, order)
    except Exception as e:
        ok = False
        log.error(f"Spread close {pos['spread_group_id']} (leg {hit_leg} stopped out): "
                  f"FAILED to flatten surviving {sym} leg -- needs immediate human attention: {e}")

    with get_db(db_path) as con:
        con.execute(
            "UPDATE commands SET status='CLOSED', exit_time=?, exit_reason=? "
            "WHERE spread_group_id=?", (now, "SL_HIT", pos["spread_group_id"])
        )
        con.execute(
            "UPDATE spread_positions SET status='CLOSED', closed_at=?, close_reason=?, "
            "updated_at=? WHERE spread_group_id=?",
            (now, "SL_HIT", now, pos["spread_group_id"])
        )
    log.info(f"Spread closed (SL_HIT, leg {hit_leg}): group={pos['spread_group_id']}, "
             f"flattened surviving {sym} leg")
    return ok


def _leg_stops_hit(ibc) -> set:
    """Order ids of any 'Filled' trades on the paper connection right now --
    same trades()-scan pattern as broker.py's poll_tp_sl_fills(). Used to detect
    a spread leg's own resting stop having fired."""
    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.error(f"_leg_stops_hit: error fetching trades: {e}")
        return set()
    return {t.order.orderId for t in trades if t.orderStatus.status == "Filled"}


def check_spread_signals(ibc, db_path, cfg, bars_db_path=None) -> int:
    """One poll cycle: check all 6 pairs for an AI-35c entry trigger; for each
    that fires, open BOTH the literal and control-direction variants, once per
    bracket_size in cfg.spread.bracket_sizes (2026-09-14: was a single fixed
    bracket_size -- see that config key's own comment for why this is a bigger
    real-exposure step than it looks). "Already open" is now scoped per
    (pair, bracket_size), not just per pair, so each bracket size runs its own
    independent open/closed lifecycle -- a still-open 4-point group must not
    block a fresh 32-point one on the same pair, and vice versa. Returns count
    of (pair, bracket_size) combos opened this cycle."""
    init_db(db_path)
    bars_db_path = bars_db_path or (Path(db_path).parent / "bars.db")
    gap_mult = getattr(cfg.spread, "gap_multiplier", 1.75)
    max_denom = getattr(cfg.spread, "max_contract_ratio_denominator", 6)
    bracket_sizes = getattr(cfg.spread, "bracket_sizes", None) or \
        [getattr(cfg.spread, "bracket_size", DEFAULT_BRACKET_SIZE)]

    opened = 0
    for sym_a, sym_b in ALL_PAIRS:
        signal = check_spread_entry(bars_db_path, sym_a, sym_b, gap_multiplier=gap_mult)
        if not signal:
            continue

        ratio = contract_ratio(bars_db_path, sym_a, sym_b, max_denominator=max_denom)
        if ratio is None:
            log.warning(f"Spread signal on {sym_a}/{sym_b} but no ATR data for sizing -- skipped")
            continue
        qty_a, qty_b = ratio

        for bracket_size in bracket_sizes:
            with get_db(db_path) as con:
                already_open = con.execute(
                    "SELECT 1 FROM spread_positions WHERE status='OPEN' "
                    "AND sym_a=? AND sym_b=? AND bracket_size=?",
                    (sym_a, sym_b, bracket_size)
                ).fetchone()
            if already_open:
                continue

            for source, literal in (("spread", True), ("spread_control", False)):
                action_a, action_b = resolve_leg_directions(signal["direction"], literal)
                open_spread_position(ibc, db_path, sym_a, sym_b, qty_a, qty_b,
                                     action_a, action_b, source, signal["diff"],
                                     bracket_size=bracket_size)
            opened += 1

    return opened


def check_spread_exit(ibc, db_path, bars_db_path=None, confirm_frac: float = 0.10) -> int:
    """One poll cycle: for every OPEN spread position, first check whether
    either leg's own resting stop already fired at IB (2026-09-14 addition --
    see module docstring); if so, close via _close_spread_position_after_leg_stop()
    and skip the DIFF-pattern check for that position this cycle. Otherwise
    advance the exit state machine and close any that trigger GAP_CLOSED or
    ADVERSE_BREAK. Returns count closed (either path)."""
    bars_db_path = bars_db_path or (Path(db_path).parent / "bars.db")

    with get_db(db_path) as con:
        positions = [dict(r) for r in con.execute(
            "SELECT * FROM spread_positions WHERE status='OPEN'"
        ).fetchall()]

    closed = 0
    filled_ids = _leg_stops_hit(ibc) if positions else set()
    for pos in positions:
        hit_leg = None
        if pos.get("sl_order_id_a") and pos["sl_order_id_a"] in filled_ids:
            hit_leg = "a"
        elif pos.get("sl_order_id_b") and pos["sl_order_id_b"] in filled_ids:
            hit_leg = "b"
        if hit_leg:
            if _close_spread_position_after_leg_stop(ibc, db_path, pos, hit_leg):
                closed += 1
            continue

        series = diff_series(bars_db_path, pos["sym_a"], pos["sym_b"], limit_bars=1)
        if not series:
            continue
        current_diff = series[-1][1]

        # confirm_dist scales with the pair's own recent swing, same "how far
        # is meaningful" logic as correlation_signal.py's RETEST/CONFIRM_TICKS,
        # just in dollar terms here since diff already is.
        from lib.spread_diff import average_daily_swing
        avg_swing = average_daily_swing(bars_db_path, pos["sym_a"], pos["sym_b"]) or 1.0
        confirm_dist = max(avg_swing * confirm_frac, 0.01)

        pos["closing_down"] = pos["action_a"] == "SELL"
        decision = _advance_exit_state(pos, current_diff, confirm_dist)

        with get_db(db_path) as con:
            con.execute(
                "UPDATE spread_positions SET extreme_diff=?, point1_diff=?, updated_at=? "
                "WHERE spread_group_id=?",
                (decision["extreme_diff"], decision["point1_diff"], _now(),
                 pos["spread_group_id"])
            )

        if decision["close"]:
            pos["extreme_diff"] = decision["extreme_diff"]
            pos["point1_diff"] = decision["point1_diff"]
            if _close_spread_position(ibc, db_path, pos, decision["close"]):
                closed += 1

    return closed


def check_portfolio_kill_switch(ibc, db_path, cfg) -> int:
    """
    Sums current unrealized $ PnL across each OPEN spread_group_id's two legs
    (current price vs. the shared entry_diff isn't quite right for per-leg
    PnL, so this uses each leg's own current price vs its commands.entry_price
    -- but entry_price is stored as 0 for spread legs (see the sentinel note
    in open_spread_position); PnL is instead derived from the diff itself:
    (current_diff - entry_diff), signed by which side of the pair is long the
    diff, times $1 (diff is already dollar-scaled) -- no separate per-symbol
    price lookup needed. Flattens both legs if the combined loss crosses
    cfg.spread.kill_switch_usd. Returns count of groups flattened.
    """
    threshold = getattr(cfg.spread, "kill_switch_usd", None)
    if not threshold:
        return 0

    bars_db_path = Path(db_path).parent / "bars.db"
    with get_db(db_path) as con:
        positions = [dict(r) for r in con.execute(
            "SELECT * FROM spread_positions WHERE status='OPEN'"
        ).fetchall()]

    flattened = 0
    for pos in positions:
        series = diff_series(bars_db_path, pos["sym_a"], pos["sym_b"], limit_bars=1)
        if not series:
            continue
        current_diff = series[-1][1]
        closing_down = pos["action_a"] == "SELL"
        # Profits as diff falls (closing_down) -> pnl = entry - current.
        # Profits as diff rises -> pnl = current - entry.
        pnl = (pos["entry_diff"] - current_diff) if closing_down else (current_diff - pos["entry_diff"])

        if pnl <= -abs(threshold):
            log.error(f"KILL SWITCH: spread {pos['spread_group_id']} "
                      f"({pos['sym_a']}/{pos['sym_b']}, {pos['source']}) "
                      f"unrealized ${pnl:.2f} past -${threshold} -- flattening both legs")
            if _close_spread_position(ibc, db_path, pos, "KILL_SWITCH"):
                flattened += 1

    return flattened


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        # ── Pure functions first ──
        assert resolve_leg_directions("A_OVER", literal=True) == ("BUY", "SELL")
        assert resolve_leg_directions("A_OVER", literal=False) == ("SELL", "BUY")
        assert resolve_leg_directions("B_OVER", literal=True) == ("SELL", "BUY")
        assert resolve_leg_directions("B_OVER", literal=False) == ("BUY", "SELL")

        # Exit state machine: closing_down position (entered on a diff spike UP).
        pos = {"entry_diff": 100.0, "extreme_diff": 100.0, "point1_diff": None,
               "closing_down": True}
        # Diff keeps rising -- extreme updates, no point1, no close.
        d1 = _advance_exit_state(pos, current_diff=110.0, confirm_dist=5.0)
        assert d1 == {"extreme_diff": 110.0, "point1_diff": None, "close": None}
        pos.update(extreme_diff=110.0)
        # Diff turns back down past confirm_dist -- point1 established.
        d2 = _advance_exit_state(pos, current_diff=104.0, confirm_dist=5.0)
        assert d2["point1_diff"] == 104.0 and d2["close"] is None
        pos.update(point1_diff=104.0)
        # Diff makes it back to entry -- GAP_CLOSED.
        d3 = _advance_exit_state(pos, current_diff=99.0, confirm_dist=5.0)
        assert d3["close"] == "GAP_CLOSED"
        # Alternate path: instead, diff breaks back UP past point1+confirm_dist -> adverse.
        d4 = _advance_exit_state(pos, current_diff=110.0, confirm_dist=5.0)
        assert d4["close"] == "ADVERSE_BREAK", f"Expected ADVERSE_BREAK, got {d4}"
        # Between point1 and entry -- hold, no close.
        d5 = _advance_exit_state(pos, current_diff=102.0, confirm_dist=5.0)
        assert d5["close"] is None

        # Mirror: closing_down=False (entered on a diff spike DOWN).
        pos_up = {"entry_diff": -100.0, "extreme_diff": -100.0, "point1_diff": None,
                  "closing_down": False}
        u1 = _advance_exit_state(pos_up, current_diff=-110.0, confirm_dist=5.0)
        assert u1 == {"extreme_diff": -110.0, "point1_diff": None, "close": None}
        pos_up.update(extreme_diff=-110.0)
        u2 = _advance_exit_state(pos_up, current_diff=-104.0, confirm_dist=5.0)
        assert u2["point1_diff"] == -104.0
        pos_up.update(point1_diff=-104.0)
        u3 = _advance_exit_state(pos_up, current_diff=-99.0, confirm_dist=5.0)
        assert u3["close"] == "GAP_CLOSED"
        u4 = _advance_exit_state(pos_up, current_diff=-110.0, confirm_dist=5.0)
        assert u4["close"] == "ADVERSE_BREAK"

        # contract_ratio: reproduce AI-35d's own worked example via a fake bars.db
        # (ES-scale ATR/multiplier values, not this system's real micros, purely to
        # verify the ratio math against the doc's own numbers).
        import tempfile
        import sqlite3
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bars_test.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE bars_15m (symbol TEXT, ts TEXT, "
                        "open REAL, high REAL, low REAL, close REAL, volume REAL)")
            # 21 days, MES-style symbol with a fixed 16.4pt daily range (-> ATR
            # 82pt/5 since we don't have a real ES here, so scale via mult=50
            # patched in below), and MNQ-style with a fixed 77.8pt daily range.
            for day in range(21):
                d = f"2026-06-{day+1:02d}"
                con.execute("INSERT INTO bars_15m VALUES ('MES', ?, 5500,5508.2,5500,5500,100)", (f"{d}T14:00:00Z",))
                con.execute("INSERT INTO bars_15m VALUES ('MES', ?, 5500,5500,5500,5500,100)", (f"{d}T14:30:00Z",))
                con.execute("INSERT INTO bars_15m VALUES ('MNQ', ?, 20000,20038.9,20000,20000,100)", (f"{d}T14:00:00Z",))
                con.execute("INSERT INTO bars_15m VALUES ('MNQ', ?, 20000,20000,20000,20000,100)", (f"{d}T14:30:00Z",))
            con.commit()
            con.close()

            with patch.dict(SYMBOL_MULTIPLIERS, {"MES": 50.0, "MNQ": 20.0}):
                ratio = contract_ratio(db_path, "MES", "MNQ", max_denominator=6)
            assert ratio is not None
            # $ATR(MES-as-ES) ~= 8.2*50=410*20(days avg, true-range not pure high-low,
            # close enough)... just assert the DIRECTION is right: MNQ's $ATR is ~4x
            # MES's here, so MES needs ~4x the contracts for dollar-parity.
            qty_a, qty_b = ratio
            assert qty_a > qty_b, f"Expected more MES contracts than MNQ, got {ratio}"

            # Missing ATR data -> None, not a crash.
            assert contract_ratio(db_path, "MES", "NOPE") is None

        # ── IB-wrapper: dual-leg open with fill-safety ──
        class _FakeOrder:
            def __init__(self, order_id, action, qty):
                self.orderId, self.action, self.totalQuantity = order_id, action, qty

        class _FakeTrade:
            def __init__(self, order): self.order = order

        class _FakeContract:
            def __init__(self, symbol): self.symbol = symbol

        class _FakePaper:
            def __init__(self, fail_on=None):
                self.placed = []
                self._next_id = 900
                self._fail_on = fail_on or set()
            def placeOrder(self, contract, order):
                if contract.symbol in self._fail_on:
                    raise ConnectionError(f"simulated failure for {contract.symbol}")
                self._next_id += 1
                self.placed.append((contract, order))
                return _FakeTrade(_FakeOrder(self._next_id, order.action, order.totalQuantity))

        class _FakeIBC:
            def __init__(self, fail_on=None):
                self.paper = _FakePaper(fail_on)
            def get_contract(self, symbol): return _FakeContract(symbol)

        with tempfile.TemporaryDirectory() as tmp2:
            db_path2 = Path(tmp2) / "galao_test.db"
            init_db(db_path2)

            # Happy path: both legs fill.
            fake_ibc = _FakeIBC()
            group_id = open_spread_position(
                fake_ibc, db_path2, "MES", "MNQ", qty_a=2, qty_b=1,
                action_a="BUY", action_b="SELL", source="spread", entry_diff=150.0
            )
            assert group_id is not None
            assert len(fake_ibc.paper.placed) == 2
            with get_db(db_path2) as con:
                cmds = con.execute(
                    "SELECT * FROM commands WHERE spread_group_id=?", (group_id,)
                ).fetchall()
                pos_row = con.execute(
                    "SELECT * FROM spread_positions WHERE spread_group_id=?", (group_id,)
                ).fetchone()
            assert len(cmds) == 2
            assert all(c["status"] == "FILLED" for c in cmds)
            assert all(c["tp_price"] == 0 and c["sl_price"] == 0 for c in cmds), \
                "spread legs must carry the inert sentinel, never a real TP/SL"
            assert pos_row["status"] == "OPEN"
            assert pos_row["entry_diff"] == 150.0

            # Unhappy path: leg B (MNQ) fails -- leg A (MES) must get flattened
            # immediately, and NOTHING should be written to the DB for this attempt.
            fake_ibc_fail = _FakeIBC(fail_on={"MNQ"})
            with get_db(db_path2) as con:
                cmds_before = con.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            group_id_fail = open_spread_position(
                fake_ibc_fail, db_path2, "MES", "MNQ", qty_a=2, qty_b=1,
                action_a="BUY", action_b="SELL", source="spread", entry_diff=150.0
            )
            assert group_id_fail is None, "A failed leg B must not produce a spread_group_id"
            # 2 placeOrder calls: the original MES BUY, then the flattening MES SELL.
            assert len(fake_ibc_fail.paper.placed) == 2
            flatten_call = fake_ibc_fail.paper.placed[1]
            assert flatten_call[1].action == "SELL" and flatten_call[0].symbol == "MES", \
                "Must flatten leg A (MES) with the OPPOSITE action after leg B fails"
            with get_db(db_path2) as con:
                cmds_after = con.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            assert cmds_after == cmds_before, "A failed pair must not leave partial commands rows"

            # ── Portfolio kill-switch ──
            with tempfile.TemporaryDirectory() as tmp3:
                db_path3 = Path(tmp3) / "galao_test3.db"
                init_db(db_path3)
                bars_path3 = Path(tmp3) / "bars.db"
                con = sqlite3.connect(bars_path3)
                con.execute("CREATE TABLE bars_15m (symbol TEXT, ts TEXT, "
                            "open REAL, high REAL, low REAL, close REAL, volume REAL)")
                # Current diff far below entry -- a closing_down (action_a=SELL)
                # position loses money as diff falls further past entry, i.e. this
                # constructs a big unrealized loss on purpose.
                con.execute("INSERT INTO bars_15m VALUES ('MES', '2026-06-01T14:00:00Z', 5500,5500,5500,5500,100)")
                con.execute("INSERT INTO bars_15m VALUES ('MNQ', '2026-06-01T14:00:00Z', 20000,20000,20000,20000,100)")
                con.commit()
                con.close()

                fake_ibc2 = _FakeIBC()
                with get_db(db_path3) as con:
                    con.execute("""
                        INSERT INTO spread_positions
                            (spread_group_id, sym_a, sym_b, qty_a, qty_b, action_a, action_b,
                             source, entry_diff, extreme_diff, status, opened_at, updated_at)
                        VALUES ('g1','MES','MNQ',1,1,'SELL','BUY','spread',
                                -1000.0, -1000.0, 'OPEN', ?, ?)
                    """, (_now(), _now()))
                    con.execute("""
                        INSERT INTO commands
                            (symbol, line_price, line_type, line_strength, direction,
                             entry_type, entry_price, tp_price, sl_price, bracket_size,
                             source, quantity, logical_trade_id, status, spread_group_id)
                        VALUES ('MES',0,'SUPPORT',1,'SELL','MKT',0,0,0,0,'spread',1,'ltx','FILLED','g1')
                    """)

                # current diff (from the bars fixture above): MES*5.0 - MNQ*2.0 =
                # 5500*5 - 20000*2 = 27500-40000 = -12500. entry_diff=-1000, closing_down
                # (action_a='SELL') -> pnl = entry - current = -1000 - (-12500) = +11500
                # (huge PROFIT, not loss, with these numbers) -- flip entry_diff to make
                # this a realistic large LOSS instead: entry should be far ABOVE current
                # for a closing_down position to be losing.
                with get_db(db_path3) as con:
                    con.execute("UPDATE spread_positions SET entry_diff=-20000.0, extreme_diff=-20000.0 WHERE spread_group_id='g1'")

                cfg_stub = type("Cfg", (), {"spread": type("S", (), {"kill_switch_usd": 500})()})()
                # check_portfolio_kill_switch derives bars_db_path from db_path.parent --
                # db_path3 and bars_path3 are already siblings in the same tmp3 dir.
                n = check_portfolio_kill_switch(fake_ibc2, db_path3, cfg_stub)
                assert n == 1, f"Expected the kill switch to flatten 1 group, got {n}"
                with get_db(db_path3) as con:
                    row = con.execute(
                        "SELECT status, close_reason FROM spread_positions WHERE spread_group_id='g1'"
                    ).fetchone()
                assert row["status"] == "CLOSED" and row["close_reason"] == "KILL_SWITCH"

                # Re-running must be a no-op -- the position is already CLOSED.
                n2 = check_portfolio_kill_switch(fake_ibc2, db_path3, cfg_stub)
                assert n2 == 0

            # ── Per-leg stop-loss (2026-09-14 addition) ──
            # Tracks (orderId -> status) directly on _FakePaper, so _leg_stops_hit()'s
            # trades() scan can find them (orders in .placed don't carry the assigned id).
            class _FakePaperWithTrades(_FakePaper):
                def __init__(self, fail_on=None):
                    super().__init__(fail_on)
                    self.order_ids = {}   # orderId -> (contract, order)
                    self.filled_ids = set()
                    self.cancelled = []
                def placeOrder(self, contract, order):
                    trade = super().placeOrder(contract, order)
                    self.order_ids[trade.order.orderId] = (contract, order)
                    return trade
                def cancelOrder(self, order):
                    self.cancelled.append(order.orderId)
                def trades(self):
                    class _T:
                        def __init__(self, order_id, filled):
                            self.order = _FakeOrder(order_id, "", 0)
                            self.orderStatus = type("OS", (), {"status": "Filled" if filled else "Submitted"})()
                    return [_T(oid, oid in self.filled_ids) for oid in self.order_ids]

            class _FakeIBCFull:
                def __init__(self, prices):
                    self.paper = _FakePaperWithTrades()
                    self._prices = prices
                def get_contract(self, symbol): return _FakeContract(symbol)
                def get_price(self, symbol, contract=None): return self._prices[symbol]

            with tempfile.TemporaryDirectory() as tmp4:
                db_path4 = Path(tmp4) / "galao_test4.db"
                init_db(db_path4)
                bars_path4 = Path(tmp4) / "bars.db"
                con = sqlite3.connect(bars_path4)
                con.execute("CREATE TABLE bars_15m (symbol TEXT, ts TEXT, "
                            "open REAL, high REAL, low REAL, close REAL, volume REAL)")
                con.execute("INSERT INTO bars_15m VALUES ('MES', '2026-06-01T14:00:00Z', 5500,5500,5500,5500,100)")
                con.execute("INSERT INTO bars_15m VALUES ('MNQ', '2026-06-01T14:00:00Z', 20000,20000,20000,20000,100)")
                con.commit()
                con.close()

                fake_ibc4 = _FakeIBCFull({"MES": 5500.0, "MNQ": 20000.0})
                group_id4 = open_spread_position(
                    fake_ibc4, db_path4, "MES", "MNQ", qty_a=2, qty_b=1,
                    action_a="BUY", action_b="SELL", source="spread", entry_diff=150.0,
                    bracket_size=8.0
                )
                assert group_id4 is not None
                # 2 entry orders + 2 stop orders.
                assert len(fake_ibc4.paper.placed) == 4
                with get_db(db_path4) as con:
                    pos4 = dict(con.execute(
                        "SELECT * FROM spread_positions WHERE spread_group_id=?", (group_id4,)
                    ).fetchone())
                assert pos4["bracket_size"] == 8.0
                # BUY leg (MES): stop is BELOW current price (protects a long).
                assert pos4["sl_price_a"] == 5492.0, f"MES stop: {pos4['sl_price_a']}"
                # SELL leg (MNQ): stop is ABOVE current price (protects a short).
                assert pos4["sl_price_b"] == 20008.0, f"MNQ stop: {pos4['sl_price_b']}"
                assert pos4["sl_order_id_a"] and pos4["sl_order_id_b"]

                # Simulate leg A's (MES) stop firing at IB.
                fake_ibc4.paper.filled_ids.add(pos4["sl_order_id_a"])
                n_closed = check_spread_exit(fake_ibc4, db_path4, bars_db_path=bars_path4)
                assert n_closed == 1, f"Expected 1 position closed via leg-stop, got {n_closed}"
                with get_db(db_path4) as con:
                    pos4_after = dict(con.execute(
                        "SELECT * FROM spread_positions WHERE spread_group_id=?", (group_id4,)
                    ).fetchone())
                assert pos4_after["status"] == "CLOSED"
                assert pos4_after["close_reason"] == "SL_HIT"
                # Leg B's (MNQ) stop must have been cancelled, and MNQ flattened at market.
                assert pos4["sl_order_id_b"] in fake_ibc4.paper.cancelled
                flatten_calls = [o for c, o in fake_ibc4.paper.placed[4:] if c.symbol == "MNQ"]
                assert len(flatten_calls) == 1 and flatten_calls[0].action == "BUY", \
                    "MNQ was SELL -- flattening it must BUY"

                # Re-running must be a no-op -- position already CLOSED.
                assert check_spread_exit(fake_ibc4, db_path4, bars_db_path=bars_path4) == 0

                # ── DIFF-pattern close must also cancel both legs' resting stops ──
                fake_ibc5 = _FakeIBCFull({"MES": 5500.0, "MNQ": 20000.0})
                group_id5 = open_spread_position(
                    fake_ibc5, db_path4, "MES", "MNQ", qty_a=2, qty_b=1,
                    action_a="BUY", action_b="SELL", source="spread", entry_diff=150.0,
                    bracket_size=8.0
                )
                with get_db(db_path4) as con:
                    pos5 = dict(con.execute(
                        "SELECT * FROM spread_positions WHERE spread_group_id=?", (group_id5,)
                    ).fetchone())
                _close_spread_position(fake_ibc5, db_path4, pos5, "GAP_CLOSED")
                assert pos5["sl_order_id_a"] in fake_ibc5.paper.cancelled
                assert pos5["sl_order_id_b"] in fake_ibc5.paper.cancelled

            # ── check_spread_signals: multi-bracket fan-out (2026-09-14 addition) ──
            with tempfile.TemporaryDirectory() as tmp5:
                db_path5 = Path(tmp5) / "galao_test5.db"
                init_db(db_path5)
                bars_path5 = Path(tmp5) / "bars.db"

                fake_ibc6 = _FakeIBCFull({s: 100.0 for s in ("MES", "MNQ", "MYM", "M2K")})
                cfg6 = type("Cfg", (), {"spread": type("S", (), {
                    "gap_multiplier": 1.75, "max_contract_ratio_denominator": 6,
                    "bracket_sizes": [4, 8],
                })()})()

                fixed_signal = {"pair": ("MES", "MNQ"), "diff": 100.0, "swing": 50.0,
                                 "avg_swing": 20.0, "direction": "A_OVER"}

                def _entry_side(bars_db_path, sym_a, sym_b, gap_multiplier=None):
                    return fixed_signal if (sym_a, sym_b) == ("MES", "MNQ") else None

                # Direct module-namespace monkeypatch (not unittest.mock.patch's dotted-path
                # target, which fetches a SEPARATE "trader.spread_manager" import distinct
                # from this file's own __main__ execution when run directly via
                # `python trader/spread_manager.py --self-test`, silently patching the wrong
                # copy -- same footgun decider.py's self-test already works around with its
                # own mod = sys.modules[__name__] pattern).
                mod = sys.modules[__name__]
                _orig_check_spread_entry, _orig_contract_ratio = check_spread_entry, contract_ratio
                mod.check_spread_entry = _entry_side
                mod.contract_ratio = lambda *a, **kw: (2, 1)
                try:
                    opened = check_spread_signals(fake_ibc6, db_path5, cfg6, bars_db_path=bars_path5)
                    # 2 bracket sizes on the one signaling pair -- each opens BOTH the
                    # literal ('spread') and control-direction ('spread_control') variant,
                    # same as before -- return value counts (pair, bracket) combos, not
                    # individual open_spread_position calls.
                    assert opened == 2, f"Expected 2 (pair,bracket) combos opened, got {opened}"
                    with get_db(db_path5) as con:
                        groups = [dict(g) for g in con.execute(
                            "SELECT bracket_size, source FROM spread_positions WHERE status='OPEN'"
                            " ORDER BY bracket_size, source"
                        ).fetchall()]
                    assert groups == [
                        {"bracket_size": 4.0, "source": "spread"},
                        {"bracket_size": 4.0, "source": "spread_control"},
                        {"bracket_size": 8.0, "source": "spread"},
                        {"bracket_size": 8.0, "source": "spread_control"},
                    ], f"Expected one spread+spread_control pair per bracket size, got {groups}"

                    # Re-running the same cycle: both brackets already have an OPEN
                    # position on this pair -- must be a clean no-op (the new per-bracket
                    # already-open guard), not a duplicate set of groups.
                    opened_again = check_spread_signals(fake_ibc6, db_path5, cfg6, bars_db_path=bars_path5)
                    assert opened_again == 0, f"Expected 0 (already open on every bracket), got {opened_again}"
                    with get_db(db_path5) as con:
                        n_open_total = con.execute(
                            "SELECT COUNT(*) FROM spread_positions WHERE status='OPEN'"
                        ).fetchone()[0]
                    assert n_open_total == 4, f"Must not duplicate groups on a repeat signal, got {n_open_total}"

                    # A THIRD bracket size, not yet open on this pair, must still open
                    # fresh -- proves each bracket size has its own independent
                    # open/closed lifecycle, not gated by "pair already has something open."
                    cfg6b = type("Cfg", (), {"spread": type("S", (), {
                        "gap_multiplier": 1.75, "max_contract_ratio_denominator": 6,
                        "bracket_sizes": [4, 8, 16],
                    })()})()
                    opened_third = check_spread_signals(fake_ibc6, db_path5, cfg6b, bars_db_path=bars_path5)
                    assert opened_third == 1, f"Expected 1 new (pair,16) combo opened, got {opened_third}"
                finally:
                    mod.check_spread_entry = _orig_check_spread_entry
                    mod.contract_ratio = _orig_contract_ratio

        print("[self-test] spread_manager: PASS")
        return True

    except Exception as e:
        print(f"[self-test] spread_manager: FAIL — {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("spread_manager — run --self-test to verify logic")
