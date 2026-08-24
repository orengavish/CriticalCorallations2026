# Galao System — Release Notes

---

## v0.1.0 — 2026-04-06

**Initial design and documentation release.**

### Decisions Made
- Architecture: 5 components — Decider, Broker, Fetcher, Visualizer (later), Analyzer (later)
- Inter-component communication: SQLite DB only (no direct coupling)
- IB connections: two simultaneous — LIVE (data) + PAPER (trading)
- Trading port: PAPER only until explicitly approved for LIVE
- Instruments: futures micro contracts only, up to 5 symbols
- Strategy: critical lines (manual CSV input for now), both directions per line
- Bracket sizes: symmetric, configurable subset of [2, 4, 8, 16] points
- Replenishment: owned by Decider (Option A) — Decider polls DB for fills, writes replenishment commands
- Shutdown mechanism: T-60min cancel pending → tighten brackets → orderly exit → panic mode
- DB: SQLite with WAL mode
- History data: CSV files per symbol per date
- All scripts: must implement `--self-test` flag

### Documents Created
- `rules_book.md` v0.1.0
- `design_book.md` v0.1.0
- `tech_solutions_book.md` v0.1.0
- `release_notes.md` v0.1.0

---

## v0.5.3 — 2026-04-07

### New Document: Trader Book
- Created `docs/trader_book.md` — manual for a human broker
- Covers: order types in plain English, decision rule, bracket parameters, session timing
- 8 examples: morning setup, price movement, entering position, TP exit, SL exit + cool-down,
  stagnation exit, emergency exit, end-of-day shutdown
- Replenishment guide with worked example
- Quick reference card (cheat sheet)

### Tech Solutions Book v0.3.0
- Added section 9: Order Type Mental Model
- LMT/STP decision table confirmed by both Claude and GPT
- Example with current price 6200 and lines at 6150, 6250, 6400

---

## v0.5.2 — 2026-04-07

### Walkthrough Book v0.2.0 — Hardened as Implementation Spec

Following ChatGPT review. All decisions confirmed by user.

**Added to walkthrough_book.md:**
- Section 0: Engine Contract — 13 invariants (I-01 to I-13)
- Section 0.1: Config values table — all timeouts/thresholds labeled as configurable
- Section 0.2: Full command status state machine with allowed transitions only
  - New states: `SUBMITTING` (claim lock), `EXITING`, `RECONCILE_REQUIRED`
- Section 0.3: 7 failure & reconciliation scenarios with detection + resolution

**Key decisions locked:**
- Shutdown = replenishment fully dead immediately (I-07)
- Virtual strategy legs, not broker net positions (I-12)
- Partial fills ignored in V1 (I-13)
- Single broker process only (I-10)
- PnL source of truth = `positions` table (I-11)
- Replenishment exactly once per fill (I-03)

**Updated design_book.md:**
- `commands.status` expanded to full 9-state set
- Added `replenishment_issued`, `claimed_at` fields to commands table

**Updated rules_book.md:**
- R-ORD-12: SUBMITTING claim lock rule
- R-ORD-13: Partial fills ignored V1
- R-ORD-14: Virtual legs declaration
- R-SHD-07: Replenishment fully dead on shutdown

---

## v0.5.1 — 2026-04-07

### New Document: Walkthrough Book
- Created `docs/walkthrough_book.md`
- Full lifecycle trace for a single critical line (MES 6250, strength=2)
- Covers: day start, DB writes, IB submission, fill detection, OCO activation, stagnation, TP/SL/stagnation exits, replenishment with cool-down, IB status query patterns, shutdown sequence
- Includes exact SQL, IB order IDs, timestamps, and full flow diagram

---

## v0.5.0 — 2026-04-07

### Regression Testing Added

