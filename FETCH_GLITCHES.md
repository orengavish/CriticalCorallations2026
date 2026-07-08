# Galgo Fetch — Known Glitches, Causes & Fixes

Last updated: 2026-07-04 (after overnight reliability overhaul)

---

## G1 — Watchdog reads wrong fetch_progress.db

**Symptom:** Fetcher hangs for hours with no progress. Watchdog log shows no stale alerts.

**Cause:** `fetch_watchdog.py` had `_PROGRESS_DB = _ROOT / "data" / "fetch_progress.db"` which
is an EMPTY file. The actual DB is at `_ROOT / "trader" / "data" / "fetch_progress.db"`.
`_progress_age_seconds()` always returned None → stale detection never fired.

**Fix:** Corrected path in `fetch_watchdog.py`. **Deployed 2026-07-04.**

---

## G2 — Watchdog detects stale fetch but never auto-restarts

**Symptom:** Scheduler is stuck in reconnect loop, email is sent, but fetching never resumes.
7+ hour hang on MNQ 2026-05-06 BID_ASK confirmed.

**Cause:** `check_and_heal()` logged a stale warning and sent an email (after 15 min) but the
comment said "Don't auto-kill — it might be waiting for IB pacing." This is too conservative:
IB pacing delays are 1-60 seconds, never 30 minutes.

**Fix:** Added `STALE_KILL_THRESHOLD = 1800s` (30 min). If gateway is UP and no DB progress
for 30 min, watchdog kills the hung scheduler PID(s) and restarts fresh. **Deployed 2026-07-04.**

---

## G3 — Infinite skipped_active spin loop after restart

**Symptom:** Scheduler log floods with `--- MNQ 2026-05-06 ---` / `skipped_active` at
1000 iterations/second, using 100% CPU, fetching nothing.

**Root cause (chain of 3 failures):**
1. `_mark_started()` in fetcher.py is called when a fetch begins. It sets `updated_at=now`.
2. If `fetch_day()` raises an exception immediately after (e.g., IB pacing error), the
   exception is caught and the target is retried immediately.
3. On the next iteration, `_is_actively_running()` sees `updated_at=<2s ago>` → returns True
   → `skipped_active`.
4. The cooldown check was `all(v == "skipped_active" ...)` but TRADES was `"skipped"` (done),
   not `"skipped_active"` → cooldown never fired → infinite spin.

**Fix A:** Changed cooldown trigger from `all()` to `any()` skipped_active.

**Fix B:** Added 60s cooldown on ANY `fetch_day()` exception, so `_mark_started`'s fresh
timestamp can't poison the very next iteration.

**Deployed 2026-07-04.**

---

## G4 — _mark_started resets records_fetched=0 on resume

**Symptom:** After scheduler restart, the dashboard shows "MNQ May 6 B: 0 records" even though
305k were already fetched. Progress bar resets to 0%.

**Cause:** `_mark_started()` SQL: `ON CONFLICT DO UPDATE SET records_fetched=0`. This wiped the
existing partial count every time a fetch (re)started.

**Fix:** SQL CASE: preserve existing records_fetched if incoming value is 0 and existing > 0:
```sql
records_fetched=CASE WHEN excluded.records_fetched=0 AND records_fetched>0
                     THEN records_fetched ELSE 0 END
```
**Deployed 2026-07-04.**

---

## G5 — IB Gateway socket disconnect → fetcher hangs in reconnect

**Symptom:** Fetcher log shows `W0 BID_ASK error: Socket disconnect — retrying` then
`IB not connected — waiting for reconnect` — and then NOTHING for 7+ hours.
Gateway IS up (port 4002 responds) but the fetcher's `ib_client` never re-established.

**Cause:** The fetcher's reconnect loop checks `ib.isConnected()` and loops. If the
IB session object is in a broken state (even with the gateway up), the reconnect
never completes. The ib_insync library sometimes needs a full disconnect/reconnect cycle.

**Fix:** The new `STALE_KILL_THRESHOLD` in the watchdog handles this: after 30 min with
no progress and gateway up, it kills and restarts the scheduler, which gets a fresh
IB connection. **Deployed 2026-07-04.**

