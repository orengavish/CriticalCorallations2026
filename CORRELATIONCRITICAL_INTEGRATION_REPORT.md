# CriticalCorallations2026 (CC2026) — Integration Inventory Report

> **Scope**: conservative, code-grounded inventory of this repo prior to a platform rebuild.
> **Method**: read the source; verified every claim against `trader/data/galao.db`,
> `trader/data/bars.db`, `june/trader/data/bt.db`, `trader/logs/*`, and live listening ports.
> **Date of investigation**: 2026-08-22. Repo state: branch `master`, HEAD `c88d53a`.
> **Nothing in this repo was modified.** This file is the only artifact created.
>
> **Standing rule applied throughout**: *no algorithm here is treated as proven.* Execution
> reliability and strategy quality are reported as two separate axes. Documentation claims
> are reported as claims, and are marked ✅ verified / ❌ contradicted by code or data.

---

## 1. Executive Summary

CC2026 is two systems wearing one coat:

1. **A live paper-trading execution engine** (`trader/broker.py`, `trader/decider.py`,
   `trader/session.py`, `lib/order_builder.py`, `lib/ib_client.py`, `lib/db.py`). This is
   real, running right now (IB Gateway paper on `:4002`, dashboard on `:5003`, session
   supervisor holding a PID lock since 2026-08-15), and it is the most valuable asset in the
   repo. It has survived and been hardened against several genuine production incidents,
   each of which left a specific, deliberate guard in the code.

2. **A research/backtest layer** (`back-trading/*`, `lib/algo_engine.py`, `lib/algo_lab.py`,
   `june/back-trading/*`) that is architecturally more complete than expected — there *is* a
   batch runner, a scorer, and a learner that closes the loop — but which is **currently
   inert, mis-wired, and produces results that cannot be trusted as evidence**.

### The seven findings that matter most

