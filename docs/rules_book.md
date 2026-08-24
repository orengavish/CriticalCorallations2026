# Galao System — Rules Book
Version: 0.6.0 | Date: 2026-04-12

---

## 1. Development Rules

| # | Rule |
|---|------|
| R-DEV-01 | Every Python script must support a `--self-test` flag that runs a basic self-diagnostic and exits with code 0 (pass) or 1 (fail) |
| R-DEV-01a | A coding task is only declared **complete** after `--self-test` passes. No exceptions. |
| R-DEV-01b | GUI programs must have a headless (non-GUI) self-test mode — e.g. `visualizer.py --self-test` starts the backend, checks DB and routes, exits without opening a browser |
| R-DEV-01c | Self-test must cover: config loads, DB reachable, IB connection attempt, core logic of that component |
| R-DEV-02 | All changes must be documented in release notes (DB table + `release_notes.py` reader) before task is complete |
| R-DEV-03 | No production (LIVE) trading until explicitly approved — all trading uses PAPER port only |
| R-DEV-04 | Components communicate via SQLite DB only — no direct inter-process calls |
| R-DEV-05 | Config values must never be hardcoded — all tunables live in `config.yaml` |
| R-DEV-06 | System purpose is learning and A/B testing — data correctness takes priority over P&L optimization |
| R-DEV-07 | Config is loaded at startup only. Mid-session config changes are not supported. Restart required. |

---

## 2. Versioning Rules

| # | Rule |
|---|------|
| R-VER-01 | Before modifying any source file, copy the current version to `versions/` with timestamp suffix: `{filename}.{YYYYMMDD_HHMM}` (e.g. `broker.py.20260407_1151`) |
| R-VER-02 | Coding workflow: (1) copy to versions/ → (2) make changes → (3) run self-test → (4) write release notes → (5) announce complete |
| R-VER-03 | Release notes are stored in the DB table `release_notes` AND readable via `release_notes.py` |
| R-VER-04 | `release_notes.py` supports `--program <name>` filter to show notes for a specific file |
| R-VER-05 | Version number bumps are at the user's discretion |
| R-VER-06 | Git strategy: `main` branch = stable tested code only. Feature/fix work on separate branches. Commit message format: `[COMPONENT] description` |
| R-VER-07 | Never force-push to `main` |

---

## 3. Logging Rules

| # | Rule |
|---|------|
| R-LOG-01 | All log levels are used: DEBUG, INFO, WARNING, ERROR, CRITICAL. Level per statement is decided by the developer. |
| R-LOG-02 | Every IB interaction must be logged at minimum INFO level |
| R-LOG-03 | Each component writes to its own log file: `logs/{component}.log` |
| R-LOG-04 | Log format: `{timestamp_utc} | {level} | {component} | {message}` |
| R-LOG-05 | A log viewer is required in the Visualizer — filterable by level and component |
| R-LOG-06 | Errors are also written to the DB `ib_events` table (for IB errors) and `system_state` table (for system errors) so the Visualizer can surface them |

---

## 4. Error Handling Rules

| # | Rule |
|---|------|
| R-ERR-01 | IB disconnect mid-session: attempt reconnect up to 5 times over 5 minutes (60s between attempts), then abort session |
| R-ERR-02 | DB write failure: log the error to the log file (not DB — it may be unavailable), then abort |
| R-ERR-03 | All errors must be logged before any abort |
| R-ERR-04 | Error display: components write errors to DB + log → Visualizer reads and displays. No direct process-to-process alerting. |
| R-ERR-05 | Abort means: trigger shutdown sequence if session is active, then exit process |

---

## 5. Code Structure Rules

| # | Rule |
|---|------|
| R-CODE-01 | Maximum 500 lines per Python file. Split into modules if exceeded. |
| R-CODE-02 | Shared utilities live in `lib/` only. Never duplicate logic across components. |
| R-CODE-03 | One primary concern per file (e.g. `lib/order_builder.py` only builds orders) |
| R-CODE-04 | No deep folder nesting — `lib/` is flat |
| R-CODE-05 | `versions/` folder stores all historical file snapshots — never edit files in `versions/` |

