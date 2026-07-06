"""
trader/fetch_watchdog.py
Watchdog for the Galgo fetch system. Runs every 60 seconds and:
  1. Checks IB Gateway health (port 4002) — restarts IBC if down
  2. Checks fetch_scheduler process — restarts if dead (and gateway is up)
  3. Checks fetch_progress freshness — emails if stale with no scheduler
  4. Kills duplicate schedulers (keeps lowest PID)
  5. Cleans stale lock files

Emails user immediately on anything it cannot self-heal.

Usage:
  python trader/fetch_watchdog.py          # runs forever
  python trader/fetch_watchdog.py --once   # single check (for testing)
"""

import argparse
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

import psutil
from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("fetch_watchdog")

_LOCK_FILE    = _ROOT / "data" / "fetch_scheduler.lock"
_PROGRESS_DB  = _ROOT / "trader" / "data" / "fetch_progress.db"  # actual DB location
_SCHEDULER    = _ROOT / "trader" / "fetch_scheduler.py"
_EMAIL_SCRIPT = _ROOT.parent / "send_email.py"
_EMAIL_TO     = "gavish.oren@gmail.com"

CHECK_INTERVAL      = 60    # seconds between checks
STALE_THRESHOLD     = 600   # seconds with no DB update → log warning (10 min)
STALE_KILL_THRESHOLD = 1200  # seconds (20 min) → kill & restart stuck scheduler
RESTART_COOLDOWN    = 180   # seconds to wait before restarting scheduler again

_last_restart_ts: float = 0.0
_consecutive_failures: int = 0
_last_records_total: int | None = None   # for heartbeat-mask detection
_last_records_ts: float = 0.0            # when we last saw records change


# ── Helpers ───────────────────────────────────────────────────────────────────

def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _send_email(subject: str, body: str):
    if not _EMAIL_SCRIPT.exists():
        log.warning("Email script not found: %s", _EMAIL_SCRIPT)
        return
    try:
        subprocess.run(
            [sys.executable, str(_EMAIL_SCRIPT), subject, body],
            check=False, timeout=30
        )
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.warning("Email failed: %s", e)


