"""
trader/scripts/ib_dayclean.py
Runs the Day Start panel's IB-touching actions (verify / cancel-flatten) in their own
process. ib_insync needs a real asyncio event loop in the calling thread; the in-thread
guard in lib/ib_client.py's connect() (2026-09-10) doesn't reliably hold across Flask's
threaded request handler in practice (confirmed live 2026-09-11: /api/dayclean/verify and
/api/dayclean/cancel-flatten both intermittently threw "no current event loop in thread
'Thread-N (process_request_thread)'"). Same fix already used for
import_geva_manual_lines.py's dashboard route -- a subprocess gets its own clean main
thread and sidesteps the problem entirely instead of chasing the race.

Usage:
    python trader/scripts/ib_dayclean.py verify
    python trader/scripts/ib_dayclean.py cancel-flatten
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.ib_client import IBClient
from lib.config_loader import get_config


def verify() -> dict:
    result = {"connected": False, "resting_orders": None, "open_positions": None, "error": None}
    try:
        cfg = get_config(_ROOT / "trader" / "config.yaml")
        ibc = IBClient(cfg)
        ibc.connect(live=False, paper=True)
        try:
            ibc.paper.reqAllOpenOrders()
            ibc.paper.sleep(1.0)
            ibc.paper.reqPositions()
            ibc.paper.sleep(1.0)
            result["connected"] = True
            result["resting_orders"] = len(ibc.paper.openOrders())
            result["open_positions"] = len([p for p in ibc.paper.positions() if p.position != 0])
        finally:
            ibc.disconnect()
    except Exception as e:
        result["error"] = str(e)
    return result


def cancel_flatten() -> dict:
    from ib_insync import MarketOrder

    result = {"ib_cancel": "skipped", "ib_flatten": 0, "errors": []}
    ibc = None
    try:
        cfg = get_config(_ROOT / "trader" / "config.yaml")
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=True)

        for label, ib_conn in [("LIVE", ibc.live), ("PAPER", ibc.paper)]:
            if ib_conn and ib_conn.isConnected():
                try:
                    ib_conn.reqGlobalCancel()
                except Exception as e:
                    result["errors"].append(f"reqGlobalCancel {label}: {e}")
        result["ib_cancel"] = "ok"

        if ibc.paper and ibc.paper.isConnected():
            try:
                ibc.paper.reqPositions()
                ibc.paper.sleep(1.5)
                for pos in ibc.paper.positions():
                    qty = pos.position
                    if qty == 0:
                        continue
                    action = "SELL" if qty > 0 else "BUY"
                    try:
                        ibc.place_order(pos.contract, MarketOrder(action, abs(qty)))
                        result["ib_flatten"] += 1
                    except Exception as e:
                        result["errors"].append(f"close {pos.contract.symbol}: {e}")
            except Exception as e:
                result["errors"].append(f"positions: {e}")
    except Exception as e:
        result["errors"].append(f"IB connect: {e}")
        result["ib_cancel"] = "failed"
    finally:
        if ibc:
            try:
                ibc.disconnect()
            except Exception:
                pass
    return result


def prices(symbols: list) -> dict:
    """
    LIVE-only price snapshot for the given symbols, as its own process for the same
    reason verify()/cancel_flatten() are -- ib_insync's event-loop requirement doesn't
    reliably hold up called directly from a Flask request thread. Replaces
    trading_dashboard.py's _fetch_live_prices(), which did this in-thread and silently
    swallowed the resulting per-symbol failure as "price unavailable" (confirmed live
    2026-09-11).
    """
    result = {s: None for s in symbols}
    try:
        cfg = get_config(_ROOT / "trader" / "config.yaml")
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=False)
        try:
            for s in symbols:
                try:
                    result[s] = ibc.get_price(s)
                except Exception:
                    result[s] = None
        finally:
            ibc.disconnect()
    except Exception:
        pass
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["verify", "cancel-flatten", "prices"])
    parser.add_argument("--symbols", help="comma-separated symbol list, for mode=prices")
    args = parser.parse_args()
    if args.mode == "verify":
        print(verify())
    elif args.mode == "cancel-flatten":
        print(cancel_flatten())
    else:
        print(prices((args.symbols or "").split(",")))
