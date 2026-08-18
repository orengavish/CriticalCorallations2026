# CriticalCorallations2026 (CC2026) — Galgo2027 Handoff Document
> Complete briefing for a fresh Claude instance. No prior context needed.
> Written: 2026-07-21 | Version at time of writing: v4.26
> **CORRECTIONS ADDED 2026-08-18 — READ THIS FIRST.** The dashboard shipped 8 more versions
> (v4.27 → v5.03) after this doc was written, without anyone updating it. The most
> significant drift: **§6 (Dashboard GUI) below describes the All tab's overlay+diff panel
> and Long View bars.db integration as current — both were fully deleted in v5.03.** The
> nav structure also changed completely (flat tabs → left rail + contextual top tabs). New
> functionality this doc doesn't mention at all: Algo Lab, Sup/Res Viz, and Correlation
> tabs (v4.27+), a real price feed (v5.02), and 7 years of bars.db history (v5.01). See
> `CLAUDE_STATE.md`'s 2026-08-18 section for the full accurate rundown, and treat §6 below
> as historical/wrong rather than current. The rest of this doc (DB schema, config, rules,
> git, permissions) is still substantially accurate as of 2026-08-18 — only the GUI/nav
> section and the version number are known-stale.

---

## 1. PURPOSE

CriticalCorallations2026 is the **algo analysis and live paper-trading dashboard**. It:
- Runs broker.py (submits bracket orders to IB paper gateway)
- Runs decider.py (generates commands from critical S/R lines)
- Provides a web dashboard (port 5003) for analysis, trade management, and system control
- Reads tick-CSV history (written by Fetcher2026) for chart analysis
- Owns `galao.db` — the shared trading database also read/written by GevaExtract

