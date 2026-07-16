"""
back-trading/cl_algo_learner.py
Iterative learning machine: reads scores → narrows parameter search space.

Architecture: Iterative Bayesian Grid Narrowing
  Run 0 (exploration): coarse grid, tp/sl at [2,4,6,8,12]
  Run N+1: top-20% combos define "hot zone" → finer grid around centroid
            + 20% random exploration from unexplored space
  Convergence: top-5 combo fingerprints identical across 3 consecutive scoring runs

The learner does NOT run the backtester or scorer — it reads scores and writes
recommendations that the pipeline (run_cl_algo_pipeline.py) will act on.

Output:
  - DB row in cl_algo_learner_runs with recommended_tp_ticks + recommended_sl_ticks (JSON)
  - docs/learner_state.md snapshot (human-readable)

Usage:
    python back-trading/cl_algo_learner.py --symbol MES
    python back-trading/cl_algo_learner.py --symbol MES --dry-run
    python back-trading/cl_algo_learner.py --self-test
"""

import sys
import json
import math
import random
import argparse
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db, init_db

DEFAULT_TP_TICKS = [2, 4, 6, 8, 12]
DEFAULT_SL_TICKS = [2, 4, 6, 8, 12]

# All possible tick values (the full search space)
_ALL_TP = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20]
_ALL_SL = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20]

_TOP_PCT     = 0.20   # top 20% define the hot zone
_EXPLORE_PCT = 0.20   # 20% random exploration budget
_FINE_RADIUS = 3      # ticks on each side of the hot-zone centroid for fine grid
_CONVERGENCE_RUNS = 3 # stable top-5 for this many runs → CONVERGED
_TOP_K = 5            # fingerprint uses top-K combos
_MIN_N_FILLS_FOR_CONVERGENCE = 30  # Monte Carlo guard — a fingerprint-stable top combo
                                    # can't be declared CONVERGED until it has this many
                                    # resolved fills; below that, small-sample noise can
                                    # produce a "stable" top by chance.


def _load_scores(db_path: Path, symbol: str) -> list[dict]:
    """Load all scored combos for symbol, newest scoring run first."""
    with get_db(db_path) as con:
        # Get the two most recent scored_at timestamps
        times = con.execute("""
            SELECT DISTINCT scored_at FROM cl_algo_combo_scores
            WHERE symbol=? ORDER BY scored_at DESC LIMIT 10
        """, (symbol,)).fetchall()
        if not times:
            return []
        latest = times[0][0]
        rows = con.execute("""
            SELECT * FROM cl_algo_combo_scores
            WHERE symbol=? AND scored_at=?
            ORDER BY rank ASC NULLS LAST, composite_score DESC
        """, (symbol, latest)).fetchall()
    return [dict(r) for r in rows]


def _load_score_history(db_path: Path, symbol: str) -> list[dict]:
    """Load cl_algo_score_history for symbol, newest first."""
    with get_db(db_path) as con:
        rows = con.execute("""
            SELECT * FROM cl_algo_score_history
            WHERE symbol=? ORDER BY scored_at DESC LIMIT 20
        """, (symbol,)).fetchall()
    return [dict(r) for r in rows]


def _combo_fingerprint(c: dict) -> str:
    return f"{c['algo_type']}|{c['tp_ticks']}|{c['sl_ticks']}|{c['direction_filter']}|{c['strength_max']}"


def _check_convergence(history: list[dict]) -> tuple[str, str | None]:
    """Return (status, note). status is 'converged', 'narrowing', or 'exploring'.
    note explains why convergence was held back, if applicable."""
    if len(history) < _CONVERGENCE_RUNS:
        return "exploring", None
    # Check last N top combos are identical
    recent = history[:_CONVERGENCE_RUNS]
    fingerprints = [h.get("top_algo_type") or "" for h in recent]
    tp_vals  = [h.get("top_tp_ticks") for h in recent]
    sl_vals  = [h.get("top_sl_ticks") for h in recent]
    fingerprint_stable = (len(set(fingerprints)) == 1 and
                           len(set(tp_vals)) == 1 and len(set(sl_vals)) == 1)
    if fingerprint_stable:
        # Monte Carlo guard: the stable top combo also needs enough fills to trust it —
        # a fingerprint that's "stable" on a handful of fills can just be noise.
        min_n_fills = min((h.get("top_n_fills") or 0) for h in recent)
        if min_n_fills >= _MIN_N_FILLS_FOR_CONVERGENCE:
            return "converged", None
        return "narrowing", (
            f"Top combo fingerprint stable across {_CONVERGENCE_RUNS} runs but only "
            f"N={min_n_fills} fills (need ≥{_MIN_N_FILLS_FOR_CONVERGENCE}) — "
            f"holding at 'narrowing' pending more data (Monte Carlo guard)."
        )
    # More than 1 scoring run but not yet converged
    return ("narrowing" if len(history) >= 2 else "exploring"), None


