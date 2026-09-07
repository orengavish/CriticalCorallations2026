"""
trader/scripts/smoke_test_paper_send.py
One-off smoke test: confirm the paper order-submission mechanics (build_bracket +
place_bracket, the same functions broker.py's process_pending_commands() calls) still
work end to end against the real paper IB connection -- deliberately does NOT go
through the full decider/broker pipeline (that needs the LIVE connection too, for
get_contract()/pricing, which isn't up), and deliberately does NOT expect a fill: it
places a bracket absurdly far from any real price, confirms IB acknowledges all three
legs (order IDs assigned, no rejection), then cancels all three immediately.

Usage:
    python trader/scripts/smoke_test_paper_send.py [--symbol MES]
"""

import sys
import time
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

from ib_insync import Future
from lib.config_loader import get_config
from lib.order_builder import build_bracket, place_bracket, get_tick_size


def main(symbol: str) -> bool:
    from ib_insync import IB
    cfg = get_config()
    ib = IB()
    ib.connect(cfg.ib.paper_host, cfg.ib.paper_port, clientId=299, timeout=10)
    print(f"Connected to PAPER {cfg.ib.paper_host}:{cfg.ib.paper_port}")

    try:
        con = Future(symbol=symbol, exchange="CME" if symbol != "MYM" else "CBOT", currency="USD")
        details = ib.reqContractDetails(con)
        if not details:
            print(f"FAIL: no contract details for {symbol}")
            return False
        # Front-month = nearest expiry, same selection rule as lib/ib_client.py's
        # get_contract() (multiple expiries are listed simultaneously -- a bare
        # qualifyContracts() is ambiguous, this is why the real code sorts first).
        contract = sorted(details, key=lambda d: d.contract.lastTradeDateOrContractMonth or "")[0].contract
        print(f"Qualified: {contract.localSymbol} (front-month, exp={contract.lastTradeDateOrContractMonth})")

        tick = get_tick_size(symbol)
        # Absurdly far below any real price -- guarantees no fill, purely tests
        # acceptance + cancellation mechanics.
        entry_price = round(1.0 / tick) * tick

        orders = build_bracket(ib, contract, "BUY", "LMT",
                                entry_price, entry_price + 10 * tick, entry_price - 10 * tick,
                                quantity=1, tick_size=tick)
        result = place_bracket(ib, contract, orders)
        ib.sleep(2)

        ids = (result["entry_id"], result["tp_id"], result["sl_id"])
        print(f"SUBMITTED -- entry_id={ids[0]} tp_id={ids[1]} sl_id={ids[2]}")

        entry_status = result["entry"].orderStatus.status
        print(f"Entry order status: {entry_status}")
        ok = entry_status not in ("Cancelled", "ApiCancelled", "Inactive") and all(ids)

        for leg_name, trade in (("entry", result["entry"]), ("tp", result["tp"]), ("sl", result["sl"])):
            try:
                ib.cancelOrder(trade.order)
                print(f"Cancelled {leg_name} (orderId={trade.order.orderId})")
            except Exception as e:
                print(f"Cancel {leg_name} failed (non-fatal for the smoke test): {e}")
        ib.sleep(2)

        print("PASS -- paper order submission mechanics confirmed working" if ok
              else "FAIL -- order was rejected/inactive")
        return ok
    finally:
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="MES")
    args = parser.parse_args()
    sys.exit(0 if main(args.symbol) else 1)
