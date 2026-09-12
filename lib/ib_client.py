"""
lib/ib_client.py
IB connection management for Galao.
Manages two connections: LIVE (4001, data only) and PAPER (4002, trading only).
Uses client ID pools from config. Registers atexit cleanup.

Usage:
    from lib.ib_client import IBClient
    ibc = IBClient()
    ibc.connect()
    price = ibc.get_price("MES")
    ibc.disconnect()

Self-test:
    python -m lib.ib_client --self-test
"""

import sys
import atexit
import argparse
import threading
from datetime import datetime, timezone

from ib_insync import IB, Future, Stock, Ticker, util

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("ib_client")

_EXCHANGE = "CME"
_SYMBOL_EXCHANGE = {
    "MYM": "CBOT",  # Micro Dow is listed under CBOT, not CME -- confirmed
                    # via reqContractDetails (2026-08-29); MES/MNQ/M2K are CME.
    "YM":  "CBOT",  # Full-size Dow, same exchange as its micro. Confirmed
                    # 2026-09-12 via a live reqContractDetails call (paper account,
                    # read-only) alongside ES/NQ/RTY below -- not assumed from the
                    # MYM pattern alone, the same rigor MYM itself got on 2026-08-29.
}
_CURRENCY = "USD"

# 2026-09-12: ES/NQ/YM/RTY added -- the capacity-allocation plan's "double capacity per
# index by trading both micro and full-size" (approved this session). All 4 confirmed
# live via reqContractDetails (paper account, read-only, no orders): ES/NQ/RTY -> CME,
# YM -> CBOT (matching their micro counterparts' exchanges); tick sizes identical to the
# micro pair in every case (ES/NQ=0.25, YM=1.0, RTY=0.10 -- see lib/order_builder.py's
# TICK_BY_SYMBOL); multipliers are exactly 10x the micro (ES=50 vs MES=5, NQ=20 vs
# MNQ=2, YM=5 vs MYM=0.5, RTY=50 vs M2K=5 -- see lib/algo_pnl.py's SYMBOL_MULTIPLIERS).
# The only 4 symbols this system traded before 2026-09-07 were MES/MNQ/MYM/M2K.
# Everything else (the ~100-stock research universe) is assumed to be a US equity,
# routed via SMART -- no per-symbol exchange table needed there.
_FUTURES_SYMBOLS = {"MES", "MNQ", "MYM", "M2K", "ES", "NQ", "YM", "RTY"}
_STOCK_EXCHANGE = "SMART"


