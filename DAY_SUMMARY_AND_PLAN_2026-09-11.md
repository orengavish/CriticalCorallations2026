# Day Summary (2026-09-11)
> v1.0 — 2026-09-11 (created)

## Bugs found and fixed

1. **Broker re-fetched the same symbol's price once per PENDING COMMAND, not once per
   cycle** — `IBClient.get_price()` did a fresh `reqMktData` + 1.5s sleep + `cancelMktData`
   on *every* call. A single MYM cycle with 8 pending commands cost 12s of pure blocking
   waits for what was, in practice, the same price fetched moments apart; `decider.py`'s
   replenishment loop paid ~51s/cycle across all 34 symbols for the same reason (the
   documented reason `replenishment_poll_seconds` was raised to 90 in the first place).
   Fixed: `get_price()` now keeps one persistent streaming subscription per symbol
   (`lib/ib_client.py`) — only the first call per symbol pays the 1.5s wait, every call
   after reads the already-live ticker synchronously. Confirmed live: 8 identical price
   fetches for MYM completed within the same second, all returning the identical value.
   Requires callers with polling loops to use `ibc.live.sleep(...)` instead of
   `time.sleep(...)` between iterations (services the event loop for free during the same
   idle wait) — applied to `broker.py`'s main loop, `decider.py`'s replenishment loop, and
   `price_feed.py`'s background poller.
2. **Futures waited on the stock-market gate for no reason** — `decider.py`'s
   `run_session_start()` had one combined wait over the entire symbol list before
   generating any commands at all. Since futures markets are open essentially the whole
   time decider runs, this meant decider restarts at 8:00 AM IL and then sits idle for
   ~9h doing nothing for futures until the shared ~17:00 IL gate cleared. Fixed: futures
   now generate immediately, unconditionally, on every decider start; stocks keep the
   existing gate (with a same-day override, see Day Start panel below).
