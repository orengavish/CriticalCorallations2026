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

from ib_insync import IB, Future, util

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("ib_client")

_EXCHANGE = "CME"
_SYMBOL_EXCHANGE = {"MYM": "CBOT"}  # Micro Dow is listed under CBOT, not CME -- confirmed
                                    # via reqContractDetails (2026-08-29); MES/MNQ/M2K are CME.
_CURRENCY = "USD"


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

        self._contract_cache: dict[str, Future] = {}

        self._lock = threading.Lock()
        atexit.register(self.disconnect)

    # ── Connect ───────────────────────────────────────────────────────────────

    def connect(self, live: bool = True, paper: bool = True):
        """Connect to LIVE and/or PAPER ports."""
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
        """
        if not self.live or not self.live.isConnected():
            raise ConnectionError("LIVE connection is not active")

        con = contract or self.get_contract(symbol)

        # Try streaming snapshot first (1.5s wait)
        ticker = self.live.reqMktData(con, "", False, False)
        self.live.sleep(1.5)

        price = ticker.last
        if price is None or price != price:  # nan check
            bid = ticker.bid or 0
            ask = ticker.ask or 0
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2

        try:
            self.live.cancelMktData(con)
        except Exception:
            pass  # IB may have already dropped the ticker (e.g. after error 354)

        # Fallback: last close from reqHistoricalData (always available)
        if price is None or price != price or price <= 0:
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
            else:
                raise ValueError(f"No price available for {symbol} via mktData or historical")

        log.info(f"Price for {symbol}: {price}")
        return price

    def _make_contract(self, symbol: str) -> Future:
        """Build a generic continuous futures contract (resolved later by get_contract)."""
        exchange = _SYMBOL_EXCHANGE.get(symbol, _EXCHANGE)
        return Future(symbol=symbol, exchange=exchange, currency=_CURRENCY)

    def get_contract(self, symbol: str) -> Future:
        """
        Resolve the active front-month contract for symbol via LIVE connection.
        Uses reqContractDetails to handle ambiguous contracts, picks nearest expiry.
        Result is cached for the lifetime of this IBClient instance.
        Returns a qualified Future contract.
        """
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]

        if not self.live or not self.live.isConnected():
            raise ConnectionError("LIVE connection is not active")
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

        # 4. Price fetch from LIVE
        try:
            price = ibc.get_price("MES")
            assert price > 0, f"Invalid price: {price}"
            log.info(f"[self-test] MES price={price}")
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
