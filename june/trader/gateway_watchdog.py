"""
trader/gateway_watchdog.py
Keeps IB Gateway running 24/7.

Polls port 4002 every 60 seconds. If the gateway is down, launches it via
IBC and waits up to 90 seconds for it to come back. Logs every restart.

Run at Windows login via Task Scheduler (see scripts/run_watchdog.bat).
Safe to run alongside the broker, fetcher, and dashboard — read-only probe.

Usage:
  python trader/gateway_watchdog.py           # run forever
  python trader/gateway_watchdog.py --once    # single probe and exit (for testing)
  python trader/gateway_watchdog.py --interval 30   # poll every 30s
"""

import argparse
import socket
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.ibc_launcher import try_start_gateway
from lib.logger import get_logger

log = get_logger("gateway_watchdog")

_POLL_INTERVAL  = 60   # seconds between probes when gateway is up
_PROBE_TIMEOUT  = 2    # TCP connect timeout
_RESTART_WAIT   = 90   # max seconds to wait for gateway after restart
_RESTART_STEP   = 5    # seconds between restart probes


def _is_up(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _wait_for_gateway(host: str, port: int) -> bool:
    """Poll until gateway answers or timeout. Returns True if it came up."""
    deadline = time.time() + _RESTART_WAIT
    attempt  = 0
    while time.time() < deadline:
        time.sleep(_RESTART_STEP)
        attempt += 1
        if _is_up(host, port):
            log.info("Gateway back up after %ds (attempt %d)", attempt * _RESTART_STEP, attempt)
            return True
        log.debug("Waiting for gateway… %ds elapsed", attempt * _RESTART_STEP)
    return False


def run_once(cfg) -> bool:
    """Single probe + restart if needed. Returns True if gateway is up at exit."""
    host = cfg.ib.live_host
    port = cfg.ib.live_port
    if _is_up(host, port):
        log.debug("Gateway OK on %s:%s", host, port)
        return True

    log.warning("Gateway DOWN on %s:%s — launching via IBC", host, port)
    launched = try_start_gateway(cfg, label="watchdog")
    if not launched:
        log.error("IBC launch failed — check C:\\IBC\\StartGateway.bat")
        return False

    up = _wait_for_gateway(host, port)
    if up:
        log.info("Gateway recovered successfully")
    else:
        log.error("Gateway did not come up within %ds — will retry next poll", _RESTART_WAIT)
    return up


def run_forever(cfg, interval: int = _POLL_INTERVAL):
    host = cfg.ib.live_host
    port = cfg.ib.live_port
    log.info("Watchdog started — polling %s:%s every %ds", host, port, interval)
    print(f"[watchdog] Polling {host}:{port} every {interval}s  (Ctrl+C to stop)", flush=True)

    consecutive_failures = 0
    while True:
        try:
            up = run_once(cfg)
            if up:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    log.error("Gateway failed to restart 3 times in a row — check IBC / credentials")
                    consecutive_failures = 0  # reset so we keep trying
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Watchdog stopped by user")
            print("\n[watchdog] Stopped.", flush=True)
            break
        except Exception as e:
            log.error("Watchdog error: %s", e)
            time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IB Gateway watchdog")
    parser.add_argument("--once",     action="store_true", help="Single probe then exit")
    parser.add_argument("--interval", type=int, default=_POLL_INTERVAL,
                        help=f"Poll interval in seconds (default {_POLL_INTERVAL})")
    args = parser.parse_args()

    cfg = get_config()
    if args.once:
        up = run_once(cfg)
        sys.exit(0 if up else 1)
    else:
        run_forever(cfg, interval=args.interval)
