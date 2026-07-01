# Galgo July 2026 — Implementation Plan

**Revised:** 2026-07-01 (v2 — research + code audit incorporated)
**Status:** Approved for implementation

---

## Architecture Decisions (locked)

1. **Simulation hook:** `simulator.simulate_exit()` — existing, tested, perfect for Cartesian product. Feed real IB fill, vary TP/SL/params across the matrix. No re-entry simulation needed.
2. **Verified trades source:** SQL VIEW `verified_trades` in `june/lib/db.py` over `completed_trades` — strict validation already built in. Keep as-is.
3. **Multiplier:** symbol-aware (MES=5.0, MNQ=2.0, MYM=0.5, M2K=10.0) — add to `config.yaml`.
4. **DB:** Extend `june/back-trading/bt_db.py` with 4 new tables in `data/bt.db`.
5. **Scoring:** 12 metrics + 4 anti-overfitting guards. Weights in `config.yaml`.
6. **LOOCV:** Used instead of walk-forward (< 100 trades). Switch to walk-forward when N >= 100.
7. **Email:** `send_email.py` at project root — called after every step test passes.
8. **Rollback:** Git tag before each step. Failed step = `git checkout <tag>`.
9. **Test rule:** Test must PASS before writing code for the next step. One code-review-and-improve cycle per step.

---

## Rollback Tags

| Tag | After |
|---|---|
| `bt-v0-baseline` | Before any changes |
| `bt-v1-db` | Step 1: DB schema |
| `bt-v2-params` | Step 2: Param sets |
| `bt-v3-runner` | Step 3: Matrix runner |
| `bt-v4-scorer` | Step 4: Scoring engine |
| `bt-v5-fetch` | Step 5: Fetch priority |
| `bt-v6-resume` | Step 6: Resume partial files |
| `bt-v7-dash` | Step 7: Dashboard panel |

---

## Step 0 — Baseline Commit

Tag the current state before any changes.

```bash
git add -A
git commit -m "bt: baseline before Cartesian backtrader"
git tag bt-v0-baseline
```

**Test:** `git tag | grep bt-v0` returns the tag.
**Email:** "Step 0 done — baseline tagged bt-v0-baseline."

---

## Step 1 — DB Schema: 4 New Tables

**File:** `june/back-trading/bt_db.py`

Add to `_DDL` string:

### bt_param_sets — Cartesian product cells
```sql
CREATE TABLE IF NOT EXISTS bt_param_sets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tp_ticks        INTEGER NOT NULL,
    sl_ticks        INTEGER NOT NULL,
    entry_delay_s   INTEGER NOT NULL DEFAULT 0,
    entry_offset_t  INTEGER NOT NULL DEFAULT 0,
    tp_confirm_t    INTEGER NOT NULL DEFAULT 2,
    session_window  TEXT    NOT NULL DEFAULT 'ALL',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(tp_ticks, sl_ticks, entry_delay_s, entry_offset_t, tp_confirm_t, session_window)
);
```

### bt_matrix_results — one row per (trade x param_set)
```sql
CREATE TABLE IF NOT EXISTS bt_matrix_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL,
    param_set_id    INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    exit_reason     TEXT    NOT NULL,
    pnl_ticks       REAL,
    ticks_to_exit   INTEGER,
    ms_to_exit      INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(trade_id, param_set_id)
);
CREATE INDEX IF NOT EXISTS idx_mx_param ON bt_matrix_results(param_set_id);
CREATE INDEX IF NOT EXISTS idx_mx_trade ON bt_matrix_results(trade_id);
```