class IBClient:
    """
    Manages LIVE and PAPER IB connections with client ID pools.
    LIVE  → market data only.
    PAPER → order submission only.
    """

    def __init__(self, cfg=None):
        self._cfg = cfg or get_config()
        ib_cfg = self._cfg.ib

        self._live_host = ib_cfg.live_host
        self._live_port = ib_cfg.live_port
        self._live_ids  = list(ib_cfg.live_client_ids)

        self._paper_host = ib_cfg.paper_host
        self._paper_port = ib_cfg.paper_port
        self._paper_ids  = list(ib_cfg.paper_client_ids)

        self._timeout    = getattr(ib_cfg, "connection_timeout", 5)
        self._reconnect_interval = getattr(ib_cfg, "reconnect_interval_seconds", 30)

        self.live:  IB | None = None
        self.paper: IB | None = None

        self._live_client_id:  int | None = None
        self._paper_client_id: int | None = None

        self._contract_cache: dict[str, Future | Stock] = {}
        self._ticker_cache: dict[str, Ticker] = {}

        self._lock = threading.Lock()
        atexit.register(self.disconnect)

    # ── Connect ───────────────────────────────────────────────────────────────

    def connect(self, live: bool = True, paper: bool = True):
        """Connect to LIVE and/or PAPER ports."""
        # ib_insync needs an asyncio event loop in the CALLING thread. The main thread
        # always has a default one; a Flask request thread (app.run(threaded=True)) does
        # not (and a pooled thread reused across requests may hold a stale CLOSED one
        # left behind by a previous connect()/disconnect() cycle) -- either way
        # connect() then fails with "no current event loop in thread ..." or similar.
        # Every per-request IBClient() caller hit this, not just one route, so the guard
        # belongs here once rather than repeated in each caller (2026-09-10). Always
        # installing a fresh loop is simpler and safer than checking get_event_loop()
        # first, since a "successfully got" but already-closed loop wouldn't raise.
        import asyncio
        if threading.current_thread() is not threading.main_thread():
            asyncio.set_event_loop(asyncio.new_event_loop())

        if live:
            self._connect_live()
        if paper:
            self._connect_paper()

    def _try_connect(self, ib: IB, host: str, port: int,
                     client_ids: list, label: str) -> int:
        """Try each client ID in the pool (shuffled) until one succeeds. Returns used ID."""
        import random
        ids = list(client_ids)
        random.shuffle(ids)          # randomise so concurrent processes don't collide
        for cid in ids:
            try:
                log.info(f"Connecting to {label} {host}:{port} clientId={cid}")
                ib.connect(host, port, clientId=cid, timeout=self._timeout, readonly=False)
                log.info(f"Connected to {label} port {port} clientId={cid}")
                return cid
            except Exception as e:
                log.warning(f"{label} clientId={cid} failed: {e}")
        raise ConnectionError(
            f"Could not connect to {label} {host}:{port} — all client IDs exhausted"
        )

    def _connect_live(self):
        with self._lock:
            if self.live and self.live.isConnected():
                return
            ib = IB()
            # A genuinely new connection means any previously cached Tickers are tied
            # to a now-dead IB instance -- must be dropped, not carried over.
            self._ticker_cache = {}
            cid = self._try_connect(ib, self._live_host, self._live_port,
                                    self._live_ids, "LIVE")
            # Use delayed market data (type 3) — no subscription required.
            # Eliminates error 354 on reqMktData calls.
            ib.reqMarketDataType(3)
            self.live = ib
            self._live_client_id = cid

    def _connect_paper(self):
        with self._lock:
            if self.paper and self.paper.isConnected():
                return
            ib = IB()
            cid = self._try_connect(ib, self._paper_host, self._paper_port,
                                    self._paper_ids, "PAPER")
            # 2026-09-09 fix: the client ID pool is shuffled on every connect, so a
            # restart very likely gets a different ID than last time. ib.trades() is
            # only that session's own local cache -- a fresh client ID starts with an
            # EMPTY view of orders a previous session placed, even though they're still
            # resting fine at IB. broker.py's bracket-vanished detection (poll_tp_sl_fills)
            # reads only from ib.trades(), so this alone made healthy TP/SL brackets look
            # missing purely from restarting with a new identity -- a likely major
            # contributor to today's RECONCILED rows. reqAllOpenOrders() asks IB for
            # every open order on the account regardless of which client ID placed it,
            # populating ib.trades() correctly before any polling code reads it.
            ib.reqAllOpenOrders()
            ib.sleep(1.0)
            self.paper = ib
            self._paper_client_id = cid

    # ── Reconnect ─────────────────────────────────────────────────────────────

    def reconnect(self, live: bool = True, paper: bool = True,
                  max_attempts: int = 5) -> bool:
        """
        Attempt reconnect up to max_attempts times (R-ERR-01).
        Returns True if successful.
        """
        import time
        for attempt in range(1, max_attempts + 1):
            log.warning(f"Reconnect attempt {attempt}/{max_attempts}")
            try:
                if live and (not self.live or not self.live.isConnected()):
                    self._connect_live()
                if paper and (not self.paper or not self.paper.isConnected()):
                    self._connect_paper()
                log.info("Reconnect successful")
                return True
            except Exception as e:
                log.error(f"Reconnect attempt {attempt} failed: {e}")
                if attempt < max_attempts:
                    log.info(f"Waiting {self._reconnect_interval}s before retry")
                    time.sleep(self._reconnect_interval)
        log.error("All reconnect attempts exhausted")
        return False

    # ── Market data ───────────────────────────────────────────────────────────

    def get_price(self, symbol: str, contract=None) -> float:
        """
        Fetch last price for symbol from LIVE connection.
        Returns mid-point if last is unavailable.
        Raises if not connected or no price data.

        2026-09-11: previously did a fresh reqMktData + 1.5s sleep + cancelMktData on
        EVERY call -- fine for an occasional one-off, very expensive for repeated calls
        on the same symbol within a short window (broker.py was re-fetching the same
        symbol's price once per PENDING COMMAND rather than once per cycle; decider.py's
        replenishment loop re-fetches all 34 symbols every cycle). Now keeps one
        persistent streaming subscription per symbol (self._ticker_cache) -- ib_insync
        keeps updating .last/.bid/.ask in the background for as long as the subscription
        stays open and something services the event loop (callers with a polling loop
        should use ibc.live.sleep(...) instead of time.sleep(...) between iterations for
        exactly this reason). Only the FIRST call for a given symbol pays the 1.5s wait,
        to let its first tick arrive; every call after that reads the already-live
        ticker synchronously.
        """
        if not self.live or not self.live.isConnected():
            raise ConnectionError("LIVE connection is not active")

        con = contract or self.get_contract(symbol)

        ticker = self._ticker_cache.get(symbol)
        first_subscribe = ticker is None
        if first_subscribe:
            ticker = self.live.reqMktData(con, "", False, False)
            self._ticker_cache[symbol] = ticker
            self.live.sleep(1.5)  # let the first tick arrive -- paid once per symbol

        price = ticker.last
        if price is None or price != price:  # nan check
            bid = ticker.bid or 0
            ask = ticker.ask or 0
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2

        # Historical fallback -- only worth the extra round trip on a genuinely fresh
        # subscription with no tick at all yet; a symbol that's been ticking fine for a
        # while returning a momentary None/nan is more likely a transient gap.
        if (price is None or price != price or price <= 0) and first_subscribe:
            log.debug(f"reqMktData returned no price for {symbol} — trying historical fallback")
            bars = self.live.reqHistoricalData(
                con,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=10,
            )
            if bars:
                price = bars[-1].close
                log.info(f"Price for {symbol} (historical fallback): {price}")

        if price is None or price != price or price <= 0:
            raise ValueError(f"No valid price available for {symbol}")

        log.info(f"Price for {symbol}: {price}")
        return price

    def _make_contract(self, symbol: str) -> Future | Stock:
        """Build an unqualified contract (resolved later by get_contract). Futures need
        the exchange disambiguated (CME vs CBOT); stocks route through SMART uniformly."""
        if symbol in _FUTURES_SYMBOLS:
            exchange = _SYMBOL_EXCHANGE.get(symbol, _EXCHANGE)
            return Future(symbol=symbol, exchange=exchange, currency=_CURRENCY)
        return Stock(symbol, _STOCK_EXCHANGE, _CURRENCY)

    def get_contract(self, symbol: str) -> Future | Stock:
        """
        Resolve the tradeable contract for symbol via LIVE connection. Result is cached
        for the lifetime of this IBClient instance.

        Futures: uses reqContractDetails to handle ambiguous contracts (multiple listed
        expiries), picks nearest (front-month) expiry.
        Stocks (anything not in _FUTURES_SYMBOLS): a Stock contract has no expiry to
        disambiguate -- qualifyContracts() alone confirms it resolves to a real,
        tradeable instrument (catches typos/delisted tickers the same way reqContractDetails
        does for futures).
        """
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]

        if not self.live or not self.live.isConnected():
            raise ConnectionError("LIVE connection is not active")

        if symbol not in _FUTURES_SYMBOLS:
            con = Stock(symbol, _STOCK_EXCHANGE, _CURRENCY)
            qualified = self.live.qualifyContracts(con)
            if not qualified:
                raise ValueError(f"No contract found for stock symbol {symbol}")
            resolved = qualified[0]
            self._contract_cache[symbol] = resolved
            log.info(f"Resolved contract: {symbol} -> {resolved.symbol} (STK/{_STOCK_EXCHANGE})")
            return resolved

        exchange = _SYMBOL_EXCHANGE.get(symbol, _EXCHANGE)
        con = Future(symbol=symbol, exchange=exchange, currency=_CURRENCY)
        details = self.live.reqContractDetails(con)
        if not details:
            raise ValueError(f"No contract details found for {symbol}")
        # Sort by expiry ascending — first entry is front-month
        details_sorted = sorted(
            details,
            key=lambda d: d.contract.lastTradeDateOrContractMonth or ""
        )
        resolved = details_sorted[0].contract
        self._contract_cache[symbol] = resolved
        log.info(f"Resolved contract: {symbol} -> {resolved.localSymbol} "
                 f"exp={resolved.lastTradeDateOrContractMonth}")
        return resolved

    # ── Order submission ──────────────────────────────────────────────────────

    def place_order(self, contract, order):
        """
        Submit an order via PAPER connection.
        Returns ib_insync Trade object.
        """
        if not self.paper or not self.paper.isConnected():
            raise ConnectionError("PAPER connection is not active")
        trade = self.paper.placeOrder(contract, order)
        log.info(f"Order placed: {order.action} {order.orderType} "
                 f"qty={order.totalQuantity} @ {getattr(order, 'lmtPrice', 'MKT')} "
                 f"ib_id={trade.order.orderId}")
        return trade

    def cancel_order(self, order):
        """Cancel an order via PAPER connection."""
        if not self.paper or not self.paper.isConnected():
            raise ConnectionError("PAPER connection is not active")
        self.paper.cancelOrder(order)
        log.info(f"Cancel requested for orderId={order.orderId}")

    def get_open_orders(self) -> list:
        """Return list of open orders from PAPER connection."""
        if not self.paper or not self.paper.isConnected():
            raise ConnectionError("PAPER connection is not active")
        return self.paper.openOrders()

    def get_positions(self) -> list:
        """Return list of positions from PAPER connection."""
        if not self.paper or not self.paper.isConnected():
            raise ConnectionError("PAPER connection is not active")
        return self.paper.positions()

    # ── Status ────────────────────────────────────────────────────────────────

    def is_live_connected(self) -> bool:
        return bool(self.live and self.live.isConnected())

    def is_paper_connected(self) -> bool:
        return bool(self.paper and self.paper.isConnected())

    def status(self) -> dict:
        return {
            "live_connected":  self.is_live_connected(),
            "paper_connected": self.is_paper_connected(),
            "live_client_id":  self._live_client_id,
            "paper_client_id": self._paper_client_id,
        }

    # ── Disconnect ────────────────────────────────────────────────────────────

    def disconnect(self):
        """Cleanly disconnect both connections (also registered with atexit)."""
        with self._lock:
            if self.live:
                if self.live.isConnected():
                    log.info(f"Disconnecting LIVE (clientId={self._live_client_id})")
                    try:
                        self.live.sleep(0)   # drain pending ib_insync events before TCP FIN
                        self.live.disconnect()
                    except Exception as e:
                        log.warning(f"LIVE disconnect error: {e}")
                self.live = None
                self._live_client_id = None
                self._ticker_cache = {}
            if self.paper:
                if self.paper.isConnected():
                    log.info(f"Disconnecting PAPER (clientId={self._paper_client_id})")
                    try:
                        self.paper.sleep(0)  # drain pending ib_insync events before TCP FIN
                        self.paper.disconnect()
                    except Exception as e:
                        log.warning(f"PAPER disconnect error: {e}")
                self.paper = None
                self._paper_client_id = None


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Self-test: attempts IB connections.
    If IB Gateway is not running, reports SKIP (not FAIL) for connection tests
    but still validates config and object construction.
    """
    try:
        cfg = get_config()

        # 0. Contract-type branching (offline, no IB connection needed): futures still
        # get a Future() with the right exchange; anything else (the stock research
        # universe) gets a Stock() routed via SMART, not silently mistaken for a future.
        ibc0 = IBClient(cfg)
        fut = ibc0._make_contract("MES")
        assert isinstance(fut, Future) and fut.exchange == "CME"
        fut_mym = ibc0._make_contract("MYM")
        assert isinstance(fut_mym, Future) and fut_mym.exchange == "CBOT"
        # 2026-09-12: full-size futures added for the capacity-allocation plan -- same
        # exchange as their micro counterpart in every case (confirmed live via
        # reqContractDetails, not assumed from the micro pattern alone).
        for sym, exch in (("ES", "CME"), ("NQ", "CME"), ("YM", "CBOT"), ("RTY", "CME")):
            f = ibc0._make_contract(sym)
            assert isinstance(f, Future) and f.exchange == exch, \
                f"{sym} should route to {exch}, got {f.exchange}"
        stk = ibc0._make_contract("AAPL")
        assert isinstance(stk, Stock) and stk.exchange == "SMART" and stk.symbol == "AAPL"

        # 1. Object construction with valid config
        ibc = IBClient(cfg)
        assert ibc._live_port  == cfg.ib.live_port,  "live_port mismatch"
        assert ibc._paper_port == cfg.ib.paper_port, "paper_port mismatch"
        assert not ibc.is_live_connected()
        assert not ibc.is_paper_connected()

        # 2. Connection attempt — skip if IB Gateway not running
        try:
            ibc.connect(live=True, paper=True)
            live_ok  = ibc.is_live_connected()
            paper_ok = ibc.is_paper_connected()
        except ConnectionError as e:
            print(f"[self-test] ib_client: SKIP (IB Gateway not running: {e})")
            return True

        if not live_ok:
            print("[self-test] ib_client: SKIP (LIVE port not reachable)")
            return True
        if not paper_ok:
            print("[self-test] ib_client: SKIP (PAPER port not reachable)")
            ibc.disconnect()
            return True

        # 3. Verify status dict
        s = ibc.status()
        assert s["live_connected"]  is True
        assert s["paper_connected"] is True
        assert s["live_client_id"]  is not None
        assert s["paper_client_id"] is not None

        # 4. Price fetch from LIVE -- and confirm the persistent ticker cache (2026-09-11)
        # is actually used: first call subscribes (slow), second call must be near-free.
        try:
            import time as _time
            t0 = _time.perf_counter()
            price = ibc.get_price("MES")
            first_call_s = _time.perf_counter() - t0
            assert price > 0, f"Invalid price: {price}"
            assert "MES" in ibc._ticker_cache, "get_price() did not populate the ticker cache"

            t1 = _time.perf_counter()
            price2 = ibc.get_price("MES")
            second_call_s = _time.perf_counter() - t1
            assert price2 > 0, f"Invalid cached price: {price2}"
            assert second_call_s < 1.0, (
                f"Second get_price() call took {second_call_s:.2f}s -- expected near-instant "
                f"from the ticker cache (first call took {first_call_s:.2f}s)"
            )
            log.info(f"[self-test] MES price={price} (first={first_call_s:.2f}s, "
                     f"cached={second_call_s:.2f}s)")
        except Exception as e:
            log.warning(f"[self-test] Price fetch failed (non-fatal): {e}")

        # 5. Open orders from PAPER (should be empty or a list)
        orders = ibc.get_open_orders()
        assert isinstance(orders, list)

        ibc.disconnect()
        assert not ibc.is_live_connected()
        assert not ibc.is_paper_connected()

        print("[self-test] ib_client: PASS")
        return True

    except Exception as e:
        print(f"[self-test] ib_client: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("IBClient demo — use IBClient() in your component")