---

## 6. Startup Rules (Pre-flight Checklist)

| # | Rule |
|---|------|
| R-START-01 | A pre-flight check must run before any trading session begins |
| R-START-02 | Pre-flight checks: (1) LIVE port 4001 connection, (2) PAPER port 4002 connection, (3) price fetch from LIVE port, (4) DB read/write test |
| R-START-03 | Any pre-flight failure = hard abort. Session does not start. |
| R-START-04 | Pre-flight results are logged and displayed in Visualizer |
| R-START-05 | Pre-flight is also triggered by `--self-test` on the main runner script |

---

## 7. IB Connection Rules

| # | Rule |
|---|------|
| R-IB-01 | Two IB connections must always run simultaneously: LIVE (4001) and PAPER (4002) |
| R-IB-02 | Price queries and market data always use the LIVE port (4001) |
| R-IB-03 | All order submission, modification, and cancellation uses the PAPER port (4002) |
| R-IB-04 | Client IDs managed as pools — LIVE: [101,102,103], PAPER: [201,301,401] |
| R-IB-05 | On reconnect, re-sync open orders and positions from IB before resuming |
| R-IB-06 | Always disconnect cleanly using an `atexit` handler |

---

## 8. Trading Rules

| # | Rule |
|---|------|
| R-TRD-01 | Instruments: futures only. Up to 5 symbols simultaneously |
| R-TRD-02 | Contract type: micro contracts only (e.g. MES, not ES) |
| R-TRD-03 | Active contract month resolved at startup from IB and stored in DB |
| R-TRD-04 | Trading session opens 30 minutes after CME official open time |
| R-TRD-05 | Shutdown sequence begins 60 minutes before CME official close time |
| R-TRD-06 | No new orders after shutdown sequence begins |
| R-TRD-07 | All positions and pending orders must be fully cleared before session end |

---

## 9. Critical Lines Rules

| # | Rule |
|---|------|
| R-CL-01 | Critical lines provided manually: `data/critical_lines/levels_daily_YYYYMMDD.txt` |
| R-CL-02 | File format per line: `TYPE, PRICE, STRENGTH` (e.g. `SUPPORT, 6250.00, 1`). Filename includes symbol: `levels_daily_{SYMBOL}_{YYYYMMDD}.txt` |
| R-CL-03 | Strength: integer 1 (strong) to 3 (low) — 1=strong, 2=medium, 3=low. Recorded for A/B testing, does not affect order logic |
| R-CL-04 | Up to 10 critical lines per symbol per day |
| R-CL-05 | Files must be present before session opens — missing file blocks that symbol |

---

## 10. Order Rules