def _hot_zone(top_combos: list[dict]) -> tuple[float, float]:
    """Return (centroid_tp, centroid_sl) of the top combos in tp/sl space."""
    tp_vals = [c["tp_ticks"] for c in top_combos]
    sl_vals = [c["sl_ticks"] for c in top_combos]
    return sum(tp_vals) / len(tp_vals), sum(sl_vals) / len(sl_vals)


def _fine_grid_around(centroid_tp: float, centroid_sl: float,
                       radius: int, all_tp: list[int], all_sl: list[int],
                       already_explored: set[tuple]) -> tuple[list[int], list[int]]:
    """Generate a finer grid ±radius ticks around the centroid, from the full search space."""
    ctp = round(centroid_tp)
    csl = round(centroid_sl)

    candidate_tp = sorted({t for t in all_tp
                            if abs(t - ctp) <= radius and (t, csl) not in already_explored})
    candidate_sl = sorted({s for s in all_sl
                            if abs(s - csl) <= radius and (ctp, s) not in already_explored})

    if not candidate_tp:
        candidate_tp = [ctp] if ctp in all_tp else [min(all_tp, key=lambda x: abs(x - ctp))]
    if not candidate_sl:
        candidate_sl = [csl] if csl in all_sl else [min(all_sl, key=lambda x: abs(x - csl))]

    return candidate_tp, candidate_sl


def _exploration_sample(all_tp: list[int], all_sl: list[int],
                         already_explored: set[tuple],
                         n_samples: int) -> tuple[list[int], list[int]]:
    """Pick random (tp, sl) pairs from unexplored space."""
    unexplored = [(t, s) for t in all_tp for s in all_sl
                  if (t, s) not in already_explored]
    if not unexplored:
        return [], []
    picks = random.sample(unexplored, min(n_samples, len(unexplored)))
    return sorted({p[0] for p in picks}), sorted({p[1] for p in picks})