### bt_scores — current aggregate score per param_set
```sql
CREATE TABLE IF NOT EXISTS bt_scores (
    param_set_id    INTEGER PRIMARY KEY,
    n_trades        INTEGER NOT NULL,
    win_rate        REAL,
    profit_factor   REAL,
    expectancy      REAL,
    sharpe          REAL,
    sortino         REAL,
    calmar          REAL,
    max_drawdown_t  REAL,
    avg_win_loss    REAL,
    sqn             REAL,
    fill_rate       REAL,
    max_consec_loss INTEGER,
    mc_pvalue       REAL,
    composite_score REAL,
    loocv_score     REAL,
    stability_zone  REAL,
    status          TEXT NOT NULL DEFAULT 'ok',
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

### bt_score_history — daily snapshots for trend tracking
```sql
CREATE TABLE IF NOT EXISTS bt_score_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT    NOT NULL,
    param_set_id    INTEGER NOT NULL,
    rank            INTEGER,
    composite_score REAL,
    n_trades        INTEGER,
    win_rate        REAL,
    expectancy      REAL,
    sqn             REAL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(snapshot_date, param_set_id)
);
```

**Test:**
```bash
python -c "
import sys; sys.path.insert(0,'C:/Projects/Galgo2026/june')
from pathlib import Path
from back_trading.bt_db import init_bt_db
db = init_bt_db(Path('C:/Projects/Galgo2026/june/trader/data/bt.db'))
tables = {r[0] for r in db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
required = {'bt_param_sets','bt_matrix_results','bt_scores','bt_score_history'}
missing = required - tables
assert not missing, f'Missing: {missing}'
db.close()
print('PASS')
"
```

**Code review:** Check DDL for index coverage, WAL mode, correct NOT NULL constraints.
**Tag:** `bt-v1-db`
**Email:** "Step 1 PASS — 4 new tables installed in bt.db."

---

## Step 2 — Parameter Set Generator

**New file:** `june/back-trading/bt_params.py`

**Cartesian axes (Phase 1):**
```python
AXES = {
    "tp_ticks":       [2, 4, 6, 8, 10, 12],
    "sl_ticks":       [2, 4, 6, 8, 10, 12],
    "entry_delay_s":  [0, 5, 15, 30, 60],
    "entry_offset_t": [-2, -1, 0, 1, 2],
    "tp_confirm_t":   [1, 2, 3],
    "session_window": ["ALL", "MORNING", "MIDDAY", "AFTERNOON"],
}
# Total: 6 x 6 x 5 x 5 x 3 x 4 = 10,800
```

**Session window time boundaries (CT):**
- ALL: 08:30–15:15
- MORNING: 08:30–11:00
- MIDDAY: 11:00–13:30
- AFTERNOON: 13:30–15:15

**Public API:**
```python
def generate_param_sets() -> list[dict]       # all 10,800 dicts
def seed_param_sets(conn) -> int               # insert into bt_param_sets, returns count inserted
def get_active_param_sets(conn) -> list        # read back all rows
def get_neighbors(conn, param_set_id) -> list  # 8 nearest for stability zone
```

**Self-test:**
```bash
python june/back-trading/bt_params.py --self-test
```
Must print: `PASS — 10800 param sets, all inserted, neighbors tested`

**Code review:** Verify neighbor function correctly returns ±1 on each axis (not diagonal-only).
**Tag:** `bt-v2-params`
**Email:** "Step 2 PASS — 10,800 parameter combinations seeded."

---

## Step 3 — Matrix Runner

**New file:** `june/back-trading/bt_matrix_runner.py`

**Algorithm:**
```
for each verified_trade (from verified_trades VIEW):
    load tick CSV for (symbol, date) — cache in memory
    for each param_set in bt_param_sets:
        if (trade_id, param_set_id) in bt_matrix_results: skip
        apply session_window filter to trades_df
        adjusted_fill_time = fill_time + timedelta(seconds=entry_delay_s)
        adjusted_tp = fill_price + tp_ticks*tick if BUY else fill_price - tp_ticks*tick
        adjusted_sl = fill_price - sl_ticks*tick if BUY else fill_price + sl_ticks*tick
        result = simulator.simulate_exit(
            fill_price, adjusted_fill_time, adjusted_tp, adjusted_sl,
            direction, trades_df_filtered, session_end,
            tp_confirm_ticks=param_set["tp_confirm_t"]
        )
        write to bt_matrix_results
```

**Performance:** Load each CSV once, run all 10,800 param_sets against it.
Target: < 60 seconds for 10 trades x 10,800 combinations.

**CLI:**
```bash
python june/back-trading/bt_matrix_runner.py           # run all pending
python june/back-trading/bt_matrix_runner.py --dry-run # count only
python june/back-trading/bt_matrix_runner.py --self-test
```

**Self-test:** Insert 3 synthetic trades in completed_trades, run runner, verify:
- 3 x 10,800 = 32,400 rows in bt_matrix_results
- UNIQUE constraint prevents duplicates on re-run
- Timing < 30s

**Code review:** Check CSV caching doesn't leak memory; UNIQUE insert uses INSERT OR IGNORE.
**Tag:** `bt-v3-runner`
**Email:** "Step 3 PASS — Matrix runner: 3 synthetic trades x 10,800 param sets = 32,400 results in Xs."

---

## Step 4 — Scoring Engine

**New file:** `june/back-trading/bt_scorer.py`

**Per-param_set computation:**
```python
def score_param_set(param_set_id, results: list[dict]) -> dict:
    pnl = [r["pnl_ticks"] for r in results if r["exit_reason"] != "EXPIRED"]
    n = len(pnl)
    if n < 5:
        return {"status": "insufficient_data", ...}

    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]

    metrics = {
        "win_rate":       len(wins) / n,
        "profit_factor":  sum(wins) / abs(sum(losses)) if losses else float('inf'),
        "expectancy":     mean(pnl),
        "sharpe":         mean(pnl) / std(pnl) * sqrt(n) if std(pnl) > 0 else 0,
        "sortino":        mean(pnl) / std(losses) * sqrt(n) if losses and std(losses) > 0 else 0,
        "calmar":         sum(pnl) / max_drawdown(pnl) if max_drawdown(pnl) > 0 else 0,
        "max_drawdown_t": max_drawdown(pnl),
        "avg_win_loss":   mean(wins) / abs(mean(losses)) if losses else float('inf'),
        "sqn":            mean(pnl) / std(pnl) * sqrt(n) if std(pnl) > 0 else 0,
        "fill_rate":      n / total_trades,
        "max_consec_loss": max_consecutive_losses(pnl),
        "mc_pvalue":      monte_carlo_pvalue(pnl, n_permutations=1000),
    }
    # status: ok | insufficient_data | low_confidence (mc_pvalue > 0.10)
