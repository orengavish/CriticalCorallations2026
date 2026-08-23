# ARCHITECTURE_RESPONSE.md — CriticalCorallations2026 (CC2026)
> Architecture-question round only. No implementation, no refactor, no restart performed.
> Builds directly on `BACK_TRADING_INTEGRATION_REPORT.md` (same repo, written earlier this
> session, read-only forensic trace of the same code) — this document reframes those
> findings as architecture answers; it does not re-derive them from scratch, and cites back
> to that report's section numbers where useful instead of repeating evidence verbatim.

```text
PROJECT: CriticalCorallations2026 (CC2026)
REPO PATH: C:\Projects\CriticalCorallations2026
ROLE: Dual — (1) live paper/real trading execution brain (trader/broker.py, decider.py,
      position_manager.py, session.py, preflight.py) AND (2) backtest/research experiment
      engine (back-trading/*, lib/algo_engine.py, lib/algo_lab.py, lib/algo_pnl.py, the
      CL Algo pipeline). Both roles live in this one repo today — this doc answers Part D
      and Part E both, per the instruction to answer every applicable role section.
CURRENT COMMIT: c88d53a (2026-08-18) — note: BACK_TRADING_INTEGRATION_REPORT.md was
      written after this commit and is not yet committed; this document will need to be
      committed alongside it.
```

---

# PART A — COMMON QUESTIONS

## A1. Proposed responsibility

**Five-sentence definition**: CC2026's execution role should own turning an approved,
normalized trade intent into IB orders and tracking that order through fill, exit, and
final P&L recording — the trade lifecycle, not the trading decision. CC2026's experiment
role should own simulating strategy+parameter combinations against historical market data,
scoring them, and recommending next candidates — the "does this idea work" question, not
"is this idea good enough to run for real," which belongs to an explicit promotion gate
(see E18). Neither role should acquire market data itself, decide what a good trading
signal is, or scrape external sources — those are Fetcher2026's, an open Algorithm
Registry's, and GevaExtract's jobs respectively. Today this repo also hosts a broad
dashboard UI spanning both roles plus data-quality research (Correlation, Sup/Res Viz) —
that presentation layer is a separable concern once the two backend services have real
contracts. Both roles are proven and incident-hardened (see `BACK_TRADING_INTEGRATION_REPORT.md`
§18) — the recommendation throughout this document is to wrap and generalize, not rewrite.

