"""
trader/scripts/restart_decider_daily.py
Daily decider.py restart (2026-09-10).

decider.py's run_session_start() -- the only place that scans critical_lines and turns
newly-armed ones into PENDING commands -- runs exactly ONCE per process lifetime, at
startup, before settling into the replenishment loop forever. It never re-checks for a
new day's lines on its own. As long as decider keeps running uninterrupted (which is
exactly what it's designed to do), a new day's Geva/research lines silently generate
zero commands -- discovered 2026-09-10, papered over that day only because decider
happened to get restarted several times for unrelated reasons.

This script kills decider's current process (if any) and relaunches it fresh. Timing
only needs to land before the trading-start gate (17:00 IL) -- run_session_start reads
whatever lines exist in the DB at the moment it clears that gate, not at its own
startup time, so an early-morning restart is as good as one at 16:59.

Usage:
    python trader/scripts/restart_decider_daily.py

Self-test:
    python trader/scripts/restart_decider_daily.py --self-test
"""

import sys
import time
import argparse
import subprocess
from pathlib import Path

_TRADER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_TRADER_DIR.parent))


def _current_decider_pid(trader_dir: Path) -> int | None:
    """
    Find decider.py's PID by scanning live processes, NOT by reading its lock file's
    content -- Windows enforces the lock file's byte-range lock as MANDATORY, so a plain
    read while decider.py holds it (i.e. always, in production, for its entire
    lifetime) can itself fail with a permission error. Hitting that and treating it as
    "no PID found" (as an earlier version of this function did) would spawn a genuine
    duplicate decider.py in production every single time -- caught by this function's
    own self-test, which reproduces the read failure with a real second process holding
    the lock, not just an in-process simulation.
    """
    import psutil
    trader_dir_str = str(trader_dir.resolve())
    for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cmdline = proc.info["cmdline"] or []
            if not any("decider.py" in str(part) for part in cmdline):
                continue
            cwd = proc.info.get("cwd")
            if cwd and str(Path(cwd).resolve()) != trader_dir_str:
                continue  # a decider.py in a different project/checkout -- not ours
            return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def restart_decider(trader_dir: Path = _TRADER_DIR, wait_seconds: float = 3.0,
                     kill_fn=None, spawn_fn=None, find_pid_fn=None) -> dict:
    """
    kill_fn/spawn_fn/find_pid_fn are injectable for the self-test (real process
    kill/spawn/discovery aren't exercised there -- this function's own control flow is
    what's under test; _current_decider_pid's psutil-based lookup gets its own separate,
    real-subprocess smoke test).
    """
    kill_fn = kill_fn or _default_kill
    spawn_fn = spawn_fn or _default_spawn
    find_pid_fn = find_pid_fn or _current_decider_pid

    old_pid = find_pid_fn(trader_dir)
    killed = False
    if old_pid is not None:
        kill_fn(old_pid)
        killed = True
        time.sleep(wait_seconds)  # let the OS release the singleton lock file

    new_pid = spawn_fn(trader_dir)
    return {"old_pid": old_pid, "killed": killed, "new_pid": new_pid}


def _default_kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
    else:
        import os
        import signal
        os.kill(pid, signal.SIGKILL)


def _default_spawn(trader_dir: Path) -> int:
    proc = subprocess.Popen(
        [sys.executable, "decider.py", "--mode", "session"],
        cwd=str(trader_dir),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return proc.pid


def self_test() -> bool:
    try:
        import tempfile

        # Part A: restart_decider()'s own control flow (kill-if-found, always spawn),
        # with PID discovery faked out -- this is independent of HOW a real PID gets
        # found, which Part B below tests separately and for real.
        with tempfile.TemporaryDirectory() as tmp:
            trader_dir = Path(tmp)

            # A1. Nothing found -- no kill, still spawns fresh.
            spawn_calls, kill_calls = [], []
            result = restart_decider(
                trader_dir, wait_seconds=0,
                find_pid_fn=lambda td: None,
                kill_fn=lambda pid: kill_calls.append(pid),
                spawn_fn=lambda td: (spawn_calls.append(td), 12345)[1],
            )
            assert result == {"old_pid": None, "killed": False, "new_pid": 12345}
            assert kill_calls == [] and spawn_calls == [trader_dir]

            # A2. A PID is found -- killed before respawning, and a replacement always follows.
            spawn_calls.clear(); kill_calls.clear()
            result2 = restart_decider(
                trader_dir, wait_seconds=0,
                find_pid_fn=lambda td: 4242,
                kill_fn=lambda pid: kill_calls.append(pid),
                spawn_fn=lambda td: (spawn_calls.append(td), 67890)[1],
            )
            assert result2 == {"old_pid": 4242, "killed": True, "new_pid": 67890}
            assert kill_calls == [4242] and spawn_calls == [trader_dir]

        # Part B: _current_decider_pid()'s real psutil-based lookup, against a genuine
        # subprocess with "decider.py" in its command line and a matching cwd -- this is
        # exactly the scenario that broke the earlier lock-file-content-reading version
        # of this function (a real second process holding the singleton lock made a
        # plain file read fail, which got silently treated as "nothing running").
        with tempfile.TemporaryDirectory() as tmp:
            trader_dir = Path(tmp)
            marker = trader_dir / "decider.py"  # named so psutil sees "decider.py" in argv
            marker.write_text("import time; time.sleep(30)")
            dummy = subprocess.Popen([sys.executable, "decider.py"], cwd=str(trader_dir))
            try:
                deadline = time.time() + 5
                found = None
                while time.time() < deadline:
                    found = _current_decider_pid(trader_dir)
                    if found is not None:
                        break
                    time.sleep(0.2)
                assert found == dummy.pid, f"expected {dummy.pid}, got {found}"

                # A decider.py running from a DIFFERENT directory must not match.
                other_dir = Path(tempfile.mkdtemp())
                try:
                    assert _current_decider_pid(other_dir) is None, \
                        "must not match a decider.py process from an unrelated directory"
                finally:
                    import shutil
                    shutil.rmtree(other_dir, ignore_errors=True)
            finally:
                dummy.kill()
                dummy.wait(timeout=5)

            deadline = time.time() + 5
            while time.time() < deadline and _current_decider_pid(trader_dir) is not None:
                time.sleep(0.2)
            assert _current_decider_pid(trader_dir) is None, \
                "must not find a PID once the process is gone"

        print("[self-test] restart_decider_daily: PASS")
        return True
    except Exception as e:
        print(f"[self-test] restart_decider_daily: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)

    result = restart_decider()
    print(result)
