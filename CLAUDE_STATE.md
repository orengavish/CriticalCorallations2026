# Claude State — CriticalCorallations2026
> **Living doc. Update every time scope changes, a task completes, or context shifts.**
> Last updated: 2026-07-15

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

Current version: **v4.00** — Sandbox tab with price-level microstructure profile

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
- [ ] **CL Algo pipeline** — `back-trading/run_cl_algo_pipeline.py` — runs after 17:00 when tick data is available. Command: `python back-trading/run_cl_algo_pipeline.py --symbol MES --verbose`
  - Pending: june/ path compatibility fix
  - Pending: next-run grid in UI
  - Pending: Monte Carlo guard at N≥30

### Infra (needs admin action by Oren — run both once)
- [ ] Install CC2026 Task Scheduler (auto-start port 5003 on boot):
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
v3.12: Draw mode popup on dblclick — Support/Resistance color buttons, green/red lines
v3.11: Fix Draw mode — remove !important, timed dblclick, robust _pixelToPrice fallback
v3.10: Transpose bars mode — price on Y axis, ticks on X
v3.9:  Draw mode wired — dblclick add/remove, click to name, Auto gray toggle, Save/Send
v3.8:  collapse header to single 40px bar
```
