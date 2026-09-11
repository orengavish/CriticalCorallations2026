"""
trader/correlation_trail.py
Trailing-stop management for the correlation algorithm (AI-31: "immediate SL
beyond entry once moving; very tight trailing stop... hold while [momentum]
runs your way, exit when it flips"). No trailing-stop code existed anywhere in
this repo before this (confirmed by a full grep) -- every other algorithm uses
a fixed, one-shot bracket, submitted once and left alone until it fills
(trader/position_manager.py's own docstring: "No stagnation kill-switch -- let
the bracket ride"). The correlation algorithm's SL genuinely needs to move.

Mechanics mirror trader/broker.py's _drain_rebase_queue() (same IB call
pattern: fetch ibc.paper.trades(), find the SL child order by
ib_sl_order_id, modify its price, ibc.paper.modifyOrder()) -- that function
rebases a bracket ONCE, right after fill, to the actual fill price; this one
runs every poll cycle for as long as a source='correlation' position stays
open, moving the SL only in the profitable direction, never back (the pure
"how far should the SL move" decision is _compute_new_sl(), fully unit-tested
without any IB dependency; the IB-interaction wrapper below is structurally
identical to the already-proven _drain_rebase_queue pattern).

Usage (called from the broker's main loop, alongside _drain_rebase_queue):
    from trader.correlation_trail import trail_correlation_positions
    trail_correlation_positions(ibc, db_path)

Self-test:
    python trader/correlation_trail.py --self-test
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db
from lib.order_builder import get_tick_size, round_tick
from lib.logger import get_logger

log = get_logger("correlation_trail")

# AI-31: "very tight trailing stop." Distance the SL trails behind the best
# price seen since fill, in ticks -- tighter than a normal fixed bracket's
# SL distance (that's a one-shot risk budget; this is actively defending
# profit already made).
TRAIL_TICKS = 3


def _compute_new_sl(direction: str, current_sl: float, current_price: float,
                    trail_ticks: int, tick: float) -> float | None:
    """
    Pure trailing-stop decision, no IB dependency. Returns a new SL price if
    the market has moved far enough in the profitable direction to justify
    tightening the stop, or None if the SL should stay where it is.

    Never loosens the stop (AI-31: "never back") -- for a BUY, the new SL is
    only accepted if it's HIGHER than the current one; for a SELL, only if
    LOWER.
    """
    trail_dist = trail_ticks * tick
    if direction == "BUY":
        candidate = round_tick(current_price - trail_dist, tick)
        return candidate if candidate > current_sl else None
    else:
        candidate = round_tick(current_price + trail_dist, tick)
        return candidate if candidate < current_sl else None


def trail_correlation_positions(ibc, db_path, trail_ticks: int = TRAIL_TICKS) -> int:
    """
    For each FILLED source='correlation' command, check the live price and
    tighten its SL if the market has moved far enough in its favor. Returns
    the number of SL orders actually modified.
    """
    with get_db(db_path) as con:
        positions = [dict(r) for r in con.execute(
            "SELECT * FROM commands WHERE source='correlation' AND status='FILLED'"
        ).fetchall()]

    if not positions:
        return 0

    if not ibc.is_paper_connected():
        return 0

    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.warning(f"trail: could not fetch IB trades: {e}")
        return 0

    trades_by_oid = {t.order.orderId: t for t in trades}
    _DONE = ("Filled", "Cancelled", "Inactive")
    modified = 0

    for cmd in positions:
        sl_oid = cmd["ib_sl_order_id"]
        if not sl_oid or sl_oid not in trades_by_oid:
            continue
        sl_trade = trades_by_oid[sl_oid]
        if sl_trade.orderStatus.status in _DONE:
            continue

        try:
            price = ibc.get_price(cmd["symbol"])
        except Exception as e:
            log.warning(f"trail: no price for {cmd['symbol']}: {e}")
            continue
        if price is None:
            continue

        tick = get_tick_size(cmd["symbol"])
        new_sl = _compute_new_sl(cmd["direction"], cmd["sl_price"], price, trail_ticks, tick)
        if new_sl is None:
            continue

        sl_order = sl_trade.order
        old_sl = sl_order.lmtPrice if sl_order.orderType == "LMT" else sl_order.auxPrice
        if sl_order.orderType == "LMT":
            sl_order.lmtPrice = new_sl
        else:
            sl_order.auxPrice = new_sl

        try:
            ibc.paper.modifyOrder(sl_trade.contract, sl_order)
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET sl_price=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id=?", (new_sl, cmd["id"])
                )
            log.info(f"Cmd {cmd['id']} ({cmd['symbol']}): trailed SL {old_sl} -> {new_sl} "
                     f"(price={price})")
            modified += 1
        except Exception as e:
            log.warning(f"Cmd {cmd['id']}: trail modify failed: {e}")

    return modified


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        # BUY: SL only moves UP, and only once price has moved far enough that
        # (price - TRAIL_TICKS*tick) actually exceeds the current SL.
        tick = 0.25
        # Price barely above entry -- trailed SL would be BELOW the current one.
        assert _compute_new_sl("BUY", current_sl=5495.0, current_price=5495.5,
                               trail_ticks=3, tick=tick) is None
        # Price has run up enough that trailing 3 ticks behind it is still
        # higher than the current SL -- must tighten.
        new_sl = _compute_new_sl("BUY", current_sl=5495.0, current_price=5500.0,
                                 trail_ticks=3, tick=tick)
        assert new_sl == 5499.25, f"Expected 5499.25, got {new_sl}"

        # Never loosen: price pulled back since the last trail -- must not move
        # the SL back down even though a naive "SL = price - trail" would.
        assert _compute_new_sl("BUY", current_sl=5499.25, current_price=5499.5,
                               trail_ticks=3, tick=tick) is None

        # SELL: mirror image -- SL only moves DOWN.
        assert _compute_new_sl("SELL", current_sl=5505.0, current_price=5504.5,
                               trail_ticks=3, tick=tick) is None
        new_sl_sell = _compute_new_sl("SELL", current_sl=5505.0, current_price=5500.0,
                                      trail_ticks=3, tick=tick)
        assert new_sl_sell == 5500.75, f"Expected 5500.75, got {new_sl_sell}"
        assert _compute_new_sl("SELL", current_sl=5500.75, current_price=5500.5,
                               trail_ticks=3, tick=tick) is None

        # Different tick size (MYM=1.0) scales the trail distance correctly.
        new_sl_mym = _compute_new_sl("BUY", current_sl=42000.0, current_price=42010.0,
                                     trail_ticks=3, tick=1.0)
        assert new_sl_mym == 42007.0, f"Expected 42007.0, got {new_sl_mym}"

        # ── IB-wrapper smoke test (fake IB, mirrors broker.py's own _FakeIB style) ──
        import tempfile
        from lib.db import init_db

        class _FakeOrder:
            def __init__(self, order_id, order_type, price):
                self.orderId = order_id
                self.orderType = order_type
                if order_type == "LMT":
                    self.lmtPrice = price
                else:
                    self.auxPrice = price

        class _FakeOrderStatus:
            status = "Submitted"

        class _FakeTrade:
            def __init__(self, order, contract):
                self.order = order
                self.contract = contract
                self.orderStatus = _FakeOrderStatus()

        class _FakePaper:
            def __init__(self, trades):
                self._trades = trades
                self.modified = []
            def trades(self):
                return self._trades
            def modifyOrder(self, contract, order):
                self.modified.append((contract, order))

        class _FakeIBC:
            def __init__(self, trades, price):
                self.paper = _FakePaper(trades)
                self._price = price
            def is_paper_connected(self):
                return True
            def get_price(self, symbol):
                return self._price

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength, direction,
                         entry_type, entry_price, tp_price, sl_price, bracket_size,
                         source, quantity, logical_trade_id, status,
                         ib_tp_order_id, ib_sl_order_id)
                    VALUES ('MES', 5490, 'SUPPORT', 1, 'BUY', 'LMT', 5490, 5498, 5486, 8,
                            'correlation', 1, 'lt1', 'FILLED', 101, 102)
                """)

            sl_order = _FakeOrder(102, "STP", 5486.0)
            fake_trades = [_FakeTrade(sl_order, contract="MESU6")]
            fake_ibc = _FakeIBC(fake_trades, price=5500.0)  # far enough to trail

            n = trail_correlation_positions(fake_ibc, db_path)
            assert n == 1, f"Expected 1 SL trailed, got {n}"
            assert len(fake_ibc.paper.modified) == 1
            assert sl_order.auxPrice == 5499.25, \
                f"Expected the fake order's auxPrice updated to 5499.25, got {sl_order.auxPrice}"
            with get_db(db_path) as con:
                row = con.execute("SELECT sl_price FROM commands WHERE id=1").fetchone()
            assert row["sl_price"] == 5499.25, "commands.sl_price must reflect the new trailed SL"

            # A second call at the SAME price must be a no-op (nothing to trail further).
            n2 = trail_correlation_positions(fake_ibc, db_path)
            assert n2 == 0, f"Expected 0 (no further movement), got {n2}"

        print("[self-test] correlation_trail: PASS")
        return True

    except Exception as e:
        print(f"[self-test] correlation_trail: FAIL — {e}")
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
    print("correlation_trail — run --self-test to verify logic")
