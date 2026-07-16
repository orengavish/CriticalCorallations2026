# Claude State — CriticalCorallations2026
> **Living doc. Update every time scope changes, a task completes, or context shifts.**
> Last updated: 2026-07-16

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

Current version: **v4.11** — Session manager (Start/Stop broker+decider from dashboard)

**trader/broker.py and trader/decider.py are no longer homeless.** `trader/session.py` supervises
them as managed subprocesses (spawn, stdout capture to `trader/logs/{broker,decider}_stdout.log`,
crash-restart with backoff, clean shutdown via SESSION=SHUTDOWN). The dashboard's top bar has a
Start/Stop Session button + live Broker/Decider status badges, backed by
`GET/POST /api/session/{status,start,stop}`.

**I have NOT yet actually clicked Start Session against real IB** — that launches real order-
generation processes (paper account, but still). `trader/session.py --self-test` proves the
supervision mechanics in isolation (fake stand-in scripts, no real IB/DB). Starting a real session
is Oren's call.

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

## My Brother — Fetcher2026

- Lives at: `C:\Projects\Fetcher2026`
- Has his own Claude session (separate project in Claude Code)
- Runs: `dashboard.py` on port **5050**
- His job: pull tick data from IB (paper port 4002) → writes CSVs to `C:\Projects\Fetcher2026\data\history\`
- My job: read those CSVs to build graphs, run backtests, show lines

**Dependency:** I need Fetcher running to get fresh data. If graphs are empty or stale, check port 5050 first.

Also exists: **Backtrader2026** at `C:\Projects\Backtrader2026` (its own Claude session, backtesting system)

These three were split out from the old `C:\Projects\Galgo2026\june\` monolith on 2026-07-10.

---

## Planned Next Steps

### Active / Short Term
- [x] **Session manager** — `trader/session.py` + dashboard Start/Stop Session button (v4.11, 2026-07-16). Note: `trader/runner.py` already existed and does something broader (launches decider/broker/position_manager/**visualizer**/fetch_scheduler/random_gen with its own crash-restart) but isn't wired to anything and includes the forbidden legacy visualizer — session.py is deliberately narrower (broker+decider only) and dashboard-integrated. Left runner.py untouched; worth deciding later whether to retire it.
- [ ] **CL Algo pipeline** — `back-trading/run_cl_algo_pipeline.py` — runs after 17:00 when tick data is available. Command: `python back-trading/run_cl_algo_pipeline.py --symbol MES --verbose`
  - [x] Monte Carlo guard at N≥30 — learner won't declare CONVERGED until the top combo has ≥30 fills on all 3 most recent scoring runs, even if the fingerprint looks stable (`cl_algo_learner.py`, v4.10)
  - Deferred: june/ path compatibility fix — `trading_dashboard.py` / `lib/price_profile.py` still read from `C:\Projects\Galgo2026\june\trader\data\history`. Checked 2026-07-16: that's still where live data actually lands — `Fetcher2026\data\history` is empty despite its fetch_progress.db/lock existing. Do NOT switch until Fetcher2026 is confirmed writing there, or the dashboard goes dark.
  - Pending: next-run grid in UI — no spec yet, needs design input before building

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
v4.11: Session manager (trader/session.py) + dashboard Start/Stop Session + broker/decider status
v4.10: Monte Carlo N>=30 guard on learner convergence; install_scheduler.ps1 honest error reporting
v4.09: price profile module, DB scheduler updates, Task Scheduler install script
v3.12: Draw mode popup on dblclick — Support/Resistance color buttons, green/red lines
v3.11: Fix Draw mode — remove !important, timed dblclick, robust _pixelToPrice fallback
v3.10: Transpose bars mode — price on Y axis, ticks on X
v3.9:  Draw mode wired — dblclick add/remove, click to name, Auto gray toggle, Save/Send
v3.8:  collapse header to single 40px bar
```
