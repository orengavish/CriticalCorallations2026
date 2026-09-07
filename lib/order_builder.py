"""
lib/order_builder.py
Builds IB bracket orders for Galao.
Implements the toggle rule: entry type depends on current price vs line price.

Toggle rule (R-ORD-02 to R-ORD-04):
  Price ABOVE line → LMT BUY  + STP SELL
  Price BELOW line → STP BUY  + LMT SELL

Bracket is symmetric: TP distance == SL distance == bracket_size (R-ORD-08).
All prices must already be tick-rounded before calling these functions (R-ORD-06).

Usage:
    from lib.order_builder import build_bracket, determine_entry_type
    entry_type = determine_entry_type("BUY", current_price, line_price)
    orders = build_bracket(ib_paper, contract, "BUY", entry_type, entry_price, bracket_size)

Self-test:
    python -m lib.order_builder --self-test
"""

import sys
import argparse

from ib_insync import IB, LimitOrder, StopOrder, MarketOrder

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("order_builder")

# Canonical per-symbol tick sizes. Single source of truth for anything that rounds a
# price for order placement -- decider.py's calc_bracket_prices() and broker.py's fill
# reconciliation must use the SAME table, or the same command can be computed with two
# different tick sizes depending which module touched it last. Added 2026-09-06: found
# decider.py was rounding EVERY symbol at the flat cfg.orders.tick_size (0.25, MES's
# tick) while broker.py already had its own private per-symbol dict -- harmless while
# symbols:[MES] only, but MYM (tick=1.0) and M2K (tick=0.10) would get silently
# mis-rounded entry/TP/SL prices the moment either was added to the live symbols list.
TICK_BY_SYMBOL = {"MES": 0.25, "MNQ": 0.25, "MYM": 1.0, "M2K": 0.10}


def get_tick_size(symbol: str, default: float = 0.25) -> float:
    return TICK_BY_SYMBOL.get(symbol, default)


def round_tick(price: float, tick_size: float = 0.25) -> float:
    """Round price to nearest tick. MES tick = 0.25."""
    return round(round(price / tick_size) * tick_size, 10)


def determine_entry_type(direction: str, current_price: float,
                         line_price: float) -> str:
    """
    Apply toggle rule to determine entry order type.

    direction     : "BUY" or "SELL"
    current_price : last market price
    line_price    : critical line price

    Returns "LMT" or "STP"

    Toggle rule table:
    ┌──────────────┬─────────────────────────┬────────────┐
    │ Price vs Line│ Direction               │ Entry type │
    ├──────────────┼─────────────────────────┼────────────┤
    │ ABOVE line   │ BUY                     │ LMT        │
    │ ABOVE line   │ SELL                    │ STP        │
    │ BELOW line   │ BUY                     │ STP        │
    │ BELOW line   │ SELL                    │ LMT        │
    └──────────────┴─────────────────────────┴────────────┘
    """
    price_above_line = current_price >= line_price
    if direction == "BUY":
        return "LMT" if price_above_line else "STP"
    else:  # SELL
        return "STP" if price_above_line else "LMT"


def calc_bracket_prices(direction: str, entry_type: str,
                        line_price: float, bracket_size: float,
                        tick_size: float = 0.25) -> dict:
    """
    Calculate entry, TP, and SL prices for a bracket order.

    For LMT BUY  : entry = line_price,       TP = line + bracket, SL = line - bracket
    For STP BUY  : entry = line_price + tick, TP = entry + bracket, SL = entry - bracket
    For LMT SELL : entry = line_price,        TP = line - bracket, SL = line + bracket
    For STP SELL : entry = line_price - tick, TP = entry - bracket, SL = entry + bracket

    Returns dict with entry_price, tp_price, sl_price.
    """
    tick = tick_size

    if direction == "BUY" and entry_type == "LMT":
        entry = round_tick(line_price, tick)
        tp    = round_tick(entry + bracket_size, tick)
        sl    = round_tick(entry - bracket_size, tick)

    elif direction == "BUY" and entry_type == "STP":
        entry = round_tick(line_price + tick, tick)
        tp    = round_tick(entry + bracket_size, tick)
        sl    = round_tick(entry - bracket_size, tick)

    elif direction == "SELL" and entry_type == "LMT":
        entry = round_tick(line_price, tick)
        tp    = round_tick(entry - bracket_size, tick)
        sl    = round_tick(entry + bracket_size, tick)

    elif direction == "SELL" and entry_type == "STP":
        entry = round_tick(line_price - tick, tick)
        tp    = round_tick(entry - bracket_size, tick)
        sl    = round_tick(entry + bracket_size, tick)

    else:
        raise ValueError(f"Unknown direction/entry_type combo: {direction}/{entry_type}")

    return {"entry_price": entry, "tp_price": tp, "sl_price": sl}


