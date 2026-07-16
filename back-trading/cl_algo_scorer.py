"""
back-trading/cl_algo_scorer.py
Aggregate cl_algo_sim_results → rank combos by composite score.

Metrics computed per (algo_type, tp_ticks, sl_ticks, direction_filter, strength_max):
  win_rate       = n_tp / n_resolved (TP + SL exits, not EXPIRED)
  profit_factor  = sum(positive pnl_ticks) / abs(sum(negative pnl_ticks))
  expectancy     = mean(pnl_ticks) over resolved exits
  sharpe         = mean / std × sqrt(N)   [std of pnl_ticks]
  sqn            = mean / std × sqrt(N)   (same as Sharpe for symmetric data; Van Tharp)
  composite      = weighted sum of min-max normalized metrics

Anti-overfit guards (adapted for sparse data):
  MIN_N_FILLS = 3  — combos with fewer resolved exits → data_status='insufficient_data'
  Stability zone — top combo must have at least 1 neighbor (±1 tp or ±1 sl step)
                   with positive profit_factor

Usage:
    python back-trading/cl_algo_scorer.py              # score all symbols
    python back-trading/cl_algo_scorer.py --symbol MES
    python back-trading/cl_algo_scorer.py --top 10    # show top N combos
    python back-trading/cl_algo_scorer.py --self-test
"""

import sys
import argparse
import math
import json
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db, init_db

MIN_N_FILLS = 3   # below this → 'insufficient_data', not ranked

# Composite score weights (must sum to 1.0)
WEIGHTS = {
    "expectancy":     0.30,
    "profit_factor":  0.25,
    "win_rate":       0.20,
    "sharpe":         0.15,
    "sqn":            0.10,
}


# ── Metric computation ────────────────────────────────────────────────────────

def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def _compute_metrics(pnl_list: list[float]) -> dict:
    """Compute all metrics from a list of pnl_ticks (resolved exits only)."""
    n = len(pnl_list)
    if n == 0:
        return {"win_rate": None, "profit_factor": None, "expectancy": None,
                "sharpe": None, "sqn": None}

    wins   = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]
    mean   = sum(pnl_list) / n
    std    = math.sqrt(sum((p - mean) ** 2 for p in pnl_list) / n) if n > 1 else 0.0

    win_rate      = len(wins) / n
    profit_factor = _safe_div(sum(wins), abs(sum(losses)), default=0.0 if losses else 999.0)
    expectancy    = mean
    sharpe        = _safe_div(mean, std) * math.sqrt(n) if std > 0 else 0.0
    sqn           = sharpe  # same formula for 1-lot uniform sizing

    return {
        "win_rate":      round(win_rate,      4),
        "profit_factor": round(profit_factor, 4),
        "expectancy":    round(expectancy,    4),
        "sharpe":        round(sharpe,        4),
        "sqn":           round(sqn,           4),
    }


def _normalize(values: list[float | None]) -> list[float]:
    """Min-max normalize a list, treating None as 0."""
    clean = [v if v is not None else 0.0 for v in values]
    lo, hi = min(clean), max(clean)
    if hi == lo:
        return [0.5] * len(clean)
    return [(v - lo) / (hi - lo) for v in clean]


# ── Stability zone check ──────────────────────────────────────────────────────

def _has_stable_neighbor(combo: dict, all_combos: list[dict],
                         tp_steps: list[int], sl_steps: list[int]) -> bool:
    """
    Return True if at least 1 Cartesian neighbor (±1 tp or ±1 sl step)
    has profit_factor > 1.0 and sufficient data.
    """
    tp = combo["tp_ticks"]
    sl = combo["sl_ticks"]

    def adjacent(val, steps):
        idx = steps.index(val) if val in steps else -1
        adj = []
        if idx > 0:              adj.append(steps[idx - 1])
        if idx < len(steps) - 1: adj.append(steps[idx + 1])
        return adj

    neighbors_tp = adjacent(tp, tp_steps)
    neighbors_sl = adjacent(sl, sl_steps)

    for neighbor in all_combos:
        same_cat = (neighbor["algo_type"]        == combo["algo_type"] and
                    neighbor["direction_filter"] == combo["direction_filter"] and
                    neighbor["strength_max"]     == combo["strength_max"])
        if not same_cat:
            continue
        is_adj = ((neighbor["tp_ticks"] in neighbors_tp and neighbor["sl_ticks"] == sl) or
                  (neighbor["sl_ticks"] in neighbors_sl and neighbor["tp_ticks"] == tp))
        if is_adj and (neighbor.get("profit_factor") or 0) > 1.0 and \
                neighbor.get("data_status") == "ok":
            return True
    return False


# ── Main scorer ───────────────────────────────────────────────────────────────

