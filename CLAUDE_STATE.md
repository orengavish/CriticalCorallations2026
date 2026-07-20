# Claude State — CriticalCorallations2026
> **Living doc. Update every time scope changes, a task completes, or context shifts.**
> Last updated: 2026-07-20

---

## Who Am I

I am the Claude instance for **CriticalCorallations2026** — the algo analysis and trading dashboard brain.

- Repo: `C:\Projects\CriticalCorallations2026` (GitHub remote: `cc2026`)
- Owner: Oren Gavish — solo quant trader, micro-futures (MES, MNQ, MYM, M2K), IB paper port 4002
- Working style: Oren sets a goal and disappears. I work autonomously and email updates to gavish.oren@gmail.com

---

## What I Run

**One app. One port. That's it.**

| App | Port | Command |
|-----|------|---------|
| `back-trading/trading_dashboard.py` | **5003** | `python back-trading/trading_dashboard.py` |

Tabs: **Lines \| Graph \| Create Trades \| Submitted**

DB: `trader/data/galao.db`
History data (tick CSVs): read from `C:\Projects\Fetcher2026\data\history\` (owned by brother)

Current version: **v4.20** — All tab: unified Day/Week/Month/2mo/6mo/Year range presets

**Session manager is live and proven.** `trader/session.py` supervises broker.py + decider.py as
managed subprocesses (stdout to `trader/logs/{broker,decider}_stdout.log`, crash-restart with
backoff, clean shutdown via SESSION=SHUTDOWN). Dashboard top bar: Start/Stop Session button + live
Broker/Decider badges, via `GET/POST /api/session/{status,start,stop}`. Actually started for real
multiple times since — one run went ~2.9 days continuously with no crashes. Restarting the session
(stop then start) is required after any trading_dashboard.py/decider.py/broker.py/lib/db.py code
change to pick it up — same for restarting the dashboard process itself after a trading_dashboard.py
change (they're independent processes; edits on disk don't hot-reload).

**Known incident (2026-07-17, fixed in v4.17):** repeated session restarts while deploying that
day's versions piled up 425 stale MES commands stuck at status=SUBMITTED (IB itself showed 0 open
orders — pure DB desync). Root cause: `decider.py`'s `generate_commands()` had no dedup, so every
`run_session_start()` call re-generated a full batch regardless of what was already unresolved.
Fixed with a dedup guard (skips any (line, direction, bracket) combo already in flight) — verified
via extended self-test. Also hit and fixed a genuine race in `lib/db.py`: broker.py and decider.py
both call `init_db()` on startup, and session.py launches them near-simultaneously, so their
concurrent `verified_trades` view recreation could collide and crash broker on startup (session.py's
own crash-recovery caught it live before the fix landed). Point: **always restart the session after
touching decider.py/broker.py/lib/db.py**, and don't be surprised by a `broker: restarting` blip
right after a restart — check `trader/logs/broker_stdout.log` if it doesn't settle to `running`.

**Cross-dashboard menu (v4.11, fixed v4.18/v4.19):** a 🔗 icon exists on all three sibling
dashboards (this one, Fetcher2026 :5050, GevaExtract :5005 — see below), linking to each other via
`location.hostname` so it works unchanged from localhost/LAN/Tailscale. Lesson learned the hard way:
`#top-bar` originally had `overflow:hidden`, which silently clipped the icon off-screen once enough
controls piled up in that one row (fixed with `overflow-x:auto` in v4.18) — and a Bootstrap
`position:absolute` dropdown popup gets clipped by an ancestor's `overflow-y:hidden` even when the
toggle button itself is visible and clickable (fixed by switching to a custom `position:fixed`
dropdown in v4.19, positioned via `getBoundingClientRect()` at click time). If you add another
control to that top bar and something "does nothing" when clicked, check for this exact clipping
pattern before assuming the JS is broken.

