"""
trader/session.py
Session manager — launches broker.py and decider.py as supervised subprocesses,
streams their stdout/stderr to log files under trader/logs/, restarts them if
they die unexpectedly, and shuts both down cleanly via the SESSION=SHUTDOWN
flag in system_state (which broker.py/decider.py already poll for).

Distinct from trader/runner.py, which launches the broader legacy engine
(position_manager, visualizer, fetch_scheduler, random_gen) and is not wired
into anything currently. This launches only broker + decider, and is meant to
be imported by back-trading/trading_dashboard.py for a Start/Stop Session
button and a GET /api/session/status endpoint.

Only one SessionManager should own broker/decider at a time — start() takes a
PID-file lock under trader/logs/session.pid so a standalone CLI run and a
dashboard-owned session can't both spawn them against the same DB at once.

Usage:
    python trader/session.py              # foreground, blocking, Ctrl+C to stop
    python trader/session.py --self-test

Self-test:
    python trader/session.py --self-test
"""

import sys
import os
import time
import signal
import argparse
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_system_state, set_system_state

log = get_logger("session", log_dir=str(Path(__file__).parent / "logs"))

_COMPONENTS = ("broker", "decider")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check for a PID (used by the lock file guard)."""
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


class SessionManager:
    """Supervises broker.py + decider.py as subprocesses. See module docstring."""

    def __init__(self, cfg=None, db_path: Path = None, trader_dir: Path = None,
                 log_dir: Path = None, component_cmds: dict[str, list[str]] = None):
        # Explicit path, not bare get_config(): lib.config_loader._find_config()
        # picks a config.yaml near the *caller's* script, and get_config() caches
        # globally regardless of path once called once. When this class is
        # imported into trading_dashboard.py (which lives in back-trading/, and
        # has its own separate back-trading/config.yaml for the backtest engine),
        # ambient resolution would silently load the wrong config — wrong DB path,
        # missing session.* keys. Always load trader/config.yaml by its own
        # location instead, regardless of who's importing this module.
        self._cfg = cfg or get_config(Path(__file__).parent / "config.yaml")
        self._trader_dir = Path(trader_dir) if trader_dir else Path(__file__).parent
        self._db_path = Path(db_path) if db_path else Path(self._cfg.paths.db)
        self._log_dir = Path(log_dir) if log_dir else Path(self._cfg.paths.logs)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file = self._log_dir / "session.pid"

        self._component_cmds = component_cmds or {
            "broker":  [sys.executable, "broker.py"],
            "decider": [sys.executable, "decider.py", "--mode", "session"],
        }

        s = self._cfg.session
        self._monitor_poll_seconds = s.monitor_poll_seconds
        self._max_restarts         = s.max_restarts
        self._backoff_base         = s.restart_backoff_base_seconds
        self._backoff_cap          = s.restart_backoff_cap_seconds
        self._stop_grace_seconds   = s.stop_grace_seconds

        self._procs: dict[str, subprocess.Popen] = {}
        self._log_files: dict[str, object] = {}
        self._state: dict[str, str]        = {c: "dead" for c in _COMPONENTS}
        self._restart_count: dict[str, int] = {c: 0 for c in _COMPONENTS}
        self._restart_delay: dict[str, int] = {c: self._backoff_base for c in _COMPONENTS}
        self._started_at: float | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.RLock()

    # ── Public API ───────────────────────────────────────────────────────

    def is_running(self) -> bool:
        with self._lock:
            return self._started_at is not None

    def status(self) -> dict:
        with self._lock:
            uptime = (time.time() - self._started_at) if self._started_at else 0
            return {
                "broker":         self._state["broker"],
                "decider":        self._state["decider"],
                "uptime_seconds": int(uptime),
            }

    def start(self) -> dict:
        with self._lock:
            if self._started_at is not None:
                log.info("start() called but session already running — ignoring")
                return self.status()
            if not self._acquire_pid_lock():
                raise RuntimeError(
                    f"Another session.py supervisor already owns this session "
                    f"(see {self._pid_file}). Stop it first."
                )
            init_db(self._db_path)
            self._clear_stale_shutdown()
            self._stopping.clear()
            for name in _COMPONENTS:
                self._restart_count[name] = 0
                self._restart_delay[name] = self._backoff_base
                self._spawn(name)
            self._started_at = time.time()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="session-monitor", daemon=True
            )
            self._monitor_thread.start()
            log.info("Session started — broker + decider launched")
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            if self._started_at is None:
                log.info("stop() called but session not running — ignoring")
                return self.status()
            self._stopping.set()

        # Clean stop: broker.py/decider.py both already poll system_state for this.
        with get_db(self._db_path) as con:
            set_system_state(con, "SESSION", "SHUTDOWN")
        log.info("SESSION=SHUTDOWN written — waiting for broker/decider to exit")

        deadline = time.time() + self._stop_grace_seconds
        for name in _COMPONENTS:
            p = self._procs.get(name)
            if p is None:
                continue
            remaining = max(0, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.warning(f"{name} did not exit within {self._stop_grace_seconds}s — killing")
                p.kill()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        monitor = self._monitor_thread
        with self._lock:
            for name in _COMPONENTS:
                self._state[name] = "dead"
            self._started_at = None
            self._close_log_files()
            self._release_pid_lock()

        if monitor:
            monitor.join(timeout=self._stop_grace_seconds + 5)
        log.info("Session stopped — broker + decider shut down")
        return self.status()

    # ── Internals ────────────────────────────────────────────────────────

    def _spawn(self, name: str) -> None:
        old = self._log_files.get(name)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        cmd = self._component_cmds[name]
        log_path = self._log_dir / f"{name}_stdout.log"
        f = open(log_path, "a", buffering=1, encoding="utf-8")
        f.write(f"\n===== session.py starting {name} @ {_now_iso()} =====\n")
        p = subprocess.Popen(cmd, cwd=str(self._trader_dir), stdout=f, stderr=subprocess.STDOUT)
        self._procs[name]     = p
        self._log_files[name] = f
        self._state[name]     = "running"
        log.info(f"Spawned {name} (pid={p.pid}) — stdout: {log_path}")

    def _monitor_loop(self) -> None:
        while not self._stopping.wait(self._monitor_poll_seconds):
            shutdown_flagged = self._is_shutdown_flagged()
            all_dead = True
            for name in _COMPONENTS:
                p = self._procs.get(name)
                dead = p is None or p.poll() is not None
                if not dead:
                    all_dead = False
                    with self._lock:
                        self._state[name] = "running"
                        self._restart_count[name] = 0
                        self._restart_delay[name] = self._backoff_base
                    continue

                rc = p.returncode if p is not None else None

                if shutdown_flagged or self._stopping.is_set():
                    with self._lock:
                        self._state[name] = "dead"
                    continue

                # Unexpected death — restart with backoff.
                with self._lock:
                    self._state[name] = "restarting"
                self._restart_count[name] += 1
                if self._restart_count[name] > self._max_restarts:
                    log.error(f"{name} crashed {self._restart_count[name]} times "
                              f"(last rc={rc}) — giving up, marking dead")
                    with self._lock:
                        self._state[name] = "dead"
                    continue

                delay = self._restart_delay[name]
                log.warning(f"{name} exited unexpectedly (rc={rc}) — "
                            f"restart {self._restart_count[name]}/{self._max_restarts} in {delay}s")
                if self._stopping.wait(delay):
                    break
                self._restart_delay[name] = min(delay * 2, self._backoff_cap)
                with self._lock:
                    self._spawn(name)
                all_dead = False

            if shutdown_flagged and all_dead:
                log.info("SESSION=SHUTDOWN detected and both components exited — monitor stopping")
                break

    def _is_shutdown_flagged(self) -> bool:
        try:
            with get_db(self._db_path) as con:
                return get_system_state(con, "SESSION") == "SHUTDOWN"
        except Exception:
            return False

    def _clear_stale_shutdown(self) -> None:
        """
        broker.py checks SESSION=='SHUTDOWN' on its very first loop iteration and
        exits immediately if so. decider.py's run_session_start() sets SESSION=
        RUNNING itself once it finishes startup, but broker has no equivalent —
        so a stale SHUTDOWN left over from the previous stop() must be cleared
        before launching, or broker would exit instantly on this run too.
        """
        with get_db(self._db_path) as con:
            if get_system_state(con, "SESSION") == "SHUTDOWN":
                set_system_state(con, "SESSION", "STARTING")
                log.info("Cleared stale SESSION=SHUTDOWN before launch")

    def _close_log_files(self) -> None:
        for f in self._log_files.values():
            try:
                f.close()
            except Exception:
                pass
        self._log_files.clear()

    def _acquire_pid_lock(self) -> bool:
        if self._pid_file.exists():
            try:
                other_pid = int(self._pid_file.read_text().strip())
                if other_pid != os.getpid() and _pid_alive(other_pid):
                    return False
            except (ValueError, OSError):
                pass  # stale/corrupt lock file — safe to overwrite
        self._pid_file.write_text(str(os.getpid()))
        return True

    def _release_pid_lock(self) -> None:
        try:
            if self._pid_file.exists():
                self._pid_file.unlink()
        except Exception:
            pass


_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Process-wide singleton — used by trading_dashboard.py's Start/Stop/status routes."""
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    print("Running session self-test (fake broker/decider stand-ins)...")
    import tempfile
    from types import SimpleNamespace
    from lib.config_loader import reset_cache

    _GOOD_SCRIPT = '''
import sys, time
from pathlib import Path
sys.path.insert(0, r"{root}")
from lib.db import get_db, get_system_state
db_path = Path(r"{db}")
print("started", flush=True)
while True:
    with get_db(db_path) as con:
        if get_system_state(con, "SESSION") == "SHUTDOWN":
            print("shutdown detected, exiting", flush=True)
            break
    time.sleep(0.3)
'''

    _CRASHY_SCRIPT = '''
import sys, os
marker = r"{marker}"
if not os.path.exists(marker):
    open(marker, "w").close()
    print("crashing on first run", flush=True)
    sys.exit(1)
print("started after crash", flush=True)
import time
from pathlib import Path
sys.path.insert(0, r"{root}")
from lib.db import get_db, get_system_state
db_path = Path(r"{db}")
while True:
    with get_db(db_path) as con:
        if get_system_state(con, "SESSION") == "SHUTDOWN":
            print("shutdown detected, exiting", flush=True)
            break
    time.sleep(0.3)
'''

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            trader_dir = tmp_p / "trader"
            trader_dir.mkdir()
            db_path = tmp_p / "galao.db"
            init_db(db_path)

            marker = tmp_p / "decider_crashed_once.marker"
            (trader_dir / "broker.py").write_text(
                _GOOD_SCRIPT.format(root=str(_ROOT), db=str(db_path)))
            (trader_dir / "decider.py").write_text(
                _CRASHY_SCRIPT.format(root=str(_ROOT), db=str(db_path), marker=str(marker)))

            fast_cfg = SimpleNamespace(
                paths=SimpleNamespace(db=str(db_path), logs=str(tmp_p / "logs")),
                session=SimpleNamespace(
                    monitor_poll_seconds=0.5,
                    max_restarts=3,
                    restart_backoff_base_seconds=0.5,
                    restart_backoff_cap_seconds=2,
                    stop_grace_seconds=5,
                ),
            )

            mgr = SessionManager(
                cfg=fast_cfg, db_path=db_path, trader_dir=trader_dir,
                log_dir=tmp_p / "logs",
                component_cmds={
                    "broker":  [sys.executable, "broker.py"],
                    "decider": [sys.executable, "decider.py"],
                },
            )

            # start() spawns both
            st = mgr.start()
            assert st["broker"] == "running", f"broker should be running: {st}"
            time.sleep(1.0)

            # decider crashed once (rc=1) then should have been auto-restarted
            deadline = time.time() + 8
            while time.time() < deadline:
                st = mgr.status()
                if st["decider"] == "running":
                    break
                time.sleep(0.3)
            assert st["decider"] == "running", \
                f"decider should have restarted after its induced crash: {st}"
            assert mgr._restart_count["decider"] >= 1, "restart_count should reflect the crash"

            assert mgr.status()["uptime_seconds"] >= 0

            # stdout capture
            broker_log = tmp_p / "logs" / "broker_stdout.log"
            decider_log = tmp_p / "logs" / "decider_stdout.log"
            assert broker_log.exists(), "broker stdout log not created"
            assert decider_log.exists(), "decider stdout log not created"
            assert "started" in broker_log.read_text()
            assert "crashing on first run" in decider_log.read_text()

            # double-start (PID lock) guard — must be a genuinely different OS
            # process, since two SessionManagers in this same test process
            # share a PID and the lock can't tell them apart (as intended:
            # it only guards against a *second process* double-supervising).
            dummy = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            try:
                mgr._pid_file.write_text(str(dummy.pid))
                mgr2 = SessionManager(
                    cfg=fast_cfg, db_path=db_path, trader_dir=trader_dir,
                    log_dir=tmp_p / "logs",
                    component_cmds={
                        "broker":  [sys.executable, "broker.py"],
                        "decider": [sys.executable, "decider.py"],
                    },
                )
                raised = False
                try:
                    mgr2.start()
                except RuntimeError:
                    raised = True
                assert raised, "second supervisor should be blocked by a live foreign PID"
            finally:
                dummy.terminate()
                dummy.wait(timeout=5)
                mgr._pid_file.write_text(str(os.getpid()))  # restore mgr's own lock

            # stop() — clean shutdown via SESSION=SHUTDOWN
            st = mgr.stop()
            assert st["broker"] == "dead" and st["decider"] == "dead", f"expected both dead: {st}"
            assert st["uptime_seconds"] == 0
            assert not mgr._pid_file.exists(), "PID lock should be released on stop"

            with get_db(db_path) as con:
                assert get_system_state(con, "SESSION") == "SHUTDOWN"

            # Idempotent stop (already stopped)
            st2 = mgr.stop()
            assert st2["broker"] == "dead"

            reset_cache()

        print("PASS -- session: spawn, stdout capture, crash-restart, PID lock, clean stop")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


# ── CLI ───────────────────────────────────────────────────────────────────────

def _run_foreground():
    mgr = get_session_manager()

    def _handle_sigint(sig, frame):
        log.info("Ctrl+C received — stopping session")
        mgr.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    st = mgr.start()
    print(f"Session started: {st}")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(5)
            st = mgr.status()
            print(f"[{_now_iso()}] broker={st['broker']} decider={st['decider']} "
                  f"uptime={st['uptime_seconds']}s")
    except KeyboardInterrupt:
        mgr.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao session manager (broker + decider)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    _run_foreground()
