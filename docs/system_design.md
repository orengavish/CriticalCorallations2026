# Galgo 2026 — Full System Design Document
**Version:** 1.0  
**Status:** Design complete, implementation pending  
**Last updated:** 2026-04-26

> **2026-08-23 — bug 6 & bug 7 resolved** (see `docs/release_notes.md` v0.7.0). CL Algo's
> `history_dir` path resolution and the auto-detected-line strength scale (which the
> engine's `strength_max` filter was silently dropping) are both fixed. **All Algo Lab
> auto-detected-line results and all CL Algo pipeline results produced before this date
> are invalid** and should be re-run.

---

## Table of Contents
1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Data Pipeline](#3-data-pipeline)
4. [Signal Categories — 5×5×5](#4-signal-categories)
   - [CL — Critical Lines](#41-cl--critical-lines)
   - [CORR — Inter-Market Correlation](#42-corr--inter-market-correlation)
   - [VP — Volume Profile](#43-vp--volume-profile)
   - [RD — Regime Detection](#44-rd--regime-detection)
   - [TOD — Time-of-Day Bias](#45-tod--time-of-day-bias)
5. [Combining Layer](#5-combining-layer)
6. [A/B Harness](#6-ab-harness)
7. [Implementation Sequence](#7-implementation-sequence)
8. [Config Reference](#8-config-reference)

---

## 1. System Overview

### What This System Does
An AI-ready intraday algorithmic trading system for US equity index futures (MES/ES). It runs 25 parallel signal-generating algorithms across 5 categories, combines them through a gated weighting layer, and produces a single trade signal with position size at each bar.

Every parameter in every algorithm is configurable. An A/B harness continuously tests config variants against the current baseline to find optimal settings — no manual tuning.

### Design Principles
- **AI-ready:** Every algorithm has a config file with all tunable parameters exposed. The A/B harness can mutate any parameter and evaluate the result.
- **Composable:** Each algorithm outputs a standard signal schema. Combining logic is separate from signal generation.
- **Incremental:** The system runs with 2 algorithms as validly as it runs with 25. Categories and approaches can be enabled/disabled via config.
- **Data-grounded:** All signals derive from `trades.csv` (actual executed trades). No bid/ask order book required, though quote data can optionally enhance direction classification.

### Instruments
| Phase | Contract | Reason |
|---|---|---|
| Current | MES (Micro E-mini S&P 500) | Higher trade count → better statistical testing |
| Live (~2 months) | ES (E-mini S&P 500) | Full-size, more liquid |
| CORR expansion | MNQ, MYM, M2K | Added to daily fetcher when data available |

### The 5×5×5 Structure
- **5 categories:** CL, CORR, VP, RD, TOD
- **5 approaches per category:** 25 total signal generators
- **5+ config parameters per approach:** full A/B surface

---

## 2. Architecture

```
trades.csv (MES + others)
        │
        ▼
┌─────────────────┐
│  DATA PIPELINE  │  Fetch → Validate → Preprocess → Store
└────────┬────────┘
         │ processed bars, volume profiles, features
         ▼
┌─────────────────────────────────────────────────────────┐
│                  SIGNAL LAYER (25 approaches)           │
│  ┌──────────┐ ┌──────────┐ ┌──────┐ ┌──────┐ ┌──────┐ │
│  │  CL 1-5  │ │ CORR 1-5 │ │VP 1-5│ │RD 1-5│ │TOD1-5│ │
│  └──────────┘ └──────────┘ └──────┘ └──────┘ └──────┘ │
└────────────────────────┬────────────────────────────────┘
                         │ 25 Signal objects
                         ▼
┌─────────────────────────────────────────────────────────┐
│                  COMBINING LAYER                         │
│  L2: Category aggregation (5 → 1 per category)         │
│  L3: Context gating (RD × VP × TOD-4 multipliers)      │
│  L4: Final signal (CL + CORR + TOD → direction+strength)│
│  L5: Position sizing (strength × confidence → contracts) │
└────────────────────────┬────────────────────────────────┘
                         │ FinalSignal
                         ▼
                    ORDER / BACKTEST
                         │
                         ▼
              ┌─────────────────────┐
              │    A/B HARNESS      │
              │  Mutate → Test →    │
              │  Compare → Store    │
              └─────────────────────┘
```

---

## 3. Data Pipeline

### 3.1 Directory Structure
```
data/
├── raw/
│   ├── trades/
│   │   ├── MES/  {date}.csv
│   │   ├── MNQ/  {date}.csv      (added when fetcher expanded)
│   │   ├── MYM/  {date}.csv
│   │   └── M2K/  {date}.csv
│   └── quotes/                   (optional, for bid/ask enhancement)
│       └── MES/  {date}.csv
├── processed/
│   ├── bars/
│   │   └── MES/ 1min/ 5min/ 15min/  {date}.parquet
│   ├── volume_profiles/
│   │   └── MES/ 1tick/ 3tick/ 5tick/  {date}.parquet
│   └── features/
│       └── MES/  {date}.parquet
└── models/
    ├── tod_bias_table.parquet     (rebuilt weekly)
    ├── hmm_MES.pkl                (RD-3, retrained per config)
    └── cointegration_results.json (CORR-4)
```

### 3.2 Raw Schema
Every `raw/trades/{symbol}/{date}.csv`:
```
timestamp (ISO ms) | price (float) | quantity (int) | session (rth/eth/overnight)
```

### 3.3 Validation Steps
| Check | Rule | On Failure |
|---|---|---|
| Schema | All columns present, correct types | Reject file |
| Deduplication | Remove identical timestamp+price+qty rows | Remove, log count |
| Outlier detection | Price jump > `outlier_atr_mult × ATR` | Exclude row, flag |
| Gap detection | RTH gaps > `max_gap_minutes` | Log warning |
| Minimum rows | RTH trades > `min_trades_per_session` | Flag thin session |
| Session bounds | All RTH trades 09:30–16:00 ET | Trim out-of-bounds |

### 3.4 Preprocessing Steps
1. **Trade direction (tick rule):** `price > prev_price → buy`, `< → sell`, `== → carry_forward`
2. **Volume profiles (1/3/5 tick):** `bucket = floor(price/W)*W`, accumulate qty + buy_vol + sell_vol + delta per bucket
3. **Bar resampling (1/5/15 min):** OHLCV + bar_delta per bucket
4. **Feature computation per session:** ATR(14), realized_vol, session_high/low/open/close, POC/VAH/VAL

### 3.5 Schedule
```yaml
after_market_close:        # ~16:15 ET daily
  - fetch_raw_trades
  - validate
  - preprocess
  - update_tod_bias_table
  - run_pending_ab_tests

weekly_sunday:
  - retrain_hmm
  - rerun_cointegration
  - rebuild_full_tod_table

real_time_during_market:
  - stream_trades
  - update_developing_vp
  - emit_signals
```

### 3.6 Pipeline Config
```yaml
data_pipeline:
  source:
    type: broker_api               # file_drop | data_vendor
    symbols: [MES]                 # expands to [MES, MNQ, MYM, M2K]
  preprocessing:
    bar_resolutions: [1min, 5min, 15min]
    tick_bucket_sizes: [1, 3, 5]
    direction_method: tick_rule    # lee_ready when quotes available
    value_area_pct: 0.70
    outlier_atr_mult: 5.0
  retention:
    raw_days: 30
    processed_days: 365
    models: keep_all_versions
```

---

## 4. Signal Categories

### Universal Signal Schema
Every approach outputs this structure:
```yaml
Signal:
  approach_id: "CL-3"
  category: "CL"
  timestamp: datetime
  direction: long | short | neutral
  strength: float        # 0.0–1.0
  confidence: float      # 0.0–1.0
  expiry: datetime
  tags: list[str]        # e.g. [breakout, bounce, vol_confirmed]
```

---

## 4.1 CL — Critical Lines

**What it outputs:** Price levels (static prices) where the market is expected to react — bounce, stall, or accelerate through.  
**Data source:** `trades.csv` (tick data required — OHLCV bars insufficient)  
**Granularity runs:** All CL approaches run simultaneously at 1-tick, 3-tick, and 5-tick bucket widths.

---

### CL-1 — Volume Histogram Peaks

**Core idea:** Build a histogram of accumulated trade volume by price bucket. Peaks in this histogram = prices where the market concentrated trading = natural attractors and repellers.

**Pipeline:**
1. Round each trade to bucket: `bucket = floor(price / W) * W`
2. Sum quantities per bucket → `(price_bucket, total_volume)` sorted low→high
3. Optionally smooth histogram (rolling average over N buckets)
4. Detect local maxima where volume exceeds neighbors by `peak_prominence_ratio`
5. Filter peaks below `min_volume_abs`
6. Classify: above current price → resistance; below → support; within dead zone → neutral

**Output columns:** `price_bucket, total_volume, is_peak, classification, strength_score`

**Config:**
```yaml
CL1:
  bucket_size_ticks: 3             # run all: [1, 3, 5]
  lookback_days: 1                 # [1, 3, 5, 10]
  smoothing_window: 0              # [0, 2, 3] buckets
  peak_prominence_ratio: 2.0       # [1.5, 2.0, 3.0]
  peak_neighborhood_buckets: 3     # [2, 3, 5]
  min_volume_abs: 500              # [100, 500, 1000]
  classification_dead_zone_ticks: 10 # [5, 10, 20]
```

---

### CL-2 — Directional Traffic Analysis

**Core idea:** For each price bucket, track how price moved through it: arrivals from below/above, departures upward/downward. High bounce rate from below = support. High bounce rate from above = resistance.

**Pipeline:**
1. Reconstruct time-ordered price path from `trades.csv`
2. Assign each trade to bucket
3. Per bucket compute: `total_visits`, `arrivals_from_below`, `arrivals_from_above`, `departures_to_below`, `departures_to_above`
4. Derive: `bounce_rate_from_below`, `bounce_rate_from_above`, `support_score`, `resistance_score`
5. Classify by score vs threshold

**Output columns:** `price_bucket, total_visits, arr_from_below, arr_from_above, dep_to_below, dep_to_above, support_score, resistance_score, classification`

**Config:**
```yaml
CL2:
  bucket_size_ticks: 3             # [1, 3, 5]
  lookback_days: 1                 # [1, 3, 5, 10]
  primary_scoring_metric: composite # [support_score, resistance_score, total_visits]
  min_total_visits: 10             # [5, 10, 20]
  bounce_threshold: 0.70           # [0.60, 0.70, 0.80]
  recency_weighting: linear        # [none, linear, exponential]
```

---

### CL-3 — Cumulative Delta Flip Zones

**Core idea:** Track running cumulative delta (buy_vol − sell_vol) through the session in time order. Local peaks in the delta curve = where selling dominated; troughs = where buying dominated. The price at each flip = critical line.

**Requires:** Trade direction classification (tick rule from Phase 0e)

**Pipeline:**
1. Classify all trades → buy/sell (tick rule)
2. Compute running `cumulative_delta(t)` in time order
3. Smooth delta curve by `delta_smoothing_window` trades
4. Find local maxima (selling peaked → resistance) and minima (buying peaked → support)
5. Filter by `min_delta_swing` magnitude
6. Round flip prices to tick buckets

**Config:**
```yaml
CL3:
  direction_method: tick_rule      # [tick_rule, lee_ready, bvc]
  delta_smoothing_window: 10       # [0, 5, 10, 20] trades
  min_delta_swing: 1000            # [500, 1000, 2000] contracts
  lookback_days: 1                 # [1, 3, 5]
  bucket_size_ticks: 3             # [1, 3, 5]
```

---

### CL-4 — Multi-Session Structural Persistence

**Core idea:** Run CL-1 independently on each of the last N days. Price levels that appear as peaks across multiple separate sessions are structurally persistent — the market repeatedly returns to them. These are stronger than single-day peaks.

**Difference from CL-1 with lookback_days > 1:** CL-1 pools all days and finds peaks in the combined histogram. CL-4 finds peaks in each day separately, then identifies levels that repeat.

**Pipeline:**
1. For each of last N days, run CL-1 independently → list of peaks per day
2. Group peaks within `cluster_radius_ticks` of each other across days → level clusters
3. Count how many days each cluster appears → `day_count`
4. Filter by `persistence_threshold` (day_count / N)
5. Score = persistence ratio × average single-day strength

**Config:**
```yaml
CL4:
  lookback_days: 10                # [5, 10, 20]
  persistence_threshold: 0.60     # [0.40, 0.60, 0.80]
  cluster_radius_ticks: 5         # [2, 5, 10]
  recency_decay: linear           # [none, linear, exponential]
  base_algo: cl1                  # [cl1, cl2, cl3]
```

---

### CL-5 — Price Rejection / Velocity Reversal

**Core idea:** Find prices reached quickly (high velocity) and immediately reversed (velocity sign flip). The turnaround price = the market hit a wall. High speed of arrival + instant reversal = strongest critical line signal.

**The sine-line connection:** The peaks and troughs of any oscillation are where velocity goes to zero and reverses. This approach directly measures that zero-crossing in the actual price data.

**Pipeline:**
1. Convert trades to time-ordered price path
2. Compute rolling velocity: `Δprice / Δtime` or `Δprice / Δtrade_count`
3. Find velocity reversals: `high_positive_velocity → zero → high_negative_velocity` (or inverse)
4. Record price at the zero-crossing → rejection point
5. Cluster nearby rejection points by `cluster_radius_ticks`
6. Score by velocity magnitude and reversal sharpness

**Config:**
```yaml
CL5:
  velocity_window: 10              # [5, 10, 20] trades
  min_approach_velocity: 5.0      # [2, 5, 10] ticks/sec
  max_reversal_delay: 5           # [2s, 5s, 10 trades]
  min_reversal_magnitude: 3       # [2, 3, 5] ticks
  cluster_radius_ticks: 3         # [1, 3, 5]
```

---

### Bid/Ask Enhancement (All CL Approaches)

Applies when `quotes.csv` is available. The `direction_method` config controls mode:
- `tick_rule` — inferred from trades.csv only (~83% accurate)
- `lee_ready` — requires quotes.csv (~95% accurate)

Enhancements:
- **CL-1:** Delta per bucket (buy_vol − sell_vol). Positive delta peak → support; negative → resistance; near-zero → absorption zone (strongest signal type)
- **CL-2:** Failed breakout detection: aggressive buyers hit level, price reversed → confirmed resistance
- **CL-3:** More accurate delta flips

---

## 4.2 CORR — Inter-Market Correlation

**What it outputs:** Directional signal when two instruments diverge from their historical relationship.  
**Data source:** OHLCV bars (resampled from trades.csv for each symbol)  
**Status:** Disabled until multi-symbol data available (enabled via `enabled: true` in config)

**Symbols:** MES/MNQ/MYM/M2K (micro contracts) → ES/NQ/YM/RTY at live phase  
**Pairs:** All 6 combinations of 4 symbols, auto-generated from symbol list

**Shared CORR config:**
```yaml
CORR:
  enabled: false                   # true when multi-symbol data available
  symbol_pairs: all_combinations   # or list specific pairs
  bar_resolution: 5min             # [1min, 5min, 15min]
  session_filter: rth_only         # [rth_only, full_session]
  min_history_days: 20             # before enabling a pair
```

---

### CORR-1 — Rolling Pearson Correlation

**Core idea:** Compute rolling correlation of returns between two symbols. When r drops significantly below baseline → divergence → trade the lagging symbol.

**Config:**
```yaml
CORR1:
  correlation_window: 30           # [20, 30, 60] bars
  baseline_window: 200             # [100, 200, 390] bars
  divergence_threshold_sigma: 2.0  # [1.5, 2.0, 2.5]
  min_baseline_r: 0.75             # [0.70, 0.75, 0.80]
  signal_decay_bars: 10            # [5, 10, 20]
```

---

### CORR-2 — Spread Z-Score (Pairs Trading)

**Core idea:** Compute price spread A − h×B, normalize to z-score, trade when extreme.

**Config:**
```yaml
CORR2:
  hedge_ratio_method: rolling_ols  # [fixed_1:1, rolling_ols, kalman_filter]
  spread_window: 120               # [60, 120, 390] bars
  entry_zscore: 2.0                # [1.5, 2.0, 2.5]
  exit_zscore: 0.25                # [0.0, 0.25, 0.5]
  max_holding_bars: 20             # [10, 20, 60]
```

---

### CORR-3 — Lead-Lag Detection

**Core idea:** Compute cross-correlation at lags 0–N. Find which symbol leads by how many bars. Trade the lagger when the leader moves.

**Config:**
```yaml
CORR3:
  max_lag_bars: 10                 # [5, 10, 20]
  xcorr_window: 120                # [60, 120, 390] bars
  min_xcorr_at_lag: 0.60          # [0.50, 0.60, 0.70]
  stability_window: 5              # [3, 5, 10] days
  leader_move_threshold: 0.10     # [0.05%, 0.10%, 0.20%]
```

---

### CORR-4 — Cointegration

**Core idea:** Statistically prove a long-run equilibrium exists between two symbols before trading. Trade the residual (deviation from equilibrium). Retest on schedule.

**Config:**
```yaml
CORR4:
  coint_test_method: engle_granger # [engle_granger, johansen]
  coint_pvalue_threshold: 0.05     # [0.01, 0.05, 0.10]
  retest_frequency: weekly         # [daily, weekly, every_N_bars]
  lookback_for_test: 60d           # [30d, 60d, 90d]
  entry_zscore: 2.0                # [1.5, 2.0, 2.5]
```

---

### CORR-5 — Conditional / Regime-Gated Correlation

**Core idea:** Only trade correlation signals when the market regime makes them statistically valid. In stressed regimes, correlations break down — suppress all CORR signals.

**Config:**
```yaml
CORR5:
  regime_source: rd_module_output  # [vix_level, realized_vol_pct, rd_module_output]
  stressed_vol_threshold: 85       # percentile [75, 85, 90]
  base_corr_algo: corr1            # [corr1, corr2, corr3, corr4]
  stressed_action: suppress        # [suppress, invert, reduce_size]
  regime_confirmation_bars: 5      # [3, 5, 10]
```

---

## 4.3 VP — Volume Profile / Market Auction Theory

**What it outputs:** Market context (day type label, value area levels, distribution type) used to gate and weight CL, CORR, and TOD signals.  
**Data source:** `trades.csv` — same preprocessing as CL  
**Distinction from CL:** CL finds specific price lines. VP classifies market structure and day type.

---

### VP-1 — Developing Value Area

**Core idea:** Build the volume profile in real time. Track POC/VAH/VAL as the session progresses. POC migration direction = intraday bias.

**Config:**
```yaml
VP1:
  value_area_pct: 0.70             # [0.60, 0.70, 0.80]
  update_frequency: every_5min     # [every_bar, every_5min, every_N_trades]
  bucket_size_ticks: 3             # [1, 3, 5]
  poc_migration_window: 5          # [3, 5, 10] updates
  session_type: rth_only           # [rth_only, full_session]
```

---

### VP-2 — Prior Day Value Area Classification (Day Type)

**Core idea:** Compare today's open to yesterday's value area. This single comparison is the most reliable pre-open bias signal available.

| Open Type | Condition | Expected behavior |
|---|---|---|
| Open Inside VA | Open between PDVAl and PDVAh | Range day |
| Open Above VAH | Initiative buying | Trend day up |
| Open Below VAL | Initiative selling | Trend day down |
| Open Drive | Opens outside VA, moves further away | Strong trend day |
| Open Rejection | Opens outside VA, immediately returns inside | Aggressive fade |

**Config:**
```yaml
VP2:
  value_area_pct: 0.70             # [0.60, 0.70, 0.80]
  confirmation_bars: 5             # [3, 5, 10]
  open_drive_min_ticks: 8         # [5, 8, 12]
  rejection_return_ticks: 5        # [3, 5, 8]
  bucket_size_ticks: 3             # [1, 3, 5]
```

---

### VP-3 — TPO Profile Shape

**Core idea:** Count how many time-buckets visited each price level. Shape of resulting distribution classifies market structure: bell curve (balanced), P-shape (bearish), b-shape (bullish), double distribution (two-phase).

**Config:**
```yaml
VP3:
  time_bucket_minutes: 30          # [15, 30, 60]
  bucket_size_ticks: 3             # [1, 3, 5]
  shape_method: statistical        # [statistical, pattern_match, ml_classifier]
  skew_threshold: 0.75             # [0.50, 0.75, 1.00]
  bimodal_gap_ticks: 15            # [10, 15, 20]
```

---

### VP-4 — Poor High / Poor Low Detection

**Core idea:** Session extremes with thin volume = unfinished business. Price will return to explore that area in a future session. Forward-looking critical lines.

**Config:**
```yaml
VP4:
  thin_vol_ratio: 0.20             # bucket is thin if < X × session avg [0.15, 0.20, 0.30]
  min_thin_buckets: 3              # [2, 3, 5]
  bucket_size_ticks: 3             # [1, 3, 5]
  extreme_anchor: rth_high_low     # [session_high_low, rth_high_low, overnight_high_low]
  lookback_days: 1                 # [1, 3, 5]
```

---

### VP-5 — Volume at Extremes (Distribution / Accumulation)

**Core idea:** Heavy volume at session high + price reversal = distribution (institutional selling). Heavy volume at session low + reversal = accumulation (buying). Classifies institutional intent.

**Config:**
```yaml
VP5:
  extreme_zone_pct: 0.15           # top/bottom % of range [0.10, 0.15, 0.20]
  distribution_vol_threshold: 0.30 # min ratio of vol at extreme [0.25, 0.30, 0.35]
  reversal_confirmation_ticks: 8   # [5, 8, 12]
  use_delta: true                  # tick rule direction confirmation
  lookback_days: 1                 # [1, 3, 5]
```

---

## 4.4 RD — Regime Detection

**What it outputs:** Regime label (`trending_up/down`, `range_bound`, `compressed`, `volatile_shock`) at each bar, used as a multiplier on all other category signal weights.  
**Data source:** OHLCV bars from `processed/bars/`  
**Special role:** RD never generates trade signals. It gates all other modules.

---

### RD-1 — ADX (Average Directional Index)

**Core idea:** Trend strength from directional movement. ADX > threshold = trending. +DI vs −DI = direction.

**Config:**
```yaml
RD1:
  adx_period: 14                   # [7, 14, 21]
  bar_resolution: 5min             # [5min, 15min, 30min]
  trend_threshold: 25              # [20, 25, 30]
  chop_threshold: 20               # [15, 20, 25]
  direction_confirmation_bars: 2   # [1, 2, 3]
```

---

### RD-2 — Hurst Exponent

**Core idea:** Time-series memory. H > 0.55 = trending. H < 0.45 = mean-reverting. H ≈ 0.5 = random walk. The most fundamental statistical regime test.

**Config:**
```yaml
RD2:
  estimation_method: dfa           # [rs_analysis, dfa, variance_ratio]
  window_bars: 100                 # [50, 100, 200]
  trend_threshold_h: 0.55          # [0.55, 0.60, 0.65]
  reversion_threshold_h: 0.45      # [0.40, 0.45, 0.50]
  bar_resolution: 15min            # [5min, 15min, 30min]
```

---

### RD-3 — Hidden Markov Model (HMM)

**Core idea:** Probabilistic state machine. Learns regime structure from data. Outputs probability of each state (bull/bear/sideways) at each bar. Retrained on rolling schedule.

**Config:**
```yaml
RD3:
  n_states: 3                      # [2, 3, 4]
  feature_set: [return, vol, range, volume]
  retrain_frequency: weekly        # [daily, weekly, every_20_days]
  retrain_window_days: 60          # [30, 60, 90]
  emission_distribution: gaussian  # [gaussian, student_t]
```

---

### RD-4 — Realized Volatility Percentile

**Core idea:** Rank current volatility vs historical distribution. High percentile = volatile shock. Low percentile = compressed (coiling). Normal = standard regime.

**Config:**
```yaml
RD4:
  vol_metric: realized_vol         # [realized_vol, atr, bar_range_pct]
  vol_window_bars: 14              # [10, 14, 20]
  percentile_lookback_days: 60     # [20, 60, 252]
  hot_threshold_pct: 80            # [75, 80, 90]
  cold_threshold_pct: 20           # [10, 20, 25]
```

---

### RD-5 — Bollinger Band Width (Squeeze)

**Core idea:** BBW compression to historic lows = coiling, breakout building. BBW expansion = releasing energy, trending. Detects the coil-and-release pattern specifically.

**Config:**
```yaml
RD5:
  bb_period: 20                    # [10, 20, 30]
  bb_std_mult: 2.0                 # [1.5, 2.0, 2.5]
  squeeze_pct_threshold: 15        # BBW percentile below this = squeeze [10, 15, 20]
  expansion_rate_threshold: 0.001  # ΔBBW/bar above this = expanding [0.0005, 0.001, 0.002]
  lookback_bars: 100               # [50, 100, 200]
```

---

### RD Composite
```yaml
RD_composite:
  method: weighted_vote            # [majority_vote, weighted_vote, primary_only]
  weights: {RD1: 0.25, RD2: 0.20, RD3: 0.25, RD4: 0.15, RD5: 0.15}
```

---

## 4.5 TOD — Time-of-Day Bias

**What it outputs:** Time-based directional bias and signal suppression modifiers.  
**Data source:** Historical archive of all past `trades.csv` sessions (months → years)  
**Special role:** TOD-4 (lunch) suppresses other modules. TOD-1/2/5 generate directional signals.

### RTH Session Time Map (ET)
| Window | Time | Character |
|---|---|---|
| Opening 5 min | 09:30–09:35 | Extreme volatility |
| Opening range | 09:30–10:00 | OR forms |
| First reversal window | 10:00–10:30 | Many opening moves reverse |
| Morning trend | 10:30–11:30 | Most reliable directional window |
| Lunch compression | 11:30–13:30 | Low vol, choppy, suppress signals |
| Afternoon positioning | 13:30–15:00 | Re-engagement |
| Power hour | 15:00–15:45 | Institutional activity |
| MOC window | 15:45–16:00 | Mechanical rebalancing flow |

---

### TOD-1 — Historical Time-Bucket Bias Model

**Core idea:** Statistical lookup. For each N-minute bucket, what is the historical win rate for long? Built from all past sessions.

**Config:**
```yaml
TOD1:
  bucket_size_minutes: 30          # [15, 30, 60]
  lookback_sessions: 126           # [60, 126, 252]
  recency_weighting: linear        # [none, linear, exponential]
  condition_splits: vp2_day_type   # [none, day_of_week, vp2_day_type, rd_regime]
  min_sessions_for_signal: 50      # [30, 50, 100]
```

---

### TOD-2 — Opening Range System

**Core idea:** First N minutes define OR high/low. Trade breakout through OR or fade the false probe.

**Config:**
```yaml
TOD2:
  or_duration_minutes: 15          # [5, 15, 30]
  strategy: adaptive               # [breakout, fade, adaptive]
  min_break_ticks: 3               # [1, 3, 5]
  volume_confirmation_ratio: 1.5   # [1.0 (off), 1.5, 2.0]
  signal_expiry_minutes: 60        # [30, 60, 120]
```

---

### TOD-3 — Open Drive Detection

**Core idea:** Classify in first 15–30 min whether price is driving in one direction without pullback (trend day). Override all other signal weights if detected.

**Config:**
```yaml
TOD3:
  detection_window_minutes: 20     # [15, 20, 30]
  min_distance_ticks: 12           # [8, 12, 20]
  max_pullback_pct: 0.33           # [0.20, 0.33, 0.50]
  consistency_threshold: 0.70      # [0.60, 0.70, 0.80]
  volume_required: true            # + volume_ratio: 1.3
```

---

### TOD-4 — Lunch Compression Filter

**Core idea:** Dynamically detect low-vol lunch window. Suppress all module signals during compression. Do not suppress on trend days.

**Config:**
```yaml
TOD4:
  lunch_window_start: "11:30"      # ["11:30", "12:00"]
  lunch_window_end: "13:30"        # ["13:00", "13:30", "14:00"]
  vol_suppression_threshold: 0.50  # [0.40, 0.50, 0.60] × session avg
  suppression_factor: 0.30         # multiply signal weights by this [0.2, 0.3, 0.5]
  override_on_trend_day: true
```

---

### TOD-5 — MOC Window

**Core idea:** 15:45–16:00 ET is dominated by mechanical institutional flows. Most statistically persistent intraday edge.

**Config:**
```yaml
TOD5:
  window_start_time: "15:45"
  window_end_time: "16:00"
  mode: statistical                # [statistical, moc_data, combined]
  min_historical_edge: 0.58        # [0.55, 0.58, 0.60]
  condition_on_day_type: true
```

---

## 5. Combining Layer

### Layer 2 — Per-Category Aggregation

Within each category, 5 approach signals → 1 category signal:

```
category_raw_score(direction) =
  Σ( approach_weight_i × strength_i × confidence_i )
  for all approaches where direction_i == direction

category_direction = direction with highest raw_score
category_strength  = raw_score(winner) / Σ(all weights)
category_confidence = fraction of weighted votes that agreed
```

**Category aggregation config (example — CL):**
```yaml
CL_aggregation:
  approach_weights:
    CL1: 0.20
    CL2: 0.20
    CL3: 0.25
    CL4: 0.20
    CL5: 0.15
  min_approaches_active: 2
  neutral_if_split: true
  split_threshold: 0.40
  min_category_strength: 0.30
```

### Layer 3 — Context Gating (RD × VP × TOD-4)

**RD Multiplier Table:**
```yaml
rd_gate_multipliers:
  trending_up:   {CL: 1.30, CORR: 0.70, TOD: 1.00, size: 1.00}
  trending_down: {CL: 1.30, CORR: 0.70, TOD: 1.00, size: 1.00}
  range_bound:   {CL: 0.90, CORR: 1.30, TOD: 1.00, size: 0.90}
  compressed:    {CL: 0.60, CORR: 0.60, TOD: 0.80, size: 0.60}
  volatile_shock:{CL: 0.40, CORR: 0.30, TOD: 0.50, size: 0.30}
```

**VP Day-Type Multiplier Table:**
```yaml
vp_gate_multipliers:
  trend_day:       {CL_bounce: 0.30, CL_breakout: 1.40, CORR: 0.80, TOD: 1.00}
  range_day:       {CL_bounce: 1.30, CL_breakout: 0.50, CORR: 1.20, TOD: 1.00}
  open_drive:      {CL_bounce: 0.10, CL_breakout: 1.60, CORR: 0.60, TOD: 1.20}
  open_rejection:  {CL_bounce: 1.50, CL_breakout: 0.40, CORR: 1.10, TOD: 0.90}
```

**Effective weight formula:**
```
effective_weight = approach_weight × category_base_weight × RD_mult × VP_mult × lunch_mult
```

### Layer 4 — Final Signal

```yaml
final_aggregation:
  category_base_weights:
    CL:   0.45
    CORR: 0.30
    TOD:  0.25
  confluence:
    min_categories_agreeing: 2
    min_final_strength: 0.30
    disagree_action: no_trade      # [no_trade, trade_primary_only, reduce_size]
  direction_resolution: weighted_vote
```

**Final signal output:**
```yaml
FinalSignal:
  timestamp: datetime
  direction: long | short
  final_strength: float
  final_confidence: float
  agreeing_categories: list
  dissenting_categories: list
  rd_regime: str
  vp_day_type: str
  lunch_active: bool
  expiry: datetime
```

### Layer 5 — Position Sizing

```
contracts = floor(
  base_contracts × final_strength × final_confidence × rd_size_mult
)
contracts = clamp(contracts, min=0, max=max_contracts_cap)
```

```yaml
position_sizing:
  base_contracts: 1                # [1, 2, 5]
  max_contracts_cap: 3             # [3, 5, 10]
  min_strength_to_trade: 0.30      # [0.25, 0.30, 0.40]
  min_confidence_to_trade: 0.50    # [0.40, 0.50, 0.60]
  account_risk_pct: 1.0            # [0.5%, 1.0%, 2.0%]
```

---

## 6. A/B Harness

### What Can Be Tested
| Level | Example test |
|---|---|
| Approach config param | CL1 `bucket_size_ticks`: 3 vs 5 |
| Category aggregation weight | CL3 approach weight: 0.25 vs 0.35 |
| Gating multiplier | `volatile_shock → CL`: 0.40 vs 0.20 |
| Combining config | CL base weight: 0.45 vs 0.55 |

### Test Protocol
1. Select: param, control value, variant value
2. Select: evaluation window (start_date → end_date)
3. Run both configs on identical historical data
4. Simulate trades from each signal stream (same entry/exit rules, same slippage)
5. Measure metrics for both
6. Run statistical significance tests
7. Store result; update baseline if significant

### Walk-Forward Structure
```
Train [───────────] Test [────]
      Train [───────────] Test [────]
            Train [───────────] Test [────]

Reported metric = average of all Test window results
```

```yaml
walk_forward:
  train_window_sessions: 90
  test_window_sessions: 20
  walk_step_sessions: 10
  min_walks: 5
  anchored_start: false
```

### Evaluation Metrics
| Metric | Primary use |
|---|---|
| Sharpe Ratio | Primary decision metric |
| Profit Factor | Robustness check |
| Win Rate | Signal quality |
| Max Drawdown | Survivability |
| Signal Count | Avoid over-filtering |
| Level Quality (CL only) | First-touch reaction rate |
| Regime Accuracy (RD only) | Does label improve other modules? |

### Statistical Significance
```yaml
significance:
  significance_threshold: 0.05    # p-value cutoff (t-test on session returns)
  robustness_threshold: 0.60      # variant must win in 60%+ of test windows
  min_improvement_pct: 2.0        # must improve primary metric by ≥2%
```

### Config Versioning
```
configs/
  baseline_v001.yaml
  baseline_v002.yaml
  ab_results/
    test_{date}_{param}.json
```

Each result file:
```json
{
  "param": "CL1.bucket_size_ticks",
  "control": 3, "variant": 5,
  "control_sharpe": 0.81, "variant_sharpe": 0.94,
  "p_value": 0.031, "robustness": 0.72,
  "accepted": true, "new_baseline": "baseline_v003.yaml"
}
```

### Automated Test Queue
```yaml
ab_harness:
  scheduling: sequential
  auto_generate_tests: true
  max_tests_per_day: 10
  primary_metric: sharpe_ratio
  param_sweep_values:
    CL1.bucket_size_ticks: [1, 3, 5]
    CL1.lookback_days: [1, 3, 5, 10]
    CL2.bounce_threshold: [0.6, 0.7, 0.8]
    # ... all params enumerated in full config
```

---

## 7. Implementation Sequence

### Phases

| Phase | Builds | First real output | Depends on |
|---|---|---|---|
| **0** | Data pipeline | Clean processed data | — |
| **1** | CL-1 | Level file + chart | Phase 0 |
| **2** | CL-2 + A/B harness | First comparative test | Phase 1 |
| **3** | VP-1, VP-2 | Day type label | Phase 0 |
| **4** | RD-1, RD-4 | Regime label per bar | Phase 0 |
| **5** | Combining layer | **First P&L number** | Phases 1–4 |
| **6** | TOD-1, TOD-2 | Time-filtered signals | Phase 0 + history |
| **7** | CL-3, CL-4, CL-5 | Full CL suite | Phase 0e (direction) |
| **8** | Remaining VP/RD/TOD | 20 of 25 approaches | Phases 3–6 |
| **9** | CORR | All 25 approaches | Multi-symbol data |
| **10** | Live trading | Real orders | Phase 5+ |

**MVP = Phase 5.** First moment the system produces a judgeable number.

### Phase 0 Detail (Foundation)
- 0a: Raw trades.csv fetcher + scheduler
- 0b: Validator (schema, dedup, outlier, gap)
- 0c: Volume profile builder (1/3/5 tick)
- 0d: Bar resampler (1/5/15 min)
- 0e: Trade direction classifier (tick rule)

**Done when:** 10 past sessions process cleanly. Output files match expected formats.

### Phase 2 A/B Test Target
First A/B test: CL-1 `bucket_size_ticks` 1 vs 3 vs 5.
Validates the harness produces non-random, consistent results before expanding.

### Phase 5 Success Criteria
- Backtest on 60 sessions
- Sharpe > 0
- Profit factor > 1.0
- Signal count > 2/day average

### CORR Activation Trigger
CORR module auto-enables when:
- ≥2 symbols each have ≥20 days of processed data
- Cointegration test passes for at least one pair

---

## 8. Config Reference

### Master Config Structure
```yaml
system:
  instrument: MES                  # MES → ES at live phase
  session: rth_only
  enabled_categories: [CL, VP, RD, TOD]   # CORR added when data available

data_pipeline:
  # see Section 3.6

CL1: # ... see Section 4.1
CL2: # ...
CL3: # ...
CL4: # ...
CL5: # ...

CORR:
  enabled: false
CORR1: # ...
CORR2: # ...
CORR3: # ...
CORR4: # ...
CORR5: # ...

VP1: # ... see Section 4.3
VP2: # ...
VP3: # ...
VP4: # ...
VP5: # ...

RD1: # ... see Section 4.4
RD2: # ...
RD3: # ...
RD4: # ...
RD5: # ...
RD_composite: # ...

TOD1: # ... see Section 4.5
TOD2: # ...
TOD3: # ...
TOD4: # ...
TOD5: # ...

CL_aggregation:   # see Section 5
CORR_aggregation: # ...
VP_aggregation:   # ...
RD_aggregation:   # ...
TOD_aggregation:  # ...

rd_gate_multipliers:  # see Section 5 Layer 3
vp_gate_multipliers:  # ...

final_aggregation:    # see Section 5 Layer 4
position_sizing:      # see Section 5 Layer 5

ab_harness:           # see Section 6
walk_forward:         # ...
significance:         # ...
```

---

*End of design document. Version 1.0 — 2026-04-26.*