| # | Rule |
|---|------|
| R-ORD-01 | Every critical line generates orders in BOTH directions: BUY and SELL |
| R-ORD-02 | Entry order type depends on current price vs line price (toggle rule) |
| R-ORD-03 | Price ABOVE line: `LMT BUY` + `STP SELL` |
| R-ORD-04 | Price BELOW line: `STP BUY` + `LMT SELL` |
| R-ORD-05 | Toggle re-evaluated at command generation and every replenishment |
| R-ORD-06 | All prices rounded to nearest 0.25 (MES tick size) at Decider before writing to DB |
| R-ORD-07 | Bracket sizes configurable — any positive value. Default: [2, 4, 8, 16] points |
| R-ORD-08 | All brackets symmetric (TP distance = SL distance from entry) |
| R-ORD-09 | Max quantity: 1 contract per order during learning phase |
| R-ORD-10 | On fill: Decider replenishes same order (re-evaluating toggle) — exactly once per fill, never twice |
| R-ORD-11 | Line strength and bracket size recorded on every order for A/B testing |
| R-ORD-12 | Broker must write status=`SUBMITTING` to DB before calling IB — this is the claim lock. Prevents duplicate orders on restart. |
| R-ORD-13 | Partial fills are ignored in V1 — all fills treated as complete and atomic |
| R-ORD-14 | The system tracks virtual strategy legs, not broker net positions. IB showing net zero while 4 brackets are active is intentional. |
| R-ORD-15 | `RECONCILE_REQUIRED` is not a terminal/dead-end status — `broker.py`'s `reconcile_stuck_commands()` sweeps it every poll cycle (bugs 4 & 5, 2026-08-23). Two branches, disambiguated by `fill_price`: never-filled entries (`fill_price IS NULL`) resolve against IB's current `trades()` — found+filled → `FILLED`, found+cancelled → `CANCELLED`, genuinely absent → `CANCELLED`. FILLED commands whose TP/SL bracket order id vanished from IB's cache (`fill_price NOT NULL`, flagged by `poll_tp_sl_fills()`'s staleness check past `_STALE_FILLED_MINUTES`) resolve against current IB positions — flat → `CLOSED` (exit price approximated from `price_cache`), still open → left alone with a logged warning pending human review (protected in the meantime by `reconcile_naked_positions()`, which also now runs every poll cycle instead of startup-only). |

---

## 11. Position Management Rules

| # | Rule |
|---|------|
| R-POS-01 | **Stagnation kill-switch:** position open > `stagnation_seconds` (60) AND price moved < `stagnation_min_move_points` (0.5pt) → market exit, log reason = `STAGNATION` |
| R-POS-02 | **SL cool-down:** after stop-loss hit, disarm line for `sl_cooldown_seconds` (30) before replenishment |
| R-POS-03 | All three values are configurable — they are A/B test candidates |

---

## 12. Shutdown Rules

| # | Rule |
|---|------|
| R-SHD-01 | At T-60min: cancel all pending orders |
| R-SHD-02 | At T-60min: tighten all open position stops to 1 point |
| R-SHD-03 | Exit open positions one by one with `exit_patience_seconds` wait between each |
| R-SHD-04 | Within `panic_threshold_minutes` of close: market exit all remaining simultaneously |
| R-SHD-05 | Shutdown cannot be aborted once started |
| R-SHD-07 | Replenishment is fully disabled the moment SHUTDOWN is written to DB — including for positions that close during the shutdown sequence |
| R-SHD-06 | All shutdown activity logged to DB and log file |

---

## 13. Data Rules

| # | Rule |
|---|------|
| R-DAT-01 | Historical data: `data/history/{SYMBOL}_{YYYY-MM-DD}.csv` |
| R-DAT-02 | Critical lines: `data/critical_lines/levels_daily_{YYYYMMDD}.txt` |
| R-DAT-03 | Fetcher always uses LIVE port only |
| R-DAT-04 | DB is single source of truth for all order state |

---

## 14. Regression Testing Rules

| # | Rule |
|---|------|
| R-REG-01 | `regression.py` is the single regression test runner — run on demand only, not on every change |
| R-REG-02 | Regression uses `data/test_galao.db` exclusively — never touches production `data/galao.db` |
| R-REG-03 | Three test layers: (1) self-tests, (2) feature/logic tests, (3) IB integration tests |
| R-REG-04 | Layer 3 requires IB Gateway to be running — hard fail if unavailable |
| R-REG-05 | Layer 3 submits a real LMT BUY order to PAPER port priced 500 points below market (guaranteed no fill), verifies IB accepted it, then cancels immediately |
| R-REG-06 | Output format: `[PASS/FAIL/SKIP] layer: test_name (time) — reason if failed` with summary line |
| R-REG-07 | Results printed to console and written to `logs/regression.log` |
| R-REG-08 | Flags: `--quick` (layers 1+2 only), `--layer3-only`, `--program <name>` (filter by component) |
| R-REG-09 | A new regression test must be written for every new feature — it is part of the definition of done |
| R-REG-10 | Regression must pass fully before any version bump |