- New program: `regression.py` — on-demand, 3 layers
- Layer 1: self-tests on all components
- Layer 2: feature/logic tests using `test_galao.db` (no IB)
- Layer 3: real IB integration — hard fail if IB unavailable, no-fill order test (LMT BUY at price-500, cancel immediately)
- Output: `[PASS/FAIL/SKIP]` per test + summary line, logged to `logs/regression.log`
- Flags: `--quick`, `--layer3-only`, `--program <name>`
- Rule: every new feature requires a Layer 2 regression test
- Rule: regression must fully pass before version bump

### Documents Updated
- `rules_book.md` → v0.5.0 (section 14: Regression Testing Rules)
- `design_book.md` → v0.5.0 (section 9: full regression spec)

---

## v0.4.0 — 2026-04-07

### Dev Rules Finalized

**Versioning workflow:**
- Before any edit: copy file to `versions/{filename}.{YYYYMMDD_HHMM}`
- After: self-test → release notes → announce complete
- Release notes stored in DB table `release_notes` + readable via `release_notes.py --program <name>`

**Logging:**
- All levels required (DEBUG/INFO/WARNING/ERROR/CRITICAL)
- Every IB interaction logged at minimum INFO
- Each component has its own log file in `logs/`
- Log viewer required in Visualizer (filterable by level + component)

**Error handling:**
- IB disconnect: 5 retries over 5 minutes → abort
- DB write fail: log to file → abort
- Alerting: errors written to DB → Visualizer displays them

**Code structure:** Max 500 lines per file. `lib/` is flat.

**Startup pre-flight:** LIVE port + PAPER port + price fetch + DB test. Any failure = hard abort.

**Config:** Startup only. No mid-session changes.

**New files added to structure:**
- `release_notes.py` — CLI reader with `--program` filter
- `preflight.py` — startup checklist
- `lib/logger.py` — shared logging setup
- `versions/` — timestamped file snapshots

**New DB table:** `release_notes` (id, timestamp, program, version, change_type, description)

### Documents Updated
- `rules_book.md` → v0.4.0 (full restructure — 14 sections)
- `design_book.md` → v0.4.0
- `.cursorrules` → updated with workflow, logging, error handling

---

## v0.3.0 — 2026-04-06

### Gemini Brainstorm Review — Decisions Made

**Adopted from Gemini:**
- **LMT/STP toggle:** Entry order type now depends on current price vs line price (see rules R-ORD-02 to R-ORD-05). Fixes bug where LMT order on wrong side of line fills immediately.
- **Stagnation kill-switch:** Position open >60s AND price moved <0.5pt → market exit. Reason logged as `STAGNATION`.
- **SL cool-down:** 30s wait after stop-loss hit before re-arming line. Avoids whipsaw.
- **Tick rounding:** All prices rounded to nearest 0.25 (MES tick) at Decider command generation.
- **Max 1 contract** per order during learning phase.