**Prevention:** Consider adding explicit `ib.disconnect(); ib.connect()` in the fetcher's
reconnect loop after N failed attempts, rather than just polling `isConnected()`.

---

## G6 — Dashboard throughput shows "0 records/min" after server restart

**Symptom:** Dashboard shows "1 min: 0 records, 1 hour: 0 records" even though the scheduler
is actively fetching. Users think fetching stopped.

**Cause:** `_fetch_throughput_hist` and `_fetch_rate_state` are in-memory module globals that
reset to empty on every server restart. The 1min/1hour rates are computed from this history —
after restart, they're empty, so rates show 0 for the first minute.

**Workaround:** Wait ~60s after dashboard restart for rates to populate.

**Long-term fix:** Seed `_fetch_throughput_hist` from the last N minutes of DB timestamps
on server startup, so rates are available immediately.

---

## G7 — BID_ASK data_type key mismatch in grid

**Symptom:** Fetch tab grid shows dashes (—) for all BID_ASK columns even though the DB
has BID_ASK records.

**Cause:** `"BID_ASK".lower()` = `"bid_ask"` (with underscore) but the grid JS lookup
used `"bidask"` (no underscore). Grid cells for BID_ASK were always missing.

**Fix:** `dtype = r["data_type"].lower().replace("_", "")` in `api_fetch_status()`.
**Deployed 2026-07-03.**

---

## G8 — BID_ASK % shows 100% (1/1) when only 1 of 20 dates done

**Symptom:** Dashboard shows "MNQ B: 100% ✓" when only 1 BID_ASK date is finished.

**Cause:** For BID_ASK, `n_total` was counting only DB rows, not the expected number of
dates. With 1 DB row (finished=1), the percentage was 1/1 = 100%.

**Fix:** For BID_ASK slots, use the TRADES-done count as the denominator (since BID_ASK
should match the number of completed TRADES dates). **Deployed 2026-07-03.**

---

## G9 — Holiday date (July 3) appears as active fetch target

**Symptom:** July 3, 2026 (Independence Day observed) appeared in the "active fetch" row
of the dashboard live panel, making the count/progress confusing.

**Cause:** `_FETCH_HOLIDAYS` set was correctly defined but not applied to the `active_rows`
filter in `api_fetch_live`.

**Fix:** Added `and r["date"] not in _FETCH_HOLIDAYS` to `active_rows` filter and
`rows_trading` list. **Deployed 2026-07-03.**

---

## G10 — Partial BID_ASK CSV counted as "done" in ETA calculator

**Symptom:** ETA shows "MNQ May 6 done" and skips it when it's actually 3% complete,
making the ETA wildly underestimate remaining work.

**Cause:** `api_fetch_eta` scanned CSV files and counted any `bidask_*.csv` > 100 bytes
as done. But CSVs are written incrementally — a 4MB partial file triggered "done."

**Fix:** Only count a BID_ASK CSV as done if no unfinished DB row exists for the same
(sym, date). Added `active_bidask_keys` exclusion set. **Deployed 2026-07-04.**

---

## G11 — ETA rate shows "3k/min" after file switch (was "24k/min")

**Symptom:** After the first BID_ASK file finishes and the next one starts, the ETA
rate drops from "24k/min" to "3k/min", making the ETA wrong.

**Cause:** Rate was computed as `active_file_records / scheduler_lock_age`. After file
switch, new file has few records but lock_age is large (scheduler ran for hours) → tiny rate.

**Fix:** Use `_fetch_throughput_hist` (rolling 5min window) or `_fetch_rate_state`
(per-slot rolling window) instead of lock-based rate. **Deployed 2026-07-04.**

---

## G12 — fetch_progress table missing error at scheduler startup

**Symptom:** Scheduler log: `WARNING: Could not query partial files: no such table: fetch_progress`

**Cause:** Race condition: `_get_priority_dates()` ran before the progress DB was fully
initialized.

**Fix:** `_ensure_progress_db()` is called before any query. The warning is harmless —
the scheduler continues without the partial-file list (P2 priority). **Known, not critical.**

---

## G14 — Watchdog false-kills healthy scheduler after file transition