---

## 15. A/B Testing Rules

| # | Rule |
|---|------|
| R-ABT-01 | Every order record must store: bracket size, line strength, line price, symbol, entry type (LMT/STP) |
| R-ABT-02 | Bracket sizes and position management params are primary A/B test variables |
| R-ABT-03 | No runtime decisions based on strength or bracket — record only |
| R-ABT-04 | Analyzer (future) will process DB data for A/B results |

---

## 16. Back-Trading Rules

### Fill Model

| # | Rule |
|---|------|
| R-BT-01 | Entry LMT BUY fills when `ask_p ≤ entry_price` (BID_ASK tick source). Fill price = entry_price (our limit, not the ask). |
| R-BT-02 | Entry LMT SELL fills when `bid_p ≥ entry_price` (BID_ASK tick source). Fill price = entry_price. |
| R-BT-03 | If BID_ASK data is unavailable, entry fill falls back to TRADES touch (less accurate — log a warning). |
| R-BT-04 | TP (long: LMT SELL, short: LMT BUY) fills when a TRADE tick reaches the TP price (conservative / trade-at model). Fill price = tp_price. |
| R-BT-05 | SL (long: STP SELL, short: STP BUY) fills when a TRADE tick reaches the SL price. Fill price = sl_price ± 1 tick (slippage in the adverse direction). |
| R-BT-06 | OCO priority: if both TP and SL are triggered by the same tick, SL wins (conservative, matches IB behaviour). |
| R-BT-07 | Orders not filled by session end are marked EXPIRED with pnl = NULL. EXPIRED orders are excluded from grading. |

### Generator

| # | Rule |
|---|------|
| R-BT-08 | Generated orders use LMT entry only. Entry placed at `market_price ± random_offset` where offset ∈ [entry_offset_min, entry_offset_max], tick-rounded. |
| R-BT-09 | LMT BUY entry placed BELOW market price (market is above "line" → toggle rule → LMT BUY). |
| R-BT-10 | LMT SELL entry placed ABOVE market price (market is below "line" → toggle rule → LMT SELL). |
| R-BT-11 | No two generated orders may share a placement timestamp within 120 seconds of each other (prevents clustering). |
| R-BT-12 | All generated bracket sizes are symmetric (TP distance = SL distance from entry), per R-ORD-08. |

### Reality Model

| # | Rule |
|---|------|
| R-BT-13 | Reality model submits to IB PAPER port only — never LIVE. |
| R-BT-14 | Orders are submitted at their scheduled ts_placed time (real-time clock comparison). |
| R-BT-15 | Fills are collected event-driven via `execDetailsEvent` — not by polling. |
| R-BT-16 | At session end (15:00 CT), all unfilled entry orders are cancelled and recorded as EXPIRED. |
| R-BT-17 | Reality model and simulator run on the same generated order list for the same day. The generator seed is NOT fixed — each day uses a fresh random set. |

### Grader

| # | Rule |
|---|------|
| R-BT-18 | A trade is "graded" only when BOTH simulator and paper show TP or SL exit. EXPIRED on either side = excluded. |
| R-BT-19 | Grade metric: `grade_pct = (fills within fill_match_ticks) / total_graded_trades × 100`. |
| R-BT-20 | Default `fill_match_ticks = 1` (configurable in `grader.fill_match_ticks`). |
| R-BT-21 | Target grade: ≥80% of fills within 1 tick (configurable in `grader.target_grade_pct`). |
| R-BT-22 | Grades are stored per bracket_size per run. Results accumulate over days for trend analysis. |
| R-BT-23 | `pnl_diff = paper_pnl − sim_pnl`. Large pnl_diff (positive or negative) indicates slippage model miscalibration. |