```

**Monte Carlo Permutation Test:**
```python
def monte_carlo_pvalue(pnl: list, n_permutations: int = 1000) -> float:
    real_sharpe = mean(pnl) / std(pnl) * sqrt(len(pnl))
    shuffled_sharpes = [mean(shuffle(pnl)) / std(pnl) * sqrt(len(pnl))
                        for _ in range(n_permutations)]
    return sum(s >= real_sharpe for s in shuffled_sharpes) / n_permutations
```

**Stability Zone (after scoring all param_sets):**
```python
def compute_stability_zone(conn, param_set_id: int) -> float:
    neighbors = get_neighbors(conn, param_set_id)
    center_score = get_composite_score(conn, param_set_id)
    neighbor_scores = [get_composite_score(conn, nb) for nb in neighbors]
    return mean(neighbor_scores) / center_score if center_score > 0 else 0
    # >= 0.70 = stable; < 0.70 = spiky (reject)
```

**LOOCV:**
```python
def loocv_score(param_set_id, all_results: list) -> float:
    scores = []
    for i in range(len(all_results)):
        train = all_results[:i] + all_results[i+1:]
        test  = [all_results[i]]
        train_score = score_param_set(param_set_id, train)["composite_score"]
        test_score  = score_param_set(param_set_id, test)["composite_score"]
        scores.append(test_score / train_score if train_score > 0 else 0)
    return mean(scores)
