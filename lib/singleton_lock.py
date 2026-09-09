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
