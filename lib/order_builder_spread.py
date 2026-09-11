"""
lib/order_builder_spread.py
Builds and submits a single SPREAD LEG -- a plain entry order with NO TP and
NO SL children at all. Deliberately a separate, new file from
lib/order_builder.py's build_bracket()/place_bracket(): those are battle-
tested, incident-hardened, and every existing algorithm in this system relies
on their always-3-orders-per-command shape (ib_insync's bracketOrder() helper
itself hard-requires both a TP and SL price; the manual STP path unconditionally
builds an SL child too). Modifying them to make SL optional would touch code
this repo's own integration notes call KEEP-AS-IS -- a new, simpler function
is the smaller, safer change.

Per Claims/spread-diff-trading.md AI-35: a spread position is 1 LONG + 1 SHORT
leg, hedged, with NO stop-loss placed in the system at all -- the hedge itself
bounds risk. Exit is driven entirely by the strategy's own DIFF "1-2-3"
pattern (trader/spread_manager.py), never a resting order.

MARKET orders, not LMT: AI-35c already accepts some entry-price slippage
("accepting the gap may keep opening past it"), and — more importantly — both
legs need to fill together. A resting LMT leg that never fills would leave
the OTHER leg's fill sitting alone with no hedge and no stop (broker.py's
reconcile_naked_positions() is explicitly told to leave source='spread'
positions unprotected, see its own docstring) -- an unacceptable risk this
system doesn't need to carry when MKT execution on liquid index futures
avoids it almost entirely. trader/spread_manager.py still checks both legs
actually filled and flattens a lone fill if the other leg's submission fails.

Usage:
    from lib.order_builder_spread import build_spread_leg, place_spread_leg
    order = build_spread_leg("BUY", quantity=2)
    result = place_spread_leg(ib_paper, contract, order)   # {"entry_id": ...}

Self-test:
    python -m lib.order_builder_spread --self-test
"""

import sys
import argparse

from ib_insync import MarketOrder


def build_spread_leg(direction: str, quantity: int = 1):
    """One leg of a spread pair: a plain MarketOrder, no TP/SL children."""
    return MarketOrder(direction, quantity)


def place_spread_leg(ib_paper, contract, order) -> dict:
    """Submit one spread leg. Returns {"entry_id", "trade"} -- the caller
    (spread_manager.py) needs the raw `trade` object to check fill status
    right after submission, since MKT fills are expected near-instantly."""
    trade = ib_paper.placeOrder(contract, order)
    return {"entry_id": trade.order.orderId, "trade": trade}


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        order = build_spread_leg("BUY", quantity=2)
        assert order.action == "BUY"
        assert order.totalQuantity == 2
        assert order.orderType == "MKT", \
            f"Spread legs must be MKT (both-legs-fill certainty), got {order.orderType}"

        order_sell = build_spread_leg("SELL", quantity=3)
        assert order_sell.action == "SELL"
        assert order_sell.totalQuantity == 3

        class _FakeOrder:
            def __init__(self, order_id): self.orderId = order_id

        class _FakeTrade:
            def __init__(self, order_id): self.order = _FakeOrder(order_id)

        class _FakePaper:
            def __init__(self): self.placed = []
            def placeOrder(self, contract, order):
                self.placed.append((contract, order))
                return _FakeTrade(order_id=555)

        paper = _FakePaper()
        result = place_spread_leg(paper, contract="MESU6", order=order)
        assert result["entry_id"] == 555
        assert result["trade"].order.orderId == 555
        assert len(paper.placed) == 1

        print("[self-test] order_builder_spread: PASS")
        return True

    except Exception as e:
        print(f"[self-test] order_builder_spread: FAIL — {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("order_builder_spread — run --self-test to verify logic")