**Symptom:** Scheduler finishes a large BID_ASK file (e.g., MNQ May 6 done at 07:33 UTC),
immediately watchdog fires "stale 643 min" and kills it — even though the scheduler was fine
and was about to begin the next target.

**Cause:** `_progress_age_seconds()` queried `WHERE finished=0 ORDER BY updated_at DESC LIMIT 1`.
When MNQ May 6 finishes (`finished=1`), the query now returns the NEXT queued target (MYM May 6),
which had `updated_at` from hours ago (never started yet). The watchdog read this as "fetcher
stuck 643 min" and killed the scheduler mid-transition.

**Fix:** Remove `WHERE finished=0`. Query ALL rows. `_mark_finished` always sets `updated_at=now`,
so the just-completed file row is the freshest and keeps the age low during transitions.
**Deployed 2026-07-05.**

---

## G15 — Watchdog kills healthy scheduler on restart (STALE_KILL_THRESHOLD too short)

**Symptom:** After watchdog restarts, it immediately kills the scheduler. Scheduler was running
fine (e.g., MNQ May 7 BID_ASK W0 in progress, started 14 min ago). No records logged yet
because W0 takes 32 min. Watchdog sees age=840s > STALE_KILL_THRESHOLD=300s → kill.

**Cause:** 5-min kill threshold is shorter than a single IB window request for large BID_ASK
files. W0 for MNQ takes 32 min; W2 can take 2-4 hours. The reporter updates DB every 15s
WHILE INSIDE `paginate_ticks`, but `_mark_started` only writes once at the start — so the
gap between `_mark_started` and first reporter heartbeat can be > 5 min if IB is slow.

**Fix:** Increased STALE_KILL_THRESHOLD from 300s to 1200s (20 min). IB's slowest legitimate
response is < 15 min. Any 20-min silence is a true hang. Combined with G14 fix (all-rows
query), false kills from file transitions are also eliminated.
**Deployed 2026-07-05.**

---

## G13 — Duplicate schedulers accumulate after dashboard restarts

**Symptom:** Multiple `fetch_scheduler.py --backfill` processes running simultaneously.
Each uses an IB connection slot, causing pacing conflicts and data corruption risk.

**Cause:** Each call to "start scheduler" (from Task Scheduler, from watchdog, from manual)
creates a new process. Without a reliable lock check, multiple instances accumulate.

**Fix:** `fetch_scheduler.py` checks for existing lock at startup and exits if another
instance is alive. The `fetch_watchdog.py` kills duplicate instances (keeps lowest PID).
**Deployed 2026-07-03.**

---

## Process Startup Checklist (overnight operation)

Run in order, from `C:\Projects\Galgo2026\june`:

```
# 1. Gateway watchdog (handles IBC restart)
python trader/gateway_watchdog.py

# 2. Fetch watchdog (handles scheduler restart + stale detection)
python trader/fetch_watchdog.py

# 3. Fetch scheduler (starts automatically, watchdog ensures it stays up)
python trader/fetch_scheduler.py --backfill

# 4. Dashboard (for monitoring only — not required for fetching)
cd C:\Projects\Galgo2026
python trader/visualizer/app.py
```

The **fetch_watchdog** is the most important for overnight reliability:
- Detects gateway down → restarts IBC
- Detects no scheduler → restarts scheduler
- Detects fetch hung 30+ min (new!) → kills stuck scheduler, restarts fresh
- Kills duplicate schedulers

---

## G16 — Heartbeat masks IB stall from watchdog

**Symptom:** Dashboard shows 0 records/min and 0 records/hour. DB `updated_at` refreshes
every ~15s but `records_fetched` is frozen. Watchdog sees age < 20s and does nothing.
Scheduler process is alive at 0% CPU — stuck waiting for IB to return ticks.

**Cause:** The fetcher's reporter thread updates `updated_at` every 15 seconds regardless
of whether new ticks arrived. The watchdog's `_progress_age_seconds()` checked `updated_at`
freshness, so a live heartbeat masked a completely stalled fetch from the kill logic.