3. **`DeciderDailyRestart` scheduled task registered out-of-band, failing every day** —
   not in `scripts/install_scheduler.ps1` at all, `ERROR_FILE_NOT_FOUND` (bare `python.exe`
   with no resolvable PATH/working directory in the task's own run context — the same
   class of bug as Fetcher2026's 19-day outage). Fixed and properly registered.
4. **Dashboard's admission-cap "At cap (held back)" stat was simply wrong** — its own
   same-side-only approximation (`COUNT(SUBMITTED) >= cap`) silently disagreed with
   `broker.py`'s real gate (same-side entries + 2x OPPOSITE-side entries). Confirmed live:
   reported "none held back" while M2K/MYM SELL commands were actually held back at
   ~11/10 resting every single cycle. Fixed by extracting the real gate math into
   `lib.db.compute_side_resting()` and having both broker.py and the dashboard call the
   same function — can't drift apart again.
5. **`_restart_decider_process()` (Day Start's Force buttons) was silently broken two
   ways** — it read `decider.lock`'s PID via a plain file read, which fails under
   Windows' mandatory byte-range lock while decider actually holds it (i.e. always in
   production), so it reported "not running" on every call regardless of reality; and
   even fixed, it only ever killed the process, relying on a `session.py` supervisor to
   respawn it that isn't actually running in this environment — a bare kill would have
   left command-generation dead with nothing bringing it back. Fixed by reusing
   `restart_decider_daily.py`'s self-sufficient kill-and-respawn.
6. **`_fetch_live_prices()` (Algo Lab's price chips, and today's new Submitted-row price
   gap) silently returned "unavailable" for every symbol, always** — same root cause as
   #5's first half: calling `IBClient` directly on a Flask request thread hits
   `ib_insync`'s "no current event loop in thread" failure, caught per-symbol and never
   logged. Fixed by running it as a subprocess (new `prices` mode on
   `trader/scripts/ib_dayclean.py`).
7. **`archive_and_delete_commands()` couldn't actually delete any CLOSED command** —
   `positions`/`completed_trades` foreign-key onto `commands.id` with no handling for
   that; deleting a command with FK children raised `IntegrityError`. Fixed by deleting
   the child rows first (the parent's full row is already preserved in
   `commands_archive`, so nothing is lost).
8. **704 legacy rows with `source='critical_line'`** (source mislabeled by a decider.py
   bug fixed 2026-09-09, before the fix propagated the line's real source) archived and
   removed from the live `commands` table — all `CANCELLED`/`CLOSED`, none open, all from
   the 2026-09-09 incident.

## Root-caused, not yet fixed (separate project)

**Fetcher2026's futures bar-fetch pipeline has been dead for a week.** All 4 futures
symbols' 1s-resolution bar CSVs (`Fetcher2026/data/bars1s/`) stop dead at 2026-09-04 —
confirmed by direct file listing, not a query artifact. `Fetcher2026/data/
bars_watchdog_supervisor.log`'s last entry is 2026-07-24: `bars_fetch_watchdog.py exited
(code=1073807364) — restarting in 10s`, then nothing — that supervision chain appears to
have died 7 weeks ago (data kept flowing until Sep 4 via some other since-stopped manual
restart). This is what caused today's futures line extraction to silently find lines for
only 2 of 4 symbols — the "previous day" reference every symbol's Algo 1/2 calculation
depends on is a week stale for all four, and it was coincidence which two symbols'
patterns still happened to exact-match a winning reason against stale data. **Not fixed
today** — a real, separate investigation into Fetcher2026's supervisor chain.

**Naked-position safety net fired 6 times on today's broker restart** — AMD, GOOGL, XOM,
QCOM, TSLA, MSFT all had open positions with no resting protective stop; broker's existing
`reconcile_naked_positions()` placed emergency stops for all 6 automatically on restart.
Not caused by anything changed today (this runs on every broker startup, unconditionally)
but worth knowing why those 6 went naked in the first place — not investigated today.

## Built

- **Day Start panel** (Broker tab): 7 buttons, all server-gated (grayed out whenever
  there's nothing for them to do) — Verify, Clean DB noise, Cancel & Flatten IB, Force
  Futures Now, Force All Symbols Now, Extract Futures Lines, Extract Stock Lines — plus a
  live extraction-coverage summary (N/4 futures symbols with lines + per-symbol count,
  N/30 stock symbols + total line count). Auto-refreshes every 15s while the Broker tab
  is open.
- **Broker screen status columns**: every Pending/Submitted/Filled row now shows *why* —
  Pending shows the real admission-cap status (`at cap (N/cap resting)` or `queued`),
  Submitted shows the live price gap to entry (`price X, needs to rise/fall Y to entry
  Z`), Filled shows `open position`.
- Dashboard version discipline formalized: every edit to `trading_dashboard.py` now bumps
  its `verchip` badge + adds a `_RELEASE_NOTES` entry (now at **v5.05**, was v5.03 this
  morning) — a standing rule going forward, not a one-off.

## Open items for you

- Fetcher2026's dead bar-fetch supervisor (see above) — needs its own session to
  diagnose why `bars_watchdog_supervisor.py` stopped restarting `bars_fetch_watchdog.py`
  after 2026-07-24, and why data kept flowing until 09-04 anyway (a manual restart
  somewhere not logged by that supervisor).
- Why 6 stock positions (AMD, GOOGL, XOM, QCOM, TSLA, MSFT) went naked before today's
  broker restart — not urgent (the safety net caught it) but worth knowing the mechanism.
- `_fetch_live_prices()` now shells out to a subprocess on every call — used by the
  Submitted-row price gap, which the Broker screen polls every 5s. For however many
  distinct symbols have SUBMITTED orders, that's a new OS process + a fresh IB
  connect/disconnect cycle every 5 seconds, indefinitely, while the tab is open — a real,
  ongoing resource/connection-churn cost, not just one-time latency. Worth a persistent
  background price cache (mirroring `trader/visualizer/price_feed.py`'s existing pattern)
  if the Broker tab is typically left open for long stretches.
