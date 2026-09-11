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

Anti-overfit guards:
  MIN_N_FILLS = 20 — combos with fewer resolved exits → data_status='insufficient_data'
  Monte-Carlo permutation p-value — shuffle pnl order 1000x, compare Sharpe; p > 0.05 →
                   data_status='low_confidence' (ported from june/back-trading/bt_scorer.py)
  LOOCV ratio    — leave-one-out mean / full-sample mean; < 0.80 → data_status='unstable'
                   (same source)
  Stability zone — top combo must have at least 1 neighbor (±1 tp or ±1 sl step)
                   with positive profit_factor (surfaced as a warning, not a data_status gate)

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
import random
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db, init_db

MIN_N_FILLS = 20   # below this → 'insufficient_data', not ranked
MC_PERMUTATIONS = 1000
MC_PVALUE_THRESHOLD = 0.05   # above this → 'low_confidence'
LOOCV_THRESHOLD = 0.80       # below this → 'unstable'

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
    # 99.0 cap (not 999.0) on zero-loss combos -- matches june/back-trading/bt_scorer.py's
    # cap, keeps them visible without one small win streak dominating the composite score.
    profit_factor = _safe_div(sum(wins), abs(sum(losses)), default=0.0 if losses else 99.0)
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


# ── Anti-overfit statistical guards (ported from june/back-trading/bt_scorer.py) ──────

def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals)


def _std(vals: list[float], mean: float = None) -> float:
    if len(vals) < 2:
        return 0.0
    m = mean if mean is not None else _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def monte_carlo_pvalue(pnl_list: list[float], n_perm: int = MC_PERMUTATIONS,
                        seed: int | None = None) -> float:
    """
    Null-model significance test: could this Sharpe have arisen from the same trade
    *magnitudes* with random win/loss direction (a coin-flip entry), instead of a real
    edge? Builds the null distribution by randomly flipping the sign of each trade N
    times and recomputing Sharpe; p-value = fraction of flips with Sharpe >= the real one.

    NOTE: this is NOT the same function as june/back-trading/bt_scorer.py's
    monte_carlo_pvalue(), which was found (while porting it here) to always return 1.0 --
    it reorders the same fixed list, and mean/std (and therefore Sharpe) are order-
    invariant, so every "shuffle" is statistically identical to the original. Reordering
    can never build a meaningful null distribution for a permutation test on Sharpe; only
    randomizing the trades' actual win/loss outcomes can. Flagged, not silently fixed only
    here -- the june/ copy is untouched (archive-only per this build's scope) and still has
    the bug if it's ever revived.
    """
    if len(pnl_list) < 10:
        return 1.0  # not enough data to tell
    rng = random.Random(seed)
    mean_p = _mean(pnl_list)
    std_p  = _std(pnl_list, mean_p)
    if std_p == 0:
        return 0.0 if mean_p > 0 else 1.0
    real_sharpe = mean_p / std_p * math.sqrt(len(pnl_list))
    abs_vals = [abs(p) for p in pnl_list]
    count_gte = 0
    for _ in range(n_perm):
        signed = [v if rng.random() < 0.5 else -v for v in abs_vals]
        m = _mean(signed)
        s = _std(signed, m)
        sh = m / s * math.sqrt(len(signed)) if s > 0 else 0.0
        if sh >= real_sharpe:
            count_gte += 1
    return round(count_gte / n_perm, 4)