**Fix:** Changed `_progress_age_seconds()` to track `records_fetched` delta on the active
(finished=0) row. If record count doesn't change, the "age" grows until it hits
STALE_KILL_THRESHOLD (20 min) and the watchdog kills and restarts the scheduler.
Falls back to `updated_at` when no active row exists (G14 safety for file transitions).
**Deployed 2026-07-06.**

---

## G17 — Watchdog process dies → 12+ hours dark with no auto-restart

**Symptom:** System completely offline for ~12 hours. No fetch progress. Gateway may have bounced.
Watchdog log shows long silence (hours gap between entries). Scheduler log goes cold.

**Cause:** The `fetch_watchdog.py` process runs in a console window started manually.
If that window is closed, the PC sleeps/hibernates, or the process crashes from an unhandled
OS-level signal, the watchdog dies. With no watchdog, a stuck or dead scheduler is never restarted.

**Timeline (2026-07-05 → 07-06):**
- 19:05 UTC: Gateway DOWN. Watchdog restarted it (first attempt failed/90s timeout, second OK).
- 19:19 UTC: Gateway DOWN again 10 min later. Watchdog restarted again.
- 19:23 UTC: Gateway back up — watchdog died immediately after (unknown cause).
- 19:23 → 07:29 next day: ~12h dark. No scheduler restarts, no fetch progress.
- 07:29: User manually noticed and restarted scheduler.

**Fix:**
1. Added single-instance guard to `fetch_watchdog.py`: if another watchdog is already running,
   the new process exits immediately. Safe to call from Task Scheduler every 5 min.
2. Added `GalgoFetchWatchdog` Windows Task Scheduler task (every 5 min) via `install_scheduler.ps1`.
   The task fires every 5 minutes and exits in <1s if watchdog is healthy. If watchdog is dead,
   it starts a new one — maximum dark time is now 5 minutes instead of unlimited.
3. Created `scripts/run_fetch_watchdog.bat` for the Task Scheduler action.

**Deployed 2026-07-06.**

---

## G18 — Zombie PID blocks scheduler restart after watchdog kill

**Symptom:** Watchdog kills stuck scheduler. New scheduler never starts. System stays dark
indefinitely. Watchdog log shows no SCHEDULER_RESTARTED after the kill.

**Cause (two bugs, both needed):**

**Bug 1 — Zombie PID in lock file (`psutil.pid_exists` false positive):**
After killing a Python process on Windows, `psutil.pid_exists(killed_pid)` may return True
for several seconds while the OS reaps the zombie. `_clean_stale_lock()` called
`psutil.pid_exists()` and concluded the old scheduler was still alive — lock not cleaned.
The new scheduler saw the lock, checked `pid_exists` → True → exited immediately.
The watchdog thought it had restarted the scheduler; in fact the new process exited in <1s.

**Bug 2 — PowerShell cmdline false-positive in single-instance guard:**
`_watchdog_already_running()` searched for any process with "fetch_watchdog" AND "python"
anywhere in its cmdline string. PowerShell `-Command` invocations embed the entire script body
as a single string — a PowerShell that ran `Get-WmiObject | Where { $_.CommandLine -match
'fetch_watchdog|fetch_scheduler' -and 'python' }` matched both patterns. Every new watchdog
start saw the PowerShell process as a "running watchdog" and exited immediately.

**Fix:**
- `_clean_stale_lock()` and `_acquire_lock()`: use `psutil.Process(pid).status()` to confirm
  process is not zombie/dead before treating the lock as valid.
- `_watchdog_already_running()` and `_scheduler_pids()`: check `cmd_list[0]` (the executable)
  contains "python" before scanning the joined cmdline. Filters out PowerShell processes.

**Deployed 2026-07-07.**

---

## Rate & Time Estimates

| Symbol | TRADES records (typical) | BID_ASK records | BID_ASK time @ 24k/min |
|--------|--------------------------|-----------------|-------------------------|
| MES    | ~300-400k                | ~3-5M           | ~2-4h                   |
| MNQ    | ~600k-1.5M               | ~6-15M          | ~4-10h                  |
| MYM    | ~60-120k                 | ~600k-1.5M      | ~30-90min               |

BID_ASK ratio vs TRADES: ~9.5x (measured from June 2026 data).
Fetch rate: ~24k records/min = 400 rec/sec (IB pacing limited).