**Not:** it does not fetch tick data (that's Fetcher2026) and does not scrape Facebook (GevaExtract).

---

## 2. DISK LAYOUT

```
C:\Projects\CriticalCorallations2026\
  back-trading/
    trading_dashboard.py     MAIN APP — port 5003 — Flask + in-process Flask
    config.yaml              Backtest/dashboard config (separate from trader/config.yaml!)
    algo_dashboard.py        LEGACY — DO NOT START — wrong port 5002
    cl_algo_*.py             CL Algo pipeline scripts
    run_cl_algo_pipeline.py  CL Algo runner (runs after 17:00 CT)
    simulator.py             Tick-by-tick OCO bracket simulator
    engine.py, grader.py     Backtest engine + grader
    versions/                Simulator iteration snapshots
  trader/
    config.yaml              LIVE TRADING config (source of truth for broker/decider)
    broker.py                IB order submission — polls DB for PENDING commands
    decider.py               Generates commands from critical lines; replenishes after fills
    session.py               Process supervisor — manages broker + decider as subprocesses
    position_manager.py      Monitors positions, SL cooldown
    preflight.py             Pre-session checks
    fetch_scheduler.py       LEGACY from june/ monolith — NOT used (CC2026 has no fetcher)
    fetch_priority.py        LEGACY from june/ monolith — NOT used
    visualizer/app.py        LEGACY — DO NOT START — wrong port 5001
    data/
      galao.db               LIVE TRADING DB (WAL) — shared with GevaExtract
      bars.db                1-year 30-min bars (backfilled via scripts/backfill_bars.py)
      history/               Tick CSVs (LEGACY june/ path — CC2026 still reads from here)
      fetch_progress.db      Fetcher progress (CC2026 uses this via path derivation)
  lib/
    config_loader.py         Loads config.yaml — caches on first call, picks nearest file
    db.py                    SQLite schema: commands, positions, ib_events, verified_trades view
    ib_client.py             IB EClient/EWrapper connection manager
    ibc_launcher.py          IBC gateway launcher
    order_builder.py         Builds IB bracket orders (entry + TP + SL child orders)
    critical_lines.py        Parses critical line files, loads armed S/R levels from DB
    price_profile.py         Per-price microstructure builder (price_profile table)
    algo_engine.py           Algo engine (CL Algo pipeline)
    gdrive.py                Google Drive upload (not wired — enabled: false)
    data_availability.py     Data availability checker
    day_params.py            Day-level parameter store
  scripts/
    backfill_bars.py         Fills bars.db from IB (run: python scripts/backfill_bars.py --port 4001)
    build_bars_diffs.py      Builds bars_30m_diffs_normalized table in bars.db
    build_bars_normalized.py Builds bars_30m_normalized table in bars.db
    sanity_check_bars.py     Sanity checks for bars.db
    install_scheduler.ps1    Windows Task Scheduler installer (run ELEVATED — see §7)
  june/                      LEGACY monolith — archive candidate (see §12)
  CLAUDE_STATE.md            Living brief — keep updated every session
```

---

## 3. DATABASE (galao.db — the shared truth)

**Location:** `C:\Projects\CriticalCorallations2026\trader\data\galao.db`
**Mode:** WAL (Write-Ahead Logging) — supports concurrent readers + one writer

### Tables
| Table | Owner | Purpose |
|-------|-------|---------|
| `commands` | broker/decider/GevaExtract | All bracket orders: PENDING→SUBMITTED→FILLED→CLOSED |
| `positions` | broker | Active IB positions |
| `ib_events` | broker | Raw IB callback log |
| `system_state` | all | Key/value store (REPLENISH_ENABLED, etc.) |
| `critical_lines` | decider | Armed S/R levels from files |
| `price_cache` | broker | Last known IB prices per symbol |
| `bracket_map` | broker | Maps IB order IDs back to command_id |
| `price_profile` | dashboard | Per-price microstructure per (symbol, date) |
| `verified_trades` | VIEW | `CREATE VIEW verified_trades AS SELECT * FROM commands WHERE exit_reason IN ('TP','SL')` |

### bars.db
**Location:** `C:\Projects\CriticalCorallations2026\trader\data\bars.db`
- 1 year of 30-min OHLCV bars for MES, MYM, M2K (not MNQ — it's dropped from All tab)
- Tables: `bars_30m`, `bars_30m_normalized`, `bars_30m_diffs_normalized`
- Backfilled via `python scripts/backfill_bars.py --port 4001` (live port, not paper)
- No auto-update — must run script manually to extend

### galao.db write safety
- broker.py and decider.py both call `init_db()` on startup → concurrent `verified_trades` VIEW
  recreation can race and crash broker. Session.py's crash-recovery catches it. Just restart the
  session if broker shows "restarting" right after launch — it usually self-heals in <30s.

---

## 4. CONFIGURATION — TWO CONFIG FILES (CRITICAL GOTCHA)

There are **two separate config.yaml files**. `lib.config_loader` caches on first call and picks
the nearest file to the **launching script's directory**. If you import session.py into
back-trading/trading_dashboard.py, the wrong config may load silently.

| File | Used by | Key settings |
|------|---------|-------------|
| `trader/config.yaml` | broker.py, decider.py, session.py | IB port 4002, symbols, DB path, order params |
| `back-trading/config.yaml` | trading_dashboard.py, CL algo pipeline | Different DB paths, ports |

session.py deliberately loads `trader/config.yaml` by its own file location. Never call
`get_logger()` with ambient config resolution when importing session.py cross-directory.

### trader/config.yaml key values
```yaml
ib:
  live_port: 4002           # PAPER gateway — data AND orders
  live_client_ids: [101..120]
  ibc_startgateway_bat: "C:\\IBC\\StartGateway.bat"
  ibc_mode: paper

symbols: [MES]              # TRADING symbols only — decider generates commands for these

fetcher:
  symbols_override: [MES, MNQ, MYM]   # data symbols (independent of trading)
  fetch_bid_ask: true

session:
  max_restarts: 5
  restart_backoff_base_seconds: 5
  restart_backoff_cap_seconds: 60
  stop_grace_seconds: 20

orders:
  active_brackets: [2, 4]  # TP=2t, SL=4t (or [4,4] depending on config state)
  tick_size: 0.25

paths:
  db: data/galao.db         # relative to CWD when process launches
  history: data/history     # relative — reads Galgo2026 legacy path in practice
```

---

## 5. WORKING FUNCTIONALITY

### Session Manager (trader/session.py)
- Supervises broker.py + decider.py as managed subprocesses
- stdout redirected to `trader/logs/broker_stdout.log`, `trader/logs/decider_stdout.log`
- Crash-restart with exponential backoff (base 5s, cap 60s, max 5 restarts)
- Clean shutdown: sends `SESSION=SHUTDOWN` env signal, waits `stop_grace_seconds`, then kill()
- Dashboard start/stop: `POST /api/session/start` and `POST /api/session/stop`
- Status polling: `GET /api/session/status` → `{broker: running|stopped|restarting, decider: ...}`
- **ALWAYS restart session after editing broker.py, decider.py, lib/db.py, or session.py**

### Decider (trader/decider.py)
- Generates PENDING bracket commands from critical S/R lines in galao.db
- Dedup guard: skips any (line, direction, bracket) combo already in flight (fixed v4.17)
- Replenishes after fills: watches for FILLED commands and generates replacements
- Mode: `python trader/decider.py --mode session` (session-managed) or standalone

### Broker (trader/broker.py)
- Polls `commands` table every 5s for PENDING rows
- Submits IB bracket orders (3-order OCO: entry + TP child + SL child)
- Tracks fills via `execDetails` callback → updates commands to FILLED/CLOSED
- Stores `bracket_map` so fills can be traced back to command_id across reconnects
- Naked position reconciliation on startup (v4.23) — closes orphaned positions

### CL Algo Pipeline (back-trading/run_cl_algo_pipeline.py)
- Runs after 17:00 CT when tick data is available
- Command: `python back-trading/run_cl_algo_pipeline.py --symbol MES --verbose`
- Learner convergence requires N≥30 fills on 3 most recent scoring runs (v4.10 Monte Carlo guard)
- Does NOT auto-run — must be triggered manually or scheduled separately

---

## 6. DASHBOARD GUI (back-trading/trading_dashboard.py — port 5003)

Browser: `http://localhost:5003`

**Start:** `python back-trading/trading_dashboard.py`
Changes to trading_dashboard.py require restarting the process (no hot reload).

### Top Bar (always visible)
- Version badge (v4.26)
- Start Session / Stop Session button → broker+decider liveness
- Broker badge (running / stopped / restarting) + Decider badge
- 🔗 cross-dashboard menu: CC2026 (5003), Fetcher2026 (5050), GevaExtract (5005)
  - Uses `location.hostname` — works from localhost/LAN/Tailscale
  - Implemented as `position:fixed` dropdown (NOT Bootstrap dropdown — that gets clipped by
    overflow:hidden ancestors). getBoundingClientRect() at click time for positioning.

### Tab: Lines
- Table of critical S/R lines from galao.db `critical_lines` table
- Strength indicators, armed/disarmed status

### Tab: Graph
- Price chart using bars.db / tick CSV data
- Day/Week/Month/2mo/6mo/Year range presets (unified row, v4.20)
- Long View uses bars.db (30-min bars); short ranges use tick CSVs

### Tab: All
- Multi-symbol overlay chart: MES, MYM, M2K (MNQ removed v4.15)
- Diff panel: pairwise-normalized diff chart (bars_30m_diffs_normalized)
- Symbols checkboxes per chart (v4.26)
- Day/Week/Month/2mo/6mo/Year presets
- Zoom-sync between overlay and diff panels (v4.24)

### Tab: Create Trades
- Manual bracket order builder — select symbol, direction, line, bracket size
- Preview entry/TP/SL prices before submitting
- Submits to galao.db commands table as PENDING

### Tab: Submitted
- All commands from galao.db (all statuses, all sources including GevaExtract)
- Color-coded status pills
- **No P&L calculation in this tab** (GevaExtract's Monitor tab has P&L — not replicated here yet)

### Tab: Sandbox
- Sandbox analysis tab — price profile microstructure per (symbol, date)
- Sortable columns, green/red S/R coloring
- Line creation triggers profile build in background thread
- `GET /api/sandbox/profile/<symbol>/<date>` endpoint

---

## 7. SCHEDULERS AND WATCHDOGS

### Windows Task Scheduler — STATUS: NOT INSTALLED
Install script exists but must be run **elevated** (admin PowerShell):
```powershell
Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\CriticalCorallations2026\scripts\install_scheduler.ps1'
```
After install: port 5003 auto-starts within 5 min of Windows boot.

The script was fixed (v4.10) — it now reports FAILED honestly instead of printing OK when
firewall/task registration silently failed in a non-elevated shell.

### Manual start (until scheduler installed)
```powershell
cd C:\Projects\CriticalCorallations2026
python back-trading/trading_dashboard.py     # port 5003 — keep running
```

### Post-edit hook (settings.json)
Every Write or Edit tool call triggers:
```
python C:/Projects/galgo2026/.claude/hooks/selftest_on_edit.py
```
This runs `--self-test` on the modified module. If it fails, fix before continuing.

---

## 8. RULES AND INVARIANTS

1. **Paper only.** IB LIVE port 4001 is NEVER connected. All orders go through paper port 4002.
2. **One app, one port.** Only `back-trading/trading_dashboard.py` on port 5003. Do NOT start
   `trader/visualizer/app.py` (port 5001) or `back-trading/algo_dashboard.py` (port 5002).
3. **Config is truth.** No hardcoded ports, symbols, or paths in Python. Everything in config.yaml.
4. **DB is the log.** Every command, fill, and event is in SQLite. Never trust in-memory state.
5. **Bracket traceability.** bracket_map stores all 3 IB order IDs per command. Never lose this.
6. **Restart session after code changes.** broker.py and decider.py only pick up changes after restart.
7. **Two config files.** trader/config.yaml ≠ back-trading/config.yaml. Get this wrong and the wrong
   DB silently loads.
8. **galao.db WAL.** Never open with two concurrent writers. GevaExtract uses Python bridge for writes.
9. **Dedup guard in decider.** Already in-flight (line, direction, bracket) combos are skipped.
   This fixed a 425-stale-order incident on 2026-07-17. Do not remove it.

---

## 9. KNOWN GOTCHAS

- **Two galao.db files.** CC2026 uses `trader/data/galao.db`. Fetcher2026 reads
  `C:\Projects\Galgo2026\june\data\galao.db` (different file, OLD path). Galgo2027 must unify these.

- **Tick CSV path mismatch.** CC2026's `trading_dashboard.py` and `lib/price_profile.py` read from
  `C:\Projects\Galgo2026\june\trader\data\history\`. Fetcher writes there too (also legacy path).
  If Galgo2026 is ever deleted, both break. Fix in Galgo2027 by unifying paths.

- **Cross-dashboard menu overflow bug (fixed v4.18/v4.19).** If top bar gets more controls,
  check `overflow-x:auto` on `#top-bar` and use `position:fixed` (not Bootstrap) for dropdowns.
  Bootstrap `position:absolute` dropdowns are clipped by `overflow-y:hidden` ancestors.

- **broker crash on startup (race, fixed).** `lib/db.py:init_db()` creates the `verified_trades`
  view. If broker and decider both call it simultaneously on session start, one crashes. Session.py
  catches it and restarts automatically — just wait <30s.

- **June/ folder is legacy.** `C:\Projects\CriticalCorallations2026\june\` is the OLD Galgo2026
  monolith, kept for reference. It has its own CLAUDE.md, its own config.yaml, its own broker/decider.
  Do not run anything from there. Archive it.

---

## 10. IB INFRASTRUCTURE

```
Gateway mode:   Paper (IBC)
Paper port:     4002 (handles both data and order submission)
Live port:      4001 (NEVER USED in any current project)
IBC launcher:   C:\IBC\StartGateway.bat
Client IDs:
  broker.py:    101–120 (live_client_ids)
  fetcher:      801–804 (fetcher_client_ids)
  bars backfill: --port 4001 (live port, read-only for historical bars)
```

---

## 11. PERMISSIONS (Claude Code settings)

```json
{
  "permissions": {
    "defaultMode": "bypassPermissions"
  },
  "hooks": {
    "Stop": [{ "type": "command", "command": "cmd /c title CC: CriticalCorallations2026" }],
    "PostToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{ "type": "command",
                  "command": "python C:/Projects/galgo2026/.claude/hooks/selftest_on_edit.py",
                  "timeout": 35 }]
    }]
  }
}
```
**For Galgo2027: allow all, no confirmation prompts.**

---

## 12. GIT

```
Repo:     C:\Projects\CriticalCorallations2026
Remote:   cc2026  → https://github.com/orengavish/CriticalCorallations2026.git
          origin  → https://github.com/orengavish/galgo2006.git (legacy remote, keep)