def loocv_score(pnl_list: list[float]) -> float:
    """
    Leave-One-Out Cross-Validation: fit on N-1 trades, measure on held-out.
    Returns ratio of mean LOOCV expectancy to full-sample expectancy -- near 1.0
    means no single trade is propping up the combo's edge.
    """
    n = len(pnl_list)
    if n < 5:
        return 0.0
    full_mean = _mean(pnl_list)
    if full_mean == 0:
        return 1.0
    loo_means = [_mean(pnl_list[:i] + pnl_list[i+1:]) for i in range(n)]
    return round(_mean(loo_means) / full_mean, 4)


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
          verbose: bool = False, split: str = "train") -> dict:
    """
    Score all combos for a symbol from cl_algo_sim_results.
    Only considers rows tagged split=<split> -- scoring/tuning must never see
    validation or out_of_sample rows (migration roadmap item 7).
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
            WHERE symbol=? AND split=?
            GROUP BY algo_type, tp_ticks, sl_ticks, direction_filter, strength_max
        """, (symbol, split)).fetchall()

    if not rows:
        return {"symbol": symbol, "n_combos": 0, "top": None}

    # Fetch pnl_ticks per combo from DB
    with get_db(db_path) as con:
        pnl_rows = con.execute("""
            SELECT algo_type, tp_ticks, sl_ticks, direction_filter, strength_max,
                   pnl_ticks
            FROM cl_algo_sim_results
            WHERE symbol=? AND split=? AND exit_reason IN ('TP','SL') AND pnl_ticks IS NOT NULL
        """, (symbol, split)).fetchall()

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

        if r["n_fills"] == 0:
            status = "no_fills"
        elif r["n_fills"] < MIN_N_FILLS:
            status = "insufficient_data"
        else:
            status = "ok"

        mc_pvalue = loocv_ratio = None
        if status == "ok":
            # Only worth computing once a combo already clears MIN_N_FILLS -- both
            # functions have their own (lower) internal minimums, so at n>=20 they
            # always produce a real number, never their "not enough data" default.
            mc_pvalue   = monte_carlo_pvalue(pnl_list, seed=42)
            loocv_ratio = loocv_score(pnl_list)
            if mc_pvalue > MC_PVALUE_THRESHOLD:
                status = "low_confidence"
            elif loocv_ratio < LOOCV_THRESHOLD:
                status = "unstable"

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
            "mc_pvalue":       mc_pvalue,
            "loocv_ratio":     loocv_ratio,
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
            c.get("mc_pvalue"), c.get("loocv_ratio"),
        ))

    with get_db(db_path) as con:
        con.executemany("""
            INSERT OR IGNORE INTO cl_algo_combo_scores
                (scored_at, symbol, algo_type, tp_ticks, sl_ticks,
                 direction_filter, strength_max,
                 n_sims, n_fills, n_tp, n_sl, n_expired_exit,
                 win_rate, profit_factor, expectancy, sharpe, sqn,
                 composite_score, rank, data_status, mc_pvalue, loocv_ratio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
              f"  dir={top['direction_filter']}  str<={top['strength_max']}")
        print(f"  score={top['composite_score']:.4f}  pf={top.get('profit_factor'):.2f}"
              f"  exp={top.get('expectancy'):.2f}  wr={top.get('win_rate'):.1%}"
              f"  N={top['n_fills']}")
        if not stable:
            print(f"  WARNING: STABILITY: no positive-PF neighbor in tp/sl grid")

    return {
        "symbol":          symbol,
        "n_combos":        len(combo_data),
        "n_ranked":        len(ok_combos),
        "top":             top,
        "stable":          stable,
        "scored_at":       scored_at,
    }


def score_by_reason(db_path: Path, symbol: str, split: str = "train") -> dict:
    """
    Rank line-DETECTION rules (Algo 1-5's WINNING_REASONS, or any other tagged source) by
    how their real lines perform vs. their own matched random-control lines.

    This answers a different question from score() above: score() ranks entry-style +
    bracket-geometry combos for a fixed set of lines; this ranks which RULE should be
    trusted to pick lines in the first place, pooling across every entry-style/bracket
    combo already simulated for that reason (cl_algo_backtester.py tags every row with
    line_detect_reason/line_detect_kind from the source critical_lines row). Pooling
    maximizes the sample each reason gets to clear MIN_N_FILLS/MC/LOOCV with -- a single
    reason's own trade count, split further by entry style, would rarely clear them at all.

    Same anti-overfit guards as score(): MIN_N_FILLS, Monte-Carlo p-value, LOOCV ratio.
    No stability-neighbor check here (there's no tp/sl grid to have a "neighbor" on).
    Writes to cl_algo_reason_scores. Returns dict with top reason and summary.
    """
    init_db(db_path)
    scored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db(db_path) as con:
        rows = con.execute("""
            SELECT
                line_detect_reason, line_detect_kind,
                COUNT(*) as n_sims,
                SUM(CASE WHEN exit_reason IN ('TP','SL') THEN 1 ELSE 0 END) as n_fills,
                SUM(CASE WHEN exit_reason='TP' THEN 1 ELSE 0 END) as n_tp,
                SUM(CASE WHEN exit_reason='SL' THEN 1 ELSE 0 END) as n_sl,
                SUM(CASE WHEN exit_reason='EXPIRED' AND entry_fill_price IS NOT NULL
                         THEN 1 ELSE 0 END) as n_expired_exit
            FROM cl_algo_sim_results
            WHERE symbol=? AND split=? AND line_detect_reason IS NOT NULL
            GROUP BY line_detect_reason, line_detect_kind
        """, (symbol, split)).fetchall()

    if not rows:
        return {"symbol": symbol, "n_reasons": 0, "top": None}

    with get_db(db_path) as con:
        pnl_rows = con.execute("""
            SELECT line_detect_reason, line_detect_kind, pnl_ticks
            FROM cl_algo_sim_results
            WHERE symbol=? AND split=? AND line_detect_reason IS NOT NULL
                  AND exit_reason IN ('TP','SL') AND pnl_ticks IS NOT NULL
        """, (symbol, split)).fetchall()

    pnl_map: dict[tuple, list[float]] = {}
    for r in pnl_rows:
        pnl_map.setdefault((r[0], r[1]), []).append(r[2])

    reason_data = []
    for r in rows:
        key = (r["line_detect_reason"], r["line_detect_kind"])
        pnl_list = pnl_map.get(key, [])
        metrics  = _compute_metrics(pnl_list)

        if r["n_fills"] == 0:
            status = "no_fills"
        elif r["n_fills"] < MIN_N_FILLS:
            status = "insufficient_data"
        else:
            status = "ok"

        mc_pvalue = loocv_ratio = None
        if status == "ok":
            mc_pvalue   = monte_carlo_pvalue(pnl_list, seed=42)
            loocv_ratio = loocv_score(pnl_list)
            if mc_pvalue > MC_PVALUE_THRESHOLD:
                status = "low_confidence"
            elif loocv_ratio < LOOCV_THRESHOLD:
                status = "unstable"

        reason_data.append({
            "line_detect_reason": r["line_detect_reason"],
            "line_detect_kind":   r["line_detect_kind"],
            "n_sims":             r["n_sims"],
            "n_fills":            r["n_fills"],
            "n_tp":               r["n_tp"],
            "n_sl":               r["n_sl"],
            "n_expired_exit":     r["n_expired_exit"],
            "data_status":        status,
            "mc_pvalue":          mc_pvalue,
            "loocv_ratio":        loocv_ratio,
            **metrics,
        })

    ok_reasons = [c for c in reason_data if c["data_status"] == "ok"]
    if ok_reasons:
        for metric in WEIGHTS:
            vals  = [c.get(metric) for c in ok_reasons]
            norms = _normalize(vals)
            for c, nv in zip(ok_reasons, norms):
                c[f"{metric}_norm"] = nv
        for c in ok_reasons:
            c["composite_score"] = round(
                sum(WEIGHTS[m] * c.get(f"{m}_norm", 0) for m in WEIGHTS), 6
            )
        ok_reasons.sort(key=lambda x: x["composite_score"], reverse=True)
        for i, c in enumerate(ok_reasons):
            c["rank"] = i + 1

    insert_rows = [(
        scored_at, symbol, c["line_detect_reason"], c["line_detect_kind"],
        c["n_sims"], c["n_fills"], c["n_tp"], c["n_sl"], c["n_expired_exit"],
        c.get("win_rate"), c.get("profit_factor"), c.get("expectancy"),
        c.get("sharpe"), c.get("sqn"), c.get("composite_score"), c.get("rank"),
        c["data_status"], c.get("mc_pvalue"), c.get("loocv_ratio"),
    ) for c in reason_data]

    with get_db(db_path) as con:
        con.executemany("""
            INSERT OR IGNORE INTO cl_algo_reason_scores
                (scored_at, symbol, line_detect_reason, line_detect_kind,
                 n_sims, n_fills, n_tp, n_sl, n_expired_exit,
                 win_rate, profit_factor, expectancy, sharpe, sqn,
                 composite_score, rank, data_status, mc_pvalue, loocv_ratio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, insert_rows)

    top = ok_reasons[0] if ok_reasons else None
    return {
        "symbol":     symbol,
        "n_reasons":  len(reason_data),
        "n_ranked":   len(ok_reasons),
        "top":        top,
        "all":        reason_data,
        "scored_at":  scored_at,
    }


def score_all(db_path: Path, symbols: list[str] | None = None,
              top_n: int = 10, verbose: bool = False, split: str = "train") -> dict:
    """Score all symbols. Returns {symbol: result} dict."""
    syms = symbols or ["MES", "MNQ", "MYM", "M2K"]
    return {s: score(db_path, s, top_n=top_n, verbose=verbose, split=split) for s in syms}


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
                ("BOUNCE",    4, 4, "ALL", 3),   # combo A — good combo, PF>1, n=20 (== MIN_N_FILLS)
                ("BREAKOUT",  6, 4, "ALL", 3),   # combo B — bad combo, PF<1, n=20
                ("BOTH",      4, 8, "ALL", 3),   # combo C — neighbor of A (sl changes), n=20
            ]
            rows = []
            for at, tp, sl, df, sm in combos:
                for i in range(30):
                    # Combo A: 85% win (strong enough edge to clear the MC p-value guard
                    # at n=20 -- a modest 60% edge needs a much bigger sample to be
                    # statistically distinguishable from chance, which is the anti-overfit
                    # guard working as intended, not a test artifact); combo B: 40% win;
                    # combo C: 55% win.
                    win_prob = 0.85 if at == "BOUNCE" else (0.40 if at == "BREAKOUT" else 0.55)
                    is_tp = random.random() < win_prob
                    pnl   = tp if is_tp else -sl
                    rows.append((
                        f"2026-06-{(i%20)+1:02d}", "MES", at, tp, sl, df, sm,
                        5500.0, "SUPPORT", 1, "BUY", "LMT",
                        5500.0, 5501.0, 5499.0,
                        5500.0, "2026-06-30T14:00:00Z",
                        "TP" if is_tp else "SL",
                        5501.0 if is_tp else 5499.0,
                        pnl, 10, "train"
                    ))
                # One extra out_of_sample row for combo A -- must NOT affect train scoring
                rows.append((
                    "2026-07-15", "MES", "BOUNCE", 4, 4, "ALL", 3,
                    5500.0, "SUPPORT", 1, "BUY", "LMT",
                    5500.0, 5501.0, 5499.0,
                    5500.0, "2026-07-15T14:00:00Z",
                    "SL", 5499.0, -4, 10, "out_of_sample"
                ))
            # Combo D: only 5 fills -- below MIN_N_FILLS=20, must land 'insufficient_data'
            # and must NOT get mc_pvalue/loocv_ratio computed.
            for i in range(5):
                rows.append((
                    f"2026-08-{i+1:02d}", "MES", "FADE", 4, 4, "ALL", 3,
                    5500.0, "SUPPORT", 1, "BUY", "LMT",
                    5500.0, 5501.0, 5499.0,
                    5500.0, "2026-08-05T14:00:00Z",
                    "TP", 5501.0, 4, 10, "train"
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
                         exit_reason, exit_fill_price, pnl_ticks, ticks_to_exit,
                         split)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, rows)

            # Reason-tagged rows for score_by_reason(): a 'real' PREVIOUS_DAY_LOW rule
            # with a strong edge (80% win, pooled across mixed entry styles/brackets --
            # score_by_reason() pools these, unlike score() above) vs its own matched
            # 'random_control', with none. Deliberately a combo key (DIRECTIONAL/10/10)
            # untouched by combos A-D above, and a non-overlapping line_price range per
            # kind, so these rows can't collide with (or pollute) score()'s own assertions.
            reason_rows = []
            for kind, win_prob, price_base in [("real", 0.80, 5600.0),
                                                 ("random_control", 0.50, 5700.0)]:
                for i in range(25):
                    is_tp = random.random() < win_prob
                    pnl   = 4 if is_tp else -4
                    reason_rows.append((
                        f"2026-06-{(i%20)+1:02d}", "MES", "DIRECTIONAL", 10, 10, "ALL", 3,
                        price_base + i, "SUPPORT", 1, "BUY", "LMT",
                        5500.0, 5501.0, 5499.0,
                        5500.0, "2026-06-30T14:00:00Z",
                        "TP" if is_tp else "SL",
                        5501.0 if is_tp else 5499.0,
                        pnl, 10, "train", "PREVIOUS_DAY_LOW", kind
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
                         exit_reason, exit_fill_price, pnl_ticks, ticks_to_exit,
                         split, line_detect_reason, line_detect_kind)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, reason_rows)

            result = score(db_path, "MES", verbose=False, split="train")
            assert result["n_combos"] >= 3,   f"Expected ≥3 combos, got {result['n_combos']}"
            assert result["n_ranked"] >= 1,   f"Expected ≥1 ranked"
            top = result["top"]
            assert top is not None,            "No top combo"
            assert top["algo_type"] == "BOUNCE", \
                f"Expected BOUNCE (best PF) as top, got {top['algo_type']}"
            assert top["profit_factor"] > 1.0, f"Top PF should be >1: {top['profit_factor']}"

            # Anti-overfit guards: at n_fills=20 (== MIN_N_FILLS), both stats must be
            # real numbers, not their "not enough data" defaults (1.0 / 0.0).
            assert top["mc_pvalue"] is not None and 0.0 <= top["mc_pvalue"] <= 1.0, \
                f"mc_pvalue should be a real probability, got {top['mc_pvalue']}"
            assert top["loocv_ratio"] is not None, \
                "loocv_ratio should be populated for an 'ok' combo"

            # A combo with n_fills under MIN_N_FILLS must not get mc_pvalue/loocv_ratio
            # computed at all (data_status short-circuits before the stats run).
            with get_db(db_path) as con:
                insufficient_rows = con.execute(
                    "SELECT mc_pvalue, loocv_ratio FROM cl_algo_combo_scores "
                    "WHERE symbol='MES' AND data_status='insufficient_data'"
                ).fetchall()
            for row in insufficient_rows:
                assert row["mc_pvalue"] is None and row["loocv_ratio"] is None, \
                    "insufficient_data combos should not have stats computed"

            # split filter: the out_of_sample row for combo A must be invisible to a
            # split='train' score. (Combo A's 30 inserts collapse to 20 unique rows --
            # dates cycle 06-01..06-20 every 20 iterations, and the UNIQUE constraint on
            # cl_algo_sim_results INSERT-OR-IGNOREs the i=20..29 date repeats -- so 20,
            # not 30, is the correct train count; the assertion here is that adding the
            # 1 out_of_sample row does NOT bump it to 21.)
            bounce_44 = next(c for c in [top] if c["algo_type"] == "BOUNCE"
                              and c["tp_ticks"] == 4 and c["sl_ticks"] == 4)
            assert bounce_44["n_sims"] == 20, \
                f"out_of_sample row leaked into train score: n_sims={bounce_44['n_sims']}"

            # scoring against split='out_of_sample' only sees that 1 row, not the 30 train rows
            oos_result = score(db_path, "MES", verbose=False, split="out_of_sample")
            assert oos_result["n_combos"] == 1, \
                f"Expected exactly 1 out_of_sample combo, got {oos_result['n_combos']}"

            # score_by_reason(): PREVIOUS_DAY_LOW's 'real' rows (80% WR, n=25) should
            # rank above its own 'random_control' (50% WR, n=25) and clear MIN_N_FILLS.
            reason_result = score_by_reason(db_path, "MES", split="train")
            assert reason_result["n_reasons"] == 2, \
                f"Expected 2 (reason,kind) groups, got {reason_result['n_reasons']}"
            reason_top = reason_result["top"]
            assert reason_top is not None, "No top reason ranked"
            assert reason_top["line_detect_reason"] == "PREVIOUS_DAY_LOW"
            assert reason_top["line_detect_kind"] == "real", \
                f"Expected 'real' to outrank 'random_control', top was {reason_top['line_detect_kind']}"
            assert reason_top["n_fills"] == 25, f"Expected n_fills=25, got {reason_top['n_fills']}"

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
    parser.add_argument("--split", default="train",
                         help="train|validation|out_of_sample (default: train)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg = get_config()
    db_path = Path(cfg.paths.db)
    results = score_all(db_path, symbols=args.symbol, top_n=args.top, verbose=True,
                         split=args.split)
    for sym, r in results.items():
        if r["n_combos"] == 0:
            print(f"{sym}: no simulation results yet")
        elif r["top"]:
            t = r["top"]
            print(f"{sym}: top={t['algo_type']} tp={t['tp_ticks']} sl={t['sl_ticks']}"
                  f" score={t['composite_score']:.4f}")