def build_bracket(ib: IB, contract, direction: str, entry_type: str,
                  entry_price: float, tp_price: float, sl_price: float,
                  quantity: int = 1, tick_size: float = 0.25) -> dict:
    """
    Build and return IB bracket orders (entry + TP + SL) without placing them.
    Uses ib.bracketOrder() which automatically links TP and SL as children.

    Returns dict: {"entry": Order, "tp": Order, "sl": Order}
    """
    action  = direction  # "BUY" or "SELL"
    tp_action = "SELL" if direction == "BUY" else "BUY"

    if entry_type == "LMT":
        bracket = ib.bracketOrder(
            action       = action,
            quantity     = quantity,
            limitPrice   = entry_price,
            takeProfitPrice = tp_price,
            stopLossPrice   = sl_price,
        )
        for o in bracket:
            o.tif = 'GTC'
        return {"entry": bracket[0], "tp": bracket[1], "sl": bracket[2]}

    # bracketOrder only supports LMT entry; build manually for STP and MKT
    if entry_type == "STP":
        entry_order = StopOrder(action, quantity, entry_price)
    elif entry_type == "MKT":
        entry_order = MarketOrder(action, quantity)
    else:
        raise ValueError(f"Unknown entry_type: {entry_type}")

    tp_order = LimitOrder(tp_action, quantity, tp_price)
    sl_order = StopOrder(tp_action, quantity, sl_price)

    # Link orders: TP and SL are children of entry (parentId set in place_bracket)
    tp_order.transmit = False
    sl_order.transmit = True   # transmit=True on last child submits the group
    entry_order.transmit = False

    for o in (entry_order, tp_order, sl_order):
        o.tif = 'GTC'

    return {"entry": entry_order, "tp": tp_order, "sl": sl_order}