**Confirmed vs Gemini:**
- Both LONG and SHORT at every line (Gemini only showed LONG).
- Bracket values remain fully configurable (Gemini's [1.5,3.0,6.0,12.0] vs ours [2,4,8,16] — both valid).
- All new position management params (stagnation_seconds, stagnation_min_move_points, sl_cooldown_seconds) added to config.yaml.

**New exit reasons in DB:** `STAGNATION` added alongside TP / SL / SHUTDOWN_ORDERLY / SHUTDOWN_PANIC.

### Documents Updated
- `rules_book.md` → v0.3.0
- `design_book.md` → v0.3.0
- `.cursorrules` → updated

---

## v0.2.0 — 2026-04-06

### Design Decisions Finalized
- **IB ports:** LIVE=4001, PAPER=4002 (IB Gateway, not TWS)
- **Client IDs:** Pool approach — LIVE=[101,102,103], PAPER=[201,301,401]
- **Entry order type:** LMT (Limit) — bounce strategy, not STP breakout
- **Both directions per line:** LMT BUY + LMT SELL at every critical line regardless of label
- **Critical lines format:** `levels_daily_YYYYMMDD.txt`, format: `SYMBOL, PRICE, STRENGTH`
- **Strength scale:** 1-3 (1=weak, 3=strong)
- **DB:** `galao.db` extending V1 `trading_data.db` schema
- **System purpose:** Learning platform for A/B testing bracket sizes and line strengths
- **A/B test variables:** bracket_size (config) and line_strength (critical lines file)
- **Analyzer:** Later — not in current build scope
- **Visualizer:** Priority — must include DB viewer

### V1 Codebase Analyzed
- Located: `C:\Users\gaviShalev\Documents\Galgo\Python3\trading-analysis-system-V1`
- Reuse plan documented in design_book.md section 9

### Documents Updated
- `rules_book.md` → v0.2.0 (added A/B testing rules, corrected order types, ports, critical lines format)
- `design_book.md` → v0.2.0 (full rewrite with all confirmed decisions)
- `tech_solutions_book.md` → v0.2.0 (corrected ports, client ID pool, LMT entry order)
- `.cursorrules` → updated with all confirmed rules

---

## v0.1.1 — 2026-04-06

### IDE Setup
- Selected IDE: Cursor
- Created `.cursorrules` at project root with full project context and hard rules
- Added Cursor setup section to `tech_solutions_book.md` (section 9)
- Recommended extensions: Python, Pylance, SQLite Viewer
- Recommended terminal layout: 4-pane split (Decider / Broker / Fetcher / SQLite)

---

## v0.6.0 — 2026-04-12

### Monorepo Restructure + Back-Trading Engine

**Monorepo restructure:**
- All live trading code moved from repo root → `trader/` (git mv, history preserved)
- `lib/config_loader.py`: replaced hardcoded path with `_find_config()` — auto-discovers `config.yaml` by walking up from `sys.argv[0]`'s directory. Each sub-project finds its own config automatically.
- sys.path fixed in all 11 Python files to resolve `_ROOT = Path(__file__).parent.parent`
- Created `algo-analyzer/` placeholder (sub-project 3, TBD)

**back-trading/ sub-project — full implementation:**
- `generator.py`: synthetic order generator — N random RTH timestamps, LMT BUY at `price - offset`, LMT SELL at `price + offset`, for each bracket size in `[2, 16]`
- `simulator.py`: tick-by-tick OCO fill engine with realistic fill model (BID_ASK for entries, TRADES for TP/SL, 1-tick SL slippage). OCO priority: SL beats TP on same tick.
- `reality_model.py`: submits generated orders to IB paper at scheduled times, collects fills via `execDetailsEvent`, writes to `paper_fills` table
- `grader.py`: compares sim exit prices to paper exit prices → `grade_pct` (% within 1 tick) per bracket size per day
- `db.py`: SQLite schema — `runs`, `sim_orders`, `sim_fills`, `paper_fills`, `grades`
- `engine.py`: orchestrator — `--date`, `--from/--to`, `--reality-model`, `--fetch`, `--self-test`
- `config.yaml`: added `generator` and `grader` sections
- All self-tests pass: db, generator, simulator, grader, engine

**Trader additions (prior sessions):**
- `--dry-run` mode: broker logs commands without connecting to IB
- `/lines` GUI page: Hebrew paste format → parse → save to DB (critical_lines table)
- `preflight.py`: removed critical lines file check (lines now entered via GUI)
- `decider.py`: reads critical lines from DB instead of file
- `broker.py`: `register_ib_events()` wires errorEvent, orderStatusEvent, execDetailsEvent, connectedEvent, disconnectedEvent → `ib_events` table
- `visualizer/`: added Lines page, Reset functionality, IB Gateway raw log panel

**Documents updated:**
- `design_book.md` → v0.6.0 (monorepo structure, back-trading section 10, fill model table)
- `rules_book.md` → v0.6.0 (section 16: Back-Trading Rules R-BT-01 to R-BT-23)
- `running_book.md` → v0.6.0 (section 13: back-trading commands)

## v0.7.0 — 2026-08-23

### Bug fixes: CL Algo path resolution, strength-scale inversion, look-ahead bias, RECONCILE_REQUIRED dead end

**Bug 6 — CL Algo `history_dir` resolved to nothing:** `run_cl_algo_pipeline.py` derived `hist_dir` from `db_path.parent / "history"`, which never existed (the real tick CSVs live at a separate path outside this repo). Now reads `cfg.paths.cl_algo_history` directly. Added the same key to `back-trading/config.yaml` (not just `trader/config.yaml`) since scripts run as `python back-trading/*.py` load the nearer config file, not `trader/config.yaml`. **All CL Algo pipeline results produced before this fix are invalid** (the pipeline was silently finding zero ready days).

**Bug 7 — strength-scale inversion:** `trading_dashboard.py`'s auto-line generator (`_generate_lines`) emitted strength 10=strongest..3=weakest, while `algo_engine.py`'s `strength_max` filter and the manual critical-lines loader use 1=strongest..3=weakest. Every auto-detected line scored ≥5 and was silently dropped by the engine. Fixed by tiering the emitter's legacy 1-10 priority score into the 1-3 convention at its single point of emission (the shared `add()` helper), plus fixing two merge/dedup sort directions in the same file that depended on the old "higher=stronger" convention. **All prior Algo Lab auto-detected-line results are invalid** (the engine was filtering out essentially every auto-detected line).

**Bug 8 — look-ahead bias in batch backtest:** `/api/analyze_all` (and `/api/build_db` with `force=True`) computed PDH/PDL/PDC/PDO for date D from D's own full-session ticks, then simulated D's morning against levels only knowable at end-of-day. `_generate_lines` now accepts an optional `ohlc_ticks` parameter; the batch/force=True path in `_build_lines_for` sources it from the nearest prior session (never D's own ticks) for PDH/PDL/PDC/PDO specifically. Every other line type (pivot/ORB/VWAP/volume/round) is unaffected — those are legitimately intraday. The interactive (non-batch) path already sourced levels from a walked-back prior session and is unchanged. **All prior batch-backtest (`/api/analyze_all`) results are invalid.**

**Bug 4 — RECONCILE_REQUIRED had no repair path:** Added `reconcile_stuck_commands()` to `trader/broker.py`, called every poll cycle. Sweeps `RECONCILE_REQUIRED` rows with `fill_price IS NULL` (never-filled entries) against IB's current `trades()`: found+filled → FILLED, found+cancelled → CANCELLED, genuinely absent → CANCELLED. The ~1,400-row backlog is swept automatically on the first poll after deploy — no separate migration needed.

**Bug 5 — 57 FILLED commands with no staleness detector:** `poll_tp_sl_fills()` now builds `known_oids` from every poll's `trades()` call (before its early-return, since most cycles have zero new TP/SL fills) and flags a FILLED command `RECONCILE_REQUIRED` once both its TP and SL order ids have aged out of IB's cache for over `_STALE_FILLED_MINUTES` (90min). `reconcile_stuck_commands()` extended with a second branch for `fill_price IS NOT NULL` rows: flat position → closes with an approximated exit price from `price_cache`, position still open → left alone with a logged warning (the one genuinely ambiguous case). `reconcile_naked_positions()` promoted from startup-only to running every poll cycle alongside the others.

**Bug 10 (schema) — geva_extract grid attribution:** Added nullable `strategy_variant TEXT` column to `commands` (schema + migration in `lib/db.py`). Additive-only, no backfill, no retagging of existing `source='geva_extract'` rows. Lets a bracket-grid fan-out (32 commands from one scraped line) share a group id, distinguishing grid volume from a single signal in P&L-by-source views.

**Bugs 3 & 11 (cross-repo) — read-only `get_priority_dates()`:** Added to `lib/db.py` as a drop-in for Fetcher2026's `fetch_priority.py`, which was opening `galao.db` via a stale path. Documented calling convention in its docstring (Fetcher2026 must import it under a distinct alias to avoid a `lib.db` module-name collision).

### Files changed
`back-trading/run_cl_algo_pipeline.py`, `back-trading/config.yaml`, `back-trading/trading_dashboard.py`, `trader/broker.py`, `lib/db.py`

<!-- New releases go above this line, most recent first -->