Branch:   main
User:     Oren Gavish
```

---

## 13. WHAT TO ARCHIVE (DO NOT CARRY INTO GALGO2027 AS-IS)

| Item | Action |
|------|--------|
| `june/` (entire folder) | Archive — old Galgo2026 monolith, superseded |
| `trader/visualizer/app.py` (port 5001) | Delete or archive — forbidden legacy |
| `back-trading/algo_dashboard.py` (port 5002) | Delete or archive — forbidden legacy |
| `trader/runner.py` | Archive — not wired up, includes the forbidden visualizer |
| `trader/fetch_scheduler.py`, `fetch_priority.py` | Archive — CC2026 doesn't fetch (Fetcher2026 does) |
| `docs/*.md` (old planning docs) | Archive — superseded by CLAUDE_STATE.md and this file |
| `monthlyplanApr.md`, `weekplan_jun6.md` | Delete — stale planning |
| `back-trading/versions/` (simulator iterations) | Archive for reference |

**Keep:**
- `back-trading/trading_dashboard.py` — main app, port to Galgo2027
- `trader/broker.py` + `trader/decider.py` + `trader/session.py` — core trading engine
- `lib/` — all shared libraries
- `scripts/backfill_bars.py` — bars.db population
- `trader/data/galao.db` — migrate tables to Galgo2027 DB
- `trader/data/bars.db` — carry forward

---

## 14. FIT INTO GALGO2027

CC2026 is the **core** of Galgo2027:

1. **broker.py + decider.py + session.py** — carry forward unchanged
2. **Unified dashboard** — merge CC2026 (port 5003), Fetcher2026 (port 5050), GevaExtract (port 5005)
   into one Galgo2027 dashboard on port 5000
3. **One galao.db** — Galgo2027 owns it; GevaExtract writes via insert bridge; Fetcher reads from it
4. **bars.db** — carry forward; add auto-update (append from tick CSVs daily at 17:30)
5. **Scheduler** — install Task Scheduler task on first deploy (elevated, verified)
6. **Two config files** — merge into one config.yaml; resolve by always loading from Galgo2027 root

---

## 15. QUICK RESTART REFERENCE

```powershell
# Start CC2026 dashboard
cd C:\Projects\CriticalCorallations2026
python back-trading/trading_dashboard.py

# Start session (broker + decider) — also available via dashboard UI
# POST http://localhost:5003/api/session/start

# Check galao.db
python -c "import sqlite3; c=sqlite3.connect(r'C:\Projects\CriticalCorallations2026\trader\data\galao.db'); print(c.execute('SELECT COUNT(*) FROM commands').fetchone())"

# Restart session via curl
curl -X POST http://localhost:5003/api/session/stop
curl -X POST http://localhost:5003/api/session/start

# Check logs
Get-Content C:\Projects\CriticalCorallations2026\trader\logs\broker_stdout.log -Tail 20
Get-Content C:\Projects\CriticalCorallations2026\trader\logs\decider_stdout.log -Tail 20

# Install Task Scheduler (once, admin)
Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\CriticalCorallations2026\scripts\install_scheduler.ps1'
```