def place_bracket(ib: IB, contract, orders: dict) -> dict:
    """
    Place a bracket order group.
    Handles both bracketOrder (LMT entry) and manual STP entry grouping.

    Returns dict: {"entry": Trade, "tp": Trade, "sl": Trade,
                   "entry_id": int, "tp_id": int, "sl_id": int}
    """
    entry_order = orders["entry"]
    tp_order    = orders["tp"]
    sl_order    = orders["sl"]

    # For STP/MKT entry: assign real parent IDs after placeOrder gives us an orderId
    is_stp_entry = entry_order.orderType in ('STP', 'MKT')

    if is_stp_entry:
        # Place entry first to get the real orderId
        entry_trade = ib.placeOrder(contract, entry_order)
        ib.sleep(0.1)
        real_id = entry_trade.order.orderId
        tp_order.parentId = real_id
        sl_order.parentId = real_id
        tp_trade = ib.placeOrder(contract, tp_order)
        sl_trade = ib.placeOrder(contract, sl_order)
    else:
        # bracketOrder: place all three together
        entry_trade = ib.placeOrder(contract, entry_order)
        tp_trade    = ib.placeOrder(contract, tp_order)
        sl_trade    = ib.placeOrder(contract, sl_order)

    entry_px = (entry_order.auxPrice if entry_order.orderType == 'STP'
                else getattr(entry_order, 'lmtPrice', None))
    log.info(
        f"Bracket placed: {entry_order.action} {entry_order.orderType} "
        f"entry={entry_px} "
        f"tp={tp_order.lmtPrice} sl={getattr(sl_order,'auxPrice',sl_order.lmtPrice if hasattr(sl_order,'lmtPrice') else None)} "
        f"entry_id={entry_trade.order.orderId}"
    )

    return {
        "entry": entry_trade, "tp": tp_trade, "sl": sl_trade,
        "entry_id": entry_trade.order.orderId,
        "tp_id":    tp_trade.order.orderId,
        "sl_id":    sl_trade.order.orderId,
    }


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        cfg = get_config()
        tick = cfg.orders.tick_size  # 0.25

        # 1. Toggle rule — all 4 cases
        assert determine_entry_type("BUY",  6520, 6500) == "LMT",  "BUY above line"
        assert determine_entry_type("BUY",  6480, 6500) == "STP",  "BUY below line"
        assert determine_entry_type("SELL", 6520, 6500) == "STP",  "SELL above line"
        assert determine_entry_type("SELL", 6480, 6500) == "LMT",  "SELL below line"
        # Price exactly at line → treated as ABOVE
        assert determine_entry_type("BUY",  6500, 6500) == "LMT",  "BUY at line"

        # 2. Bracket price calculations (2-point bracket)
        # LMT BUY at 6500, bracket=2: entry=6500, TP=6502, SL=6498
        p = calc_bracket_prices("BUY", "LMT", 6500.0, 2.0, tick)
        assert p["entry_price"] == 6500.0, f"LMT BUY entry: {p['entry_price']}"
        assert p["tp_price"]    == 6502.0, f"LMT BUY TP: {p['tp_price']}"
        assert p["sl_price"]    == 6498.0, f"LMT BUY SL: {p['sl_price']}"

        # STP BUY at 6500+tick=6500.25, bracket=2: TP=6502.25, SL=6498.25
        p = calc_bracket_prices("BUY", "STP", 6500.0, 2.0, tick)
        assert p["entry_price"] == 6500.25, f"STP BUY entry: {p['entry_price']}"
        assert p["tp_price"]    == 6502.25, f"STP BUY TP: {p['tp_price']}"
        assert p["sl_price"]    == 6498.25, f"STP BUY SL: {p['sl_price']}"

        # LMT SELL at 6500, bracket=2: TP=6498, SL=6502
        p = calc_bracket_prices("SELL", "LMT", 6500.0, 2.0, tick)
        assert p["entry_price"] == 6500.0, f"LMT SELL entry: {p['entry_price']}"
        assert p["tp_price"]    == 6498.0, f"LMT SELL TP: {p['tp_price']}"
        assert p["sl_price"]    == 6502.0, f"LMT SELL SL: {p['sl_price']}"

        # STP SELL at 6500-tick=6499.75, bracket=2: TP=6497.75, SL=6501.75
        p = calc_bracket_prices("SELL", "STP", 6500.0, 2.0, tick)
        assert p["entry_price"] == 6499.75, f"STP SELL entry: {p['entry_price']}"
        assert p["tp_price"]    == 6497.75, f"STP SELL TP: {p['tp_price']}"
        assert p["sl_price"]    == 6501.75, f"STP SELL SL: {p['sl_price']}"

        # 3. Tick rounding
        assert round_tick(6500.1, 0.25) == 6500.0,  f"Round down: {round_tick(6500.1, 0.25)}"
        assert round_tick(6500.15, 0.25) == 6500.25, f"Round up: {round_tick(6500.15, 0.25)}"

        # 4. build_bracket (offline — no IB needed, just check order attributes)
        fake_ib = _FakeIB()
        orders_lmt = build_bracket(fake_ib, None, "BUY", "LMT",
                                   6500.0, 6502.0, 6498.0)
        assert orders_lmt["entry"].action == "BUY"
        assert orders_lmt["entry"].orderType in ("LMT", "MKT")

        orders_stp = build_bracket(fake_ib, None, "SELL", "STP",
                                   6499.75, 6497.75, 6501.75)
        assert orders_stp["entry"].action == "SELL"

        # 5. Per-symbol tick lookup (2026-09-06 fix): MYM/M2K must NOT round at MES's
        # 0.25 tick -- that produced invalid, non-tradeable increments for either once
        # they joined the live symbols list (MYM ticks in whole points; M2K in 0.10).
        assert get_tick_size("MES") == 0.25
        assert get_tick_size("MNQ") == 0.25
        assert get_tick_size("MYM") == 1.0
        assert get_tick_size("M2K") == 0.10
        assert get_tick_size("UNKNOWN_SYMBOL") == 0.25  # falls back to the default
        p_mym = calc_bracket_prices("BUY", "LMT", 53773.0, 8.0, get_tick_size("MYM"))
        assert p_mym["entry_price"] == 53773.0 and p_mym["tp_price"] == 53781.0, \
            "MYM at its real 1.0 tick must not get MES's 0.25 rounding"
        p_m2k = calc_bracket_prices("BUY", "STP", 2979.43, 4.0, get_tick_size("M2K"))
        assert abs(p_m2k["entry_price"] - 2979.5) < 1e-9, \
            "M2K at its real 0.10 tick must round 2979.43+0.10=2979.53 to 2979.5, not a 0.25 multiple"

        print("[self-test] order_builder: PASS")
        return True

    except Exception as e:
        print(f"[self-test] order_builder: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


class _FakeIB:
    """Minimal IB stub for offline tests."""
    _next_id = 1

    def bracketOrder(self, action, quantity, limitPrice,
                     takeProfitPrice, stopLossPrice):
        from ib_insync import LimitOrder, StopOrder
        e = LimitOrder(action, quantity, limitPrice)
        tp_action = "SELL" if action == "BUY" else "BUY"
        tp = LimitOrder(tp_action, quantity, takeProfitPrice)
        sl = StopOrder(tp_action, quantity, stopLossPrice)
        return [e, tp, sl]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("order_builder — run --self-test to verify logic")
