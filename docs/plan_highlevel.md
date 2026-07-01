# Galgo July 2026 — High-Level Plan

**Revised:** 2026-07-01 (v2 — research findings incorporated)
**Status:** Approved for implementation

---

## Mission

Build a self-improving backtesting machine for intraday micro-futures bracket orders.
Every real IB paper fill is a data point. Every data point runs through a Cartesian product
of algorithm variants and parameters. The system improves daily as more verified trades
and tick data accumulate.

---

## Three Pillars

### Pillar 1 — Verified Trade Database

**What:** A clean, growing DB of real IB paper fills that pass strict validation.

**Source of truth:** IB paper fills only. If IB did not confirm a fill price and timestamp, it does not exist.

**Acceptance criteria for a verified trade:**
- Entry fill: IB-confirmed fill_price and fill_time
- Exit fill: IB-confirmed exit_price, exit_time, exit_reason (TP/SL/EOD)
- Price sanity: entry price within 3% of CME reference price for that session
- Time sanity: fill_time during RTH or ETH — not weekend, holiday, or pre-market
- Completeness: direction, symbol, bracket_size all present and non-null
- Rationality: pnl_points matches (exit − entry) × direction × multiplier within rounding tolerance

**Discard criteria:** Incomplete bracket, IB error code on fill, irrational price, missing timestamp.

**New PC bootstrap:** Retrieve any previously verified trades from Google Drive backup.
Trades that cannot be matched to an IB fill record are discarded.

---

### Pillar 2 — Intelligent Data Fetching

**What:** Continuous tick data collection (TRADES + BID_ASK) for all 4 symbols.

**Priority rule — evaluated dynamically after each completed file:**

1. Days with >= 1 verified trade AND missing tick files: HIGHEST
2. Days sorted by verified_trade count DESC (more trades = more backtest value)
3. Recent dates with no verified trades: MEDIUM
4. All other historical dates: LOW

**Resilience on restart:**
- Lock file prevents duplicate scheduler instances
- **Half-baked files (new):** On restart, read the last timestamp in the existing CSV and resume fetching from that point rather than deleting and starting over

---

### Pillar 3 — Cartesian Product Backtrader

**What:** For each verified trade, simulate it against every combination of parameters.
Score each combination. Improve daily.

**Key insight:** `simulator.simulate_exit()` already exists and is the perfect hook. Feed
it a real IB fill price + time; vary (TP, SL, entry_delay, etc.) across the matrix.
No re-simulation of entry needed — IB already gave us the fill.

**The Cartesian product (Phase 1 — 10,800 combinations):**

| Axis | Values | Count |
|---|---|---|
| TP (ticks) | 2, 4, 6, 8, 10, 12 | 6 |
| SL (ticks) | 2, 4, 6, 8, 10, 12 | 6 |
| Entry delay (seconds after signal) | 0, 5, 15, 30, 60 | 5 |
| Entry offset (ticks from line) | -2, -1, 0, +1, +2 | 5 |
| TP confirmation ticks | 1, 2, 3 | 3 |
| Session window (CT) | ALL, MORNING (08:30-11), MIDDAY (11-13:30), AFTERNOON (13:30-15:15) | 4 |

**Total: 6 x 6 x 5 x 5 x 3 x 4 = 10,800 combinations.**
With 10 verified trades: 108,000 simulation runs (seconds).
With 50 verified trades: 540,000 runs (minutes).

---

**Scoring — 12 metrics, configurable weights:**