```

**Composite score:**
```python
# Weights from config.yaml
normalized_metrics = min_max_normalize_all(metrics, across_all_param_sets)
composite = sum(normalized_metrics[m] * WEIGHTS[m] for m in WEIGHTS)
```

**CLI:**
```bash
python june/back-trading/bt_scorer.py --run        # score all param_sets
python june/back-trading/bt_scorer.py --top 20     # print top 20
python june/back-trading/bt_scorer.py --snapshot   # write bt_score_history for today
python june/back-trading/bt_scorer.py --self-test
```

**Self-test (using Step 3's synthetic data):**
- Verify top combo has profit_factor > 1.0
- Verify insufficient_data correctly flagged for < 20 trades
- Verify bt_score_history has today's snapshot
- Verify stability zone computed (not null) for top 10

**Code review:** Check normalization handles edge cases (all-same score, inf profit_factor).
Monte Carlo must use a fixed random seed in test mode for reproducibility.

**Tag:** `bt-v4-scorer`
**Email:** "Step 4 PASS — Scorer: top param set TP=X SL=Y score=Z SQN=W. N param sets with sufficient data."

---

## Step 5 — Fetch Priority by Verified Trades

**File:** `june/trader/fetch_scheduler.py`

**Change 1 — priority sort by verified trade count:**
```python
def _get_priority_dates(cfg, symbols):
    # Query: count verified trades per (symbol, date), sort DESC
    # Any (symbol, date) with count > 0 AND missing CSV = top priority
    # Remaining dates by recency
```

**Change 2 — dynamic re-priority between files:**
```python
# OLD static loop:
for sym, target_day in pairs:
    fetch_day(...)

# NEW dynamic loop:
while True:
    pairs = _get_priority_dates(cfg, symbols)
    if not pairs:
        break
    sym, target_day = pairs[0]   # always the current best
    fetch_day(ib, sym, target_day, ...)
    # after each file, re-evaluate priority
```

**Change 3 — trigger matrix runner after file completes:**
```python
# After fetch_day() succeeds:
subprocess.Popen([sys.executable, str(runner_path), "--incremental", sym, date_str])
```

**Self-test:**
- Insert 2 synthetic verified_trades for MES 2026-06-27
- Call `_get_priority_dates()` — MES 2026-06-27 must rank #1
- Verify loop re-prioritizes after each file

**Tag:** `bt-v5-fetch`
**Email:** "Step 5 PASS — fetch priority now driven by verified trade count. Dynamic re-priority confirmed."

---

## Step 6 — Resume Partial CSV Files on Restart

**File:** `june/trader/fetcher.py`

**New function:**
```python
def _get_csv_last_ts(path: Path) -> datetime | None:
    """Read last non-empty line of CSV, return its UTC timestamp. O(1) seek."""
    if not path.exists() or path.stat().st_size < 200:
        return None
    with open(path, 'rb') as f:
        f.seek(0, 2)
        pos, buf = f.tell(), b''
        while pos > 0 and b'\n' not in buf[1:]:
            pos = max(0, pos - 512)
            f.seek(pos)
            buf = f.read()
    last_line = buf.split(b'\n')[-2].decode('utf-8', errors='ignore')
    ts_str = last_line.split(',')[0].strip()
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None
```

**Fetch logic change:**
```python
def fetch_day(ib, symbol, date, ...):
    csv_path = output_dir / f"{symbol}_trades_{date}.csv"
    last_ts = _get_csv_last_ts(csv_path) if not already_finished else None
    if last_ts:
        log.info("Resuming %s from %s", csv_path.name, last_ts)
        _resume_from(ib, symbol, date, last_ts, csv_path, ...)
    else:
        _mark_started(...)
        _fetch_full(ib, symbol, date, csv_path, ...)
```

**Self-test:**
- Create synthetic partial CSV (100 rows ending at 10:00 CT)
- Run `fetcher.py --resume-test`
- Verify: fetcher starts at 10:00, appends rows, does not delete existing rows

**Code review:** Verify file seek handles empty CSVs and header-only CSVs correctly.
**Tag:** `bt-v6-resume`
**Email:** "Step 6 PASS — fetcher resumes partial files from last timestamp."

---

## Step 7 — Dashboard: Backtest Score Panel

**Files:** `trader/visualizer/app.py`, `trader/visualizer/templates/dashboard.html`

**New API endpoint:**
```python
@app.route("/api/bt-scores")
def api_bt_scores():
    bt_db = Path(__file__).parent.parent.parent / "june/trader/data/bt.db"
    if not bt_db.exists():
        return jsonify({"scores": [], "updated_at": None})
    con = sqlite3.connect(str(bt_db))
    rows = con.execute("""
        SELECT s.param_set_id, p.tp_ticks, p.sl_ticks, p.entry_delay_s,
               p.session_window, s.composite_score, s.win_rate, s.expectancy,
               s.sqn, s.n_trades, s.status, s.updated_at
        FROM bt_scores s JOIN bt_param_sets p ON p.id = s.param_set_id
        WHERE s.status = 'ok'
        ORDER BY s.composite_score DESC LIMIT 10
    """).fetchall()
    con.close()
    return jsonify({"scores": [dict(r) for r in rows]})