def recommend(db_path: Path, symbol: str,
              dry_run: bool = False) -> dict:
    """
    Read latest scores → generate next recommended grid → write to DB + learner_state.md.
    Returns recommendation dict.
    """
    init_db(db_path)

    scores  = _load_scores(db_path, symbol)
    history = _load_score_history(db_path, symbol)

    # Determine iteration number
    with get_db(db_path) as con:
        n_prev = con.execute(
            "SELECT COUNT(*) FROM cl_algo_learner_runs WHERE symbol=?", (symbol,)
        ).fetchone()[0]
    iteration = n_prev + 1

    # Already-explored (tp, sl) pairs
    with get_db(db_path) as con:
        prev_runs = con.execute(
            "SELECT recommended_tp_ticks, recommended_sl_ticks FROM cl_algo_learner_runs"
            " WHERE symbol=? ORDER BY id DESC LIMIT 5", (symbol,)
        ).fetchall()
    already_explored: set[tuple] = set()
    for run in prev_runs:
        tps = json.loads(run[0] or "[]")
        sls = json.loads(run[1] or "[]")
        for t in tps:
            for s in sls:
                already_explored.add((t, s))

    convergence, convergence_note = _check_convergence(history)

    # Build recommendation
    ok_scores = [s for s in scores if s.get("data_status") == "ok"]
    reasoning_parts = [convergence_note] if convergence_note else []

    if convergence == "converged":
        top = ok_scores[0] if ok_scores else None
        reasoning = (f"CONVERGED after {len(history)} scoring runs. "
                     f"Best: {top['algo_type']} tp={top['tp_ticks']} sl={top['sl_ticks']}"
                     if top else "Converged with no data.")
        rec_tp = [top["tp_ticks"]] if top else DEFAULT_TP_TICKS
        rec_sl = [top["sl_ticks"]] if top else DEFAULT_SL_TICKS

    elif not ok_scores:
        # No data yet — recommend coarse exploration grid
        rec_tp = DEFAULT_TP_TICKS
        rec_sl = DEFAULT_SL_TICKS
        reasoning = "No scored combos yet. Recommending full coarse exploration grid."

    else:
        top_n    = max(1, int(len(ok_scores) * _TOP_PCT))
        top_combos = ok_scores[:top_n]

        centroid_tp, centroid_sl = _hot_zone(top_combos)
        reasoning_parts.append(
            f"Hot zone centroid: tp≈{centroid_tp:.1f}, sl≈{centroid_sl:.1f}"
            f" (from top-{top_n} of {len(ok_scores)} ranked combos)"
        )

        fine_tp, fine_sl = _fine_grid_around(
            centroid_tp, centroid_sl, _FINE_RADIUS,
            _ALL_TP, _ALL_SL, already_explored
        )
        n_explore = max(1, int((len(fine_tp) * len(fine_sl)) * _EXPLORE_PCT))
        exp_tp, exp_sl = _exploration_sample(_ALL_TP, _ALL_SL, already_explored, n_explore)

        rec_tp = sorted(set(fine_tp + exp_tp))
        rec_sl = sorted(set(fine_sl + exp_sl))
        reasoning_parts.append(
            f"Fine grid: tp={fine_tp} sl={fine_sl} | exploration adds tp={exp_tp} sl={exp_sl}"
        )
        reasoning = " | ".join(reasoning_parts)

    rec = {
        "symbol":                  symbol,
        "iteration":               iteration,
        "recommended_tp_ticks":    rec_tp,
        "recommended_sl_ticks":    rec_sl,
        "all_algo_types":          1,
        "all_direction_filters":   1,
        "all_strength_max":        1,
        "convergence_status":      convergence,
        "reasoning":               reasoning,
        "n_scored_combos":         len(ok_scores),
        "top_combo":               ok_scores[0] if ok_scores else None,
    }

    if not dry_run:
        run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_db(db_path) as con:
            con.execute("""
                INSERT INTO cl_algo_learner_runs
                    (run_at, symbol, iteration,
                     recommended_tp_ticks, recommended_sl_ticks,
                     all_algo_types, all_direction_filters, all_strength_max,
                     convergence_status, reasoning)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                run_at, symbol, iteration,
                json.dumps(rec_tp), json.dumps(rec_sl),
                1, 1, 1, convergence, reasoning,
            ))
        _write_learner_state(db_path, symbol, rec)

    return rec


def _write_learner_state(db_path: Path, symbol: str, rec: dict):
    """Write human-readable docs/learner_state.md."""
    docs_dir = _ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    out_path = docs_dir / "learner_state.md"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top = rec.get("top_combo")
    lines = [
        f"# CL Algo Learner State",
        f"**Updated:** {now}  **Symbol:** {symbol}  "
        f"**Iteration:** {rec['iteration']}  "
        f"**Status:** {rec['convergence_status'].upper()}",
        "",
        f"## Next Run Parameters",
        f"- `tp_ticks`: {rec['recommended_tp_ticks']}",
        f"- `sl_ticks`: {rec['recommended_sl_ticks']}",
        f"- All algo types: yes | All direction filters: yes | All strength levels: yes",
        "",
        f"## Reasoning",
        rec["reasoning"],
        "",
        f"## Current Best Combo",
    ]
    if top:
        lines += [
            f"- Algo: **{top['algo_type']}**",
            f"- TP: **{top['tp_ticks']}t** | SL: **{top['sl_ticks']}t**",
            f"- Dir filter: {top['direction_filter']} | Strength ≤ {top['strength_max']}",
            f"- PF: {top.get('profit_factor', 'N/A'):.3f} | "
            f"Expectancy: {top.get('expectancy', 'N/A'):.2f}t | "
            f"WR: {(top.get('win_rate') or 0):.1%}",
            f"- N fills: {top.get('n_fills', 0)} | Score: {top.get('composite_score', 0):.4f}",
        ]
    else:
        lines.append("No ranked combos yet.")

    try:
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    print("Running cl_algo_learner self-test...")
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "galao.db"
            init_db(db_path)

            # Snapshot 1: BOUNCE tp=4 sl=4 is top
            def _insert_history(scored_at, top_algo, top_tp, top_sl, n=5, n_fills=30):
                with get_db(db_path) as con:
                    con.execute("""
                        INSERT INTO cl_algo_score_history
                            (scored_at, symbol, n_combos_scored,
                             top_algo_type, top_tp_ticks, top_sl_ticks,
                             top_direction_filter, top_strength_max,
                             top_composite_score, top_n_fills, convergence_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (scored_at, "MES", n, top_algo, top_tp, top_sl,
                          "ALL", 3, 0.75, n_fills, "exploring"))
                    for tp in [2, 4, 6, 8, 12]:
                        for sl in [2, 4, 6, 8, 12]:
                            con.execute("""
                                INSERT OR IGNORE INTO cl_algo_combo_scores
                                    (scored_at, symbol, algo_type, tp_ticks, sl_ticks,
                                     direction_filter, strength_max,
                                     n_sims, n_fills, n_tp, n_sl, n_expired_exit,
                                     win_rate, profit_factor, expectancy, composite_score,
                                     rank, data_status)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """, (scored_at, "MES", top_algo, tp, sl, "ALL", 3,
                                  25, n_fills if (tp == top_tp and sl == top_sl) else 10,
                                  6, 4, 15,
                                  0.60 if (tp == top_tp and sl == top_sl) else 0.50,
                                  1.50 if (tp == top_tp and sl == top_sl) else 1.10,
                                  2.0  if (tp == top_tp and sl == top_sl) else 0.5,
                                  0.75 if (tp == top_tp and sl == top_sl) else 0.40,
                                  1    if (tp == top_tp and sl == top_sl) else tp + sl,
                                  "ok"))

            _insert_history("2026-07-04T10:00:00Z", "BOUNCE", 4, 4, n_fills=12)

            rec1 = recommend(db_path, "MES")
            assert rec1["convergence_status"] in ("exploring", "narrowing")
            assert len(rec1["recommended_tp_ticks"]) > 0
            assert len(rec1["recommended_sl_ticks"]) > 0
            # Hot zone should center near tp=4, sl=4
            assert 4 in rec1["recommended_tp_ticks"] or 3 in rec1["recommended_tp_ticks"]

            # Snapshot 2+3: same top, but N<30 on all 3 recent runs — Monte Carlo guard
            # should hold at 'narrowing' despite the fingerprint being stable.
            _insert_history("2026-07-04T11:00:00Z", "BOUNCE", 4, 4, n_fills=12)
            _insert_history("2026-07-04T12:00:00Z", "BOUNCE", 4, 4, n_fills=12)

            rec_guarded = recommend(db_path, "MES")
            assert rec_guarded["convergence_status"] == "narrowing", \
                (f"Expected 'narrowing' (N<30 guard) despite stable fingerprint, "
                 f"got {rec_guarded['convergence_status']}")
            assert "Monte Carlo guard" in rec_guarded["reasoning"], \
                "Reasoning should explain the N<30 hold-back"

            # Snapshots 4-6: same top, N>=30 on all 3 most recent runs — now convergence
            # should trigger (the guard looks at the last _CONVERGENCE_RUNS runs only).
            _insert_history("2026-07-04T13:00:00Z", "BOUNCE", 4, 4, n_fills=30)
            _insert_history("2026-07-04T14:00:00Z", "BOUNCE", 4, 4, n_fills=30)
            _insert_history("2026-07-04T15:00:00Z", "BOUNCE", 4, 4, n_fills=30)

            rec2 = recommend(db_path, "MES")
            assert rec2["convergence_status"] == "converged", \
                f"Expected converged after 3 identical tops with N>=30, got {rec2['convergence_status']}"

            # Dry-run doesn't write to DB
            with get_db(db_path) as con:
                n_before = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_learner_runs"
                ).fetchone()[0]
            recommend(db_path, "MES", dry_run=True)
            with get_db(db_path) as con:
                n_after = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_learner_runs"
                ).fetchone()[0]
            assert n_after == n_before, "dry_run must not write"

        print(f"PASS -- learner: iter={rec2['iteration']} status={rec2['convergence_status']}")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CL Algo Learner")
    parser.add_argument("--symbol", default="MES")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg = get_config()
    db_path = Path(cfg.paths.db)
    rec = recommend(db_path, args.symbol, dry_run=args.dry_run)
    print(f"[{rec['symbol']}] iteration={rec['iteration']}  status={rec['convergence_status']}")
    print(f"  tp_ticks: {rec['recommended_tp_ticks']}")
    print(f"  sl_ticks: {rec['recommended_sl_ticks']}")
    print(f"  reasoning: {rec['reasoning']}")
