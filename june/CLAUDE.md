# Galgo June 2026 — Claude Code Project Guide

This folder (`june/`) is the clean, standalone starting point for June 2026 work.
All sessions should start here. No prior history needed.

---

## What This System Does

Galgo is an intraday algorithmic trading system for US equity index micro futures on CME/CBOT.
It fetches tick data, backtests bracket orders, and executes paper trades via Interactive Brokers.

**Instruments:** MES (Micro E-mini S&P), MNQ (Micro Nasdaq), MYM (Micro Dow), M2K (Micro Russell)  
**Broker:** Interactive Brokers, paper account only  
**IB Gateway:** TWS/Gateway via IBC on port 4002 (paper). Started on demand before fetching/trading.

---

## Directory Layout

```
june/
├── lib/                    shared libraries (imported by all components)
│   ├── config_loader.py    loads trader/config.yaml → typed config object
│   ├── logger.py           shared logger (UTC timestamps)
│   ├── db.py               SQLite schema for live trading (commands, positions, ib_events, …)
│   ├── ib_client.py        IB connection manager (LIVE port=4002 data, PAPER port=4002 orders)
│   ├── ibc_launcher.py     starts/stops IB Gateway via IBC bat file
│   ├── order_builder.py    builds IB bracket orders (entry + TP + SL)
│   ├── critical_lines.py   parses critical line files, loads armed levels from DB
│   └── gdrive.py           [NEW] Google Drive upload (upload_file, file_exists_on_drive)
├── trader/
│   ├── config.yaml         ALL tunables live here — never hardcode values
│   ├── fetcher.py          tick fetcher: 8-worker parallel, TRADES + BID_ASK, all 4 symbols
│   ├── fetch_priority.py   shows which dates need fetching (verified trades but no tick files)
│   ├── fetch_scheduler.py  [NEW] scheduled daily runner: IBC start → fetch → verify → Drive upload
│   ├── verify_data.py      [NEW] data correctness checker (RTH gap, price range, bid>ask rate)
│   ├── broker.py           polls DB for PENDING commands, submits brackets, tracks fills
│   ├── decider.py          generates PENDING commands from critical lines; replenishes after fills
│   ├── position_manager.py monitors positions, SL cooldown
│   └── preflight.py        pre-session checks
├── back-trading/
│   ├── bt_command.py       [NEW] BacktradeCommand dataclass + DB serialization
│   ├── bt_db.py            [NEW] DB schema: tick_data, data_files, bt_runs, bt_commands
│   ├── bt_fetcher.py       [NEW] incremental 1000-tick fetcher for backtrading
│   ├── bt_simulator.py     [NEW] wraps simulator.py with incremental API
│   ├── bt_engine.py        [NEW] orchestrator: one command at a time, fetch+simulate loop
│   ├── bt_worker.py        [NEW] background worker process
│   ├── simulator.py        tick-by-tick OCO bracket simulator (fill model: bid/ask entry, trades exit)
│   ├── grader.py           compares sim fills vs paper fills → accuracy grade
│   └── db.py               original backtest DB (kept for reference; bt_db.py is the new one)
├── broker/                 [NEW] scheduled broker module (split from trader/)
└── docs/
    └── system_design.md    full architecture reference
```

---

## Configuration (`trader/config.yaml`)

All tunables are here. Key values:

| Setting | Value | Notes |
|---------|-------|-------|
| IB paper port | 4002 | single gateway, data + orders |
| Symbols (trading) | MES, MNQ, MYM, M2K | all 4 micros |
| Symbols (fetcher) | MES, MNQ, MYM, M2K | same |
| TP ticks | 4 | 1.0 point = $5/contract MES |
| SL ticks | 4 | symmetric 1:1 fast-exit |
| MES contracts | 2 | paper, max SL = $10 |
| MNQ/MYM/M2K | 1 each | paper |
| Fetch time | 17:30 CT | after CME close |
| IBC bat | C:\IBC\StartGateway.bat | paper mode |

---

## Key Invariants (never break these)

1. **Paper only.** Live port 4001 is never connected. All trades go through port 4002.
2. **IBC on demand.** Gateway is started before fetch/trade sessions, stopped after.
3. **Single computer.** No scheduler tasks on any other machine. All scheduled tasks are on this PC.
4. **Config is source of truth.** No hardcoded ports, symbols, or paths anywhere in Python.
5. **DB is the log.** Every command, fill, and event is written to SQLite. Never trust in-memory state across restarts.
6. **Verified before upload.** A tick file is only uploaded to Google Drive after `verify_csv()` passes.
7. **Bracket traceability.** Every bracket's 3 IB order IDs are stored in `bracket_map` table linked to the `command_id`. Never lose which fill belongs to which command.

---

## Database Layout

### `data/galao.db` — live trading
Tables: `commands`, `positions`, `ib_events`, `system_state`, `critical_lines`, `price_cache`, `bracket_map`

### `data/bt.db` — backtrader (new June schema)
Tables:
- `tick_data` — raw ticks stored in DB (also used by fetcher when complete)
- `data_files` — control table: (symbol, date, dtype) → status (missing/fetching/complete)
- `bt_commands` — queue of BacktradeCommand records
- `bt_runs` — results: entry/exit price, reason (TP/SL/EOD), pnl_ticks, ticks_consumed