| # | Metric | Weight | Min Trades |
|---|---|---|---|
| 1 | Win rate | 0.08 | 20 |
| 2 | Profit factor | 0.15 | 20 |
| 3 | Expectancy (mean pnl ticks/trade) | 0.12 | 20 |
| 4 | Sharpe ratio (mean/std x sqrt(N)) | 0.12 | 30 |
| 5 | Sortino ratio (mean / std(losses) x sqrt(N)) | 0.10 | 30 |
| 6 | Calmar ratio (return / max_drawdown) | 0.08 | 30 |
| 7 | Max drawdown (ticks) | 0.10 | 15 |
| 8 | Avg win / avg loss ratio | 0.08 | 20 |
| 9 | SQN - System Quality Number (Van Tharp) | 0.07 | 25 |
| 10 | Fill rate (1 - EXPIRED rate) | 0.05 | 3 |
| 11 | Max consecutive losses | 0.03 | 20 |
| 12 | Monte Carlo p-value (lower = better) | 0.02 | 30 |

**Benchmarks:**
- Profit Factor: <1.0 losing | 1.5-2.0 good | >3.0 suspicious (possible overfit)
- SQN: <1.6 poor | 2.5-2.9 good | 3.0-5.0 excellent | >5.0 too good to be true

Composite score = weighted sum of min-max normalized metrics. Weights in `config.yaml`, tunable daily.

---

**Anti-overfitting (research-validated, 4 guards):**

1. **Insufficient data gate:** combinations with < 20 filled exits = `insufficient_data`, not ranked
2. **Monte Carlo Permutation Test:** shuffle trade order 1,000x, rescore; only accept combinations where real score beats > 95% of shuffles (p < 0.05)
3. **Stability Zone:** winner must also score well in its 8 nearest Cartesian neighbors (±1 step per axis); reject spiky optima
4. **Leave-One-Out Cross-Validation (LOOCV):** with < 100 trades, use LOOCV instead of walk-forward; LOOCV score must be >= 80% of in-sample score

---

### System Map

```
IB Paper Gateway (port 4002)
       |
       |-- broker.py ---------> commands table -> fill -> completed_trades
       |                                                         |
       |                                                  verified_trades VIEW
       |                                                  (strict validation)
       |                                                         |
       |-- fetch_scheduler.py --> CSV files              bt_matrix_runner.py
       |    (dynamic priority)                           (Cartesian product)
       |    (resume on restart)                                   |
       |                                               bt_matrix_results table
       |                                                         |
       |                                               bt_scorer.py
       |                                               (12 metrics + 4 guards)
       |                                                         |
       |                                               bt_scores + bt_score_history
       |                                                         |
       `-- visualizer/app.py ----> dashboard (score panel)
                                   email (step reports)
```

---

## Definitions

**Cartesian product:** The set of ALL combinations of two or more parameter sets.
If TP in {2,4,6} and SL in {2,4,6}, the Cartesian product has 9 elements:
(TP=2,SL=2), (TP=2,SL=4), ... (TP=6,SL=6). We score each independently.

**Verified trade:** A completed IB paper bracket (entry + exit) passing all sanity
checks. The only input to the Cartesian product runner.

**Half-baked file:** A CSV tick file started but not completed. On restart, the
fetcher reads the last timestamp from the file and resumes from there.

**SQN (System Quality Number):** Van Tharp's formula: mean(pnl) / std(pnl) x sqrt(N).
Normalizes performance quality independent of position size.

**LOOCV:** Leave-One-Out Cross-Validation. With N trades, fit on N-1, test on 1
held out, rotate. More data-efficient than walk-forward when N < 100.

---

## July Milestones

| Week | Milestone |
|---|---|
| Jul 1-2 | DB schema, verified_trades pipeline, fetcher resume-on-restart |
| Jul 3-5 | Cartesian runner + scorer working on any verified trades |
| Jul 6-8 | Dashboard score panel, email per step, priority-driven fetching |
| Jul 9-15 | Daily improvement loop, LOOCV, Monte Carlo guard |
| Jul 16+ | Parameter convergence, stability zones, add Cartesian axes as data grows |

---

## What We Are NOT Building (July)

- Live trading auto-parameter switching
- ML / neural net models (Cartesian product first; ML when N > 200 trades)
- Multi-contract sizing optimization
- Cross-symbol correlation scoring