**Config gotcha for anyone touching trader/session.py or importing it into another process:**
there are TWO config.yaml files — `trader/config.yaml` (live trading engine) and
`back-trading/config.yaml` (separate backtest engine, different DB/ports). `lib.config_loader`
picks whichever is nearest the *launching* script and caches it globally on first call, ignoring
path args after that. session.py works around this by loading `trader/config.yaml` by its own
file location and by NOT calling `get_logger()` with ambient config resolution (passes `log_dir`
explicitly) — otherwise importing session.py into the dashboard (which lives in back-trading/)
silently loads the wrong config. If you add a new trader/*.py module that gets imported
cross-directory, watch for this.

**Do NOT start `trader/visualizer/app.py` (port 5001) or `back-trading/algo_dashboard.py` (port 5002).** Those are legacy/wrong.

---

## Siblings — Fetcher2026 and GevaExtract

**Fetcher2026** — `C:\Projects\Fetcher2026`, own Claude session, `dashboard.py` on port **5050**.
Pulls tick data from IB (paper port 4002) → writes CSVs to `C:\Projects\Fetcher2026\data\history\`.
**As of ~2026-07-17 it only keeps a rolling ~23.5h TRADES window**, not long history — this is why
Long View (below) had to backfill its own separate `trader/data/bars.db` via IB directly rather than
extending the tick-CSV path for anything beyond ~10 days. If graphs are empty/stale, check port 5050
first. That Fetcher session has also directly edited files in *this* repo before (e.g. added the
Long View feature, v4.20) — cross-repo work does happen here, verify with `git diff`/`git status`
before trusting a handoff summary at face value.

**GevaExtract** — `C:\Projects\GevaExtract`, Node.js, `server.js` on port **5005**. Scrapes Hebrew
Facebook posts for support/resistance lines, and — important — **has its own Trades/Sub/Monitor tabs
that write into the same `trader/data/galao.db` `commands` table** that this dashboard's broker.py
polls, and even reads this dashboard's `/api/session/status`. Two independent trade-creation UIs on
one shared DB. Not a bug, but worth remembering if commands show up you didn't create from here.

Also exists: **Backtrader2026** at `C:\Projects\Backtrader2026` (its own Claude session, backtesting system)

These were split out from the old `C:\Projects\Galgo2026\june\` monolith on 2026-07-10.

**All three (+ this dashboard) now cross-link via the 🔗 menu** — see above.

---

## Planned Next Steps

### Active / Short Term
- [x] **Session manager** — `trader/session.py` + dashboard Start/Stop Session button (v4.11, proven live v4.17+). Note: `trader/runner.py` already existed and does something broader (launches decider/broker/position_manager/**visualizer**/fetch_scheduler/random_gen with its own crash-restart) but isn't wired to anything and includes the forbidden legacy visualizer — session.py is deliberately narrower (broker+decider only) and dashboard-integrated. Left runner.py untouched; worth deciding later whether to retire it.
- [x] **Long View** (v4.20) — `trader/data/bars.db` (1yr of 30-min bars, MES/MYM/M2K, backfilled via `scripts/backfill_bars.py --port 4001`), `GET /api/bars-long`, unified into the All tab's Day/Week/Month/2mo/6mo/Year preset row. Bars only go to whenever backfill_bars.py was last run — no auto-update yet. Next ideas from the Fetcher session that built this: an "Update bars" button appending today from tick CSVs, R² display on pair charts, volume-profile overlay.
- [ ] **Stray duplicate work to reconcile**: `trader/visualizer/app.py` (the forbidden legacy port-5001 file) and its templates also gained a near-identical `/api/bars` route + `all.html` template from the same Fetcher session that built Long View — left uncommitted/untouched (not part of v4.20). Worth a decision: discard, or was it intentional?
- [ ] **CL Algo pipeline** — `back-trading/run_cl_algo_pipeline.py` — runs after 17:00 when tick data is available. Command: `python back-trading/run_cl_algo_pipeline.py --symbol MES --verbose`
  - [x] Monte Carlo guard at N≥30 — learner won't declare CONVERGED until the top combo has ≥30 fills on all 3 most recent scoring runs, even if the fingerprint looks stable (`cl_algo_learner.py`, v4.10)
  - Deferred: june/ path compatibility fix — `trading_dashboard.py` / `lib/price_profile.py` still read from `C:\Projects\Galgo2026\june\trader\data\history`. As of 2026-07-16 that's still where live data actually lands — `Fetcher2026\data\history` was empty. Re-check given Fetcher's ~23.5h rolling window change — may have changed again. Do NOT switch until confirmed, or the dashboard goes dark.
  - Pending: next-run grid in UI — no spec yet, needs design input before building
- [ ] **CC2026's own Sub tab has no P&L calculation** — found while diagnostic-testing trades (2026-07-17). GevaExtract's separate Monitor tab has P&L; this dashboard's Sub tab only shows status/fill_price. Not broken, just missing — ask if wanted.

### Infra (needs admin action by Oren — run both once)
- [ ] Install CC2026 Task Scheduler (auto-start port 5003 on boot). Note: `install_scheduler.ps1` was fixed 2026-07-16 — it used to print "OK" even when task/firewall registration silently failed (non-elevated shells swallowed the errors). Now it checks real success and reports FAILED honestly. Still must be run elevated:
  `Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\CriticalCorallations2026\scripts\install_scheduler.ps1'`
- [ ] Install Fetcher2026 Task Scheduler (brother's job):
  `Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\Fetcher2026\scripts\install_scheduler.ps1'`
- [ ] 24-hour stability test after both schedulers installed

### Sandbox tab (v4.00 — in progress)
- [x] DB: `price_profile` table — per-price microstructure for each (symbol, date)
- [x] `lib/price_profile.py` — builder with DB-controlled staleness detection
- [x] `GET /api/sandbox/profile/<symbol>/<date>` — API endpoint
- [x] Sandbox tab UI — table view, sortable columns, green/red S/R coloring
- [x] Line creation auto-triggers profile build in background thread
- [ ] Next: overlay lines on price profile; algo comparison view; bar chart of price levels

---

## How to Start Fresh After Reboot

**After install_scheduler.ps1 is run (once, as admin) — nothing to do. Port 5003 auto-starts within 5 min of boot.**

Manual fallback (if scheduler not installed):
```
# This project (CC2026):
python back-trading/trading_dashboard.py     # port 5003

# Brother (do in his Claude session):
cd C:\Projects\Fetcher2026
python dashboard.py                          # port 5050
```

---

## Recent Git History (for context)
```
v4.20: All tab -- unified Day/Week/Month/2mo/6mo/Year range presets (+ Long View integration)
v4.19: Fix cross-dashboard menu click doing nothing (invisible dropdown, position:fixed rewrite)
v4.18: Fix top bar overflow clipping controls (incl. the menu) off-screen
v4.17: Fix 425 stale MES orders + decider.py dedup guard + lib/db.py race condition
v4.16: Overlay mode -- multi-day span (up to 10 trading days) with Days control
v4.15: All tab -- remove MNQ, add pairwise-diff opportunity panel
v4.14: All/Overlay -- 30s interval, hourglass while loading, auto bar-resolution on zoom
v4.13: Sandbox -- thinner bars
v4.12: Noticeable hourglass overlay during busy operations
v4.11: Session manager (trader/session.py) + dashboard Start/Stop Session + broker/decider status
v4.10: Monte Carlo N>=30 guard on learner convergence; install_scheduler.ps1 honest error reporting
v4.09: price profile module, DB scheduler updates, Task Scheduler install script
```
Full history: `git log --oneline` or the in-app `_RELEASE_NOTES` list in trading_dashboard.py.