def _scheduler_pids() -> list[int]:
    pids = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "fetch_scheduler" in cmdline and "fetch_watchdog" not in cmdline:
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def _progress_age_seconds() -> float | None:
    """Return seconds since records_fetched last changed on the active (finished=0) file.

    Uses record-count delta rather than updated_at, so a heartbeat that refreshes
    updated_at without delivering new ticks does NOT mask a real stall.

    Falls back to updated_at age for finished rows (file transitions) to preserve
    the G14 fix: when an active file completes and the next target hasn't started,
    the just-finished row's updated_at keeps the age low.
    """
    global _last_records_total, _last_records_ts
    try:
        con = sqlite3.connect(str(_PROGRESS_DB), timeout=5)

        # Prefer the active (unfinished) row — that's where stalls happen
        active = con.execute(
            "SELECT records_fetched FROM fetch_progress WHERE finished=0 "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

        if active is not None:
            total = active[0]
            now   = time.time()
            if _last_records_total is None or total != _last_records_total:
                _last_records_total = total
                _last_records_ts    = now
            con.close()
            return now - _last_records_ts

        # No active row → check updated_at of most-recent finished row (G14 safety)
        finished = con.execute(
            "SELECT updated_at FROM fetch_progress ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        con.close()
        if not finished:
            return None
        ts = datetime.fromisoformat(finished[0].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        _last_records_total = None   # reset for next active file
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def _clean_stale_lock():
    if not _LOCK_FILE.exists():
        return False
    try:
        pid = int(_LOCK_FILE.read_text().strip())
        if not psutil.pid_exists(pid):
            _LOCK_FILE.unlink(missing_ok=True)
            log.info("Cleaned stale lock (dead pid=%d)", pid)
            return True
    except Exception:
        _LOCK_FILE.unlink(missing_ok=True)
        log.info("Cleaned unreadable lock file")
        return True
    return False


# ── Restart helpers ───────────────────────────────────────────────────────────

def _restart_gateway(cfg) -> bool:
    """Kill java/gateway and restart via IBC bat file. Returns True if gateway comes up."""
    log.warning("Restarting IBC gateway...")
    ibc_bat = Path(getattr(cfg.ib, "ibc_bat", r"C:\IBC\StartGateway.bat"))
    if not ibc_bat.exists():
        log.error("IBC bat not found: %s", ibc_bat)
        return False

    # Kill existing java processes bound to gateway port
    gw_port = int(getattr(cfg.ib, "live_port", 4002))
    killed = 0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if "java" in (proc.info.get("name") or "").lower():
                for conn in proc.net_connections():
                    if conn.laddr.port == gw_port:
                        proc.kill()
                        killed += 1
                        break
        except Exception:
            pass
    if killed:
        log.info("Killed %d java process(es) on port %d", killed, gw_port)
        time.sleep(5)

    subprocess.Popen(
        ["cmd", "/c", str(ibc_bat)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )

    gw_host = getattr(cfg.ib, "live_host", "127.0.0.1")
    for attempt in range(18):   # up to 90s
        time.sleep(5)
        if _port_open(gw_host, gw_port):
            log.info("Gateway up after %d attempts", attempt + 1)
            return True
    log.error("Gateway did not come up after 90s")
    return False


def _restart_scheduler() -> bool:
    global _last_restart_ts
    now = time.time()
    if now - _last_restart_ts < RESTART_COOLDOWN:
        log.info("Restart cooldown active (%ds remaining)", RESTART_COOLDOWN - (now - _last_restart_ts))
        return False

    _clean_stale_lock()
    try:
        subprocess.Popen(
            [sys.executable, str(_SCHEDULER), "--backfill"],
            cwd=str(_ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _last_restart_ts = now
        log.info("Restarted fetch_scheduler")
        return True
    except Exception as e:
        log.error("Failed to restart scheduler: %s", e)
        return False


# ── Main health check ─────────────────────────────────────────────────────────

def check_and_heal(cfg) -> list[str]:
    """Run one watchdog cycle. Returns list of action strings taken."""
    global _consecutive_failures
    actions = []

    gw_host = getattr(cfg.ib, "live_host", "127.0.0.1")
    gw_port = int(getattr(cfg.ib, "live_port", 4002))
    gw_up   = _port_open(gw_host, gw_port)

    # 1. Gateway health
    if not gw_up:
        log.warning("Gateway DOWN on %s:%d — attempting restart", gw_host, gw_port)
        ok = _restart_gateway(cfg)
        if ok:
            actions.append("GW_RESTARTED:ok")
            gw_up = True
        else:
            actions.append("GW_RESTART_FAILED")
            _consecutive_failures += 1
            if _consecutive_failures >= 3:
                _send_email(
                    "🚨 Galgo IBC restart FAILED",
                    f"Watchdog tried {_consecutive_failures}x to restart IBC Gateway but it won't come up.\n"
                    f"Manual action needed: run C:\\IBC\\StartGateway.bat\n\n"
                    f"Time: {datetime.now(timezone.utc).isoformat()}"
                )
                _consecutive_failures = 0
            return actions

    # 2. Duplicate schedulers — keep lowest PID, kill rest
    pids = _scheduler_pids()
    if len(pids) > 1:
        pids.sort()
        for dup_pid in pids[1:]:
            try:
                psutil.Process(dup_pid).terminate()
                actions.append(f"KILLED_DUP_SCHEDULER:pid={dup_pid}")
                log.warning("Killed duplicate scheduler pid=%d", dup_pid)
            except Exception as e:
                log.warning("Could not kill pid=%d: %s", dup_pid, e)
        pids = pids[:1]

    # 3. No scheduler running — restart if gateway is up
    if not pids:
        _clean_stale_lock()
        if gw_up:
            ok = _restart_scheduler()
            actions.append(f"SCHEDULER_RESTARTED:{'ok' if ok else 'failed'}")
            if not ok:
                _consecutive_failures += 1
        else:
            actions.append("SCHEDULER_DEAD:waiting_for_gateway")

    # 4. Progress freshness — is the running scheduler actually making progress?
    age = _progress_age_seconds()
    if age is not None and age > STALE_THRESHOLD and pids:
        log.warning("Fetch stale: last DB update %.0fs ago — scheduler may be stuck", age)
        actions.append(f"STALE:{age:.0f}s")

        if age > STALE_KILL_THRESHOLD and gw_up:
            # Gateway is up but no progress for 20+ min → scheduler is hung in reconnect
            # loop or IB pacing deadlock. Kill it and restart so the next file can proceed.
            log.warning("Stale for %.0f min with gateway up — killing hung scheduler pid=%s",
                        age / 60, pids[0] if pids else "none")
            for pid in pids:
                try:
                    psutil.Process(pid).terminate()
                    log.info("Killed hung scheduler pid=%d", pid)
                    actions.append(f"KILLED_HUNG_SCHEDULER:pid={pid}")
                except Exception as e:
                    log.warning("Could not kill pid=%d: %s", pid, e)
            _clean_stale_lock()
            time.sleep(3)
            ok = _restart_scheduler()
            actions.append(f"SCHEDULER_RESTARTED_AFTER_HANG:{'ok' if ok else 'failed'}")
            _send_email(
                "⚠️ Galgo fetch hang — auto-restarted",
                f"No fetch progress for {age/60:.1f} minutes (gateway was up).\n"
                f"Killed stuck scheduler pid(s) {pids} and restarted it.\n"
                f"Watchdog will continue monitoring.\n\n"
                f"Time: {datetime.now(timezone.utc).isoformat()}"
            )
        elif age > STALE_THRESHOLD * 3:   # 15 min warning (gateway may be down)
            _send_email(
                "⚠️ Galgo fetch stuck",
                f"No fetch progress for {age/60:.1f} minutes.\n"
                f"Scheduler pid={pids[0] if pids else 'none'}, gateway={'up' if gw_up else 'down'}.\n"
                f"Watchdog will auto-restart after 30min if gateway is up.\n\n"
                f"Time: {datetime.now(timezone.utc).isoformat()}"
            )

    # 5. Clean stale lock file (process dead but lock exists)
    if _clean_stale_lock():
        actions.append("STALE_LOCK_CLEANED")

    if not actions:
        _consecutive_failures = 0

    return actions


# ── Entry point ───────────────────────────────────────────────────────────────

def _watchdog_already_running() -> bool:
    """Return True if another fetch_watchdog process is already running."""
    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.info["pid"] == my_pid:
            continue
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "fetch_watchdog" in cmd and "python" in cmd.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    args = parser.parse_args()

    # Single-instance guard — safe to call from Task Scheduler every 5 min
    if not args.once and _watchdog_already_running():
        print("[WATCHDOG] already running — exiting", flush=True)
        return

    cfg = get_config()
    log.info("Watchdog started (interval=%ds, stale_threshold=%ds)", CHECK_INTERVAL, STALE_THRESHOLD)
    print(f"[WATCHDOG] started — checking every {CHECK_INTERVAL}s", flush=True)

    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            actions = check_and_heal(cfg)
            if actions:
                for a in actions:
                    log.info("action: %s", a)
                    print(f"[WATCHDOG] {ts} {a}", flush=True)
            else:
                print(f"[WATCHDOG] {ts} ok", flush=True)
        except Exception as e:
            log.error("Watchdog cycle error: %s", e)
            print(f"[WATCHDOG] {ts} ERROR: {e}", flush=True)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