```

**Dashboard panel (below Fetch Progress panel):**
- Title: "Backtest Scores — Top 10"
- Table: rank | TP | SL | delay | window | score | win% | EV(t) | SQN | N
- Color: green (score > 0.60), orange (0.40-0.60), red (< 0.40)
- "Last updated: N min ago"
- Refreshes every 60 seconds

**Self-test:**
- Seed 5 synthetic rows in bt_scores
- GET `/api/bt-scores` returns 5 entries as JSON
- Dashboard renders without breaking existing layout

**Tag:** `bt-v7-dash`
**Email:** "Step 7 PASS — dashboard score panel live at localhost:5001."

---

## Step 8 — Bootstrap: Import Verified Trades from Drive (parallel to Steps 1-7)

1. Check Google Drive for `galao.db` backups from old PC
2. If found: copy `completed_trades` rows into this PC's `galao.db`
3. Each trade re-runs through the `verified_trades` VIEW filters — discards any that fail
4. After import: trigger matrix runner on all verified trades with matching tick CSVs

If no Drive backups: wait for broker to generate new fills via paper sessions.

---

## Step 9 — Daily Improvement Loop

**Runs automatically after scorer update:**
1. Write today's snapshot to `bt_score_history`
2. Compare with yesterday's top 10 — flag gainers and losers
3. LOOCV: recompute LOOCV scores for top 50 combinations
4. Monte Carlo: re-run for combinations where n_trades increased since last run
5. Generate daily report email: top 3 stable combinations, score trends, data quality

---

## File Map

| File | Action | Step |
|---|---|---|
| `june/back-trading/bt_db.py` | EXTEND — 4 new tables | 1 |
| `june/back-trading/bt_params.py` | CREATE — axes + seeder + neighbors | 2 |
| `june/back-trading/bt_matrix_runner.py` | CREATE — main Cartesian loop | 3 |
| `june/back-trading/bt_scorer.py` | CREATE — 12 metrics + 4 guards | 4 |
| `june/trader/fetch_scheduler.py` | MODIFY — dynamic priority + trigger runner | 5 |
| `june/trader/fetcher.py` | MODIFY — resume partial CSVs | 6 |
| `trader/visualizer/app.py` | MODIFY — /api/bt-scores | 7 |
| `trader/visualizer/templates/dashboard.html` | MODIFY — score panel | 7 |
| `june/trader/config.yaml` | MODIFY — score_weights, multipliers | 1+4 |

**NOT changing:**
- `june/back-trading/simulator.py` (simulate_exit is our hook — keep as-is)
- `june/back-trading/bt_fetcher.py`
- `june/lib/db.py` (verified_trades VIEW already correct)

---

## Full Test Sequence

```bash
# Regression (must always pass):
python june/back-trading/simulator.py --self-test

# Step-by-step:
python -c "from back_trading.bt_db import init_bt_db ..."   # Step 1
python june/back-trading/bt_params.py --self-test            # Step 2
python june/back-trading/bt_matrix_runner.py --self-test     # Step 3
python june/back-trading/bt_scorer.py --self-test            # Step 4
python june/trader/fetch_scheduler.py --self-test            # Step 5
python june/trader/fetcher.py --resume-test                  # Step 6
curl http://localhost:5001/api/bt-scores | python -m json.tool  # Step 7
```

---

## Time Estimate

| Step | Hours |
|---|---|
| 0 Baseline | 0.1 |
| 1 DB schema | 0.5 |
| 2 Param generator | 1.0 |
| 3 Matrix runner | 3.0 |
| 4 Scoring engine | 4.0 |
| 5 Fetch priority | 1.5 |
| 6 Resume partial | 2.0 |
| 7 Dashboard panel | 1.5 |
| **Total** | ~13.5 hours |