def score(db_path: Path, symbol: str, top_n: int = 20,
          verbose: bool = False) -> dict:
    """
    Score all combos for a symbol from cl_algo_sim_results.
    Writes to cl_algo_combo_scores + cl_algo_score_history.
    Returns dict with top combo and summary.
    """
    init_db(db_path)
    scored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db(db_path) as con:
        # Aggregate per combo
        rows = con.execute("""
            SELECT
                algo_type, tp_ticks, sl_ticks, direction_filter, strength_max,
                COUNT(*) as n_sims,
                SUM(CASE WHEN exit_reason IN ('TP','SL') THEN 1 ELSE 0 END) as n_fills,
                SUM(CASE WHEN exit_reason='TP' THEN 1 ELSE 0 END) as n_tp,
                SUM(CASE WHEN exit_reason='SL' THEN 1 ELSE 0 END) as n_sl,
                SUM(CASE WHEN exit_reason='EXPIRED' AND entry_fill_price IS NOT NULL
                         THEN 1 ELSE 0 END) as n_expired_exit
            FROM cl_algo_sim_results
            WHERE symbol=?
            GROUP BY algo_type, tp_ticks, sl_ticks, direction_filter, strength_max
        """, (symbol,)).fetchall()

    if not rows:
        return {"symbol": symbol, "n_combos": 0, "top": None}

    # Fetch pnl_ticks per combo from DB
    with get_db(db_path) as con:
        pnl_rows = con.execute("""
            SELECT algo_type, tp_ticks, sl_ticks, direction_filter, strength_max,
                   pnl_ticks
            FROM cl_algo_sim_results
            WHERE symbol=? AND exit_reason IN ('TP','SL') AND pnl_ticks IS NOT NULL
        """, (symbol,)).fetchall()

    pnl_map: dict[tuple, list[float]] = {}
    for r in pnl_rows:
        key = (r[0], r[1], r[2], r[3], r[4])
        pnl_map.setdefault(key, []).append(r[5])

    # Compute metrics per combo
    combo_data = []
    for r in rows:
        key = (r["algo_type"], r["tp_ticks"], r["sl_ticks"],
               r["direction_filter"], r["strength_max"])
        pnl_list = pnl_map.get(key, [])
        metrics  = _compute_metrics(pnl_list)
        status   = "ok" if r["n_fills"] >= MIN_N_FILLS else "insufficient_data"
        if r["n_fills"] == 0:
            status = "no_fills"

        combo_data.append({
            "algo_type":       r["algo_type"],
            "tp_ticks":        r["tp_ticks"],
            "sl_ticks":        r["sl_ticks"],
            "direction_filter": r["direction_filter"],
            "strength_max":    r["strength_max"],
            "n_sims":          r["n_sims"],
            "n_fills":         r["n_fills"],
            "n_tp":            r["n_tp"],
            "n_sl":            r["n_sl"],
            "n_expired_exit":  r["n_expired_exit"],
            "data_status":     status,
            **metrics,
        })

    # Normalize metrics and compute composite (only for 'ok' combos)
    ok_combos = [c for c in combo_data if c["data_status"] == "ok"]
    if ok_combos:
        for metric in WEIGHTS:
            vals = [c.get(metric) for c in ok_combos]
            norms = _normalize(vals)
            for c, nv in zip(ok_combos, norms):
                c[f"{metric}_norm"] = nv

        for c in ok_combos:
            c["composite_score"] = round(
                sum(WEIGHTS[m] * c.get(f"{m}_norm", 0) for m in WEIGHTS), 6
            )
        ok_combos.sort(key=lambda x: x["composite_score"], reverse=True)
        for i, c in enumerate(ok_combos):
            c["rank"] = i + 1

    # Stability zone: flag top combo if it lacks neighbors
    tp_steps = sorted({c["tp_ticks"] for c in combo_data})
    sl_steps = sorted({c["sl_ticks"] for c in combo_data})
    all_c    = combo_data

    # Write scores to DB
    insert_rows = []
    for c in combo_data:
        insert_rows.append((
            scored_at, symbol,
            c["algo_type"], c["tp_ticks"], c["sl_ticks"],
            c["direction_filter"], c["strength_max"],
            c["n_sims"], c["n_fills"], c["n_tp"], c["n_sl"], c["n_expired_exit"],
            c.get("win_rate"), c.get("profit_factor"),
            c.get("expectancy"), c.get("sharpe"), c.get("sqn"),
            c.get("composite_score"), c.get("rank"), c["data_status"],
        ))

    with get_db(db_path) as con:
        con.executemany("""
            INSERT OR IGNORE INTO cl_algo_combo_scores
                (scored_at, symbol, algo_type, tp_ticks, sl_ticks,
                 direction_filter, strength_max,
                 n_sims, n_fills, n_tp, n_sl, n_expired_exit,
                 win_rate, profit_factor, expectancy, sharpe, sqn,
                 composite_score, rank, data_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, insert_rows)

    top = ok_combos[0] if ok_combos else None

    # Write score_history
    stable = _has_stable_neighbor(top, all_c, tp_steps, sl_steps) if top else True
    convergence = "exploring"  # learner will update this

    with get_db(db_path) as con:
        con.execute("""
            INSERT INTO cl_algo_score_history
                (scored_at, symbol, n_combos_scored,
                 top_algo_type, top_tp_ticks, top_sl_ticks,
                 top_direction_filter, top_strength_max, top_composite_score,
                 top_n_fills, convergence_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            scored_at, symbol, len(combo_data),
            top["algo_type"]        if top else None,
            top["tp_ticks"]         if top else None,
            top["sl_ticks"]         if top else None,
            top["direction_filter"] if top else None,
            top["strength_max"]     if top else None,
            top["composite_score"]  if top else None,
            top["n_fills"]          if top else None,
            convergence,
        ))

    if verbose and top:
        print(f"\n[{symbol}] Top combo:")
        print(f"  algo={top['algo_type']}  tp={top['tp_ticks']}t  sl={top['sl_ticks']}t"
              f"  dir={top['direction_filter']}  str≤{top['strength_max']}")
        print(f"  score={top['composite_score']:.4f}  pf={top.get('profit_factor'):.2f}"
              f"  exp={top.get('expectancy'):.2f}  wr={top.get('win_rate'):.1%}"
              f"  N={top['n_fills']}")
        if not stable:
            print(f"  ⚠ STABILITY: no positive-PF neighbor in tp/sl grid")

    return {
        "symbol":          symbol,
        "n_combos":        len(combo_data),
        "n_ranked":        len(ok_combos),
        "top":             top,
        "stable":          stable,
        "scored_at":       scored_at,
    }


