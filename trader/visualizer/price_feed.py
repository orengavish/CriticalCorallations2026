"""
visualizer/price_feed.py
Background thread that polls IB LIVE for current prices for all tracked symbols.
Stores latest prices in memory (per symbol) for the Flask routes to read.
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_prices: dict = {}
_price_times: dict = {}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_ibc = None

_ALL_SYMBOLS = ["MES", "MNQ", "MYM", "M2K"]


def get_latest(symbol: str = "MES") -> tuple:
    with _lock:
        return _prices.get(symbol), _price_times.get(symbol)


def _poll_loop(cfg, symbols: list, interval: int):
    global _ibc
    import sys
    import asyncio
    _HERE = Path(__file__).parent.parent.parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    # ib_insync (via eventkit) calls asyncio.get_event_loop() at import time;
    # background threads have no event loop by default in Python 3.10+.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from lib.ib_client import IBClient
    from lib.logger import get_logger
    log = get_logger("price_feed")

    ibc = IBClient(cfg)
    _ibc = ibc

    while True:
        try:
            if not ibc.is_live_connected():
                ibc.connect(live=True, paper=False)
            for sym in symbols:
                try:
                    price = ibc.get_price(sym)
                    if price and price == price:  # not nan
                        with _lock:
                            _prices[sym] = price
                            _price_times[sym] = datetime.now(timezone.utc)
                except Exception as e:
                    log.debug(f"Price poll error ({sym}): {e}")
        except Exception as e:
            log.debug(f"Price feed connection error: {e}")
        # ibc.live.sleep() instead of time.sleep(): services ib_insync's event loop
        # during this idle wait, which is what keeps get_price()'s persistent ticker
        # subscriptions (2026-09-11) actually updating in the background.
        if ibc.live and ibc.live.isConnected():
            ibc.live.sleep(interval)
        else:
            time.sleep(interval)


def start(cfg, symbols=None, interval: int = 5):
    global _thread
    if symbols is None:
        symbols = _ALL_SYMBOLS
    if isinstance(symbols, str):
        symbols = [symbols]
    _thread = threading.Thread(
        target=_poll_loop, args=(cfg, symbols, interval), daemon=True
    )
    _thread.start()


def stop():
    global _ibc
    if _ibc:
        try:
            _ibc.disconnect()
        except Exception:
            pass
        _ibc = None
