"""
singleton_lock.py
OS-level exclusive lock so a component (broker, decider) can refuse to start
a second instance -- regardless of which launcher started it (visualizer
auto-start, a SessionManager, a manual terminal run, a scheduled task).

2026-09-08 incident: trading_dashboard.py's SessionManager and visualizer.py's
own auto-start each independently launched their own broker.py/decider.py
against the same DB for ~2 hours, unnoticed, because each launcher only ever
checked its own bookkeeping (a "is this cmdline already running" scan), never
a lock shared across launch paths. This closes that gap at the one chokepoint
every path shares: the process actually starting up.

The lock is held for the life of the process -- released automatically by the
OS if the process exits or crashes, so a stale lock from a killed process
never blocks a fresh start.
"""

import os
import sys
from pathlib import Path

_lock_handles = {}  # name -> open file handle, kept alive so the OS lock persists


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check for a PID."""
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


def acquire_singleton_lock(name: str, lock_dir: Path) -> bool:
    """
    Try to exclusively acquire the lock for `name`.
    Returns True if this process now holds it, False if another live process
    already does.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    f = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False

    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _lock_handles[name] = f
    return True


def is_locked_by_other(name: str, lock_dir: Path) -> bool:
    """
    Whether another live process currently holds `name`'s lock, without acquiring or
    disturbing it.

    Used by session.py's SessionManager so a fresh supervisor (e.g. spun up after a
    dashboard restart, with no memory of what it previously launched) can recognize an
    already-running broker/decider instead of assuming "not in my bookkeeping" == dead
    and trying to respawn it -- a real live incident (2026-09-10): a fresh supervisor
    saw a genuinely-running broker as unlocked and was moments from spawning a
    duplicate against the same account before this was caught by hand.

    2026-09-10: checks the PID acquire_singleton_lock wrote into the file and its OS
    liveness, NOT an msvcrt/fcntl lock-contention probe -- that approach proved
    genuinely unreliable on Windows in practice (opening an already-locked file
    intermittently succeeded or failed with no observable pattern across otherwise
    identical calls). PID+liveness is deterministic: no lock-timing races, no
    dependence on file-locking semantics at all. Falls back to a direct lock attempt
    only in the narrow window right after acquisition, before the PID write has landed.
    """
    path = lock_dir / f"{name}.lock"
    if not path.exists():
        return False
    try:
        f = open(path, "a+")
    except OSError:
        # Can't even open it -- another process holds it exclusively.
        return True
    try:
        try:
            f.seek(0)
            content = f.read().strip()
        except OSError:
            # Windows enforces msvcrt.locking()'s byte-range lock as MANDATORY, not
            # advisory -- reading that byte range from a different handle fails
            # outright while another process holds it. That failure IS the "someone
            # else holds it" signal.
            return True
        try:
            pid = int(content)
        except ValueError:
            pid = None
        if pid is not None:
            return pid != os.getpid() and _pid_alive(pid)

        # No valid PID recorded yet (e.g. a fresh acquire() hasn't written it out this
        # instant) -- fall back to an actual lock attempt just for this edge case.
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
    finally:
        f.close()


def self_test() -> bool:
    """
    is_locked_by_other must be tested against a REAL separate process, not just
    another handle in this same process -- the 2026-09-10 bug (open() failing with
    PermissionError while a lock is held, wrongly read as "not locked") only shows up
    cross-process on Windows.
    """
    import subprocess
    import tempfile
    import gc
    import time as _time

    try:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            name = "test_component"

            # Nothing has touched the lock yet.
            assert is_locked_by_other(name, lock_dir) is False, \
                "no lock file yet -- should read as not locked"

            holder_script = f'''
import sys, time
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
from lib.singleton_lock import acquire_singleton_lock
ok = acquire_singleton_lock({name!r}, Path({str(lock_dir)!r}))
print("acquired" if ok else "failed", flush=True)
time.sleep(10)
'''
            proc = subprocess.Popen(
                [sys.executable, "-c", holder_script],
                stdout=subprocess.PIPE, text=True
            )
            try:
                line = proc.stdout.readline().strip()
                assert line == "acquired", f"holder subprocess failed to acquire: {line!r}"
                _time.sleep(0.3)  # let the OS-level lock actually land

                assert is_locked_by_other(name, lock_dir) is True, \
                    "a live separate process holds this lock -- must read as locked"
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                # On Windows, OpenProcess() (used by _pid_alive) can still succeed for
                # an exited PID as long as ANY handle to it remains open anywhere --
                # including this parent's own Popen handle. Drop it explicitly so the
                # liveness check below reflects the real-world case, where the checking
                # process never held a handle to what it's checking in the first place.
                del proc
                gc.collect()

            _time.sleep(0.3)  # let the OS release the lock after the process exits
            assert is_locked_by_other(name, lock_dir) is False, \
                "holder process is gone -- should read as not locked anymore"

        print("[self-test] singleton_lock: PASS")
        return True
    except Exception as e:
        print(f"[self-test] singleton_lock: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    import argparse as _argparse
    parser = _argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