| # | Finding | Evidence |
|---|---|---|
| **F1** | **No algorithm in this repo has evidence of profitability.** The only real closed-trade ledger (`verified_trades`, n=998, 2026-05-05 → 2026-07-31) is **−21,320.75 points** in total. Every source is net negative except `geva_extract` (n=22, not this repo's logic). | `lib/db.py:402` view + live DB query |
| **F2** | **The backtest pipeline has textbook look-ahead bias.** `_build_lines_for()` derives S/R levels (PDH/PDL/VWAP/POC/VAH/VAL) from a day's **own full session ticks**, stores them under that same `date`, and `cl_algo_backtester.run()` then "places" orders at 08:30 CT on that same day. The levels literally encode the day's future high and low. | `back-trading/trading_dashboard.py:753-816`, `:844-853`; `back-trading/cl_algo_backtester.py:241-283` |
| **F3** | **The CL Algo experiment pipeline has never produced a single stored result in the live DB.** All six `cl_algo_*` tables are **0 rows**. Root cause: `run_cl_algo_pipeline.py` resolves `history_dir = db_path.parent / "history"`, which is `back-trading/data/history` (or `trader/data/history`) — **neither directory exists**. The actual tick CSVs live at `C:\Projects\Galgo2026\june\trader\data\history` (169 files). | `run_cl_algo_pipeline.py:258-259`; `cl_algo_backtester.py:527-528`; filesystem |
| **F4** | **Algo Lab is dead on arrival due to an inverted strength scale.** `lib/algo_engine._build_cmds` skips a line when `strength > strength_max` (1=strongest). But `trading_dashboard._generate_lines` emits strength on a **10=strongest** scale (values 5–10). With `strength_max ∈ {1,2,3}`, every auto-detected line is silently filtered out. Result: **0 rows** with `source='algo_lab'` in a DB of 49,210 commands. | `lib/algo_engine.py:196-197` vs `trading_dashboard.py:332-457`; DB `critical_lines` strength distribution |
| **F5** | **`bracket_map` does not exist.** `GALGO2027_HANDOFF.md` §3/§5/§8 and `june/CLAUDE.md` describe a `bracket_map` table as a core invariant ("Never lose this"). It is **not in the schema, not in the DB, and not referenced by any `.py` file in the repo.** The three IB order IDs live in `commands.ib_order_id / ib_tp_order_id / ib_sl_order_id`. | grep across repo; `lib/db.py:63-391`; live DB `sqlite_master` |
| **F6** | **The largest experiment dataset is in the folder marked "archive".** `june/trader/data/bt.db` holds **9,720,000** `bt_matrix_results` rows over **10,800** param sets, and `june/back-trading/bt_scorer.py` is a materially more rigorous scorer than the "current" one (12 metrics, Monte-Carlo permutation p-value, LOOCV, stability zone, max drawdown). `bt_scores` is 0 rows — it was populated then cleared, or never scored. | `june/trader/data/bt.db`; `june/back-trading/bt_scorer.py:35-72` |
| **F7** | **Order throughput is badly out of balance with IB's capacity.** Of 49,210 commands: 20,136 CANCELLED, 7,166 ERROR, 1,342 RECONCILE_REQUIRED, only 4,079 ever CLOSED. `ib_events` logs **39,583× code 201 (order rejected)** and **33,351× code 202 (order cancelled)**. `RECONCILE_REQUIRED` is written but **no code ever reads or resolves that status**. | DB aggregate; `trader/broker.py:331-339`; grep for `RECONCILE_REQUIRED` |

### What is genuinely good

The **order lifecycle implementation is the crown jewel** and should be preserved close to
as-is. Specifically: the atomic claim lock, the dual event+poll fill detection, the
**price-derived exit-reason** (immune to order-ID labelling errors), the **fill-price bracket
rebase**, the **naked-position reconciliation on startup**, and the **`verified_trades`
arithmetic-integrity view**. Each of these is a scar from a real incident. Details in §7.

---

## 2. Architecture / Code Map

### 2.1 Runtime topology (verified live, 2026-08-22)

```
IB Gateway (IBC, paper)  :4002   ← both market data and order submission
        ▲                    ▲
        │ ib_insync          │ ib_insync
        │                    │
 trader/decider.py     trader/broker.py          ← supervised subprocesses
        │                    │
        └────────► trader/data/galao.db ◄────────┘   (SQLite, WAL, 56.7 MB)
                        ▲   ▲
                        │   └──── GevaExtract  :5005  (external repo, writes source='geva_extract')
                        │
     back-trading/trading_dashboard.py  :5003   ← Flask, also owns trader/session.py singleton
                        │
                        └──── trader/data/bars.db (182 MB, 340,542 bars_30m rows)
```

Confirmed listening: `4002`, `5003`, `5004` (Fetcher bars status), `5005` (GevaExtract),
`5050` (Fetcher2026). `trader/logs/session.pid` present; `decider_stdout.log` last written
2026-08-22 09:39.

### 2.2 Code map by concern

| Concern | Files / classes / functions |
|---|---|
| **Market data input** | `lib/ib_client.py::IBClient.get_price()` (reqMktData, `reqMarketDataType(3)` delayed, historical-bar fallback), `IBClient.get_contract()` (front-month resolution + per-instance cache). Tick CSVs read by `trading_dashboard._load_ticks/_find_csv` from hard-coded `_HIST_DIR` (`trading_dashboard.py:40`). Bars via `scripts/backfill_bars.py` (IB) and `scripts/import_7year_bars.py` (Databento CSVs in `data/bars_7years_30m_*.csv`). Price cache via `lib/db.py::update_price_cache/get_cached_price`. |
| **Transformations / features** | `trading_dashboard._ohlcv_bars()` (tick→OHLCV, sub-minute aware). `lib/price_profile.py` → `price_profile` table (37,031 rows): visits, up/down counts, up_vol/down_vol, bid/ask delta. `scripts/build_bars_normalized.py` (per-symbol min-max, basis persisted in `bars_30m_normalize_meta`), `scripts/build_bars_diffs.py` (raw + normalized pairwise diffs). `lib/day_params.py::_compute_two_hour_avg()` → `two_hour_avg_move`. |
| **Critical lines / levels** | `lib/critical_lines.py` — `parse_file()`, `load_critical_lines()`, `get_armed_lines()`, `disarm_line()`, `rearm_line()`, `MAX_LINES_PER_SYMBOL=20`. Auto-detection: `trading_dashboard._generate_lines()` (25 algo types, `trading_dashboard.py:295-467`), stored by `_build_lines_for()` / `api_lines_create()`. Table `critical_lines` (744 rows, latest 2026-07-16). |
| **Correlation analysis** | `lib/correlation_lab.py` — `_read_closes`, `_log_returns`, `_aligned_returns`, `pair_correlation`, `correlation_matrix`, `rolling_correlation_series`. Read-only over `bars.db`. Routes `/api/correlation/{config,matrix,timeseries}`. **Produces no signals and feeds no algorithm.** |
| **Signals / algorithms** | `lib/algo_engine.py` (`AlgoType`, `AlgoParams`, `_pairs_for_line`, `_calc_prices`, `_build_cmds`, `generate_cl_commands`). `trader/decider.py::generate_commands/replenish`. `trading_dashboard.api_trades_create()`. `trader/random_gen.py`. `back-trading/generator.py`. `back-trading/cl_algo_full_duplex.py::_find_tp_line/_find_sl_line`. |
| **Parameter configuration** | `trader/config.yaml` (live engine + `algo_lab:` + `correlation:` blocks), `back-trading/config.yaml` (backtest engine), `lib/config_loader.py::get_config` (**global single-slot cache**, path resolved relative to the config file). Plus many module-level constants — see §4. |
| **Simulation / backtest** | `back-trading/simulator.py::simulate()` / `simulate_exit()` (the shared fill engine). Drivers: `cl_algo_backtester.py::run()`, `cl_algo_full_duplex.py::run()`, `engine.py::run_day/run()`, `calibrate.py::calibrate()`, `june/back-trading/bt_matrix_runner.py::run()`. Snapshots in `back-trading/versions/simulator_iter0..6.py`. |
| **Trade generation** | All paths converge on one `INSERT INTO commands (... status='PENDING')`: `decider.py:120`, `algo_engine.py:271`, `algo_lab.py:194`, `db.py::spawn_replenishment:750`, `trading_dashboard.py:1113`, `random_gen.py`. Plus external GevaExtract bridge. |
| **Order execution** | `trader/broker.py::process_pending_commands()` → `lib/order_builder.py::build_bracket()` / `place_bracket()` → `IBClient.paper`. |
| **Order tracking / lifecycle** | `broker.py`: `_claim_command`, `register_ib_events`, `_handle_exec_fill`, `poll_fills`, `poll_tp_sl_fills`, `_drain_rebase_queue`, `reconcile_naked_positions`, `replenish_if_enabled`. Raw callback log → `ib_events` (268,063 rows). Full trace in §7. |
| **Trade lifecycle / position** | `trader/position_manager.py::check_sl_cooldowns()` (SL cool-down disarm/re-arm). `positions` table exists but is **0 rows — nothing writes to it**. |
| **P&L** | `lib/db.py::record_completed_trade()` → `completed_trades` (4,315 rows). `verified_trades` VIEW (`lib/db.py:402-478`). `lib/algo_pnl.py::get_breakdown/rollup_by_source` with `SYMBOL_MULTIPLIERS`. |
| **Result analysis / scoring** | `back-trading/cl_algo_scorer.py::_compute_metrics/score` (5 metrics). `back-trading/cl_algo_learner.py::recommend` (grid narrowing + convergence). `back-trading/grader.py::grade` (sim-vs-paper accuracy). `back-trading/calibrate.py` (simulator calibration vs real fills). `june/back-trading/bt_scorer.py` (12 metrics + 4 anti-overfit guards). |
| **Visualization** | `back-trading/trading_dashboard.py` — single 4,600-line file, Plotly embedded in an `HTML` string constant (`:1338+`), left-rail nav, v5.03. **Legacy/forbidden**: `trader/visualizer/app.py` (:5001), `back-trading/algo_dashboard.py` (:5002), `back-trading/visualizer/app.py`, `june/galao_dashboard.py`, `june/fetcher_dashboard.py`. |
| **Databases / files** | See §8.1. |
| **Logging** | `lib/logger.py::get_logger`. `trader/logs/`: `broker.log` (31.9 MB), `broker_stdout.log` (170 MB), `decider_stdout.log` (115 MB), `ib_client.log` (144 MB) — **no rotation**. Plus the `ib_events` DB table. |
| **Orchestration / schedulers** | `trader/session.py::SessionManager` (**the good one**). `trader/runner.py` (broader, unwired, launches the forbidden visualizer). `trader/daily_paper_session.py` (2-hour session, force-close). `trader/may_scheduler.py` (monthly). `scripts/install_scheduler.ps1` (not installed). `back-trading/cl_algo_worker.py` (per-symbol lock-file worker). |
| **Testing** | Every module has `--self-test`. `trader/regression.py` — 3-layer runner (self-tests / logic / live IB round-trip). `tests_gui/test_new_tabs.py`. `.claude/hooks/selftest_on_edit.py`. |

### 2.3 Dead / legacy / duplicated code (verified)

- `june/` — full pre-split monolith: its own `broker.py`, `decider.py`, `lib/`, `back-trading/`,
  two dashboards. **But it also holds the best scorer and the biggest result set** (F6).
- `back-trading/versions/simulator_iter{0,1,1_pre,2,3,3_pre,4,5,6,6_pre}.py` — 10 snapshots.
- `versions/` — 11 timestamped file snapshots (2026-04).
- `trader/fetch_scheduler.py`, `trader/fetch_priority.py`, `trader/fetcher.py`,
  `trader/ib_fetcher_paper.py` — CC2026 does not fetch; Fetcher2026 does.
- `trader/create_presentation.py` (1,152 lines) — PowerPoint generator.
- `algo-analyzer/` — empty except `.gitkeep`.
- `data/galao.db` — a **second, empty** galao.db with the full schema. Almost certainly created
  by a `run_cl_algo_pipeline.py` invocation that found no ready days. Live evidence for F3.

---

## 3. Algorithm Inventory

### 3.1 Order-generating algorithms / strategy variants

Legend for **Status**: `IMPL` implemented · `PART` partially implemented · `EXPR` experimental
· `DOC` documented-proposed only · `OBS` obsolete.

| # | ID / name | Purpose | Source file:function | Entry conditions | Exit conditions | Market data required | Parameters | Defaults | Hard-coded that should be a parameter | Side | Symbols | Timeframe | Produces | Current status | Evidence available | Tag |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 | `decider_critical_line` | Live production strategy: cover every armed line, both directions, every bracket | `trader/decider.py:67 generate_commands()` → `lib/order_builder.py:38 determine_entry_type()`, `:66 calc_bracket_prices()` | For each armed line × bracket × {BUY,SELL}: toggle rule — price ≥ line → BUY=LMT / SELL=STP; price < line → BUY=STP / SELL=LMT. STP entry offset ±1 tick. Dedup guard skips `(critical_line_id, direction, bracket_size)` already in PENDING/SUBMITTING/SUBMITTED (`decider.py:93-101`) | IB OCO bracket: symmetric TP/SL at ±`bracket_size` from entry; rebased to fill price by broker | Live IB price (LIVE conn) + `critical_lines` rows | `orders.active_brackets`, `orders.tick_size`, `orders.quantity`, `symbols`, `decider.replenishment_poll_seconds` | `[2,4]`, `0.25`, `1`, `[MES]`, `10` | Tick offset for STP entry is `tick` literally (`order_builder.py:87`); `date_str = today` only; no session-time filter; no max-open-orders cap | Both | `cfg.symbols` = `[MES]` only | Event-driven, GTC day orders | **Real orders** | Running now, but **replenishment has been a no-op since 2026-08-18** ("Could not fetch live price for MES: Socket disconnect" daily at 19:30 UTC, `trader/logs/decider.log`) | IMPL |
| A2 | `algo_engine.BOUNCE` | Mean-reversion off the line | `lib/algo_engine.py:155-159 _pairs_for_line()` | SUPPORT → BUY LMT at line; RESISTANCE → SELL LMT at line. One order per line | Asymmetric bracket: TP=`tp_ticks`×tick, SL=`sl_ticks`×tick from entry | `critical_lines` + a current price (only used by DIRECTIONAL/BOTH) | `tp_ticks`, `sl_ticks`, `direction_filter`, `strength_max`, `tick_size` | `4`,`4`,`ALL`,`3`, `0.25` | `TICK=0.25` module default (`algo_engine.py:31`) mis-prices MYM/M2K if caller forgets `tick_size` | Both (line-type driven) | MES/MNQ/MYM/M2K | Placed once at signal time | Orders (via algo_lab) / sim rows (via backtester) | Code correct + self-tested; **0 live orders ever** (F4) | IMPL (code) / EXPR (as strategy) |
| A3 | `algo_engine.BREAKOUT` | Momentum through the line | same, `:161-165` | SUPPORT → SELL STP (break below); RESISTANCE → BUY STP (break above) | same asymmetric bracket | same | same | same | same | Both | same | same | same | same | IMPL / EXPR |
| A4 | `algo_engine.DIRECTIONAL` | One canonical order per line, entry type by toggle | same, `:167-175` | SUPPORT → BUY, LMT if price ≥ line else STP. RESISTANCE → SELL, LMT if price < line else STP | same | + current price | same | same | same | Both | same | same | same | same | IMPL / EXPR |
| A5 | `algo_engine.FADE` | Contrarian: bet the line breaks | same, `:177-181` | SUPPORT → SELL LMT at line; RESISTANCE → BUY LMT at line | same | same | same | same | same | Both | same | same | same | same | IMPL / EXPR |
| A6 | `algo_engine.BOTH` | Full matrix — equivalent to A1 but with asymmetric TP/SL | same, `:183-187` | BUY + SELL at every line, entry type by toggle | same | + current price | same | same | same | Both | same | same | same | same | IMPL / EXPR |
| A7 | `algo_lab` grid submitter | Batch-submit N param combos as paper trades for P&L attribution | `lib/algo_lab.py:43 build_param_grid()`, `:137 submit_grid()` | Cartesian of `strategies × tp_ticks × sl_ticks × direction_filters × strength_max`, deterministically downsampled to `max_param_combos`; per-combo in-flight dedup keyed `(line_id, direction, algo_type, tp, sl)` | Delegated to A2–A6 | `critical_lines` + live price snapshot per symbol | `algo_lab.*` in `trader/config.yaml` | `strategies` 5, `tp_ticks [4,8,16]`, `sl_ticks [4,8,16]`, `direction_filters` 3, `strength_max [1,2,3]`, `max_param_combos 24`, `max_commands_per_submit 500` | Grid is 405 combos downsampled to 24 by an **index-stride** sampler (`algo_lab.py:69-75`) — not a design choice a researcher would make deliberately | Both | `[MES,MNQ,MYM,M2K]` | Per-submit batch | **Real orders** (paper) | **Never produced one order** — 0 `source='algo_lab'` rows. Blocked by F4 | PART |
| A8 | `cl_algo` full-duplex | Structural exits: TP/SL are *other critical lines*, not fixed ticks | `back-trading/cl_algo_full_duplex.py:154 _find_tp_line()`, `:177 _find_sl_line()`, `:195 run()` | Direction from line type (SUPPORT→BUY, RESISTANCE→SELL). Entry = line ± `tick_buffer` ticks. Both LMT and STP simulated | TP = nearest opposing line within `two_hour_avg_move` **and** ≥ `_MIN_EXIT_TICKS` away, else fallback entry ± `two_hour_avg_move`. SL = nearest same-side line beyond entry (no range cap), else fallback | TRADES + BID_ASK tick CSVs + prior-day CSV for `two_hour_avg_move` | `tick_buffer`, `_MIN_EXIT_TICKS`, `_DEFAULT_TWO_HOUR_AVG`, `_WINDOW_HOURS`, RTH bounds | `1`, `5`, `10.0`, `2`, `08:30–15:15 CT` | `_TICK=0.25` module constant (`cl_algo_full_duplex.py:47`) — **hard-codes MES tick for all 4 symbols**; RTH window; `_MIN_EXIT_TICKS`; `tick_buffer` never actually varied | Both | 4 symbols nominally, MES-correct only | Intraday, one signal at RTH open | **Simulations only** | `cl_algo_fd_results` = **0 rows** | IMPL (code) / EXPR — never run to completion |
| A9 | Dashboard "Create Trades" | Manual/UI candidate builder | `back-trading/trading_dashboard.py:1022 api_trades_create()` | Per line × bracket, 2 orders. SUPPORT: BUY LMT @ line, SELL STP @ line−tick. RESISTANCE: SELL LMT @ line, BUY STP @ line+tick. Candidates already crossed by live price are dropped (`:1068-1072`) | **Asymmetric and very tight**: TP = ±`bracket`, **SL = 1 tick past the line** (`:1057-1063`) | `critical_lines` + `price_cache` | `brackets`, `min_strength` (request body) | `[2.0,4.0,10.0]`, `1` | The whole TP/SL geometry is inline literals; `top = candidates[:200]` cap (`:1092`) | Both | 4 symbols | Ad-hoc | **Real orders** (`source='trading_dashboard'`) | 5 commands ever, all CANCELLED. **A 1-tick stop is a distinct, far more aggressive strategy than A1** and must not be merged with it | IMPL / EXPR |
| A10 | `random_mkt` | Random MKT baseline / lifecycle exerciser | `trader/random_gen.py` | Random direction, MKT entry at current price | Symmetric bracket ±`bracket_size` | Live price | `--rate`, `bracket_sizes` | 6 trades/min, `[2,4,8,16]` | rate, offsets | Both | MES | continuous | **Real orders** | Largest sample: `verified_trades` n=508, **−21,893.75 pts** | IMPL — *baseline, not a strategy* |
| A11 | `random_lmt` | Random LMT baseline | `trader/random_gen.py` | Random direction, LMT 1–8 ticks off market | same | Live price | same | same | offset range 1–8 ticks | Both | MES | continuous | Real orders | `verified_trades` n=222, **−1.25 pts** | IMPL — baseline |
| A12 | `random_stp` | Random STP baseline | `trader/random_gen.py` | Random direction, STP 1–8 ticks off market | same | Live price | same | same | same | Both | MES | continuous | Real orders | `verified_trades` n=232, **−115.0 pts** | IMPL — baseline |
| A13 | Synthetic "fake critical line" generator | Simulator-vs-reality calibration fixture | `back-trading/generator.py:64 generate()`, `:184 make_orders_for_price()` | N random RTH timestamps ≥ `_MIN_GAP_SEC` apart; LMT BUY at P−offset, LMT SELL at P+offset | Symmetric bracket per `bracket_sizes` | TRADES ticks | `generator.n_timestamps`, `entry_offset_min/max`, `bracket_sizes`, `rth_start/end` | `20`, `0.25`, `1.50`, `[2,16]`, `08:30/14:30` | `_MIN_GAP_SEC=120`, `_TICK=0.25`, `_MES_MULT=5.0` | Both | MES | intraday | Simulations (+ real paper orders in `--reality-model`) | `back-trading/data/` does not exist → no stored runs | OBS/EXPR |
| A14 | `bt_matrix` exit-parameter sweep | Vary exit params around *known real entries* | `june/back-trading/bt_matrix_runner.py:162 run()` + `bt_params.py:27 AXES` | Entries are **not** simulated — taken from real `verified_trades` fills | `simulate_exit()` with per-param-set `tp_ticks, sl_ticks, entry_delay_s, entry_offset_t, tp_confirm_t, session_window` | verified trades + TRADES CSVs | 6 axes | `tp/sl [2..12]`, `delay [0,5,15,30,60]`, `offset [-2..2]`, `confirm [1,2,3]`, `window [ALL,MORNING,MIDDAY,AFTERNOON]` | `PRE_MARKET` window deliberately excluded despite the code comment noting **69% of verified trades fall there** (`bt_params.py:45-47`) | Both | MES | intraday | Simulations | **9,720,000 rows** over 900 trades × 10,800 param sets. `bt_scores` = 0 rows — never scored | IMPL but **archived** in `june/` |
| A15 | `geva_extract` | Facebook-scraped S/R levels | **External** — `C:\Projects\GevaExtract` | n/a (writes `commands` directly) | n/a | n/a | n/a | n/a | n/a | Both | MES, MNQ | daily | Real orders | 28,750 commands here; `verified_trades` n=22, +848.75 pts — **too small to mean anything** | Out of scope, but **it is the dominant writer to this DB** |

### 3.2 S/R level-detection algorithms (produce levels, not orders)

All in `back-trading/trading_dashboard.py:295-467 _generate_lines()`. 25 `algo_type` values,
grouped by `source`. Each stores a human-readable `formula` + `inputs` into `critical_lines.note`.

| source | algo_type(s) | Formula (as coded) | Strength emitted | Line count in DB |
|---|---|---|---|---|
| `ohlc` | `PDH`, `PDL` | `max/min(all session prices)` — **not** restricted to the previous day (`:332-335`) | 10 | 87 |
| `ohlc` | `PDC`, `PDO` | last / first RTH tick; classified R vs S by side of `mid=(H+L)/2` | 9 / 8 | 31 / 24 |
| `pivot` | `PIVOT_P/R1/S1/R2/S2/R3/S3` | classic floor pivots on RTH H/L/C (`:353-368`) | 8→5 | 179 |
| `overnight` | `OVERNIGHT_H/L` | max/min of Globex window (`t ≥ 17:00` or `t < 09:30`) | 5 | 12 |
| `orb` | `ORB15_H/L`, `ORB30_H/L` | 09:30–09:45 / 09:30–10:00 extremes | 7 / 6 | 32 |
| `vwap` | `VWAP` | **equal-weighted arithmetic mean of RTH ticks — not volume-weighted** (`:403`, acknowledged in code) | 8 | 9 |
| `volume` | `POC`, `VAH`, `VAL` | tick-count histogram; POC = modal bucket; 70% value area expanded greedily (`:410-443`) | 9 / 7 / 7 | 41 |
| `round` | `ROUND_BIG/MED/SML` | per-symbol intervals from `_ROUND_LEVELS` (`:102-107`) | 7/5/3 | 20 |
| `manual` | `MANUAL` | user-entered via UI / `lib/critical_lines.py` file parser | 1–3 (**inverted scale**) | 312 |

Post-processing: dedup by tick bucket keeping highest strength (`:459-467`), then a
**merge threshold** (default `16.0` points, `api_lines_create:509`) suppressing any line
within that distance of a stronger one. *16 points on M2K (~2,600) is a very different filter
than 16 points on MYM (~44,000) — the threshold is not scaled per symbol.*

### 3.3 Correlation

`lib/correlation_lab.py` — Pearson correlation of 30-min log-returns, aligned by timestamp
(alignment done on **full history before trimming**, a deliberate fix documented at `:102-111`).
Windows `[20,50,100]`, default `50`. **Purely a visualization; feeds no algorithm.** Tagged
`IMPL` as a tool, `DOC` as a strategy — the config comment itself calls it "meant to surface
ideas for a future correlation-based algo type."

---

## 4. Parameter Inventory

### 4.1 Configured parameters (`trader/config.yaml` — the live-engine source of truth)

| Algorithm | Parameter | Type | Current value | Rational range | Defined at | Hard-coded? | Should be configurable? | Effect |
|---|---|---|---|---|---|---|---|---|
| A1 | `symbols` | list[str] | `[MES]` | any of MES/MNQ/MYM/M2K | `trader/config.yaml:19` | No | already is | Which symbols decider/broker trade |
| A1, A9 | `orders.active_brackets` | list[float] | `[2, 4]` | 1–32 pts | `:33` | No | already is | TP/SL distance (symmetric) |
| all | `orders.tick_size` | float | `0.25` | per-symbol: .25/.25/1.0/.10 | `:35` | No | **must become per-symbol** — a single global tick is wrong for a 4-symbol system | Price rounding + STP offset |
| A1 | `orders.quantity` | int | `1` | 1–n | `:34` | No | already is | Contracts per order |
| A1 | `decider.replenishment_poll_seconds` | int | `10` | 5–60 | `:48` | No | already is | Replenishment cadence |
| broker | `broker.command_poll_seconds` | int | `5` | 1–30 | `:51` | No | already is | PENDING scan cadence |
| broker | `broker.ib_poll_seconds` | int | `30` | 5–60 | `:52` | No | already is | Fill/exit poll cadence |
| pos-mgr | `position.sl_cooldown_seconds` | int | `30` | 0–3600 | `:41` | No | already is | How long a line stays disarmed after an SL |
| pos-mgr | `position.stagnation_seconds` | int | `300` | — | `:39` | No | — | **Dead**: `position_manager.py` docstring explicitly says "No stagnation kill-switch". Only `simulator.simulate_exit(stag_seconds=…)` uses the concept |
| pos-mgr | `position.stagnation_min_move_points` | float | `0.5` | — | `:40` | No | — | Same — dead in live path |
| session | `session.max_restarts` | int | `5` | 1–20 | `:28` | No | already is | Crash-restart budget per component |
| session | `session.restart_backoff_base/cap_seconds` | int | `5` / `60` | — | `:29-30` | No | already is | Exponential backoff |
| session | `session.stop_grace_seconds` | int | `20` | 5–120 | `:31` | No | already is | Clean-shutdown window before `kill()` |
| session | `session.monitor_poll_seconds` | int | `5` | 1–30 | `:27` | No | already is | Liveness check cadence |
| IB | `ib.live_port` / `paper_port` | int | `4002` / `4002` | 4001 live / 4002 paper | `:6,9` | No | already is | **Both point at paper — intentional** |
| IB | `ib.live_client_ids` / `paper_client_ids` | list[int] | `101..120` / `201..210` | — | `:7,10` | No | already is | Connection ID pools |
| IB | `ib.connection_timeout` / `reconnect_interval_seconds` | int | `5` / `30` | — | `:12,11` | No | already is | Connect/retry timing |
| A7 | `algo_lab.strategies` | list[str] | 5 AlgoTypes | subset of `AlgoType.ALL` | `:84` | No | already is | Which strategies enter the grid |
| A7 | `algo_lab.tp_ticks` / `sl_ticks` | list[int] | `[4,8,16]` | 1–20 (`TP_TICK_OPTIONS`, `algo_engine.py:72`) | `:85-86` | No | already is | Bracket geometry axis |
| A7 | `algo_lab.direction_filters` | list[str] | `[ALL,BUY_ONLY,SELL_ONLY]` | — | `:87` | No | already is | Long/short bias test |
| A7 | `algo_lab.strength_max` | list[int] | `[1,2,3]` | **1–10 given the dashboard's scale** | `:88` | No | already is | **Currently filters out 100% of auto-detected lines (F4)** |
| A7 | `algo_lab.max_param_combos` | int | `24` | 1–405 | `:93` | No | already is | Grid downsample cap |
| A7 | `algo_lab.max_commands_per_submit` | int | `500` | — | `:95` | No | already is | Hard safety valve |
| corr | `correlation.windows` / `default_window` | list / int | `[20,50,100]` / `50` | — | `:104-105` | No | already is | Rolling window in 30m bars |
| corr | `correlation.max_series_points` | int | `500` | — | `:106` | No | already is | Chart point cap |
| paths | `paths.db`, `paths.bars`, `paths.history`, `paths.critical_lines` | str | relative to config file | — | `:56-63` | No | already is | **`paths.history` = `trader/data/history` which does not exist (F3)** |

### 4.2 Hard-coded values that materially change behaviour (should be parameters)

| Algorithm | Value | Type | Current | Rational range | file:line | Effect |
|---|---|---|---|---|---|---|
| A2–A8 | `TICK` | float | `0.25` | .10 / .25 / 1.0 | `lib/algo_engine.py:31` | Silent mis-pricing for MYM/M2K if caller omits `tick_size` |
| A8, backtester | `_TICK` | float | `0.25` | per-symbol | `cl_algo_backtester.py:47`, `cl_algo_full_duplex.py:47`, `simulator.py:37`, `generator.py:41`, `day_params.py:29` | **All backtest P&L for MNQ/MYM/M2K is computed in MES ticks** |
| simulator | `_MES_MULT` | float | `5.0` | 5.0/2.0/0.5/5.0 | `simulator.py:39`, `generator.py:42`, `reality_model.py:36` | Dollar P&L wrong for non-MES |
| simulator | `_SL_SLIP_TICKS` | int | `0` | 0–2 | `simulator.py:38` | **Zero slippage assumed on stop fills.** Comment says "data shows 64% have 0 slippage (was 1)" — a calibration decision baked in as a literal |
| simulator | `tp_confirm_ticks` | int | `2` | 1–5 | `simulator.py:58` | How many consecutive at/past-TP ticks confirm a fill. Is an axis in `june/bt_params.py` but a default here |
| simulator | OCO tie-break order | order | `SL > TP > STAG` | — | `simulator.py:122-137` | Conservative; a real modelling assumption |
| backtester | `_RTH_OPEN` / `_RTH_CLOSE` | tuple | `(8,30)` / `(15,15)` CT | — | `cl_algo_backtester.py:48-49`, `cl_algo_full_duplex.py:48-49` | Session window; no session-filter axis at all (unlike `june/bt_params.py`) |
| backtester | STP entry slippage | float | `+1 tick` | 0–3 | `cl_algo_backtester.py:163,166` | Only slippage model present |
| backtester | `DEFAULT_TP_TICKS` / `DEFAULT_SL_TICKS` | list | `[2,4,6,8,12]` | 1–20 | `cl_algo_backtester.py:52-53` | Default sweep grid |
| A8 | `_MIN_EXIT_TICKS` | int | `5` | 1–20 | `lib/day_params.py:34` | Minimum TP distance to accept a structural exit line |
| A8 | `_DEFAULT_TICK_BUFFER` | int | `1` | 0–4 | `lib/day_params.py:33` | Entry offset from the line. Stored per-run but **never varied** |
| A8 | `_DEFAULT_TWO_HOUR_AVG` | float | `10.0` pts | — | `lib/day_params.py:35` | Fallback exit distance when no prior-day data |
| A8 | `_WINDOW_HOURS` | int | `2` | 1–4 | `lib/day_params.py:32` | Volatility-estimation window |
| lines | merge threshold | float | `16.0` pts | 1–50, **should scale by symbol** | `trading_dashboard.py:509,825,859` | How aggressively nearby levels collapse |
| lines | strength values | int | `3..10` | — | `trading_dashboard.py:332-457` | Per-detector confidence. **Scale is inverted vs `lib/db.py` and `algo_engine` (F4)** |
| lines | `_ROUND_LEVELS` | dict | per-symbol intervals | — | `trading_dashboard.py:102-107` | Which round numbers count |
| lines | volume-area target | float | `0.70` | 0.5–0.9 | `trading_dashboard.py:419` | Value-area width |
| lines | `MAX_LINES_PER_SYMBOL` | int | `20` | — | `lib/critical_lines.py:30` | Rejects a levels file with more lines |
| lines | RTH bounds | int | `09:30–16:00` CT | — | `trading_dashboard.py:222,299-301` | **Different from the backtester's 08:30–15:15** — two RTH definitions in one repo |
| A9 | SL = 1 tick past line | float | `tick` | — | `trading_dashboard.py:1057-1063` | Defines an entirely different risk profile |
| A9 | candidate cap | int | `200` | — | `trading_dashboard.py:1092` | Silent truncation of the candidate list |
| broker | `_TICK_BY_SYMBOL` | dict | `{MES:.25, MNQ:.25, MYM:1.0, M2K:.10}` | — | `trader/broker.py:43` | **The only correct per-symbol tick table in the repo** |
| broker | `_STALE_SUBMITTED_MINUTES` | int | `10` | 1–60 | `trader/broker.py:49` | When an unmatched SUBMITTED becomes RECONCILE_REQUIRED |
| broker | `_MAX_RECONNECT_ATTEMPTS` | int | `5` | — | `trader/broker.py:40` | Reconnect budget before forcing SHUTDOWN |
| broker | rebase threshold | float | `slippage < tick` → skip | — | `trader/broker.py:496` | Minimum slippage before TP/SL are moved |
| broker | replenish candidate cap | int | `LIMIT 50` | — | `trader/broker.py:595` | Batch size per cycle |
| db | replenishment entry offset | float | `1 tick` for LMT/STP, `0` for MKT | — | `lib/db.py:735` | Replacement-order geometry |
| db | ancestry depth guard | int | `50` | — | `lib/db.py:701` | Max parent-chain walk |
| scorer | `MIN_N_FILLS` | int | `3` | 20–100 for real inference | `cl_algo_scorer.py:38` | **A combo is "ok" and rankable at n=3.** This is the single most dangerous default in the research layer |
| scorer | `WEIGHTS` | dict | exp .30 / pf .25 / wr .20 / sharpe .15 / sqn .10 | — | `cl_algo_scorer.py:41-47` | Composite score definition |
| scorer | `profit_factor` no-loss default | float | `999.0` | — | `cl_algo_scorer.py:69` | A combo with zero losses gets PF=999 — dominates ranking on tiny samples |
| learner | `_TOP_PCT` / `_EXPLORE_PCT` | float | `0.20` / `0.20` | 0.05–0.5 | `cl_algo_learner.py:45-46` | Hot-zone width / exploration budget |
| learner | `_FINE_RADIUS` | int | `3` ticks | 1–6 | `:47` | Fine-grid width around centroid |
| learner | `_CONVERGENCE_RUNS` / `_TOP_K` | int | `3` / `5` | — | `:48-49` | Convergence fingerprint |
| learner | `_MIN_N_FILLS_FOR_CONVERGENCE` | int | `30` | 100+ | `:50` | Monte-Carlo guard floor |
| learner | `_ALL_TP` / `_ALL_SL` | list | `[1..20]` | — | `:42-43` | Full searchable space |
| `june` scorer | `MIN_TRADES_FOR_SCORE` / `_FULL` | int | `5` / `20` | — | `bt_scorer.py:49-50` | Stricter than the active scorer |
| `june` scorer | `MC_PERMUTATIONS` / `MC_PVALUE_THRESHOLD` | int/float | `1000` / `0.05` | — | `:51-52` | Permutation test — **absent from the active scorer** |
| `june` scorer | `STABILITY_THRESHOLD` / `LOOCV_THRESHOLD` | float | `0.70` / `0.80` | — | `:53-54` | Anti-overfit guards — **absent from the active scorer** |
| dashboard | `_HIST_DIR` | Path | `C:\Projects\Galgo2026\june\trader\data\history` | — | `trading_dashboard.py:40` | **Absolute path into a fourth, external repo.** Currently populated (169 files, latest 2026-08-14) |

---

## 5. Current Experiment / Backtest Mechanism

### 5.1 The intended flow, traced through code

```
lib/data_availability.py::get_ready_days(db_path, history_dir, symbols)
    ├─ scans history_dir for {SYM}_trades_{YYYYMMDD}.csv and {SYM}_bid_ask_{YYYYMMDD}.csv
    ├─ requires ≥100 rows in each (_MIN_TRADES_ROWS / _MIN_BIDASK_ROWS, :27-28)
    └─ requires ≥1 armed critical_lines row for (symbol, date)
                       │
back-trading/run_cl_algo_pipeline.py::run_pipeline()          ← the common runner
    │  Stage 1  get_ready_days + summarise
    │  Stage 2a cl_algo_backtester.run()      → cl_algo_sim_results   (half-duplex combo matrix)
    │  Stage 2b cl_algo_full_duplex.run()     → cl_algo_fd_results    (structural exits)
    │  Stage 3  cl_algo_scorer.score()        → cl_algo_combo_scores + cl_algo_score_history
    │  Stage 4  inline FD summary SELECT (printed, not stored)
    │  Stage 5  cl_algo_learner.recommend()   → cl_algo_learner_runs + docs/learner_state.md
    ▼
back-trading/cl_algo_backtester.py::run()
    ├─ preloads all armed critical_lines into lines_cache            (:241-247)
    ├─ per (symbol, date): loads + trims TRADES/BID_ASK to RTH        (:264-281)
    ├─ current_price = first tick of the session                      (:283)
    ├─ **entry-fill cache** keyed (line_price, direction, entry_type) — entry does not
    │  depend on tp/sl, so it is computed once and reused across all combos  (:285-300)
    ├─ per combo × line: lib.algo_engine._build_cmds()                (:324)
    ├─ TP/SL recomputed **from the actual fill price**, not the planned entry (:346-351)
    ├─ simulator.simulate_exit(...)                                   (:354-360)
    └─ one executemany INSERT OR IGNORE per day                       (:389-402)
```

### 5.2 Direct answers

**Can multiple algorithms be run automatically?**
**Yes.** `build_combos()` (`cl_algo_backtester.py:174`) takes `algo_types` and defaults to
`AlgoType.ALL` (all 5). `run_pipeline` loops symbols. Also `cl_algo_worker.py` runs one
symbol per process with a PID lock file for parallelism.

**Can multiple parameter configurations be run automatically?**
**Yes.** Full Cartesian: 5 algo × 5 tp × 5 sl × 3 dir × 3 strength = **1,125 combos** by
default (`DEFAULT_*` at `cl_algo_backtester.py:52-56`). `june/back-trading/bt_params.py`
does the same over 6 axes = **10,800** param sets.

**Is there a common runner?** **Yes — `run_cl_algo_pipeline.py::run_pipeline()`.** It is the
single most reusable piece of research infrastructure here.

**Is there a common result schema?** **No — there are four incompatible ones.** See §8.2.

**How are results persisted?** SQLite, `INSERT OR IGNORE` against a `UNIQUE` constraint, which
is what makes every stage resumable and idempotent (explicitly self-tested at
`cl_algo_backtester.py:476-477`, `run_cl_algo_pipeline.py:232-233`).

**How are runs uniquely identified?**
**There is no run ID.** This is a structural gap.
- `cl_algo_sim_results` is keyed by the *content tuple*
  `(date, symbol, algo_type, tp, sl, direction_filter, strength_max, line_price, direction)`.
  Re-running with different code (e.g. a changed `_SL_SLIP_TICKS`) will **silently no-op**
  rather than produce a new result. Old results are indistinguishable from new ones.
- `cl_algo_combo_scores` / `cl_algo_score_history` use a `scored_at` ISO timestamp as a
  pseudo-run-id.
- `cl_algo_learner_runs` has an `iteration` counter derived from `COUNT(*)`.
- Only `june/back-trading/bt_db.py` has a real `runs` table with an id — and only for the
  old `sim`/`reality` engine, not for the matrix runner.

**How are algorithms compared?** `cl_algo_scorer.score()` min-max normalizes each metric
across the "ok" combos of one symbol and takes a weighted sum (`:198-213`), then ranks.
Comparison is therefore **only within one symbol and one scoring run** — there is no
cross-symbol or cross-time comparison anywhere.

**How are in-sample / out-of-sample periods handled?**
**They are not.** There is no train/test split, no walk-forward, no holdout, in any active
module. The only OOS-flavoured machinery in the repo is `june/back-trading/bt_scorer.py::loocv_score()`
(leave-one-out on the P&L series) — which is leave-one-*trade*-out, not leave-one-*period*-out,
and is in the archived folder.

**How are trading costs / slippage handled?**
Barely, and inconsistently:
- Commissions/fees: **not modelled anywhere.**
- Entry LMT: fills at exactly the limit price (`simulator.py:270`) — optimistic.
- Entry STP: `+1 tick` slippage, hard-coded (`cl_algo_backtester.py:163,166`).
- Exit SL: `_SL_SLIP_TICKS = 0` (`simulator.py:38`) — **zero stop slippage**.
- Exit TP: requires `tp_confirm_ticks=2` consecutive ticks at/past TP — a genuinely
  conservative touch, and the one honest realism feature.

**What prevents look-ahead bias?**
Two things do, one thing badly does not.
- ✅ `current_price` is taken as the **first tick of the session** (`cl_algo_backtester.py:283`).
- ✅ `simulate_exit` only scans `trades_df[time_utc > fill_time]` (`simulator.py:72`).
- ✅ `day_params` explicitly searches for a **prior**-day CSV (`day_params.py:129-131`).
- ❌ **But the critical lines themselves leak the future.** `_build_lines_for(sym, target, …)`
  builds levels from `target`'s own ticks and writes them with `date=target`
  (`trading_dashboard.py:773-815`); `/api/analyze_all` (`:844-853`) and `_build_db_thread`
  call it in bulk with `force=True`. `PDH`/`PDL` are then `max/min` of that whole session,
  `VWAP` the whole-day mean, `POC/VAH/VAL` the whole-day volume profile. The backtester then
  reads `critical_lines WHERE symbol=? AND date=?` for that same date. **Any result produced
  this way is invalid.**
  *(Note: the live path `/api/lines/create` does **not** have this problem — it walks back to
  a prior trading day's CSV, `:520-532`. The bias is confined to the batch/backtest path.)*

**What metrics already exist?**
Active (`cl_algo_scorer._compute_metrics`, `:56-80`): `win_rate`, `profit_factor`,
`expectancy`, `sharpe` (= mean/std × √N), `sqn` (identical formula), `composite_score`, `rank`,
`data_status`.
Archived (`june/bt_scorer.py`): the above plus `sortino`, `calmar`, `max_drawdown_t`,
`avg_win_loss`, `fill_rate`, `max_consec_loss`, `mc_pvalue`, `loocv_score`, `stability_zone`.
Live P&L (`lib/algo_pnl.get_breakdown`): `n_trades`, `wins`, `losses`, `win_rate`,
`total/avg_pnl_points`, `total/avg_pnl_dollars`, `profit_factor`.

### 5.3 Why it has produced nothing

| Table | Rows in `trader/data/galao.db` |
|---|---|
| `cl_algo_sim_results` | **0** |
| `cl_algo_fd_results` | **0** |
| `cl_algo_combo_scores` | **0** |
| `cl_algo_score_history` | **0** |
| `cl_algo_learner_runs` | **0** |
| `cl_algo_day_params` | **0** |
| `algo_runs` | 1 |
| `algo_candidates` | 0 |

Chain of causes:
1. `run_cl_algo_pipeline.py:258-259` sets `hist_dir = Path(cfg.paths.db).parent / "history"`.
2. `lib/config_loader._find_config()` picks the `config.yaml` nearest `sys.argv[0]`, so running
   `python back-trading/run_cl_algo_pipeline.py` loads **`back-trading/config.yaml`**, whose
   `paths.db = data/backtest.db` → `back-trading/data/backtest.db`, and `history_dir` →
   `back-trading/data/history`. **`back-trading/data/` does not exist.**
3. If run so that `trader/config.yaml` wins, `history_dir` → `trader/data/history`. **That does
   not exist either.**
4. Either way `get_ready_days()` returns `[]`, the pipeline prints "No ready days" and exits.
5. The empty-but-fully-schema'd `data/galao.db` at repo root is the fossil of exactly such a run.

**However — the pipeline did run successfully at least once, historically.**
`docs/learner_state.md` (2026-07-05) records iteration 2, "top-10 of 50 ranked combos",
convergence status EXPLORING, best combo `BOTH tp=12t sl=2t`, `PF 6.050`, `expectancy 5.05t`,
**`N fills: 4`**. Those underlying result rows no longer exist in any DB. A profit factor of
6.05 on **four fills** is noise, and it was nonetheless promoted to "Current Best Combo" in a
generated document. This is the clearest single illustration of why nothing here counts as
evidence.

---

## 6. Feedback-Loop Gap Analysis

Target loop: `algorithm + configuration → run experiments → collect standardized results →
compare → analyze strengths/failures → propose/select new candidates → run again`.

| Feedback-loop stage | Existing? | Existing implementation | Missing work |
|---|---|---|---|
| **Algorithm registry** (name → callable + metadata) | **Partial** | `lib/algo_engine.AlgoType.ALL` (5 names) + `ALGO_DESCRIPTIONS` dict (`:49-70`) is a genuine, if minimal, registry. But `decider.py`'s strategy, `api_trades_create`'s geometry, `random_gen`'s three variants, `cl_algo_full_duplex`'s structural-exit logic, and all 25 line detectors are **not** in it | One registry covering *all* signal producers and *all* level detectors, with a uniform callable signature. Today adding a strategy means editing an `if/elif` chain in `_pairs_for_line` (`:148-189`) |
| **Configurable parameter sets** | **Partial** | `AlgoParams` (`algo_engine.py:76`, `__slots__`, `to_dict()`), `algo_lab.build_param_grid()`, `cl_algo_backtester.build_combos()`, and the far better `june/bt_params.py::AXES` + `bt_param_sets` table with a real `param_set_id` and `get_neighbors()` | Persisted, ID'd, hashable param sets in the **live** schema. Today a config is a `params_json` string with no identity. §4.2 lists ~35 behaviour-changing values that are not parameters at all |
| **Batch experiment runner** | **Yes** | `run_cl_algo_pipeline.py::run_pipeline()`; `cl_algo_worker.py` for per-symbol parallelism with lock files; resumability via `INSERT OR IGNORE` + `UNIQUE` | Fix the `history_dir` resolution (F3); add a run-scoped identity so a code change produces new rows instead of a silent no-op; add cost/slippage as configurable inputs |
| **Standard result / trade schema** | **No** | Four incompatible schemas coexist (§8.2): `cl_algo_sim_results`, `cl_algo_fd_results`, `bt_matrix_results`, `completed_trades`/`verified_trades`. `pnl_ticks` vs `pnl_points` vs `pnl` (dollars) all appear | One canonical trade/result record spanning simulated and real trades, so a backtested combo and a live combo are directly comparable. This is the biggest single structural gap |
| **Comparison / ranking** | **Partial** | `cl_algo_scorer.score()` — 5 metrics, min-max normalize, weighted composite, rank. `algo_pnl.get_breakdown()` groups live P&L by `(symbol, source, algo_type, params_json, line_detect_algo)` — a genuinely good attribution axis | Cross-symbol and cross-run comparison; confidence intervals; `MIN_N_FILLS=3` raised to something defensible; port `sortino/calmar/max_drawdown/mc_pvalue` back from `june/bt_scorer.py` |
| **Strength / failure analysis** | **Weak** | `_has_stable_neighbor()` (`cl_algo_scorer.py:94`) is the only structural check. `exit_reason` and `ticks_to_exit` are stored per sim row. `cl_algo_fd_results` records `tp_source`/`sl_source` so fallback-vs-structural exits are separable | Regime/session breakdowns, drawdown paths, per-line-detector attribution in the *backtest* path (it exists only in the *live* path via `algo_pnl`), failure taxonomy |
| **Propose new candidates** | **Yes** | `cl_algo_learner.recommend()` — hot-zone centroid over top-20%, fine grid at `±_FINE_RADIUS`, 20% random exploration from unexplored space, `already_explored` set from the last 5 runs, convergence fingerprint over top-K across 3 runs, **plus a Monte-Carlo guard requiring N≥30 fills before declaring CONVERGED** (`:104-111`). This is real, self-tested closed-loop logic | It only ever narrows `tp_ticks`/`sl_ticks` — `all_algo_types/all_direction_filters/all_strength_max` are hard-coded to `1` (`:236-238`) and never narrowed. Recommendations are written to the DB but **nothing consumes them**: `run_pipeline` calls the learner *after* the backtester and discards the result (`:157`) |
| **Human review of candidates** | **Partial** | `docs/learner_state.md` auto-written (`cl_algo_learner._write_learner_state`). `algo_candidates` table exists with `rank`, `combined_score`, `queued_status='QUEUED'`, `command_ids` — clearly designed as a review queue | **No UI, no route, no writer.** `algo_candidates` is 0 rows and is referenced by zero `.py` files. It is a schema stub |
| **Re-run with new candidates** | **No** | — | The loop is open. Closing it needs `run_pipeline` to read the last `cl_algo_learner_runs` row and pass its grid into `build_combos()`. `cl_algo_worker.py::_get_learner_recommendation()` (`:58`) already implements the read side — it is simply not wired into `run_cl_algo_pipeline.py` |
| **Out-of-sample validation** | **No** | Nothing in the active path | Date-based train/test split; walk-forward; the whole concept is absent |
| **Promote to live** | **No** | Nothing reads a scored/converged combo and configures `decider.py` or `algo_lab` from it | A promotion path with an explicit human gate |

**Net:** the loop is roughly **60% built and 0% turning.** Six of eleven stages have real,
self-tested code. It fails on: a wrong directory (F3), no run identity, no shared schema, and
one missing wire between learner output and runner input.

---

## 7. Execution / Order-Tracking Deep Dive

This is the most valuable code in the repo. It is documented here in full detail deliberately.

### 7.1 One complete trade, traced end to end

**Step 0 — Signal.** `trader/decider.py:226 run_session_start()` (or the replenishment loop)
gets a price via `IBClient.get_price()`, calls `generate_commands()`. For each armed line ×
bracket × direction, the **dedup guard** (`:93-101`) checks whether that
`(critical_line_id, direction, bracket_size)` already has a PENDING/SUBMITTING/SUBMITTED
command; if so it is skipped. *This guard exists because on 2026-07-17 repeated session
restarts piled up 425 stale MES resting orders.*

**Step 1 — Order creation.** `determine_entry_type()` applies the toggle rule;
`calc_bracket_prices()` computes tick-rounded `entry/tp/sl`. One row is inserted into
`commands` with `status='PENDING'`, `source='critical_line'`, `critical_line_id` set
(`decider.py:120-131`). **The `commands.id` is the permanent logical trade ID** (see §7.2).

**Step 2 — Claim.** `broker.py:694 run_broker()` polls every `broker.command_poll_seconds`.
`process_pending_commands()` calls `_claim_command()` (`:209-223`):

```sql
UPDATE commands SET status='SUBMITTING', claimed_at=?
 WHERE id=? AND status='PENDING'
```

`cur.rowcount == 1` decides who won. **This is a correct compare-and-swap** and is the
mechanism that lets the same DB be safely shared with GevaExtract's external writer.

**Step 3 — Contract + bracket construction.** `IBClient.get_contract(symbol)` resolves the
front month via `reqContractDetails` sorted by `lastTradeDateOrContractMonth` and caches it
per client instance (`ib_client.py:199-224`). `order_builder.build_bracket()` then branches:
- **LMT entry** → `ib.bracketOrder(...)`, which returns 3 linked orders; all get `tif='GTC'`.
- **STP / MKT entry** → built manually, because `bracketOrder` only supports LMT entries.
  `entry.transmit=False`, `tp.transmit=False`, `sl.transmit=True` — the last child transmits
  the group.

**Step 4 — Submission and ID capture.** `order_builder.place_bracket()` (`:153-197`) — and
this is the subtle part:

```python
is_stp_entry = entry_order.orderType in ('STP', 'MKT')
if is_stp_entry:
    entry_trade = ib.placeOrder(contract, entry_order)
    ib.sleep(0.1)                       # let IB assign the real orderId
    real_id = entry_trade.order.orderId
    tp_order.parentId = real_id         # ← children re-parented to the *assigned* id
    sl_order.parentId = real_id
    tp_trade = ib.placeOrder(contract, tp_order)
    sl_trade = ib.placeOrder(contract, sl_order)
```

**This is the closest thing in the repo to "a child order's identity changes."** The children
are constructed before the parent's broker-assigned ID exists, and are re-parented to the real
ID immediately after placement. It is not a general ID-remapping facility — see §7.6.

**Step 5 — Persist broker IDs.** `broker.py:268-274` writes `status='SUBMITTED'` plus
`ib_order_id`, `ib_tp_order_id`, `ib_sl_order_id` in one statement. On any exception the
command goes to `status='ERROR'` with `error_message` (`:278-281`).

**Step 6 — Entry fill, path A (event-driven).** `register_ib_events()` (`:104-190`) wires
`execDetailsEvent` → `on_paper_exec` → `_handle_exec_fill(orderId, avgPrice, db_path)`
(`:77-101`). That looks up `WHERE ib_order_id=? AND status='SUBMITTED'`, writes
`FILLED + fill_price + fill_time`, calls `update_price_cache()`, and **appends to a
thread-safe `_rebase_queue`** rather than calling IB from the ib_insync event thread.

**Step 7 — Entry fill, path B (polling).** `poll_fills()` (`:286-372`) runs every
`ib_poll_seconds` as a safety net. It builds `{orderId: (status, avgFillPrice)}` from
`ibc.paper.trades()` and for each SUBMITTED command:
- `Filled` / `PartiallyFilled` → FILLED (see §7.9 on partials), queue rebase if the event
  handler did not already (`:356-358`).
- `Cancelled` / `Inactive` / `ApiCancelled` → `status='CANCELLED'`.
- **Not present at all** → if `_minutes_since(updated_at) > 10`, `status='RECONCILE_REQUIRED'`.
  *This exists because of the 2026-07-20 incident where 96 commands sat SUBMITTED forever,
  some for 18 days, because their order IDs had aged out of the session cache.*

**Step 8 — Bracket rebase against the real fill.** `_drain_rebase_queue()` (`:456-570`), run
from the **main loop** so IB calls are thread-safe:
- Pops the queue under `_rebase_lock`; **re-queues everything if IB is disconnected** (`:470-481`).
- `slippage = abs(fill_price - entry_price)`; if `< tick`, skip.
- Recomputes `new_tp/new_sl` at the original bracket distances **from the actual fill price**.
- Finds the contract from whichever child order is still in `ibc.paper.trades()`.
- Skips any child whose status is in `("Filled","Cancelled","Inactive")`.
- Mutates `lmtPrice`/`auxPrice` on the *existing* Order object and calls
  `ibc.paper.modifyOrder(contract, order)` — an in-place amend, so **the order ID is preserved**.
- Only if ≥1 child was modified does it write the new `tp_price`/`sl_price` back to `commands`.

**Step 9 — Exit detection.** `poll_tp_sl_fills()` (`:375-453`) builds `{orderId: avgFillPrice}`
for `Filled` orders and checks each FILLED command's `ib_tp_order_id` then `ib_sl_order_id`.
Crucially, the exit reason is **derived from price geometry, not from which order ID filled**
(`:424-435`):

```python
if d == "BUY":
    if   exit_price >= tp_p: exit_reason = "TP"
    elif exit_price <= sl_p: exit_reason = "SL"
    else:                    exit_reason = "STAGNATION"
```

Comment in code: *"immune to order-ID swap bugs."* This is the single most defensive design
decision in the codebase and it should be carried forward verbatim.

**Step 10 — Close and ledger.** `status='CLOSED'` with `exit_price/exit_time/exit_reason/
pnl_points`, then `record_completed_trade()` (`lib/db.py:662-688`) — `INSERT OR IGNORE` on
`UNIQUE(command_id)`, giving **exactly-once** semantics and a `True/False` return. Price cache
updated again.

**Step 11 — Replenishment (two independent mechanisms).**
- `decider.replenish()` (`:145-223`): marks `replenishment_issued=1` **atomically before**
  generating the replacement (`WHERE id=? AND replenishment_issued=0`, `:173-177`); re-checks
  the line is still `armed=1`; **re-evaluates the toggle rule at the current price**. Disabled
  entirely when `SESSION=SHUTDOWN`.
- `broker.replenish_if_enabled()` (`:573-622`): gated on `system_state.REPLENISH_ENABLED='1'`;
  finds CLOSED commands with no child; calls `db.spawn_replenishment()`, which picks a
  **random direction** and inherits `critical_line_id` from the chain root via
  `_root_critical_line_id()` (`:691-709`).

### 7.2 Data structures and identity

| Concept | Representation | Notes |
|---|---|---|
| **Logical trade ID** | `commands.id` (INTEGER AUTOINCREMENT) | ✅ **Yes — a permanent logical ID distinct from broker IDs exists.** Every downstream table (`completed_trades.command_id`, `positions.command_id`, `bt_matrix_results.trade_id`) keys off it |
| **Broker order IDs** | `commands.ib_order_id`, `ib_tp_order_id`, `ib_sl_order_id` | Three columns on the parent row. **No `bracket_map` table (F5)** |
| **Parent/child (bracket)** | IB-side: `Order.parentId` set in `place_bracket`. DB-side: the three ID columns | The bracket is flat in the DB — one row, three IDs |
| **Parent/child (logical lineage)** | `commands.parent_command_id` + `critical_line_id` | Replenishment chains. Walked recursively by `_root_critical_line_id()` and by the `verified_trades` `WITH RECURSIVE ancestry` CTE (`lib/db.py:404-416`), which also computes `chain_depth` |
| **Replacement IDs** | ❌ **None** | No `permId`, no `replaces_order_id`, no ID-history table |
| **Position** | `positions` table | Schema exists, **0 rows, no writer** |
| **Fills** | Not a first-class entity | Only `fill_price` + `fill_time` scalars on `commands`. Individual `Fill`/`Execution` objects are logged as free text into `ib_events` and never parsed back |
| **Raw broker events** | `ib_events(event_type, component, code, message, created_at)` | 268,063 rows. Append-only, text `message`, never machine-read |

### 7.3 State machine (all transitions, from code)

```
                 ┌──────────────────────────── decider / algo_lab / random_gen /
                 │                              dashboard / GevaExtract / spawn_replenishment
                 ▼
             PENDING
                 │  _claim_command()  UPDATE ... WHERE status='PENDING'   [atomic CAS]
                 ▼
            SUBMITTING ─────────────► PENDING        broker restart resets orphans (broker.py:708-714)
                 │
                 │  place_bracket() OK                        place_bracket() raises
                 ├──────────────────────────────►  ERROR  ◄────────────────┘  (+ error_message)
                 ▼
            SUBMITTED
                 │
                 ├─ execDetailsEvent  ────────────────►  FILLED     (_handle_exec_fill)
                 ├─ poll: Filled | PartiallyFilled ───►  FILLED     (poll_fills)
                 ├─ poll: Cancelled|Inactive|ApiCancelled ─► CANCELLED
                 └─ poll: absent > 10 min  ───────────►  RECONCILE_REQUIRED   ← terminal, nothing resolves it
                 ▼
              FILLED  ──(rebase: modifyOrder on TP/SL children, id preserved)──► FILLED
                 │
                 │  poll_tp_sl_fills(): a child order shows Filled
                 ▼
              CLOSED  ──► record_completed_trade()  ──►  completed_trades  ──►  verified_trades (view)
                 │
                 └─ replenish() / spawn_replenishment()  ──►  new PENDING (parent_command_id set)
```

Declared but **never written by any code**: `EXITING` (`lib/db.py:84`).
Written but **never read**: `RECONCILE_REQUIRED` (1,342 rows currently stranded).
Written only by `daily_paper_session.py`: `exit_reason` values `FORCE_CLOSE` (226) and
`SESSION_CLEANUP` (249).

### 7.4 Callbacks and events

Registered in `register_ib_events()` (`broker.py:104-190`), on the PAPER connection:
`errorEvent` → `on_paper_error` (classified INFO/WARNING/ERROR via `_classify_error`, with an
`_IB_INFO_CODES` allowlist at `:56-58`), `orderStatusEvent` → `on_paper_order_status`,
`execDetailsEvent` → `on_paper_exec` (**the only event that changes DB state**),
`connectedEvent`, `disconnectedEvent`. LIVE connection gets error/connect/disconnect only.
Handlers are **re-registered after every successful reconnect** (`:747-748`) — easy to get
wrong, correct here.

### 7.5 Reconciliation logic — three distinct mechanisms

1. **Stale-SUBMITTED sweep** (`poll_fills`, `:329-340`) — detects orders IB has no record of.
2. **Naked-position reconciliation on startup** (`reconcile_naked_positions`, `:625-691`).
   Runs once before the main loop. For any symbol with a non-zero IB position and **no resting
   orders**, it places an emergency `StopOrder(..., tif="GTC")` sized to the full position.
   The docstring is worth quoting because it encodes a real, expensive lesson:
   > *"Price is taken fresh from get_price() ... never from Position.avgCost — avgCost is
   > multiplier-scaled for futures (e.g. M2K x5, MNQ x2) and using it directly for an order
   > price is exactly the mistake that turned an intended resting stop into an instant-fill
   > market order during the 2026-07-20 incident."*
3. **Orphaned-SUBMITTING reset on startup** (`run_broker`, `:708-714`) — any command left
   mid-claim by a hard kill is returned to PENDING.

Additionally `daily_paper_session.py`: `cleanup_stale_open_rows()` (`:76`) and
`force_close_all()` (`:137`) do `reqGlobalCancel` + MKT-exit every FILLED position at session end.

### 7.6 Order-ID changes — what actually exists

The brief anticipated code handling child order IDs changing. Precisely stated:

- ✅ **Deferred parent-ID binding exists.** `place_bracket` (`:165-176`) places the entry
  first, sleeps 0.1 s, reads the broker-assigned `orderId`, and re-parents both children to it.
- ✅ **Order modification preserves identity.** `_drain_rebase_queue` amends the existing
  `Order` object in place via `modifyOrder`, so `ib_tp_order_id`/`ib_sl_order_id` remain valid.
- ✅ **Identity loss is detected and quarantined**, not silently ignored — the
  `RECONCILE_REQUIRED` path.
- ✅ **Exit classification is immune to ID mislabelling** because it is derived from price
  (`:424-435`), and `verified_trades` re-derives it a second time independently (`lib/db.py:432-439`).
- ❌ **There is no ID-remapping facility.** No `permId` is captured (`ib_insync` exposes it;
  nothing here reads it). No `replaces_order_id`, no ID-history. If IB ever returned a *new*
  order ID for a modified child, the DB would go stale and the command would eventually be
  flagged RECONCILE_REQUIRED — detected, but not repaired.

**Conclusion:** the system is *resilient to* ID confusion by construction (price-derived
truth + quarantine), rather than *tracking* ID changes. That is arguably the better design,
but it must be described accurately: **the "handles order ID changes" claim is best read as
"is immune to order ID changes."**

### 7.7 Restart-recovery

| Scenario | Handling |
|---|---|
| Broker killed mid-claim | SUBMITTING → PENDING on next start (`:708-714`) |
| Broker killed while positions open | `reconcile_naked_positions()` places emergency stops (`:726`) |
| Session stop | `SESSION=SHUTDOWN` in `system_state`; both processes poll it and exit; `session.py` waits `stop_grace_seconds` then `kill()` |
| Stale SHUTDOWN blocking a restart | `SessionManager._clear_stale_shutdown()` (`session.py:267-278`) — broker would otherwise exit on its first loop iteration |
| Two supervisors racing | PID-file lock, `_acquire_pid_lock()` with a cross-platform `_pid_alive()` using `OpenProcess` on Windows (`session.py:53-68, 288-297`) |
| Component crash | Exponential backoff `5s → cap 60s`, `max_restarts=5`, then marked dead (`session.py:235-253`) |
| `init_db` race between broker and decider | Documented and swallowed: the `verified_trades` DROP/CREATE dance is wrapped in `try/except sqlite3.OperationalError` with a comment explaining that either winner leaves the same correct view (`lib/db.py:616-634`) |
| In-flight rebases lost on disconnect | Items are pushed **back** onto `_rebase_queue` (`broker.py:471-481`) |

### 7.8 Failure handling

- Submission exception → `ERROR` + `error_message`, loop continues.
- IB disconnect → `IBClient.reconnect()` shuffles the client-ID pool (`_try_connect`,
  `ib_client.py:77-93`), up to `_MAX_RECONNECT_ATTEMPTS`; on exhaustion the broker
  **writes `SESSION=SHUTDOWN` itself and exits** (`:750-755`) — fail-closed.
- Every poll call site is individually try/except-wrapped so one failure cannot kill the loop
  (`:758-791`).
- `cancelMktData` failures are swallowed deliberately — IB may already have dropped the ticker
  after error 354 (`ib_client.py:168-171`).
- `atexit.register(self.disconnect)` in `IBClient.__init__`; `disconnect()` calls `ib.sleep(0)`
  first to drain pending events before the TCP FIN (`:285, :295`).

### 7.9 Partial fills — an explicit, documented limitation

`poll_fills:344-345`:
```python
if ib_status in ("Filled", "PartiallyFilled"):
    # R-ORD-13: treat all fills as complete (partial fills ignored in V1)
```
A partial fill is recorded as a complete fill at `avgFillPrice`, and the TP/SL children keep
their original full quantity. With `quantity=1` throughout, this is currently harmless. **It
becomes a correctness bug the moment quantity > 1.**

### 7.10 What must be preserved as-is

| Mechanism | Location | Why |
|---|---|---|
| Atomic claim lock | `broker.py:209-223` | Correct CAS; the only thing making a multi-writer shared DB safe |
| Dual fill detection (event + poll) | `broker.py:77-101` + `:286-372` | Event for latency, poll for correctness. Both feed the same rebase queue with dedup |
| **Price-derived exit reason** | `broker.py:424-435` + `lib/db.py:432-439` | Independent of order-ID labelling. Derived twice, independently |
| Fill-price bracket rebase | `broker.py:456-570` | Preserves intended R:R under slippage; correctly deferred out of the event thread; re-queues on disconnect |
| Naked-position reconciliation | `broker.py:625-691` | Encodes the `avgCost` multiplier trap. Prevents an unprotected position after any outage |
| Stale-SUBMITTED → RECONCILE_REQUIRED | `broker.py:329-340` | Bounds how long an order can be invisible |
| Decider dedup guard | `decider.py:93-101` | The fix for the 425-stale-order incident |
| Atomic replenishment flag | `decider.py:173-177` | `WHERE replenishment_issued=0` prevents double-replenishment |
| Exactly-once ledger write | `lib/db.py:662-688` | `INSERT OR IGNORE` on `UNIQUE(command_id)` |
| `verified_trades` integrity filters | `lib/db.py:456-477` | P&L arithmetic cross-check, instant fill+exit exclusion, fill-inside-bracket check |
| Session supervisor | `trader/session.py` | PID lock, stdout capture, backoff, `SESSION=SHUTDOWN` clean stop, stale-flag clearing |
| Config-by-explicit-path | `session.py:84`, `trading_dashboard.py:138-154` | The documented workaround for the global config cache |
| Universal `--self-test` | every module | ~35 modules, each self-verifying without IB |

---

## 8. Existing Schemas / Ledgers

### 8.1 Storage inventory (verified counts)

| File | Size | Purpose | Key contents |
|---|---|---|---|
| `trader/data/galao.db` | 56.7 MB | **Live shared DB** (WAL) | `commands` 49,210 · `ib_events` 268,063 · `completed_trades` 4,315 · `price_profile` 37,031 · `critical_lines` 744 · `verified_trades` (view) 998 · `release_notes` 43 · `algo_runs` 1 · `price_cache` 2 · `system_state` 2 · `positions` 0 · `fetch_log` 0 · `algo_candidates` 0 · all six `cl_algo_*` **0** |
| `trader/data/bars.db` | 182 MB | 30-min OHLCV | `bars_30m` 340,542 · `bars_30m_normalized` 255,372 · `bars_30m_diffs` 255,274 · `bars_30m_diffs_normalized` 255,274 · `bars_30m_normalize_meta` 3 |
| `trader/data/bars.db.pre7y.bak` | 25 MB | Pre-Databento backup | — |
| `data/galao.db` | 176 KB | **Empty duplicate**, full schema | all tables 0 rows — fossil of a mis-pathed pipeline run (F3) |
| `june/trader/data/bt.db` | — | **Largest experiment set** | `bt_matrix_results` **9,720,000** · `bt_param_sets` 10,800 · `bt_scores` 0 · `bt_score_history` 0 · `bt_commands` 0 · `bt_runs` 0 |
| `june/trader/data/galao.db` | — | Pre-split live DB | `commands` 19,803 · `completed_trades` 4,030 · `verified_trades` 900 |
| `data/bars_7years_30m_{MES,MNQ,MYM,M2K,all}.csv` | 47 MB | Databento 30m history from 2019-05-05 | source for `import_7year_bars.py` |
| `C:\Projects\Galgo2026\june\trader\data\history\` | — | **Tick CSVs actually in use** (external repo) | 169 files, latest 2026-08-14 |
| `data/critical_lines/levels_daily_MES_2026040{7,8}.txt` | — | Original file-based line input | 2 files; superseded by the DB/UI path |
| `trader/logs/` | ~470 MB | Unrotated logs | `ib_client.log` 144 MB, `broker_stdout.log` 170 MB, `decider_stdout.log` 115 MB |

### 8.2 Concept → representation matrix (multiple incompatible structures shown, not merged)

| Concept | Representation(s) | Verdict |
|---|---|---|
| **Experiment run** | (a) `cl_algo_combo_scores.scored_at` (ISO string) · (b) `cl_algo_learner_runs.iteration` (from COUNT) · (c) `june/bt_db.runs(id, date, symbol, mode, created_at)` — a real run table, but only for the old sim/reality engine · (d) *nothing at all* for `cl_algo_sim_results` and `bt_matrix_results` | **Four different answers. No canonical run entity** |
| **Algorithm** | (a) `AlgoType.ALL` + `ALGO_DESCRIPTIONS` (`algo_engine.py:39-70`) · (b) `commands.source` TEXT (free-form: `critical_line`/`algo_lab`/`cl_algo`/`geva_extract`/`random_*`/`trading_dashboard`/`test`) · (c) `commands.algo_type` TEXT (strategy) · (d) `critical_lines.algo_type` TEXT (**level detector — same column name, different meaning**) | `algo_type` means two unrelated things depending on table. `source` is an unconstrained string |
| **Configuration** | (a) `commands.params_json` TEXT — canonical JSON via `algo_lab.combo_params_json()` (`sort_keys=True`) · (b) `cl_algo_sim_results` / `cl_algo_combo_scores` — 5 params as separate columns · (c) `cl_algo_fd_results` — `two_hour_avg_move` + `tick_buffer` columns · (d) `june/bt_param_sets` — **a real table with `param_set_id` and a UNIQUE over 6 axes** | (d) is the only one with identity. (a) is a string. (b)/(c) are wide columns |
| **Signal** | ❌ **Does not exist as an entity.** A signal is materialized directly as a `commands` row or a sim row | A signal is never observable independently of an order |
| **Logical trade** | ✅ `commands.id`, plus `parent_command_id` lineage and the `verified_trades` recursive-ancestry CTE with `root_cmd_id`/`root_critical_line_id`/`chain_depth` | **The one genuinely well-designed identity in the repo** |
| **Broker orders** | `commands.ib_order_id`, `ib_tp_order_id`, `ib_sl_order_id` (3 nullable INTEGER columns on the parent) | Flat. No `permId`, no order table, **no `bracket_map` (F5)** |
| **Fills** | `commands.fill_price`, `fill_time`, `exit_price`, `exit_time` (scalars). Raw executions only as free text in `ib_events.message` | No fill entity; partial fills unrepresentable (§7.9) |
| **Position** | `positions` table (`command_id`, `direction`, `quantity`, `entry_price`, `price_at_check`, `status`) | **Schema only — 0 rows, no writer** |
| **Closed trade** | (a) `completed_trades` (4,315) · (b) `verified_trades` VIEW (998) with **re-derived** `exit_reason` and 7 integrity filters · (c) `cl_algo_sim_results` (sim) · (d) `cl_algo_fd_results` (sim) · (e) `bt_matrix_results` (sim) | Five shapes. **`pnl_points` (a,b) vs `pnl_ticks` (c,d,e) vs `pnl` in dollars (`simulator.simulate_exit`)** — three P&L units in one codebase |
| **P&L** | `pnl_points` · `pnl_ticks` · `pnl` (USD via `_MES_MULT=5.0`) · `algo_pnl.SYMBOL_MULTIPLIERS = {MES:5.0, MNQ:2.0, MYM:0.5, M2K:5.0}` | Only `algo_pnl` gets per-symbol dollars right. **The simulator applies MES's ×5 to every symbol** |
| **Experiment metrics** | (a) `cl_algo_combo_scores` — 5 metrics + composite + rank + `data_status` · (b) `cl_algo_score_history` — top-combo snapshot per run · (c) `june/bt_scores` + `bt_score_history` — 12 metrics + 4 guards (0 rows) | (c) is the better schema and is empty and archived |

### 8.3 The `verified_trades` view — the most trustworthy artifact in the repo

`lib/db.py:402-478`. It is not a convenience view; it is a **data-integrity gate**:

1. `WITH RECURSIVE ancestry` walks `parent_command_id` to the chain root, carrying
   `root_critical_line_id` and `chain_depth`, so a 4th-generation replenishment is still
   attributable to the line that started it.
2. `exit_reason` is **re-derived from prices**, with the broker's own label preserved
   separately as `raw_exit_reason`.
3. Seven exclusion filters: `source IS NOT NULL`, `source != 'test'`, all five
   fill/exit/pnl/time fields non-null, `fill_time != exit_time` (mass-reconnect artifacts),
   `ABS(pnl_points − (exit−fill signed by direction)) < 0.01` (**arithmetic cross-check that
   catches write-path bugs**), and `NOT (fill already past TP or SL)` (stale/gap fills).

**It rejects 77% of the ledger** — 4,315 `completed_trades` → 998 rows. That gap is itself a
finding: `completed_trades` should not be used for any analysis. Compare:

| source | `completed_trades` n / total pts | `verified_trades` n / total pts |
|---|---|---|
| `random_mkt` | 3,074 / **+4,280.85** | 508 / **−21,893.75** |
| `random_stp` | 479 / +4,702.25 | 232 / −115.00 |
| `random_lmt` | 468 / −18,658.50 | 222 / −1.25 |
| `critical_line` | 245 / −392.50 | 14 / −159.50 |
| `geva_extract` | 49 / +310.25 | 22 / +848.75 |

The sign flips on three of five sources. Anyone reading `completed_trades` directly would
conclude `random_mkt` is profitable. It is not.

And within `verified_trades`, 166 of 998 rows have `raw_exit_reason` ∈
{`FORCE_CLOSE`, `SESSION_CLEANUP`} re-derived to `STAGNATION` — i.e. **17% of the "verified"
ledger is forced session-end closes, not strategy exits.**

---

## 9. KEEP AS-IS / WRAP / REWORK / RESEARCH ONLY

*Conservative on infrastructure, strict on algorithms — as instructed.*

### 9.1 KEEP AS-IS — proven infrastructure

| Component | Files / functions | Justification |
|---|---|---|
| Order lifecycle core | `trader/broker.py` — `_claim_command`, `_handle_exec_fill`, `register_ib_events`, `poll_fills`, `poll_tp_sl_fills`, `_drain_rebase_queue`, `reconcile_naked_positions` | Every guard traces to a dated production incident. Running continuously for months. §7.10 |
| Bracket construction | `lib/order_builder.py` — `determine_entry_type`, `calc_bracket_prices`, `build_bracket`, `place_bracket`, `round_tick` | Handles the `bracketOrder`-only-supports-LMT case correctly; deferred parent-ID binding; fully self-tested offline via `_FakeIB` |
| IB connection manager | `lib/ib_client.py::IBClient` | Client-ID pool with shuffle, delayed-data type 3, front-month resolution + cache, `ib.sleep(0)` drain before disconnect, `atexit` cleanup |
| Session supervisor | `trader/session.py::SessionManager` | PID lock with cross-platform liveness, stdout capture, backoff, stale-SHUTDOWN clearing. Verified 2.9-day continuous run |
| DB access layer | `lib/db.py` — `get_db` (WAL + busy_timeout + FK + rollback), `update_command_status`, `record_completed_trade`, `set/get_system_state`, `update_price_cache` | Correct, small, self-tested |
| `verified_trades` view | `lib/db.py:402-478` | §8.3. **Do not weaken any of its filters** |
| SL cool-down | `trader/position_manager.py::check_sl_cooldowns` | Small, correct, self-tested |
| Self-test discipline | ~35 `--self-test` entry points + `trader/regression.py` (3 layers) | The reason this codebase is reliable at all. Carry the *convention* forward, not just the code |
| Decider dedup + atomic replenishment | `trader/decider.py:93-101`, `:173-177` | Incident fixes |
| Bars pipeline | `scripts/backfill_bars.py`, `import_7year_bars.py`, `build_bars_normalized.py`, `build_bars_diffs.py`, `sanity_check_bars*.py` | 7 years × 4 symbols, `INSERT OR IGNORE` so live data is never clobbered, normalization basis persisted in `bars_30m_normalize_meta` for reproducibility, with sanity checkers |

### 9.2 WRAP — good code, wrong interface

| Component | Files | What to wrap |
|---|---|---|
| Fill simulator | `back-trading/simulator.py` — `simulate_exit()`, `simulate()` | The fill model is sound (BID_ASK for LMT, TRADES for STP, SL-before-TP tie-break, `tp_confirm_ticks`). Wrap so `_TICK`, `_MES_MULT`, `_SL_SLIP_TICKS`, `tp_confirm_ticks` are **injected per symbol**, not module constants. Also calibrated against real fills by `calibrate.py` — keep that link |
| Batch pipeline | `back-trading/run_cl_algo_pipeline.py`, `cl_algo_worker.py` | The stage sequencing and resumability are right. Wrap with correct path resolution, a real `run_id`, and the learner→runner wire |
| Strategy engine | `lib/algo_engine.py` | `AlgoParams` + `_pairs_for_line` + `_calc_prices` are a clean seam. Wrap behind a registry so a strategy is a registered callable, not an `elif` branch. **Fix the strength-scale contract first (F4)** |
| Scorer | `back-trading/cl_algo_scorer.py` | Structure is fine. Wrap to accept metric/weight config, raise `MIN_N_FILLS`, and add the `june/bt_scorer.py` metrics |
| Learner | `back-trading/cl_algo_learner.py` | Hot-zone + exploration + convergence + Monte-Carlo guard are real. Wrap so it can narrow **all** axes, and so its output is actually consumed |
| Live P&L attribution | `lib/algo_pnl.py` | `get_breakdown()`'s `(symbol, source, algo_type, params_json, line_detect_algo)` grouping is exactly the right attribution axis. Wrap to also read simulated results, not only `verified_trades` |
| Data availability | `lib/data_availability.py` | Good gate. Wrap to take an explicit history path instead of deriving it from the DB path |
| Correlation | `lib/correlation_lab.py` | Correct and defensive (alignment-before-trim, graceful missing-symbol). Wrap into a general feature layer |
| Line detectors | `trading_dashboard._generate_lines()` | 25 detectors with formulas + inputs recorded per line — a real asset. Extract from the Flask file into a library, one function per detector. **Fix the strength scale and the self-day leak (F2/F4)** |
| Price profile | `lib/price_profile.py` | 37,031 rows of real microstructure. Wrap the history path |

### 9.3 REWORK — genuinely needs redesign

| Component | Files | Why |
|---|---|---|
| Config loading | `lib/config_loader.py` | A single global `_cached` slot keyed on **whichever script started first**. Two `config.yaml` files with conflicting `ib.live_port` (4001 vs 4002). Three separate modules independently work around it (`session.py:84`, `trading_dashboard.py:138-154`, `:122-135`). `self_test()` asserts `live_port == 4001` and therefore **fails against the live config**. One config, explicit loading, no global cache |
| Path resolution | `trading_dashboard.py:40`, `run_cl_algo_pipeline.py:258-259`, `cl_algo_backtester.py:528`, `cl_algo_full_duplex.py:509`, `trader/config.yaml:60` | An absolute path into a fourth repo; three modules deriving `history` from a DB path to directories that do not exist. **Direct cause of F3** |
| The dashboard monolith | `back-trading/trading_dashboard.py` (4,600 lines / 234 KB) | Flask routes, 25 line-detection algorithms, tick loading, trade generation, and ~3,000 lines of HTML/JS/Plotly in one `HTML` string constant. No hot reload, no route tests. Business logic must be extracted before anything is reused |
| Result schemas | `cl_algo_sim_results`, `cl_algo_fd_results`, `bt_matrix_results`, `completed_trades` | Four shapes, three P&L units, no run identity. §8.2 |
| Strength semantics | `lib/db.py:139` vs `algo_engine.py:196` vs `trading_dashboard.py:332-457` vs `api_lines.min_strength` | Two contradictory scales in one system, silently. **F4** |
| Per-symbol constants | `_TICK`/`_MES_MULT` in `simulator.py`, `generator.py`, `algo_engine.py`, `cl_algo_backtester.py`, `cl_algo_full_duplex.py`, `day_params.py`, `reality_model.py` | MES's tick and multiplier hard-coded across a 4-symbol system. `trader/broker.py:43`'s `_TICK_BY_SYMBOL` is the only correct table — promote it |
| RECONCILE_REQUIRED handling | `broker.py:331-339` | Detected, never resolved. 1,342 stranded rows |
| Log management | `trader/logs/` | ~470 MB unrotated; `ib_client.log` alone is 144 MB |
| `ib_events` as free text | `broker.py:137-151` | 268,063 rows of unparseable prose. Should be structured columns (`order_id`, `status`, `filled`, `remaining`, `avg_fill_price`) |
| Duplicate/legacy trees | `june/`, `back-trading/versions/`, `versions/`, `trader/visualizer/`, `back-trading/algo_dashboard.py`, `back-trading/visualizer/`, `trader/runner.py`, `trader/fetch_*.py`, `trader/create_presentation.py`, `algo-analyzer/` | 4 dashboards, 2 brokers, 2 deciders, 11 simulator snapshots. **Extract `june/bt_scorer.py` and `june/bt_params.py` before archiving `june/` (F6)** |
| Documentation drift | `GALGO2027_HANDOFF.md`, `june/CLAUDE.md`, `weekplan_jun6.md`, `docs/*.md` | `bracket_map` described as a core invariant across three documents; it has never existed (F5). `CLAUDE_STATE.md` itself warns future readers not to trust its own version claims. Treat every doc in this repo as a hypothesis |

### 9.4 RESEARCH ONLY — keep for experimentation, not proven trading logic

**Everything in §3 belongs here.** No exceptions. Specifics:

| Item | Files | Why it is research only |
|---|---|---|
| A1 `decider_critical_line` | `trader/decider.py` | The most-exercised strategy; `verified_trades` n=14, **−159.5 pts, 0 TP exits, 7 SL**. Reliable code, no evidence of edge |
| A2–A6 the 5 `AlgoType` strategies | `lib/algo_engine.py` | Clean, self-tested, plausible taxonomy (BOUNCE/BREAKOUT/DIRECTIONAL/FADE/BOTH). **Zero live trades, zero stored sim results.** No evidence in either direction |
| A7 Algo Lab | `lib/algo_lab.py` | The grid/dedup/cap design is genuinely good and worth keeping. It has produced **no data at all** |
| A8 full-duplex structural exits | `back-trading/cl_algo_full_duplex.py` | The most interesting *idea* here — exits at the next real S/R level rather than a fixed tick count, with full traceability of which line served as TP/SL and why a fallback fired. **0 result rows** |
| A9 dashboard create-trades | `trading_dashboard.py:1022` | A 1-tick stop is a distinct high-frequency-stop-out profile. 5 commands, all cancelled |
| A10–A12 random baselines | `trader/random_gen.py` | Valuable **as a null hypothesis** — they are the largest verified sample and they lose money, which correctly sets the bar. Never a strategy |
| A13 synthetic generator | `back-trading/generator.py` | A calibration fixture |
| A14 `bt_matrix` sweep | `june/back-trading/*` | 9.72 M rows, but: entries are **not simulated** (real fills reused), **56% EXPIRED and excluded** from the mean (severe selection bias), 900 trades × 10,800 param sets with **no out-of-sample split**, and `PRE_MARKET` — where the code comment says **69% of verified trades occur** — excluded from the axes. The headline `avg pnl_ticks ≈ 7.0` is **not** a result |
| The 25 line detectors | `trading_dashboard._generate_lines` | Standard, well-documented formulas with per-line provenance. But `VWAP` is not volume-weighted (acknowledged in code), `PDH/PDL` are mislabelled (they are session extremes of whatever ticks are passed), and in the batch path they leak the future (F2) |
| Correlation | `lib/correlation_lab.py` | Numerically correct, feeds nothing |
| `docs/learner_state.md` | — | The "Current Best Combo" (`PF 6.050` on **N=4**) is the canonical example of what not to promote |
| All of `docs/*.md` | `system_design.md`, `algo_implementation_plan.md`, `rules_book.md`, `trader_book.md`, `walkthrough_book.md`, `cl_algo_backtest_design.md`, … | Design intent, not implementation. `cl_algo_backtest_design.md` §3 specifies 2,880 combos/day/symbol; the code implements 1,125. Read as history |

---

## 10. Future-Capability Gap Matrix

| # | Capability | Status | Evidence |
|---|---|---|---|
| 1 | **Configurable algorithm registry** | **Partially exists** | `AlgoType.ALL` + `ALGO_DESCRIPTIONS` (`algo_engine.py:39-70`) is a 5-entry registry. Strategy selection is an `if/elif` chain (`_pairs_for_line:148-189`), not a lookup. `decider.py`, `api_trades_create`, `random_gen`, `cl_algo_full_duplex`, and 25 line detectors are outside it |
| 2 | **Configurable parameter sets** | **Partially exists** | `AlgoParams` + `build_param_grid()` + `commands.params_json` (canonical `sort_keys` JSON). Real identity only in `june/bt_param_sets` (`param_set_id`, UNIQUE over 6 axes, `get_neighbors()`). ~35 behaviour-changing values are still literals (§4.2) |
| 3 | **Batch experiment runner** | **Exists (broken wiring)** | `run_cl_algo_pipeline.py::run_pipeline()` — 5 stages, symbol loop, resumable, idempotent, self-tested end-to-end. `cl_algo_worker.py` adds lock-file parallelism. Blocked entirely by F3 |
| 4 | **Standard result / trade schema** | **Absent** | Four incompatible schemas, three P&L units, no run ID (§8.2). The single biggest structural gap |
| 5 | **Comparison / ranking of configurations** | **Partially exists** | `cl_algo_scorer.score()` — normalize + weighted composite + rank + `data_status`. Within one symbol and one `scored_at` only. `MIN_N_FILLS=3` and `profit_factor=999.0` for zero-loss combos make small-sample noise win |
| 6 | **Out-of-sample validation** | **Absent** | No split, no walk-forward, no holdout anywhere in the active path. Only `june/bt_scorer.py::loocv_score()` (`:214`), which is leave-one-trade-out and is archived |
| 7 | **Feedback / optimization loop** | **Partially exists** | `cl_algo_learner.recommend()` writes a real next-grid recommendation. `cl_algo_worker._get_learner_recommendation()` (`:58`) implements the read side. **The two are never connected** — `run_pipeline:157` calls the learner last and discards its output |
| 8 | **Human review of candidates** | **Absent (schema stub only)** | `algo_candidates` table (`lib/db.py:347-369`) has `rank`, `combined_score`, `queued_status='QUEUED'`, `command_ids` — designed for exactly this. **0 rows, zero code references.** `docs/learner_state.md` is the only human-facing output |
| 9 | **Paper / live execution** | **Exists — the strongest capability** | Full stack, running now on IB paper `:4002`. §7. Live (`4001`) deliberately never connected — `trader/config.yaml:6` sets `live_port: 4002` |
| 10 | **Robust order/trade lifecycle tracker** | **Exists — with named gaps** | §7. Permanent logical ID, atomic claim, dual fill detection, price-derived exits, fill rebase, three reconciliation paths, restart recovery. Gaps: no fill entity, `positions` unwritten, partial fills ignored (`broker.py:344-345`), `RECONCILE_REQUIRED` never resolved, no `permId` |
| 11 | **Shared P&L / effect analysis** | **Partially exists** | `verified_trades` + `algo_pnl.get_breakdown()` with per-symbol multipliers and level-detector attribution — genuinely good, but **live trades only**. Backtest results have no equivalent, and use different units |
| 12 | **Visualization** | **Exists (monolithic)** | `trading_dashboard.py` v5.03: Overview, Levels (Lines/Sandbox/Create Trades), Charts (Graph/All/Test/Sup-Res Viz), Correlation, Algo Lab (Grid & Submit / P&L Breakdown), Trading (Submitted). Plotly candlesticks with colour-coded S/R overlays and formula tooltips. All in one 4,600-line file with ~3,000 lines of inline HTML/JS |

---

## 11. Mechanisms That Must Not Be Lost in a Rebuild

Ordered by cost-of-rediscovery. Each is a lesson someone already paid for.

1. **Price-derived exit classification.** `broker.py:424-435` and again, independently, in the
   `verified_trades` view (`lib/db.py:432-439`). Never trust which order ID filled; trust where
   the price was. Deriving it twice by different routes is the reason the P&L ledger is
   auditable at all.
2. **Atomic claim lock.** `broker.py:209-223`. `UPDATE ... WHERE id=? AND status='PENDING'` +
   `rowcount==1`. This is what makes a multi-process, multi-repo shared SQLite DB safe.
3. **Naked-position reconciliation with the `avgCost` warning.** `broker.py:625-691`. Preserve
   the docstring verbatim: `Position.avgCost` is multiplier-scaled for futures, and using it as
   an order price turns an intended resting stop into an instant market order. That mistake
   cost a real incident on 2026-07-20.
4. **Fill-price bracket rebase, executed off the event thread.** `broker.py:456-570`. Preserves
   intended R:R under slippage; the queue-and-drain pattern (`_rebase_queue` + `_rebase_lock`)
   is required because ib_insync event handlers must not make IB calls; and the re-queue on
   disconnect (`:471-481`) prevents silent loss.
5. **The `verified_trades` integrity filters.** `lib/db.py:456-477`. The arithmetic
   cross-check, the `fill_time != exit_time` exclusion, and the fill-inside-bracket check
   reject 77% of the raw ledger and flip the sign of three sources (§8.3).
6. **Recursive lineage.** `parent_command_id` + `_root_critical_line_id()` + the
   `WITH RECURSIVE ancestry` CTE. An Nth-generation replenishment stays attributable to the
   line that originated it.
7. **Generation dedup guards.** `decider.py:93-101` (keyed on line/direction/bracket) and
   `algo_lab.py:84-103` (additionally keyed on the exact param combo, so different combos on
   one line do not block each other). The 425-stale-order incident is what these prevent.
8. **Stale-order quarantine.** `broker.py:329-340`. Bounds invisibility to 10 minutes.
   *Also add the missing resolver.*
9. **Session supervision.** `trader/session.py` — PID lock with real liveness check, stdout to
   files, exponential backoff, `SESSION=SHUTDOWN` cooperative stop, and
   `_clear_stale_shutdown()` (without which broker exits instantly on the next start).
10. **Exactly-once ledger writes.** `record_completed_trade()` — `INSERT OR IGNORE` on
    `UNIQUE(command_id)`, returning whether it inserted.
11. **Universal `--self-test`.** ~35 modules that verify themselves without IB, plus
    `trader/regression.py`'s three layers. This convention is why the execution layer is
    trustworthy. Carry the *convention*, not just the tests.
12. **Idempotent, resumable experiment writes.** `UNIQUE` + `INSERT OR IGNORE` +
    per-(symbol,date) `done_set` pre-fetch (`cl_algo_backtester.py:302-310`). Kill the pipeline
    at any point and re-run.
13. **Entry-fill caching in the backtester.** `cl_algo_backtester.py:285-300`. Entry fills do
    not depend on TP/SL, so computing them once per `(line, direction, entry_type)` collapses
    the cost of a 1,125-combo sweep by orders of magnitude.
14. **The learner's Monte-Carlo guard.** `cl_algo_learner.py:104-111`. Refuses to declare
    CONVERGED on a stable fingerprint until N≥30 fills, and says so in the reasoning string.
    The correct instinct — the threshold just needs to be much higher.
15. **`INSERT OR IGNORE` on the bars merge.** `scripts/import_7year_bars.py`. Historical CSV
    import can never regress fresher live-backfilled rows. Plus the persisted normalization
    basis (`bars_30m_normalize_meta`) so a later pull can be normalized identically.
16. **Config-by-explicit-path workarounds.** `session.py:84` and `trading_dashboard.py:138-154`
    with their comments. Keep the *comments* as the rationale for why the new system must not
    have a global config cache.
17. **Per-line provenance.** `critical_lines.note` stores `{label, formula, inputs, from_date,
    merged}` as JSON. Every level can explain itself, including which weaker levels were merged
    into it. Rare and valuable.
18. **`_TICK_BY_SYMBOL`.** `broker.py:43`. The only correct per-symbol tick table in the repo.
19. **Fail-closed on reconnect exhaustion.** `broker.py:750-755` — the broker writes
    `SESSION=SHUTDOWN` itself and exits rather than continuing blind.
20. **`SYMBOL_MULTIPLIERS`.** `algo_pnl.py:38` — the only place dollar P&L is computed
    correctly per instrument.

---

## 12. Files the Future Project Would Likely Need

### 12.1 Port largely intact (execution core)

| File | Notes |
|---|---|
| `trader/broker.py` | The single most valuable file. Port with: fill entity, partial-fill handling, `RECONCILE_REQUIRED` resolver, `permId` capture |
| `lib/order_builder.py` | Port; parameterize `tick_size` per symbol |
| `lib/ib_client.py` | Port; add `permId` exposure |
| `trader/session.py` | Port as-is |
| `lib/db.py` | Port `get_db`, `update_command_status`, `record_completed_trade`, `set/get_system_state`, `update_price_cache`, `_root_critical_line_id`, and **the `verified_trades` view definition verbatim** |
| `trader/decider.py` | Port the dedup guard and atomic replenishment; the strategy itself is research only |
| `trader/position_manager.py` | Port `check_sl_cooldowns` |
| `lib/logger.py` | Port; add rotation |
| `trader/regression.py` | Port the 3-layer structure |

### 12.2 Port with a new interface (research core)

| File | Rework needed |
|---|---|
| `back-trading/simulator.py` | Inject per-symbol tick/multiplier/slippage; keep the fill model and OCO tie-break |
| `back-trading/run_cl_algo_pipeline.py` | Fix path resolution; add `run_id`; wire learner→runner |
| `back-trading/cl_algo_backtester.py` | Keep the entry cache + resumability; fix `_TICK`; add costs |
| `back-trading/cl_algo_full_duplex.py` | Keep the structural-exit idea and its traceability; fix `_TICK` |
| `back-trading/cl_algo_scorer.py` | Merge with `june/bt_scorer.py`'s metrics and guards; raise `MIN_N_FILLS` |
| `back-trading/cl_algo_learner.py` | Narrow all axes, not just tp/sl; make output consumable |
| `back-trading/cl_algo_worker.py` | Keep the lock-file parallelism pattern |
| `lib/algo_engine.py` | Behind a registry; **fix the strength contract** |
| `lib/algo_lab.py` | Keep grid + dedup + cap; fix the downsampler |
| `lib/algo_pnl.py` | Extend to simulated results; keep the attribution grouping and multipliers |
| `lib/data_availability.py` | Explicit history path |
| `lib/day_params.py` | Per-symbol tick; keep the prior-day search |
| `lib/critical_lines.py` | Keep arm/disarm; reconcile the strength scale |
| `lib/price_profile.py` | Explicit history path |
| `lib/correlation_lab.py` | Port as a feature module |
| `back-trading/calibrate.py` + `grader.py` | Keep the sim-vs-reality calibration loop — it is the only thing that validates the simulator |
| `back-trading/reality_model.py` | Keep the concept (same orders to sim and paper, then grade) |

### 12.3 Extract before archiving

| File | Extract |
|---|---|
| `june/back-trading/bt_scorer.py` | **12 metrics + Monte-Carlo permutation + LOOCV + stability zone + drawdown.** Materially better than the active scorer |
| `june/back-trading/bt_params.py` | `AXES` + `SESSION_WINDOWS` + `get_neighbors()` + a real `param_set_id` |
| `june/back-trading/bt_db.py` | `bt_param_sets` / `bt_matrix_results` schema; the `claim_command` pattern |
| `june/back-trading/bt_matrix_runner.py` | Resumable `(trade_id, param_set_id)` matrix pattern |
| `june/trader/data/bt.db` | 9.72 M result rows + 10,800 param sets — **re-score before discarding**, with the caveats in §9.4 |
| `back-trading/trading_dashboard.py:295-467` | The 25 line detectors, as a library |
| `back-trading/trading_dashboard.py:102-107` | `_ROUND_LEVELS` |
| `trader/broker.py:43` | `_TICK_BY_SYMBOL` |
| `lib/algo_pnl.py:38` | `SYMBOL_MULTIPLIERS` |

### 12.4 Data to migrate

| Asset | Why |
|---|---|
| `trader/data/galao.db` | 49,210 commands, 4,315 completed trades, 998 verified, 744 lines, 37,031 price-profile rows, 268,063 IB events. **Migrate `verified_trades` as the P&L baseline; treat `completed_trades` as unverified** |
| `trader/data/bars.db` | 340,542 bars, 4 symbols, back to 2019-05-05, plus normalized/diff derivatives with a persisted basis |
| `data/bars_7years_30m_*.csv` | The Databento source of truth |
| `june/trader/data/bt.db` | The 9.72 M-row sweep |
| `C:\Projects\Galgo2026\june\trader\data\history\` | **The tick CSVs the whole system depends on, in a fourth repo.** Relocating these is a prerequisite for any rebuild |
| `docs/*.md`, `CLAUDE_STATE.md`, `GALGO2027_HANDOFF.md` | Historical record — **verify every claim before reuse (F5)** |

### 12.5 Do not port

`june/` (after extraction) · `trader/visualizer/` · `back-trading/algo_dashboard.py` ·
`back-trading/visualizer/` · `back-trading/versions/` · `versions/` · `trader/runner.py` ·
`trader/fetch_scheduler.py` · `trader/fetch_priority.py` · `trader/fetcher.py` ·
`trader/ib_fetcher_paper.py` · `trader/create_presentation.py` · `trader/may_scheduler.py` ·
`algo-analyzer/` · `data/galao.db` (empty duplicate) · `tmp_vt_summary.py` ·
`monthlyplanApr.md` · `weekplan_jun6.md` · `back-trading/config.yaml` (merge into one config).

---

## MESSAGE FOR THE ARCHITECT

**Proven infrastructure — reuse, do not rewrite.** The IB paper execution stack is the real
asset: `trader/broker.py`, `lib/order_builder.py`, `lib/ib_client.py`, `trader/session.py`,
and `lib/db.py`'s access layer. It is running now and has been hardened by dated incidents.
The five mechanisms you must not lose: (1) exit reason **derived from price**, never from
which order ID filled — computed twice, independently (`broker.py:424`, `db.py:432`);
(2) the atomic claim lock `UPDATE ... WHERE status='PENDING'` + `rowcount==1`;
(3) naked-position reconciliation on startup, including its warning that `Position.avgCost` is
multiplier-scaled and must never be used as an order price; (4) TP/SL rebase to the actual
fill price, queued out of the ib_insync event thread and re-queued on disconnect; (5) the
`verified_trades` view's integrity filters — they reject 77% of the raw ledger and **flip the
P&L sign on three of five sources**. `commands.id` is already a permanent logical trade ID
distinct from broker order IDs, with recursive `parent_command_id` lineage. Keep that.
Note: `bracket_map`, which three documents call a core invariant, **has never existed**.

**Research only — no exceptions.** Fifteen order-generating algorithms/variants and 25 S/R
level detectors are implemented. **None has evidence of profitability.** The only real ledger
is 998 verified closed trades totalling **−21,320.75 points**; every source is net negative
except a 22-trade external one. Five `AlgoType` strategies (BOUNCE/BREAKOUT/DIRECTIONAL/FADE/
BOTH) are clean, self-tested, and have produced **zero live trades and zero stored backtest
results**. The 9.72 M-row sweep in `june/bt.db` reuses real entries rather than simulating
them, excludes 56% EXPIRED rows from its averages, has no out-of-sample split, and omits the
session window where 69% of trades actually occurred. `docs/learner_state.md` promotes a
"Current Best Combo" with **profit factor 6.05 on four fills**. Treat all of it as hypotheses.

**Three defects to fix before any of it means anything.** (1) **Look-ahead bias**:
`_build_lines_for()` derives PDH/PDL/VWAP/POC from a day's *own* session and stores them under
that same date; the backtester then trades them from 08:30 the same morning. Every backtest
result is invalid. (2) **Strength scale is inverted**: `lib/db.py` and `algo_engine` use
1=strongest; the dashboard emits 10=strongest — so `strength_max ∈ {1,2,3}` silently discards
every auto-detected line, which is why Algo Lab has produced 0 of 49,210 commands.
(3) **The pipeline points at a directory that does not exist** — all six `cl_algo_*` tables
are 0 rows because `history_dir` resolves to `trader/data/history` while the CSVs live in a
fourth repo at `C:\Projects\Galgo2026\june\trader\data\history`.

**Genuinely missing.** A **standard result/trade schema** — today there are four incompatible
ones and three P&L units (`pnl_points` / `pnl_ticks` / dollars-at-MES-multiplier). A **run
identity** — results are keyed by content, so re-running after a code change silently no-ops.
**Out-of-sample validation** — entirely absent. **Trading costs** — commissions unmodelled,
stop slippage hard-coded to zero. **The last wire in the feedback loop** — the learner writes
a next-grid recommendation, `cl_algo_worker._get_learner_recommendation()` can read it, and
nothing connects them; the loop is ~60% built and 0% turning. **A candidate review surface** —
`algo_candidates` is a well-shaped table with zero rows and zero code references.

**Where to look first.** `back-trading/run_cl_algo_pipeline.py` is the batch-runner skeleton
worth keeping. `june/back-trading/bt_scorer.py` and `bt_params.py` are the better scorer and
the only real param-set registry — extract them before archiving `june/`.
`lib/algo_pnl.get_breakdown()` already groups live P&L by
`(symbol, source, algo_type, params_json, line_detect_algo)`: that is the right attribution
axis for the whole platform. Extend it to cover simulated results and you have the comparison
layer. And carry forward the `--self-test`-on-every-module convention — it is the reason the
execution layer can be trusted at all.