### `data/fetch_progress.db` — fetcher state
Table: `fetch_progress` — (symbol, date, data_type) → finished flag, record count

---

## How to Run

### Start IBC gateway (paper)
```
C:\IBC\StartGateway.bat
```
Wait ~30s for "Server Version" in logs before connecting.

### Fetcher dashboard (browser UI) — run on trading PC
```
cd C:\Projects\Galgo2026\june
python fetcher_dashboard.py --real     # real mode, port 5050
python fetcher_dashboard.py            # mock mode (no gateway needed, for testing)
```
- **On this PC:** http://localhost:5050
- **From any device on home network:** http://192.168.1.132:5050

First-time firewall setup (run once in admin PowerShell):
```powershell
New-NetFirewallRule -DisplayName "Galao Dashboard" -Direction Inbound -Protocol TCP -LocalPort 5050 -Action Allow
```

Dashboard features:
- 4 process cards (one per symbol) with live progress bars + % complete
- Last price / H / L from most recent CSV (refreshes every 60s)
- Per-cell verify status: ✓ pass (green) / ⚠ warn (yellow) / ✗ fail (red)
- "Fetch All Missing" → launches 4 parallel fetcher processes
- "Verify All" → background verify run on all finished files
- Gateway Start/Stop with uptime counter

### Run all components in parallel (normal session)
Each in its own terminal, all from `C:\Projects\Galgo2026\june`:
```
# Terminal 1 — gateway watchdog (keep running 24/7)
python trader/gateway_watchdog.py

# Terminal 2 — fetcher dashboard
python fetcher_dashboard.py --real

# Terminal 3 — broker (submits IB brackets, monitors fills)
python trader/broker.py

# Terminal 4 — decider (generates commands from critical lines)
python trader/decider.py
```
Client ID allocation: fetcher=801-804, broker=101-120. No conflicts.

### Fetch tick data (manual)
```
cd june
python trader/fetcher.py --symbol MES --date 2026-06-02 --bid-ask
python trader/fetcher.py --from-date 2026-05-01 --bid-ask    # backfill range
python trader/fetch_priority.py                               # see what's missing
```

### Scheduled fetch (runs automatically at 17:30 CT)
```
python trader/fetch_scheduler.py --run-now    # manual trigger
```
Task Scheduler entry: `scripts/run_fetcher.bat` → daily 23:30 UTC

### Verify fetched data
```
python trader/verify_data.py --symbol MES --date 2026-06-02
python trader/verify_data.py --all-pending    # verify everything not yet checked
```

### Run backtrader on one command
```
python back-trading/bt_engine.py --command-id 42
python back-trading/bt_engine.py --all-pending    # process queue
```

### Run broker (paper session)
```
python trader/broker.py                           # normal poll loop
python trader/broker.py --build-trades            # send all pending at once
```

---

## June Week Goals (May 27 – June 6)

| # | Goal | Status |
|---|------|--------|
| 1 | june/ folder setup (this file) | ✅ Done |
| 2 | Fetcher: Google Drive, scheduled, data correctness | ✅ Done |
| 3 | Backtrader rewrite: DB-first, incremental fetch+simulate | ✅ Done |
| 4 | Broker: --build-trades, bracket traceability, price cache | ✅ Done |

### Pending manual steps
- **Other computer:** remove all scheduler tasks there too (we only cleared this PC)
- **Google Drive:** add credentials JSON + folder ID to `config.yaml` → set `enabled: true`
- **Task Scheduler:** `GalaoFetcherJune` registered on this PC at 23:30 daily ✅

Full plan: see `../weekplan_jun6.md`

---

## Bracket Sizes (decided)

Fast-exit for paper + backtrader simulation speed:
- **TP: 4 ticks (1.0 point)**
- **SL: 4 ticks (1.0 point)**
- 1:1 symmetric → most positions resolve within 1-2 batches of 1000 ticks
- Set in `config.yaml` under `orders.tp_ticks` / `orders.sl_ticks`

---

## Traceability — How Bracket Splits Work

IB brackets are 3 orders: parent entry + TP child + SL child.
When parent fills, IB assigns new order IDs to children.

Solution: `bracket_map` table in `galao.db`:
```sql
command_id | entry_order_id | tp_order_id | sl_order_id
```
On `execDetails` event: check order_id against all three columns → resolve to `command_id`.
This survives IB reconnects and split order IDs.

---

## Google Drive Setup (when ready)

1. Create service account in Google Cloud Console
2. Download JSON key → save to a safe path
3. Share your Drive folder with the service account email
4. Set in `config.yaml`:
   ```yaml
   google_drive:
     enabled: true
     credentials_path: "C:/secrets/gdrive_sa.json"
     history_folder_id: "your_folder_id_here"
   ```

---

## Common Issues

| Problem | Cause | Fix |
|---------|-------|-----|
| `ConnectionError: Could not connect` | IBC not started | Run StartGateway.bat, wait 30s |
| `No contract details for MES` | Gateway not ready | Wait another 30s after connected |
| Fetch stuck at 0 ticks | IB pacing violation | Wait 60s, restart fetcher |
| `fetch_progress.db` says finished but file missing | Interrupted delete | Delete DB row manually, re-run |
| Paper fills not showing | Port 4002 not paper | Check IBC config, verify paper mode |