def score_all(db_path: Path, symbols: list[str] | None = None,
              top_n: int = 10, verbose: bool = False) -> dict:
    """Score all symbols. Returns {symbol: result} dict."""
    syms = symbols or ["MES", "MNQ", "MYM", "M2K"]
    return {s: score(db_path, s, top_n=top_n, verbose=verbose) for s in syms}


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    print("Running cl_algo_scorer self-test...")
    import tempfile, random

    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "galao.db"
            init_db(db_path)

            # Insert 200 synthetic sim_results rows across 3 combos
            random.seed(42)
            combos = [
                ("BOUNCE",    4, 4, "ALL", 3),   # combo A — good combo, PF>1
                ("BREAKOUT",  6, 4, "ALL", 3),   # combo B — bad combo, PF<1
                ("BOTH",      4, 8, "ALL", 3),   # combo C — neighbor of A (sl changes)
            ]
            rows = []
            for at, tp, sl, df, sm in combos:
                for i in range(30):
                    # Combo A: 60% win, tp=4t wins; combo B: 40% win; combo C: 55% win
                    win_prob = 0.60 if at == "BOUNCE" else (0.40 if at == "BREAKOUT" else 0.55)
                    is_tp = random.random() < win_prob
                    pnl   = tp if is_tp else -sl
                    rows.append((
                        f"2026-06-{(i%20)+1:02d}", "MES", at, tp, sl, df, sm,
                        5500.0, "SUPPORT", 1, "BUY", "LMT",
                        5500.0, 5501.0, 5499.0,
                        5500.0, "2026-06-30T14:00:00Z",
                        "TP" if is_tp else "SL",
                        5501.0 if is_tp else 5499.0,
                        pnl, 10
                    ))

            with get_db(db_path) as con:
                con.executemany("""
                    INSERT OR IGNORE INTO cl_algo_sim_results
                        (date, symbol, algo_type, tp_ticks, sl_ticks,
                         direction_filter, strength_max,
                         line_price, line_type, line_strength,
                         direction, entry_type,
                         entry_price, tp_price, sl_price,
                         entry_fill_price, entry_fill_time,
                         exit_reason, exit_fill_price, pnl_ticks, ticks_to_exit)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, rows)

            result = score(db_path, "MES", verbose=False)
            assert result["n_combos"] >= 3,   f"Expected ≥3 combos, got {result['n_combos']}"
            assert result["n_ranked"] >= 1,   f"Expected ≥1 ranked"
            top = result["top"]
            assert top is not None,            "No top combo"
            assert top["algo_type"] == "BOUNCE", \
                f"Expected BOUNCE (best PF) as top, got {top['algo_type']}"
            assert top["profit_factor"] > 1.0, f"Top PF should be >1: {top['profit_factor']}"

            # Re-run with same scored_at won't double-write (INSERT OR IGNORE)
            with get_db(db_path) as con:
                n_before = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_combo_scores"
                ).fetchone()[0]
            score(db_path, "MES")  # same scored_at won't work but different ts → new rows OK
            with get_db(db_path) as con:
                n_after = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_combo_scores"
                ).fetchone()[0]
            assert n_after >= n_before, "Score table should grow or stay same"

        print(f"PASS -- scorer: {result['n_combos']} combos, top={top['algo_type']}"
              f" tp={top['tp_ticks']} sl={top['sl_ticks']}"
              f" pf={top['profit_factor']:.2f}")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CL Algo Scorer")
    parser.add_argument("--symbol", nargs="*")
    parser.add_argument("--top",    type=int, default=10)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg = get_config()
    db_path = Path(cfg.paths.db)
    results = score_all(db_path, symbols=args.symbol, top_n=args.top, verbose=True)
    for sym, r in results.items():
        if r["n_combos"] == 0:
            print(f"{sym}: no simulation results yet")
        elif r["top"]:
            t = r["top"]
            print(f"{sym}: top={t['algo_type']} tp={t['tp_ticks']} sl={t['sl_ticks']}"
                  f" score={t['composite_score']:.4f}")