**SHOULD OWN**
- IB Gateway session management for execution purposes (LIVE=data-only, PAPER=orders-only) — `lib/ib_client.py`
- Order construction, placement, lifecycle tracking, reconciliation — `lib/order_builder.py`, `trader/broker.py`
- The canonical trade ledger: attempts, fills, exits, realized P&L (`commands`, `completed_trades`, `verified_trades`)
- Process supervision of its own execution components (`trader/session.py`)
- Experiment simulation mechanics: generate candidates → simulate → score → recommend next (generalized CL Algo pipeline, §14 of the forensic report)
- Cross-algorithm P&L attribution against its own trade ledger (`lib/algo_pnl.py`'s *mechanism*, generalized)

**SHOULD NOT OWN**
- Market data acquisition — `back-trading/engine.py`'s `_fetch_ticks()` calling Fetcher2026's `fetcher.py` functions directly, and `scripts/backfill_bars.py`/`scripts/import_7year_bars.py` hitting IB directly for `bars.db`, are both **duplicate acquisition paths that should not exist** once Fetcher2026 has a real contract (flagged again in A2, A5, and Part G — this is a genuine, current architectural problem, not hypothetical)
- Strategy/signal decision logic as hardwired code — `decider.py`'s toggle-rule generation and critical-line interpretation should become one Algorithm Registry entry among many (Part E), not privileged, uneditable code inside the execution repo
- External-source ingestion — GevaExtract's Facebook scraping and its direct `commands` table writes (`insert-commands.py`) are a coupling CC2026 should not accept passively; it should define and defend a `submit_trade_intent()` contract instead
- The full dashboard UI as currently scoped — Algo Lab / Correlation / Sup-Res-Viz / Overview genuinely belong to the experiment-engine and research side, not bundled into the same Flask process as live execution's Submitted/Levels tabs

## A2. Independence / current coupling

| Dependency | Why it exists | Keep / replace | Proposed future contract |
|---|---|---|---|
| GevaExtract's `insert-commands.py` INSERTs directly into `commands` (WAL-safe, but raw SQL from a different repo) | GevaExtract needs to submit trades from scraped FB lines | **Replace** | `ExecutionService.submit_trade_intent(TradeIntent) -> OrderAttempt` (Part D) |
| GevaExtract's `galao-db.js` reads `commands`/`price_cache`/`system_state` by loading the SQLite file directly (read-only, never `.save()`s) | Monitor/Sub tab status display | **Replace** | `ExecutionService.get_status()` / `.list_open()` read API |
| GevaExtract calls `http://localhost:5003/api/session/status` | broker/decider liveness check | **Keep** — already a proper HTTP contract | formalize as `ExecutionService.health()` |
| GevaExtract's Cancel-All also calls the **forbidden legacy port 5001** (`trader/visualizer/app.py`) for an IB global-cancel relay | historical accident — that port shouldn't be running per this repo's own rules | **Replace** | `ExecutionService.cancel_all()` on the real service, not a legacy path GevaExtract shouldn't even know exists |
| `back-trading/trading_dashboard.py` / `lib/price_profile.py` read tick CSVs directly from `C:\Projects\Galgo2026\june\trader\data\history\` (Fetcher2026's actual output location, itself a pre-split legacy path) | historical, never fully migrated | **Replace** | `MarketDataService.get_dataset()` (Fetcher2026's future contract, Part B) |
| `back-trading/engine.py` imports Fetcher2026's `fetcher.py` module directly (`from fetcher import get_contract_for_date, ...`) to backfill missing tick data on demand | convenience during single-repo history | **Replace** — this is a direct cross-repo Python import, the exact anti-pattern Golden Rule 1 names | `MarketDataService.ensure_dataset()` |
| `scripts/backfill_bars.py` / `scripts/import_7year_bars.py` fetch OHLCV bars directly from IB into `bars.db` | historical, parallel to Fetcher2026's own (much more extensive) bars1s pipeline | **Replace** | same `MarketDataService` contract — CC2026 should not have its own IB-facing bar fetcher at all |
| Two `config.yaml` files in this one repo (`trader/`, `back-trading/`) resolved via `lib.config_loader`'s ambient, cache-on-first-call, nearest-file logic | historical, already a documented footgun (`CLAUDE_STATE.md`) | **Replace** | one config per service, loaded explicitly by path (the pattern `session.py` already uses defensively — generalize it, don't keep the ambient resolver) |
| Two separate results schemas: `back-trading/db.py` (`runs`/`sim_orders`/`sim_fills`) vs `lib/db.py` (`commands`/`completed_trades`/`verified_trades`), no shared ID | evolved independently | **Replace** | one Experiment Result schema, `mode: 'sim'|'paper'|'live'` discriminated (E9) |
| `cl_algo_*` tables physically live inside `galao.db` (the execution DB) even though they're experiment-engine data | organic growth, same repo | **Replace** | move to a dedicated Experiment Engine store — execution DB should contain trade-lifecycle data only |
| IB Gateway (port 4002), shared by broker.py, decider.py, `engine.py --reality-model`, `scripts/backfill_bars.py`, AND Fetcher2026's fetchers | shared physical resource, no formal ownership | **Keep the resource, formalize ownership** | an explicit pacing-budget-aware allocation contract every consumer registers against (today each maintains an independent client-ID pool that happens not to collide by convention, not by design — see Part G risk #4) |
| Tick-size / instrument spec duplicated three times: `_TICK_BY_SYMBOL` (broker.py), `order_builder.py`'s `tick_size=0.25` default, `algo_engine.py`'s `SYMBOL_TICKS` | organic growth | **Replace** | one canonical `InstrumentSpec{symbol, tick_size, multiplier, exchange}` registry shared by execution and experiment engine |

## A3. KEEP / WRAP / REWORK / RESEARCH / ARCHIVE

| Component | Classification | Why | Future interface |
|---|---|---|---|
| `lib/ib_client.py` | **KEEP** | Clean, self-contained, no strategy coupling | wrapped by `ExecutionService`, not exposed directly |
| `lib/order_builder.py` | **KEEP** | Pure price math + thin IB wrapper, generic enough already | `ExecutionService.submit()` internals |
| `trader/broker.py`'s claim/submit/event/poll/close loop | **KEEP** | Incident-hardened core (§18 of the forensic report) | becomes `ExecutionService`'s implementation, same state machine |
| `trader/broker.py`'s `reconcile_naked_positions` | **KEEP, extend** | Correct, important; extend to run after mid-session reconnect too (D9) | `ExecutionService.reconcile()` |
| Mechanism B (`lib.db.spawn_replenishment`, `parent_command_id` chain) | **KEEP** | Correct design | becomes the *only* replenishment path (D6) |
| Mechanism A (`decider.py replenish()`) | **REWORK** | Keep the toggle-rule/armed-line *decision*, discard its flag-only, lineage-losing *write path* | decision logic becomes a critical-line-specific Algorithm Registry entry; writes go through Mechanism B |
| `lib/db.py`'s `commands`/`completed_trades`/`verified_trades` schema | **WRAP** | Sound, battle-tested; needs a proper access-layer boundary, not raw SQL in 15 files | `ExecutionService`'s internal store |
| `back-trading/reality_model.py` | **REWORK** | Duplicates `broker.py`'s submission/tracking logic with its own ID dict and schema | should call `ExecutionService.submit()`/`.get_status()` instead of reimplementing |
| `back-trading/db.py` (separate schema) | **REWORK / merge** | Blocks sim-vs-real comparability (§11 of the forensic report) | folded into the unified Experiment Result schema (E9) |
| `back-trading/generator.py` + `simulator.py` core fill mechanics | **WRAP** | Solid, empirically calibrated (§12) | Experiment Engine's simulation primitive, accepting any algorithm's candidate orders, not just synthetic ones |
| `back-trading/calibrate.py` | **KEEP, narrow scope** | Honestly a simulator-accuracy tool, not a strategy evaluator (§13) — don't conflate it with the feedback loop, but it's genuinely useful for what it does | stays as `Simulator.calibrate_against_reality()` |
| `back-trading/grader.py` | **ARCHIVE** | Fully subsumed by `cl_algo_scorer.py`'s richer metrics once the pipeline is generalized | superseded, keep for historical reference only |
| `back-trading/engine.py`'s day-by-day orchestration (non-reality-model path) | **ARCHIVE** | Subsumed by `cl_algo_backtester.py`'s resumable, parallel-safe design | superseded |
| `lib/algo_engine.py` + `lib/algo_lab.py` | **WRAP** | Real, working evidence of algorithm-independent execution (§15/§17), but scoped to one family | becomes the reference implementation of the Algorithm Registry's plugin interface (E6) |
| `cl_algo_backtester.py`/`scorer.py`/`learner.py`/`worker.py`/`run_cl_algo_pipeline.py` | **WRAP** | Mechanics are exactly what's wanted (§14); combo axis needs generalizing from an `AlgoType` enum to an open registry | Experiment Engine's runner/scorer/learner |
| `lib/critical_lines.py`, all S/R-detection logic, all specific algo thresholds | **RESEARCH ONLY** | Per Golden Rule 3 — not proven profitable (confirmed by `algo_pnl.py`'s own self-test data: `random_mkt` and `critical_line` sources both net negative per the v4.27 release note) | Algorithm Registry entries, swappable, none privileged |
| `trader/fetch_scheduler.py`/`fetch_priority.py` (present in this repo) | **ARCHIVE** | Already documented as legacy/unused here — Fetcher2026 owns fetching | not evaluated further, out of this repo's future scope |
| `trader/visualizer/app.py` (port 5001), `back-trading/algo_dashboard.py` (port 5002) | **ARCHIVE** | Already forbidden legacy per this repo's own rules; GevaExtract's port-5001 cancel-all dependency (A2) needs fixing before deletion | none — retire |

## A4. Minimal future public contract (summary — full shapes in Part D/E)

```text
ExecutionService:
    submit(TradeIntent) -> OrderAttempt          # async, idempotent via idempotency_key
    cancel(attempt_id | logical_trade_id) -> bool
    get_status(attempt_id | logical_trade_id) -> LifecycleState
    list_open(source: str | None) -> [OrderAttempt]
    get_closed_trades(filter) -> [ClosedTrade]
    health() -> {broker, decider, gateway_connected, uptime}
    reconcile() -> ReconcileReport

ExperimentEngine:
    register_algorithm(AlgorithmDefinition) -> None
    run_experiment(Experiment) -> ExperimentRun   # resumable, idempotent
    get_scores(algorithm_id, symbol) -> [Score]
    get_recommendation(algorithm_id, symbol) -> LearnerRecommendation
    promote(experiment_id, decision_rationale) -> PromotionDecision  # gate before ExecutionService.submit()
```

Full input/output/side-effect/idempotency detail is in Part D §D-contracts and Part E
§E-contracts below — deriving these from a five-line stub here would understate the real
detail already worked out.

## A5. Data ownership

| Asset | Current owner | Current readers | Current writers | Proposed future owner |
|---|---|---|---|---|
| `trader/data/galao.db`: `commands`, `completed_trades`, `critical_lines`, `ib_events`, `system_state`, `price_cache`, `algo_runs`, `algo_candidates`, `positions`(appears vestigial — schema present, no write path found in this session's reading) | CC2026 (physically), co-written by an external repo | CC2026 dashboard, GevaExtract (direct file read), `algo_pnl.py` | `broker.py`, `decider.py`, `algo_lab.py`, GevaExtract's `insert-commands.py` (external, direct) | `ExecutionService` exclusively; all other access via contract |
| `trader/data/galao.db`: `cl_algo_sim_results`, `cl_algo_combo_scores`, `cl_algo_score_history`, `cl_algo_learner_runs`, `cl_algo_day_params`, `cl_algo_fd_results` | CC2026 back-trading subsystem, but physically co-located with execution data | `cl_algo_scorer.py`, `cl_algo_learner.py`, dashboard's Algo Lab tab | `cl_algo_backtester.py`, `cl_algo_scorer.py`, `cl_algo_learner.py` | Experiment Engine's own store — should not physically share a database file with `commands` |
| `back-trading/data/*.db` (`runs`, `sim_orders`, `sim_fills`, `paper_fills`, `grades`, `calib_runs`, `calib_details`) | CC2026 back-trading subsystem | `engine.py`, `calibrate.py`, `grader.py` | same | merge into the unified Experiment Result schema (E9), old data preserved as historical only |
| `trader/data/bars.db` (`bars_30m` + normalized/diff tables) | CC2026 | Correlation tab, `cl_algo_backtester.py`(indirectly, via tick CSVs — bars.db itself is mainly a Correlation-tab asset, not confirmed as a backtester input this session) | `scripts/backfill_bars.py`, `scripts/import_7year_bars.py` (both hit IB directly) | **Disputed — flagged for the central architect (Part G)**: this is market data and arguably belongs to Fetcher2026's `MarketDataService`, not a second CC2026-owned IB-fetch pipeline |
| Tick CSVs at `C:\Projects\Galgo2026\june\trader\data\history\` | physically outside every current repo | CC2026 (`engine.py`, `price_profile.py`, `trading_dashboard.py`), Fetcher2026 | Fetcher2026's fetchers | Fetcher2026, via `MarketDataService` contract — CC2026 becomes a pure consumer |
| `price_cache` table | CC2026 (`broker.py` writes on every real fill) | dashboard, `decider.py`'s toggle-rule fallback, `algo_lab.py` | `broker.py` | folded into `ExecutionService`'s status contract rather than a bare table other components query directly |

## A6. Failure boundaries

- **If CC2026's execution fails/is down**: Fetcher2026 is completely unaffected (no
  observed dependency in that direction — good evidence independence already partly
  exists). Experiments/backtesting can continue (core simulation reads tick CSVs from disk,
  not from broker.py) *except* the `--reality-model`/`calibrate.py` calibration paths,
  which need IB PAPER and would stall gracefully (orders never place). GevaExtract's
  `auto-geva-scheduled.ps1` currently **starts CC2026 itself if it finds it down** — this
  is tight coupling that should not survive the rebuild; GevaExtract shouldn't need to know
  how to launch CC2026's process, only how to call a health-checked contract and back off.
  Dashboards go stale/error. IB Gateway itself is unaffected. Stored state doesn't
  corrupt — every DB write in `broker.py` is a status-gated, idempotent transition (§18 of
  the forensic report), so a mid-write crash just leaves a row in a well-defined
  intermediate state that restart logic already handles (with the one caveat in D-question
  about `SUBMITTING`→`PENDING` reset, §8.2 of the report).
- **If Fetcher2026 fails**: CC2026's Correlation/Graph tabs go stale but don't error (data
  just stops updating); CC2026's *own* duplicate backfill scripts are unaffected since they
  talk to IB directly — again, evidence that this duplication is real and currently masks
  what should be a real failure-boundary signal.
- **If GevaExtract fails**: CC2026 execution is completely unaffected — `broker.py` is
  source-agnostic by design (§15/§17 of the forensic report). This is the cleanest existing
  failure boundary in the whole system and should be the model for the others.
- **If IB Gateway fails**: everything needing live data/order placement stalls gracefully —
  orders pile up `PENDING` with no `ib_order_id`, a well-documented, already-understood
  failure mode (per `GevaExtract/OPERATIONS.md`'s own incident log). Nothing crashes.

## A7. Migration strategy

```text
1. Wrap lib/order_builder.py + trader/broker.py's public functions behind a formal
   ExecutionService module boundary — same process, zero behavior change. REVERSIBLE.
2. Write regression tests for every item in the forensic report's §18 "DO NOT LOSE" list
   as golden fixtures (idempotent status-gated writes, claim-lock atomicity, price-derived
   exit_reason, avgCost-never-as-price, rebase-in-main-loop-not-event-thread, the three
   independent dedup-tuple checks). REVERSIBLE (pure test addition).
3. Unify replenishment: implement "Mechanism B for every source" behind a feature flag;
   SHADOW-RUN it for critical_line-sourced trades alongside the existing Mechanism A for one
   full trading period, logging any divergence, before removing Mechanism A. REVERSIBLE via
   the flag until Mechanism A is actually deleted.
4. Introduce submit_trade_intent() as a NEW contract endpoint alongside (not replacing) the
   existing raw-INSERT path GevaExtract uses; migrate GevaExtract to call it; keep the old
   path live as fallback until proven. REVERSIBLE — old path stays functional in parallel.
5. Split cl_algo_* tables out of galao.db into a dedicated experiment store — ADDITIVE
   (copy, don't move-and-delete) until the new consumer (dashboard, scorer, learner) is
   proven against it. REVERSIBLE — old tables kept read-only during the transition.
6. Point CC2026's own tick/bar reads at Fetcher2026's future MarketDataService contract
   instead of the legacy Galgo2026 path and the direct-IB backfill scripts — do this LAST,
   it's the riskiest (Graph/All/Correlation tab correctness depends on it) and easiest to
   verify only once the contract is stable elsewhere. REVERSIBLE up to the point the old
   direct-read path is deleted — recommend keeping it as fallback through one full
   production week with zero discrepancies before removing it.
```

## A8. Ten things the architect must not accidentally lose (evidence-backed)

1. **Two replenishment mechanisms, only one preserves lineage** — `decider.py replenish()`
   sets no `parent_command_id`/`critical_line_id`; `lib.db.spawn_replenishment()` does.
   (`decider.py:145-223` vs `lib/db.py:712-762`, forensic report §5.)
2. **Status-gated idempotent writes everywhere** — every transition is `UPDATE ... WHERE
   status='<expected>'`, which is what makes the event path and poll path safe to run
   redundantly. Not named as a rule anywhere; just consistently applied. (§18 item 1.)
3. **Price-derived `exit_reason`**, never order-ID-derived — defends against IB
   order-ID/role mislabeling bugs that have happened before. (`broker.py`
   `poll_tp_sl_fills()`, `lib/db.py`'s `verified_trades` view.)
4. **`Position.avgCost` is multiplier-scaled** (M2K×5, MNQ×2) — using it as a raw price
   caused a real financial-impact incident. `get_price()` only, everywhere.
   (`broker.py:635-639`.)
5. **TP/SL rebase runs in the main loop, not the event thread** — ib_insync API calls from
   inside an event callback are unsafe; `_handle_exec_fill` only queues, `_drain_rebase_queue`
   does the actual `modifyOrder()` calls. (`broker.py:456-570`.)
6. **The claim-lock pattern** (`_claim_command`, `replenishment_issued`'s own claim,
   `SessionManager`'s PID file) — atomic `UPDATE ... WHERE status=X` / `rowcount==1`, reused
   three times, never abstracted. Worth promoting to a shared primitive, not dropping.
7. **Three independent dedup-tuple checks**, different key shapes, same root incident class
   (2026-07-17, 425 stale orders) — `decider.generate_commands()`,
   `algo_lab.submit_grid()`, `broker.replenish_if_enabled()`. Unify carefully, verify each
   still prevents its specific pileup scenario.
8. **SUBMITTING→PENDING reset on restart is a heuristic, not a guarantee** — the narrow
   race window (crash between successful IB placement and the DB write) is real and
   unaddressed; don't remove the reset, but don't assume it's airtight either. (§8.2.)
9. **Two different TP-confirmation rules** between `simulate()` (plain) and
   `simulate_exit()` (calibration, requires 2 confirming ticks) — easy to accidentally
   unify and silently change calibration's meaning. (§12.)
10. **`verified_trades`' correctness filters** — excludes `source='test'`, requires all
    fill/exit fields non-null, excludes zero-duration fill=exit artifacts, **verifies
    `pnl_points` matches price arithmetic to within 0.01**, excludes fills already past
    TP/SL at fill time. This view is the single source of truth every downstream P&L tool
    trusts — its filter logic must survive intact in any schema migration.

## A9. Improvements to make only AFTER parity

- True partial-fill quantity tracking (schema currently assumes all-or-nothing —
  `poll_fills()`'s own comment: *"R-ORD-13: treat all fills as complete (partial fills
  ignored in V1)"*).
- Adopt IB `permId` for reconnect robustness (currently unused anywhere).
- Re-run `reconcile_naked_positions()` after every mid-session reconnect, not just process
  startup.
- Unify the three tick-size/instrument-spec tables into one registry.
- Reconcile the two TP-confirmation rules in `simulator.py` (item 9 above) — this changes
  behavior, so it must be a deliberate, tested decision, not incidental cleanup.
- Model commissions/fees in `simulator.py` (currently absent entirely).
- Cross-check IB's actual open orders before blindly resubmitting on the
  SUBMITTING→PENDING restart heuristic (item 8 above).
- Seed/control `spawn_replenishment()`'s `random.choice()` direction for reproducibility
  (currently unseeded — see E16).

---

# PART D — CC2026 LIVE EXECUTION

**D1. Smallest possible boundary so algorithms never need to know IB details**: an
`ExecutionService` exposing exactly `submit(TradeIntent)`, `cancel()`, `get_status()`,
`list_open()`, `get_closed_trades()`, `health()`, `reconcile()` (A4). No algorithm-side code
should import `ib_insync`, know an IB order ID exists, or see a `contract` object —
`algo_lab.py` and `decider.py` already come close to this today (neither imports
`ib_insync` directly), which is good evidence the boundary is achievable with modest, not
drastic, change.

**D2. `TradeIntent`** (replaces raw `commands` INSERTs from every producer):
```python
TradeIntent = {
    "source":            str,   # open registry key: 'critical_line' | 'algo_lab' | 'geva_extract' | ...
    "symbol":            str,
    "direction":         "BUY" | "SELL",
    "entry_type":        "LMT" | "STP" | "MKT",
    "entry_price":       float,
    "tp_price":          float,
    "sl_price":          float,
    "quantity":          int,
    "idempotency_key":   str | None,   # NEW — see D6/A2; replaces each producer inventing its own in-flight tuple check
    "strategy_context":  dict | None,  # opaque to execution: algo_type, params_json, critical_line_id, etc.
}
```
Input: as above. Output: `OrderAttempt` with `status='PENDING'`. Side effects: one DB row.
Async (returns before IB submission — the existing claim/poll loop does that). Idempotent
via `idempotency_key` — if provided and a non-terminal attempt with the same key exists,
return the existing attempt instead of creating a duplicate (this generalizes and replaces
the three bespoke dedup-tuple checks in A8 item 7).

**D3. Logical `Trade` identity, separated from attempt-level IDs**:
- `OrderAttempt.id` — one row per submission attempt (today's `commands.id`).
- `logical_trade_id` — **proposed new, explicit, first-class field** (see D4).
- `parent_attempt_id` — today's `parent_command_id`, attempt-to-attempt lineage.
- `BrokerOrder.ib_order_id` / `.ib_tp_order_id` / `.ib_sl_order_id` — transient, IB-session
  scoped, never referenced outside fill/close correlation.
- `permId`/`execId` — not currently used; recommend adopting `permId` specifically for its
  cross-session permanence (D-question implies "if we adopt them" — recommend yes, see A9).

**D4. Explicit `logical_trade_id`, not continued recursive reconstruction — recommend
storing it explicitly.** Reasoning: (a) the recursive-CTE approach (`verified_trades`'
`ancestry` walk) **already silently fails today** for `critical_line`-sourced chains, since
`decider.py replenish()` sets no `parent_command_id` at all — an architecture question this
important shouldn't inherit a mechanism that's already broken for the majority trade
population; (b) an explicit ID is directly queryable by every future consumer (Experiment
Engine wanting to compare a promoted algorithm's live results against its backtest,
dashboards, `algo_pnl`-equivalents) without needing to know the recursive-CTE trick or reimplement
it in a different query language/service; (c) recursive reconstruction doesn't scale
indefinitely and complicates any future cross-schema join (e.g., Experiment Result rows
wanting to reference "this real logical trade"). Recommend keeping `parent_attempt_id` too
(attempt-level lineage remains useful for debugging "why did we get here" step by step) —
`logical_trade_id` and `parent_attempt_id` are complementary, not either/or.

**D5. How replenishment relates to a logical trade**: a replenishment/replacement attempt
inherits the **same** `logical_trade_id` as its parent (propagated, not re-derived) — this
must be enforced by `ExecutionService.submit()` itself whenever `idempotency_key`/context
indicates "this is a replenishment of attempt X," not left to each producer to remember (a
policy gap that's exactly how Mechanism A's lineage loss happened in the first place).

**D6. Unifying the two replenishment mechanisms without losing current behavior**: keep
Mechanism A's *decision* logic (toggle-rule re-evaluation with current price, armed-line
check, SL-cooldown awareness) as a `critical_line`-specific policy function, but route its
*write* through Mechanism B's `spawn_replenishment()`-equivalent path so
`parent_attempt_id`/`logical_trade_id`/`critical_line_id` are always set. Migrate via the
shadow-run strategy in A7 step 3 — this is the single highest-risk behavior change in the
whole rebuild because it touches the largest live trade population, and evidence-based
staging (not a flag-day cutover) is warranted.

**D7. Fields specific to `critical_line` that must leave the execution layer**:
`line_price`, `line_type`, `line_strength`, and `critical_line_id` as a hard schema column
on the execution table — these should move into `TradeIntent.strategy_context` (opaque
JSON), not remain first-class execution columns. Execution genuinely does not need to know
what a "critical line" is.

**D8. Generic execution vs. strategy behavior**: generic = claim/submit/track/close/rebase/
reconcile/dedup-by-idempotency-key (all of `broker.py`'s current logic, once D7's fields are
externalized). Strategy = what price/direction/bracket to propose in the first place, and
the replenishment *decision* (not the replenishment *write path*, which is generic per D6).

**D9. After mid-session reconnect**: currently `reconcile_naked_positions()` is **not**
re-run (only at process startup — a real, currently open gap, §8.1 of the forensic report).
Recommend: yes, run it after every successful reconnect, since a disconnect gap is exactly
the scenario where a resting order could get cancelled/dropped without this process
noticing.

**D10. `RECONCILE_REQUIRED` as a real recoverable state**: today it's a terminal, manually-handled
status with no automated follow-up. Proposed: a scheduled reconciliation pass that, for each
`RECONCILE_REQUIRED` attempt, queries IB's actual current position/order state directly (not
just the stale-order-ID lookup that already failed) and either (a) finds a matching real
fill by symbol+price+time proximity and repairs the row, or (b) confirms genuine
orphan-hood and feeds it into `reconcile_naked_positions`-style protective handling — never
left as a silent dead end.

**D11. Should fills become first-class stored entities? Yes.** Today fills are collapsed
into flat fields on the attempt row (`fill_price`/`fill_time`, `exit_price`/`exit_time`) —
this already conceptually assumes exactly one entry fill and one exit fill, which partial
fills (currently ignored per A9) would violate. A first-class `Fill{attempt_id, leg:
'entry'|'tp'|'sl', price, quantity, timestamp, source: 'event'|'poll'}` table would (a)
naturally support partial fills without a schema break, (b) let rebase/reconciliation logic
reason over "the list of fills for this attempt" instead of overwriting fields, (c) make
"which mechanism resolved this" (§6 of the forensic report's authoritative-source question)
an auditable fact instead of something inferred from logs.

**D12. Compatible sim/real vocabulary while retaining separate mechanics**: give both a
`mode: 'sim'|'paper'|'live'` field on the same logical Result shape (E9) — the *mechanics*
of how a fill is determined stay completely separate (real IB events/polling vs.
`simulator.py`'s tick-replay), but the *shape* of what gets recorded (entry/exit
price+time, exit_reason, pnl, and now `logical_trade_id`/`experiment_id`) becomes directly
comparable without a translation layer, which is exactly the gap `calibrate.py` currently
bridges only by re-simulating from scratch rather than joining on shared identity.

**D13. Invariants that should become formal regression tests** (from `broker.py`,
`order_builder.py`, `ib_client.py`, `verified_trades`): every item in A8 (1-10) —
specifically, status-gated idempotent writes, claim-lock atomicity under concurrent
callers, price-derived `exit_reason` correctness under a deliberately-mislabeled-order-ID
test double, `avgCost` never used as a raw price (a static/lint check would also catch this
class of bug going forward), rebase calls only ever originating from the main loop thread,
all three dedup-tuple checks' specific pileup scenarios, and the `SUBMITTING`→`PENDING`
restart reset's assumption made explicit and tested against a simulated crash-after-`placeOrder`
race.

**D14. What execution should expose**: `submit`, `cancel`, `status` (single attempt or
whole logical trade), `events` (structured lifecycle event stream — see D11's `Fill` +
a parallel `TradeLifecycleEvent` for status transitions, replacing today's free-text
`ib_events`), `P&L` (raw, per closed trade — attribution/comparison across algorithms
belongs to the Experiment Engine reading this stream, not execution itself), and
`reconciliation` (on-demand trigger + last-run status, closing the D9 gap).

**D15. How independent can execution be from the rest of the platform?** Very — already
demonstrated: `broker.py` today has zero required knowledge of `algo_type`, `params_json`,
or which of three completely different producers (one in a different repo, one in a
different language) wrote a given row. The remaining couplings (D6's replenishment
mechanism split, D7's leaked strategy fields) are narrow and specifically named, not
structural.

### Proposed contracts

```python
TradeIntent = {source, symbol, direction, entry_type, entry_price, tp_price, sl_price,
               quantity, idempotency_key, strategy_context}

LogicalTrade = {logical_trade_id, symbol, opened_at, closed_at | None,
                 status: "OPEN" | "CLOSED", total_pnl_points, attempt_ids: [int]}

OrderAttempt = {id, logical_trade_id, parent_attempt_id, source, symbol, direction,
                 entry_type, entry_price, tp_price, sl_price, quantity, status,
                 strategy_context, created_at, updated_at}

BrokerOrder = {attempt_id, leg: "entry"|"tp"|"sl", ib_order_id, perm_id | None, status}

Fill = {attempt_id, leg, price, quantity, timestamp, source: "event"|"poll"}

TradeLifecycleEvent = {attempt_id, from_status, to_status, timestamp, mechanism,
                         detail_json}

ClosedTrade = {attempt_id, logical_trade_id, symbol, direction, entry_type, bracket_size,
                fill_price, fill_time, exit_price, exit_time, exit_reason, pnl_points,
                mode: "sim"|"paper"|"live"}

ExecutionService:
    submit(TradeIntent) -> OrderAttempt
    cancel(attempt_id | logical_trade_id) -> bool
    get_status(attempt_id | logical_trade_id) -> {attempt, fills, events}
    list_open(source: str | None) -> [OrderAttempt]
    get_closed_trades(filter) -> [ClosedTrade]
    health() -> {broker, decider, gateway_connected, uptime_seconds}
    reconcile() -> {naked_positions_found, actions_taken, ran_at}
```

---

# PART E — BACK-TRADING / EXPERIMENT ENGINE

**E1. Best reusable experiment-runner foundation**: `cl_algo_backtester.py` — resumable
(UNIQUE constraint + `INSERT OR IGNORE`), parallel-safe (WAL + symbol partitioning). Its
combo axis (`AlgoType × tp_ticks × sl_ticks × direction_filter × strength_max`) needs
generalizing to an open `(algorithm_id, ParameterSet)` pair, but the resumability/parallelism
mechanics are exactly right and should not be rebuilt.

**E2. Best reusable simulator**: `back-trading/simulator.py`'s core `_sim_one`/`simulate_exit`
— tick-by-tick, empirically slippage-tuned (§12's `_SL_SLIP_TICKS=0` finding, tuned against
real fill data via `calibrate.py`, not guessed). Must reconcile the two TP-confirmation rule
variants (A8 item 9) before treating it as one unambiguous primitive.

**E3. Best scoring implementation, and why**: `cl_algo_scorer.py` — win_rate, profit_factor,
expectancy, Sharpe, SQN, a weighted composite score, plus two real anti-overfit guards
(`MIN_N_FILLS=3` floor, a "stability zone" requiring a profitable neighbor within ±1
tp/sl step). This is meaningfully more rigorous than `grader.py`'s simple ±1-tick match
rate and should be the starting point, generalized to accept any algorithm's result rows.

**E4. Best learner/feedback implementation**: `cl_algo_learner.py`'s Bayesian Grid Narrowing
— top-20% combos define a "hot zone," next iteration's grid is finer around that centroid
plus 20% random exploration of unexplored space, with an explicit convergence criterion
(top-5 fingerprint stable across 3 consecutive scoring runs). Keep the mechanism; generalize
the parameter-space representation from raw int lists to typed `ParameterDefinition` ranges
(Golden Rule 4).

**E5. Retire, to avoid three competing experiment systems**: `back-trading/engine.py`'s
plain (non-reality-model) day-by-day path and `back-trading/grader.py` — both fully
subsumed by the CL Algo pipeline's capabilities. `back-trading/reality_model.py`'s
duplicated order-submission implementation — should call `ExecutionService` instead (D1).
Keep `calibrate.py`, narrowly, as a simulator-accuracy tool only (§13) — it answers a real,
different question ("is my simulator trustworthy") that the CL Algo pipeline doesn't
address and shouldn't absorb.

**E6. Algorithm Registry design**:
```python
AlgorithmDefinition = {
    "algorithm_id":      str,   # stable slug: "critical_line_bounce", "geva_sr", ...
    "version":           str,
    "entrypoint":        callable,  # (context, params: ParameterSet) -> [TradeIntent-shaped signal]
    "parameter_schema":  [ParameterDefinition],
    "required_context":  [str],  # e.g. "armed_critical_lines", "geva_posts", "bars_30m"
}
```
`lib/algo_engine.py`'s `AlgoParams`/`_build_cmds()` is already almost exactly this shape for
one family — the registry generalizes "one family" to "any number of families," each
self-describing its own required inputs.

**E7. `ParameterDefinition`**:
```python
ParameterDefinition = {"name": str, "type": "int"|"float"|"enum"|"bool",
                         "range_or_choices": [...], "default": Any, "unit": str | None}
```

**E8. `Experiment` definition** — exactly Golden Rule 4's list, all fields already
identifiable from this repo's existing practice (`cl_algo_combo_scores`' `UNIQUE` key is
most of this already, informally):
```python
Experiment = {"experiment_id", "algorithm_id", "algorithm_version", "parameter_set",
              "dataset_id", "dataset_version", "date_range", "simulator_version",
              "cost_slippage_assumptions", "random_seed", "code_git_version"}
```
**Additional field this repo's evidence says is necessary**: an explicit
`split: "train"|"validation"|"out_of_sample"` tag — Golden Rule 5 requires train/val/OOS
separation, and **nothing in this codebase currently implements or enforces that split**
(flagged as a real gap, not assumed solved).

**E9. Common Result schema** — generalizes `cl_algo_sim_results`' shape (already close) and
**directly closes the sim-vs-real comparability gap** (§11/§13 of the forensic report) by
adding `experiment_id` and `mode`:
```python
Result = {
    "experiment_id": str, "mode": "sim"|"paper"|"live",
    "symbol", "entry_price", "tp_price", "sl_price",
    "exit_reason": "TP"|"SL"|"STAGNATION"|"EXPIRED",
    "exit_fill_price", "pnl_points", "ticks_to_exit",
    "logical_trade_id": str | None,   # set only when mode != 'sim' — links to a real ExecutionService trade
}
```

**E10. Raw per-trade evidence retained**: already done correctly at the per-trade grain in
`cl_algo_sim_results` (not just aggregate scores) — keep this practice, it's what makes
`cl_algo_scorer.py`'s stability-zone guard possible in the first place.

**E11. Train/validation/walk-forward evaluation**: **not implemented anywhere found in this
codebase.** This is a genuine, currently-unmet requirement of Golden Rule 5, not an existing
capability to generalize. Recommend enforcing `Experiment.split` (E8) at the runner level —
a backtester run tagged `validation` should refuse to write into the same `cl_algo_combo_scores`-
equivalent table the learner reads for promotion decisions from `train` data.

**E12. Preventing the learner from overfitting the same historical period**: partially
addressed today (`MIN_N_FILLS` + stability-zone guard), but **no walk-forward or
out-of-sample separation exists** — same gap as E11, flagged again because it's the single
most consequential missing piece relative to Golden Rule 5's explicit requirement.

**E13. Running multiple algorithms/configs in parallel safely**: `cl_algo_worker.py`'s
per-symbol lock file + WAL mode already proves the pattern works. Generalize the lock key
from `symbol` to `(algorithm_id, dataset_partition)` so two different algorithms on the same
symbol, or the same algorithm on two different date ranges, can run concurrently without
false contention.

**E14. Resuming failed/interrupted experiments without duplication**: already solved —
`UNIQUE` constraint + `INSERT OR IGNORE`, proven in `cl_algo_sim_results`. Keep verbatim.

**E15. Choosing the next experiment**: `cl_algo_learner.py`'s Bayesian Grid Narrowing.
Keep the mechanism; the input just needs to come from the generalized `ParameterDefinition`
ranges (E7) instead of hardcoded `DEFAULT_TP_TICKS`/`DEFAULT_SL_TICKS` lists.

**E16. What should be deterministic, and a concrete gap found**: the simulator's fill logic
itself is deterministic given identical inputs (no randomness in `simulator.py`).
`generator.py`'s synthetic timestamp/offset selection is seedable (`random.seed()` accepted
as a parameter) and used correctly. **`lib.db.spawn_replenishment()`'s replenishment
direction is `random.choice(["BUY","SELL"])` with no seed control at all** — a genuine
reproducibility gap under Golden Rule 4: a backtest or paper-trade run that includes
replenishment chains cannot currently be exactly reproduced without capturing (or seeding)
this specific random call. Flagged for the architect, not silently fixed.

**E17. Comparisons required**:
- Same algorithm/different params: already supported (`cl_algo_combo_scores`).
- Different algorithms: needs the Algorithm Registry generalization (E6).
- Different datasets/time periods: needs explicit `dataset_id`/`dataset_version` — **doesn't
  exist today**; datasets are implicitly "whatever tick CSVs happen to be on disk," with no
  versioning or content hash. Real gap.
- Sim results vs. later paper results: **the core gap this whole document is trying to
  close** — solved by E9's `mode`+`logical_trade_id` design, not solved by anything
  currently in the codebase.

**E18. Promotion gates before reaching paper execution**: **currently none exist.**
`algo_lab.py` submits directly to real paper trading (`ExecutionService.submit()`-equivalent)
with no automated gate beyond a human clicking a UI button. Proposed `PromotionDecision`
requiring, before any `Experiment`'s output can call `ExecutionService.submit()`: (a)
`MIN_N_FILLS` met, (b) stability-zone guard passed, (c) a look-ahead/data-leakage audit
passed (Golden Rule 5 — **not implemented anywhere in this codebase today**, a real gap to
build, not to discover already solved), (d) explicit recorded human or policy approval.

**E19. Immutability/versioning**: `cl_algo_combo_scores` is already append-only by design
(`UNIQUE(scored_at, symbol, combo...)`, never updated in place) — good, keep this pattern.
Extend it: once results exist against an `Experiment`, that `Experiment`'s definition
(parameter_set, dataset reference, split) should become immutable — a "new idea" is always
a new `experiment_id`, never a silent edit of a past one.

**E20. Migrate vs. preserve as historical only**: `cl_algo_*` tables' existing rows —
preserve as historical research evidence (they already contain real, cited findings —
`random_mkt` at -$109,468.75/486 trades, `critical_line` at -$797.50/14 trades 0% win rate,
per the v4.27 release note read this session) — don't force-migrate into a new schema if
shapes don't cleanly map; a lossy migration of genuinely informative negative results would
be worse than leaving them queryable in place. Same for `back-trading/db.py`'s `runs`/
`sim_orders`/`sim_fills` — historical only, not live-migrated.

### Proposed contracts

```python
AlgorithmDefinition = {algorithm_id, version, entrypoint, parameter_schema, required_context}
ParameterDefinition = {name, type, range_or_choices, default, unit}
ParameterSet = {algorithm_id, values: {name: value}}
DatasetRef = {dataset_id, version, symbol, date_range, source: "fetcher2026"|...}
Experiment = {experiment_id, algorithm_id, algorithm_version, parameter_set, dataset_ref,
              simulator_version, cost_slippage_assumptions, random_seed, code_git_version,
              split}
ExperimentRun = {experiment_id, started_at, finished_at, status, resumable_progress}
SignalResult = {experiment_id, timestamp, symbol, direction, entry_type, entry_price,
                 tp_price, sl_price, origin_context}
SimulatedTrade = Result  # (mode='sim', see E9)
Metrics = {win_rate, profit_factor, expectancy, sharpe, sqn}
Score = {experiment_id, metrics: Metrics, composite_score, rank, data_status,
          stability_zone_passed: bool}
LearnerRecommendation = {algorithm_id, symbol, iteration, recommended_parameter_ranges,
                           convergence_status, reasoning}
PromotionDecision = {experiment_id, approved: bool, gates_passed: [str], gates_failed: [str],
                       approved_by, approved_at, rationale}
```

---

# PART F — QUESTIONS / REQUIREMENTS FOR SIBLING PROJECTS

```text
TO FETCHER2026:
1. I require historical tick (TRADES/BID_ASK) and OHLCV bar (1s/5s/30s/30m) data by
   (symbol, date-range). Can your proposed MarketDataService.get_dataset() guarantee an
   explicit dataset_id/version I can cite in an Experiment definition, so two experiments
   run a week apart against "the same" range are provably comparable?

2. My CL Algo backtester currently reads tick CSVs directly from disk
   (C:\Projects\Galgo2026\june\trader\data\history\), and I have my OWN direct-IB backfill
   scripts for bars.db (scripts/backfill_bars.py, import_7year_bars.py) that duplicate your
   job. Can your future contract fully replace both, including the 7-year Databento-sourced
   history currently merged into bars_30m (not IB-native — will your dataset versioning
   handle a mixed-provenance dataset correctly)?

3. If I ask for a dataset that's missing or incomplete, does ensure_dataset() block
   synchronously until fetched, or return a "not ready, check back" status? My experiment
   runner needs to know which, to decide whether to retry-poll or queue.

4. Your BARS1S_STATUS.md documents a real, measured throughput collapse when 3+ heavy IB
   consumers run concurrently (§0d, ~88% failure rate). If my Experiment Engine's
   reality-model calibration path (which does hit IB PAPER directly, unlike pure
   backtesting) runs at the same time as your fetchers, what pacing-budget coordination
   contract do you need from me to avoid recreating that collapse?

5. My reconcile_naked_positions() and general execution logic assume IB Gateway is a
   single, exclusively-owned resource from my perspective. Do you agree IB Gateway itself
   needs one explicit owner/allocator service that both of us register client-ID pools
   against, rather than each maintaining independent pools that happen not to collide today?

TO GEVAEXTRACT:
6. I currently accept your commands rows via direct SQLite INSERT (your
   insert-commands.py). Can your proposed AlgorithmPlugin/execution-bridge produce
   normalized TradeIntent objects and call a real submit_trade_intent() contract instead,
   so a schema change on my side doesn't silently break your submissions?

7. Your Cancel-All feature currently also calls my forbidden legacy port 5001
   (trader/visualizer/app.py) for an IB global-cancel relay. Can you confirm what
   functionality you actually need from that call, so I can provide it via
   ExecutionService.cancel_all() and let you drop the port-5001 dependency entirely?

8. If your algorithm becomes a registered AlgorithmPlugin instead of an external process
   writing directly to my DB, does your scraping/signal-generation logic have any state
   that can't be captured in a stateless (context, params) -> signals call — e.g. does it
   need to remember prior days' posts across calls, or can that live entirely in your own
   geva.db, invisible to me?

TO THE CENTRAL ARCHITECT / EXPERIMENT ENGINE OWNERS (if a role beyond mine exists):
9. My Experiment/Result schema proposal (E8/E9) adds train/validation/out-of-sample split
   enforcement and dataset versioning as NEW requirements — neither currently exists
   anywhere in this codebase. Should that gap block the rebuild's first milestone, or is it
   acceptable to launch without it and add it before any promotion gate goes live?

10. Should bars.db's ownership (disputed in A5) be resolved before or after the execution/
    experiment-engine contracts are finalized? It affects Correlation-tab behavior but not
    execution correctness directly.

11. Do we adopt IB permId now (low cost, forward-looking) or defer it to the "after parity"
    list (A9)? I lean defer, but it's cheap enough to reconsider.

12. Should logical_trade_id (D4) be a UUID generated by ExecutionService, or should it be
    deterministically derivable (e.g. from the root attempt's own integer ID, prefixed)?
    The latter is simpler and avoids a new ID scheme, but ties the "logical trade" concept
    permanently to "whichever attempt happened to be first," which is already usually true
    today but not enforced.

13. Is a single shared galao.db-equivalent SQLite file still the right choice once
    Fetcher2026, GevaExtract, and CC2026 are all calling contracts instead of touching files
    directly — or does "maximum independence" (Golden Rule 1) imply each service should own
    a physically separate database, with cross-service reads happening only through
    contracts, never even read-only file access? I lean toward the latter given GevaExtract's
    current read-only sql.js pattern already works and would generalize cleanly, but this is
    a real decision point, not something I should resolve unilaterally.

14. Should the Experiment Engine and Execution Service be separate deployable processes from
    day one, or is it acceptable to keep them as two modules in one process initially (per
    the staged migration in A7) and only physically split them once contracts are proven?

15. My forensic report and this document total roughly 1,700 lines across two files. Is
    that the right level of detail for the central architect, or should I produce a
    condensed decision-only summary as a separate artifact?
```

---

# PART G — RISKS / DISAGREEMENTS

1. **Disagreement with the proposed pipeline diagram's implied one-directionality.** The
   diagram (Data → Algorithms → Experiment Engine → Results → Feedback → Promotion →
   Execution → Lifecycle) reads as a clean forward pipeline, but real evidence from this
   repo shows execution-layer facts feed back into decision logic mid-flight:
   `decider.py`'s toggle rule and replenishment re-pricing both consult live market price
   from the execution side (`price_cache`, populated by `broker.py` on fill) *before*
   deciding the next signal. The architecture needs an explicit "live market state" shared
   read surface both the paper-execution path and any live algorithm consult — it isn't a
   strictly one-way pipeline in practice, and pretending it is would misdescribe a real,
   working mechanism.

2. **A requirement that would force unnecessary coupling if taken literally**: Golden Rule
   1's "not depend on another component's internal classes/functions" is already violated,
   usefully, today — `algo_lab.py` imports `_build_cmds` (a private, underscore-prefixed
   function) directly from `algo_engine.py`. This works because they're co-located. Once
   "algorithm" and "experiment submission" become separate services, this needs a real
   answer: does the Algorithm Registry's `entrypoint` contract become the *one* call both
   the Experiment Engine and any live-submission path use (my recommendation), or do we
   accept two divergent implementations of "what does this algorithm want to do right now"?
   I recommend the former explicitly, because a divergence here is exactly the kind of bug
   class (sim says one thing, live does another) this whole rebuild is trying to prevent.

3. **An existing mechanism "maximum independence" could weaken if applied carelessly**:
   the price-derived `exit_reason` logic (item 3 of A8) needs both the bracket definition
   (`tp_price`/`sl_price`) and the fill price in the same transaction/query to work
   correctly. If `OrderAttempt`/`BrokerOrder`/`Fill` get split across service boundaries per
   D-question 11's recommendation, whichever service computes `exit_reason` must not require
   a live cross-service call to do so — this safety-critical logic should stay computable
   from data already local to wherever it runs, not become fragile to a network hop.

4. **Concrete performance/reliability concern, evidenced by a sibling repo's own incident
   log**: `Fetcher2026\BARS1S_STATUS.md` §0d documents a *measured* ~88% per-attempt failure
   rate when 3 heavy IB consumers ran concurrently against the shared pacing budget. "Maximum
   independence" implies more services, potentially more concurrent IB touchpoints
   (my own `--reality-model`/`calibrate.py` calibration path is one; any future
   Experiment Engine worker that validates against live data would be another). This is not
   a hypothetical risk — it already happened once, to a different but related system, for
   exactly this reason. The central architect should treat IB Gateway pacing coordination
   (my Part F question 5) as a hard requirement of the design, not an operational afterthought.

5. **Reliability concern independent of the rebuild**: `RECONCILE_REQUIRED` being a
   dead-end today (D10) is a real, currently-open gap that predates any architecture
   discussion. It should not be silently inherited into the new system without an explicit
   decision — either fix it as part of this rebuild's execution-service work, or consciously
   defer it and say so.

6. **Migration risk**: unifying the two replenishment mechanisms (D6) touches the largest
   live trade population in the system. Getting it wrong silently loses lineage data or
   double-replenishes a real (paper) position. This specifically requires the shadow-run
   staging in A7 step 3 — I would push back hard on any plan that proposes a direct,
   un-staged cutover here, regardless of time pressure.

7. **Unresolved question requiring the central architect's decision**: `bars.db` ownership
   (A5) — CC2026 currently duplicates Fetcher2026's acquisition responsibility via its own
   IB-facing backfill scripts. I've proposed retiring CC2026's path in favor of Fetcher2026's
   future `MarketDataService`, but I don't have visibility into why the duplication exists
   in the first place (possibly a historical expedience, possibly a real reason I'm missing)
   — flagged rather than assumed.

---

# MESSAGE TO CENTRAL ARCHITECT

**Facts materially affecting the unified architecture:**

1. CC2026 already demonstrates working algorithm-independent execution today — three
   different producers (one in a different repo/language) write into one table, one broker
   loop executes all of them identically. This is real evidence for the target
   architecture, not aspirational design.

2. The single most important correctness gap: two replenishment mechanisms exist, and only
   one preserves trade lineage. The one that doesn't (`decider.py`) covers the *largest*
   current trade population. Unifying this must be staged (shadow-run), not a flag-day cutover.

3. Simulated and real trade results currently live in two separate, ID-disconnected
   database schemas. `calibrate.py` bridges them only by re-simulating from stored values,
   never by shared identity. This blocks any "compare sim to later real results" capability
   and is the concrete gap the proposed `mode`-discriminated `Result` schema closes.

4. A real, working feedback loop already exists (CL Algo pipeline: backtest → score → learn
   → repeat, with anti-overfit guards) — scoped to one algorithm family today. Generalizing
   its combo axis to an open Algorithm Registry is bounded, real work, not a from-scratch build.

5. Golden Rule 5 (no look-ahead before optimization) is **not currently implemented
   anywhere** in this codebase — no train/validation/out-of-sample split, no leakage audit.
   This is a genuine gap to build, not a rediscovery of an existing safeguard, and should be
   a launch-blocking requirement for any promotion gate.

6. CC2026 currently has its own IB-facing historical-data fetch scripts, duplicating
   Fetcher2026's job. This duplication is real, current, and unresolved — I recommend
   retiring CC2026's path but flag it as the central architect's call (Part G #7).

7. IB Gateway pacing collapse under concurrent heavy consumers is not hypothetical — it's
   documented, measured, and already happened once in a sibling repo. Any design adding more
   concurrent IB-touching services (calibration, live algorithm validation) needs an explicit
   pacing-budget-allocation contract from day one, not as a later optimization.

8. `spawn_replenishment()`'s direction choice is unseeded randomness — a concrete,
   currently-hidden reproducibility gap relative to Golden Rule 4's requirements.

9. Ten specific mechanisms (A8) must survive any rewrite verbatim — several are subtle
   enough (idempotent status-gated writes, price-derived exit_reason, the main-loop-only
   rebase constraint) that a clean-room reimplementation would very plausibly drop them
   without realizing it, since none are covered by an existing named rule or test today.

10. This document and its companion forensic report total roughly 1,700 lines. I'd welcome
    direction on whether that's the right granularity going forward, or whether future
    rounds should target a condensed decision-log format instead (Part F question 15).
