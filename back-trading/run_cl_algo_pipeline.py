"""
back-trading/run_cl_algo_pipeline.py
One-shot CL Algo pipeline orchestrator.

Stages (in order):
  1. data_availability   — find (symbol, day) pairs with full tick data + armed lines
  2. cl_algo_backtester  — simulate all combos on ready days
  3. cl_algo_scorer      — aggregate + rank combos per symbol
  4. cl_algo_learner     — generate next-iteration grid recommendation

Each stage is self-contained and resumable. Run this script repeatedly — it only
adds new simulation rows (INSERT OR IGNORE), never overwrites existing ones.

Parallel-symbol mode: if multiple symbols have ready days, run one worker per symbol.
Sequential mode (default): run symbols one after another to keep memory usage low.

Usage:
    python back-trading/run_cl_algo_pipeline.py              # all symbols
    python back-trading/run_cl_algo_pipeline.py --symbol MES
    python back-trading/run_cl_algo_pipeline.py --dry-run    # count only
    python back-trading/run_cl_algo_pipeline.py --verbose
    python back-trading/run_cl_algo_pipeline.py --self-test
"""

import sys
import time
import json
import importlib.util
import argparse
import tempfile
import csv
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db             import get_db, init_db
from lib.data_availability import get_ready_days, summarise, split_boundaries


def _load(name: str) -> object:
    path = Path(__file__).parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _print(msg: str):
    print(f"[{_ts()}] {msg}", flush=True)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(db_path: Path, history_dir: Path,
                 symbols: list[str] | None = None,
                 dry_run: bool = False,
                 verbose: bool = False,
                 split: str = "train") -> dict:
    """
    Full pipeline: availability → half-duplex backtest + full-duplex → score → learn.
    split selects which chronological slice (train|validation|out_of_sample, see
    lib.data_availability.split_boundaries) this run is scoped to -- every row written
    is tagged with it, and scoring only ever reads rows tagged with the same split.
    Symbols with too few ready days for a non-empty split slice are skipped.
    Returns summary dict.
    """
    bt  = _load("cl_algo_backtester")
    fd  = _load("cl_algo_full_duplex")
    sc  = _load("cl_algo_scorer")
    lr  = _load("cl_algo_learner")

    syms = symbols or ["MES", "MNQ", "MYM", "M2K"]
    init_db(db_path)

    # Stage 1: data availability
    _print("Stage 1/5 — Checking data availability...")
    ready_days = get_ready_days(db_path, history_dir, symbols=syms)
    _print(summarise(ready_days))

    if not ready_days and not dry_run:
        _print("No ready days. Nothing to backtest. Exiting.")
        return {"ready_days": 0, "symbols": syms}

    all_results = {}

    for sym in syms:
        sym_days = [d for d in ready_days if d["symbol"] == sym]
        if not sym_days and not dry_run:
            _print(f"[{sym}] 0 ready days — skipping.")
            continue

        _print(f"[{sym}] {len(sym_days)} ready day(s)")

        # Scope this run to the requested split's chronological date range --
        # never random (time-series data), computed from the FULL ready_days
        # so the boundaries are stable regardless of which split is requested.
        bounds = split_boundaries(ready_days, sym)
        min_d, max_d = bounds.get(split, (None, None))
        if min_d is None or max_d is None:
            _print(f"[{sym}] Not enough ready days for split='{split}' "
                    f"(bounds={bounds}) — skipping this symbol for this split.")
            continue
        _print(f"[{sym}] split='{split}' date range: {min_d}..{max_d}")

        # Stage 2a: half-duplex backtest (Cartesian combos)
        _print(f"[{sym}] Stage 2a/5 — Half-duplex backtester (combo matrix)...")
        bt_result = bt.run(
            db_path=db_path,
            history_dir=history_dir,
            symbols=[sym],
            dry_run=dry_run,
            verbose=verbose,
            min_date=min_d,
            max_date=max_d,
            split=split,
        )
        _print(f"[{sym}] Half-duplex: written={bt_result.get('written',0)}"
               f" skipped={bt_result.get('skipped',0)}"
               f" errors={bt_result.get('errors',0)}"
               f" elapsed={bt_result.get('elapsed_s','?')}s")

        # Stage 2b: full-duplex backtest (structural exits via critical lines)
        _print(f"[{sym}] Stage 2b/5 — Full-duplex backtester (structural exits)...")
        fd_result = fd.run(
            db_path=db_path,
            history_dir=history_dir,
            symbols=[sym],
            dry_run=dry_run,
            verbose=verbose,
            min_date=min_d,
            max_date=max_d,
            split=split,
        )
        _print(f"[{sym}] Full-duplex: written={fd_result.get('written',0)}"
               f" skipped={fd_result.get('skipped',0)}"
               f" errors={fd_result.get('errors',0)}"
               f" elapsed={fd_result.get('elapsed_s','?')}s")

        # Stage 3: score (half-duplex combos) -- only this split's rows
        _print(f"[{sym}] Stage 3/5 — Scoring combos...")
        score_result = sc.score(db_path, sym, verbose=verbose, split=split)
        top = score_result.get("top")
        if top:
            _print(f"[{sym}] Top combo: {top['algo_type']}"
                   f" tp={top['tp_ticks']}t sl={top['sl_ticks']}t"
                   f" pf={top.get('profit_factor','?'):.2f}"
                   f" exp={top.get('expectancy','?'):.2f}t"
                   f" N={top.get('n_fills',0)}")
        else:
            _print(f"[{sym}] No ranked combos yet (insufficient data)")

        # Stage 4: full-duplex summary
        with get_db(db_path) as con:
            fd_rows = con.execute("""
                SELECT COUNT(*) as n_total,
                       SUM(CASE WHEN exit_reason='TP' THEN 1 ELSE 0 END) as n_tp,
                       SUM(CASE WHEN exit_reason='SL' THEN 1 ELSE 0 END) as n_sl,
                       AVG(pnl_ticks) as avg_pnl,
                       SUM(CASE WHEN tp_source='critical_line' THEN 1 ELSE 0 END) as n_cl_tp,
                       SUM(CASE WHEN sl_source='critical_line' THEN 1 ELSE 0 END) as n_cl_sl
                FROM cl_algo_fd_results
                WHERE symbol=? AND split=? AND entry_fill_price IS NOT NULL
            """, (sym, split)).fetchone()
        if fd_rows and fd_rows["n_total"]:
            _print(f"[{sym}] Full-duplex fills: {fd_rows['n_total']} fills"
                   f" TP={fd_rows['n_tp']} SL={fd_rows['n_sl']}"
                   f" avg_pnl={fd_rows['avg_pnl']:.1f}t"
                   f" CL-exits={fd_rows['n_cl_tp']}/{fd_rows['n_cl_sl']}")

        # Stage 5: learn (uses half-duplex scores for now)
        _print(f"[{sym}] Stage 5/5 — Running learner...")
        learn_result = lr.recommend(db_path, sym, dry_run=dry_run)
        _print(f"[{sym}] Status={learn_result['convergence_status']}"
               f" iteration={learn_result['iteration']}")
        _print(f"[{sym}] Next tp_ticks={learn_result['recommended_tp_ticks']}")
        _print(f"[{sym}] Next sl_ticks={learn_result['recommended_sl_ticks']}")

        all_results[sym] = {
            "ready_days":      len(sym_days),
            "split":           split,
            "split_range":     (min_d, max_d),
            "bt":              bt_result,
            "fd":              fd_result,
            "score":           score_result,
            "learn":           learn_result,
        }

    _print("Pipeline complete.")
    return all_results


# ── Out-of-sample evaluation ─────────────────────────────────────────────────

def run_out_of_sample_eval(db_path: Path, history_dir: Path,
                           symbols: list[str] | None = None,
                           verbose: bool = False) -> dict:
    """
    The first honest read: for each symbol, take whatever cl_algo_learner_runs
    most recently recommended from a train-split run, and if that recommendation
    has actually CONVERGED to one single tp/sl combo, run the backtester ONCE
    against that symbol's out_of_sample date range with ONLY that single combo
    (no grid search -- OOS evaluates the already-chosen candidate, it doesn't
    re-tune). This data was never touched by the scorer or learner while tuning.

    Returns {symbol: {"status": ..., ...}}. status is one of:
      "ok"              -- ran OOS eval, see n_fills/n_tp/n_sl/total_pnl_ticks/avg_pnl_ticks
      "no_oos_data"      -- not enough ready days to carve out an out_of_sample slice
      "no_learner_run"   -- cl_algo_learner_runs empty for this symbol (train run never ran)
      "not_converged"    -- learner hasn't converged on a single combo yet (still a grid)
      "combo_not_found"  -- couldn't resolve the full combo (algo_type/dir/strength) for
                             the recommended tp/sl from cl_algo_combo_scores
    """
    bt = _load("cl_algo_backtester")
    syms = symbols or ["MES", "MNQ", "MYM", "M2K"]
    init_db(db_path)

    ready_days = get_ready_days(db_path, history_dir, symbols=syms)
    results = {}

    for sym in syms:
        bounds = split_boundaries(ready_days, sym)
        oos_min, oos_max = bounds["out_of_sample"]
        if oos_min is None:
            results[sym] = {"status": "no_oos_data",
                             "message": "Not enough ready days for an out_of_sample slice."}
            _print(f"[{sym}] No out_of_sample date range available — skipping OOS eval.")
            continue

        with get_db(db_path) as con:
            learner_row = con.execute(
                "SELECT * FROM cl_algo_learner_runs WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (sym,)
            ).fetchone()

        if not learner_row:
            results[sym] = {"status": "no_learner_run",
                             "message": "cl_algo_learner_runs is empty for this symbol -- "
                                        "run the train-split pipeline first."}
            _print(f"[{sym}] No learner run found — skipping OOS eval.")
            continue

        rec_tp = json.loads(learner_row["recommended_tp_ticks"] or "[]")
        rec_sl = json.loads(learner_row["recommended_sl_ticks"] or "[]")
        status = learner_row["convergence_status"]

        if status != "converged" or len(rec_tp) != 1 or len(rec_sl) != 1:
            results[sym] = {
                "status": "not_converged",
                "convergence_status": status,
                "message": (
                    f"Learner has not converged on a single combo "
                    f"(status={status}, recommended_tp_ticks={rec_tp}, "
                    f"recommended_sl_ticks={rec_sl}) -- there is no single combo to "
                    f"evaluate out-of-sample. Not forcing a number."
                ),
            }
            _print(f"[{sym}] Not converged (status={status}) — no single combo to OOS-evaluate.")
            continue

        tp, sl = rec_tp[0], rec_sl[0]

        # learner_runs only stores tp/sl ticks -- pull the full combo (algo_type,
        # direction_filter, strength_max) that earned this tp/sl from the train scores.
        with get_db(db_path) as con:
            combo_row = con.execute("""
                SELECT algo_type, tp_ticks, sl_ticks, direction_filter, strength_max
                FROM cl_algo_combo_scores
                WHERE symbol=? AND tp_ticks=? AND sl_ticks=? AND data_status='ok'
                ORDER BY rank ASC LIMIT 1
            """, (sym, tp, sl)).fetchone()

        if not combo_row:
            results[sym] = {"status": "combo_not_found",
                             "message": f"No scored combo found for tp={tp} sl={sl}."}
            _print(f"[{sym}] Could not resolve full combo for tp={tp} sl={sl} — skipping.")
            continue

        combo = dict(combo_row)
        _print(f"[{sym}] OOS eval: {combo['algo_type']} tp={tp} sl={sl} "
               f"dir={combo['direction_filter']} str<={combo['strength_max']} "
               f"over {oos_min}..{oos_max}")

        bt_result = bt.run(
            db_path=db_path, history_dir=history_dir, symbols=[sym],
            combos=[combo], min_date=oos_min, max_date=oos_max,
            split="out_of_sample", verbose=verbose,
        )

        with get_db(db_path) as con:
            row = con.execute("""
                SELECT
                    COUNT(*) as n_sims,
                    SUM(CASE WHEN exit_reason IN ('TP','SL') THEN 1 ELSE 0 END) as n_fills,
                    SUM(CASE WHEN exit_reason='TP' THEN 1 ELSE 0 END) as n_tp,
                    SUM(CASE WHEN exit_reason='SL' THEN 1 ELSE 0 END) as n_sl,
                    SUM(pnl_ticks) as total_pnl_ticks,
                    AVG(pnl_ticks) as avg_pnl_ticks
                FROM cl_algo_sim_results
                WHERE symbol=? AND split='out_of_sample'
                  AND algo_type=? AND tp_ticks=? AND sl_ticks=?
                  AND direction_filter=? AND strength_max=?
                  AND date>=? AND date<=?
            """, (sym, combo["algo_type"], combo["tp_ticks"], combo["sl_ticks"],
                  combo["direction_filter"], combo["strength_max"], oos_min, oos_max)
            ).fetchone()

        results[sym] = {
            "status":          "ok",
            "combo":           combo,
            "oos_range":       (oos_min, oos_max),
            "n_sims":          row["n_sims"] or 0,
            "n_fills":         row["n_fills"] or 0,
            "n_tp":            row["n_tp"] or 0,
            "n_sl":            row["n_sl"] or 0,
            "total_pnl_ticks": row["total_pnl_ticks"],
            "avg_pnl_ticks":   row["avg_pnl_ticks"],
            "bt_written":      bt_result.get("written", 0),
        }
        _print(f"[{sym}] OOS result: fills={results[sym]['n_fills']} "
               f"TP={results[sym]['n_tp']} SL={results[sym]['n_sl']} "
               f"total_pnl={results[sym]['total_pnl_ticks']} "
               f"avg_pnl={results[sym]['avg_pnl_ticks']}")

    return results


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    print("Running run_cl_algo_pipeline self-test (dry-run mode with synthetic data)...")
    try:
        from zoneinfo import ZoneInfo
        UTC = ZoneInfo("UTC")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p    = Path(tmp)
            hist_dir = tmp_p / "history"
            hist_dir.mkdir()
            db_path  = tmp_p / "galao.db"

            init_db(db_path)

            # Seed critical lines for 2 days -- split_boundaries needs >=2 ready days for
            # the default 60/20/20 fractions to give split='train' any days at all
            # (with only 1 day, int(1*0.6)==0 and train legitimately comes back empty).
            with get_db(db_path) as con:
                for d in ("2026-06-30", "2026-07-01"):
                    con.execute("""
                        INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                        VALUES('MES',?,'SUPPORT',5500.0,1,1)
                    """, (d,))
                    con.execute("""
                        INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                        VALUES('MES',?,'RESISTANCE',5550.0,2,1)
                    """, (d,))

            # Write 200-row synthetic CSVs for both days
            prices = [round(5525.0 + 30.0 * math.sin(i / 40.0), 2) for i in range(200)]
            for d, compact in (("2026-06-30", "20260630"), ("2026-07-01", "20260701")):
                base = datetime.fromisoformat(d).replace(hour=13, minute=30, tzinfo=UTC)
                t_path = hist_dir / f"MES_trades_{compact}.csv"
                b_path = hist_dir / f"MES_bid_ask_{compact}.csv"
                with open(t_path, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time_utc", "price", "size"])
                    for i, p in enumerate(prices):
                        w.writerow([(base + timedelta(seconds=i*30)).isoformat(), p, 100])
                with open(b_path, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time_utc", "bid_p", "bid_s", "ask_p", "ask_s"])
                    for i, p in enumerate(prices):
                        w.writerow([(base + timedelta(seconds=i*30)).isoformat(),
                                    p - 0.25, 10, p + 0.25, 10])

            # Run full pipeline (NOT dry-run — we want real writes for the integration test).
            # Default split='train' -> with 2 ready days that's just 2026-06-30 (1 day);
            # 2026-07-01 falls into out_of_sample and must NOT be touched by this run.
            results = run_pipeline(db_path, hist_dir, symbols=["MES"], dry_run=False)

            assert "MES" in results, f"MES missing from results: {results}"
            mes = results["MES"]
            assert mes["split"] == "train"
            assert mes["split_range"] == ("2026-06-30", "2026-06-30"), mes["split_range"]
            assert mes["bt"]["written"] > 0,        "Backtest should have written rows"
            assert mes["score"]["n_combos"] > 0,    "Scorer should have scored combos"
            assert mes["learn"]["iteration"] >= 1,  "Learner should have run"

            with get_db(db_path) as con:
                dates_touched = {r[0] for r in con.execute(
                    "SELECT DISTINCT date FROM cl_algo_sim_results WHERE symbol='MES'"
                ).fetchall()}
                oos_rows = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_sim_results WHERE symbol='MES' AND split='out_of_sample'"
                ).fetchone()[0]
            assert dates_touched == {"2026-06-30"}, \
                f"split='train' run touched dates outside its range: {dates_touched}"
            assert oos_rows == 0, \
                f"train-split pipeline run must never write split='out_of_sample' rows, found {oos_rows}"

            # Re-run: no new backtest rows (idempotent)
            results2 = run_pipeline(db_path, hist_dir, symbols=["MES"], dry_run=False)
            assert results2["MES"]["bt"]["written"] == 0, "Re-run must not add rows"

        # ── run_out_of_sample_eval: only ever touches OOS-range data ──────────────
        with tempfile.TemporaryDirectory() as tmp2:
            tmp2_p    = Path(tmp2)
            hist2_dir = tmp2_p / "history"
            hist2_dir.mkdir()
            db2_path  = tmp2_p / "galao.db"
            init_db(db2_path)

            # 6 ready days -> train=3 ("08-01".."08-03"), validation=1 ("08-04"),
            # out_of_sample=2 ("08-05","08-06") at the default 60/20/20 fractions.
            dates = [f"2026-08-{d:02d}" for d in range(1, 7)]
            prices = [round(5525.0 + 30.0 * math.sin(i / 40.0), 2) for i in range(200)]
            with get_db(db2_path) as con:
                for d in dates:
                    con.execute("""
                        INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                        VALUES('MES',?,'SUPPORT',5500.0,1,1)
                    """, (d,))
                    con.execute("""
                        INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                        VALUES('MES',?,'RESISTANCE',5550.0,2,1)
                    """, (d,))
            for d in dates:
                compact = d.replace("-", "")
                base = datetime.fromisoformat(d).replace(hour=13, minute=30, tzinfo=UTC)
                with open(hist2_dir / f"MES_trades_{compact}.csv", "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time_utc", "price", "size"])
                    for i, p in enumerate(prices):
                        w.writerow([(base + timedelta(seconds=i*30)).isoformat(), p, 100])
                with open(hist2_dir / f"MES_bid_ask_{compact}.csv", "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time_utc", "bid_p", "bid_s", "ask_p", "ask_s"])
                    for i, p in enumerate(prices):
                        w.writerow([(base + timedelta(seconds=i*30)).isoformat(),
                                    p - 0.25, 10, p + 0.25, 10])

            ready2 = get_ready_days(db2_path, hist2_dir, symbols=["MES"])
            bounds2 = split_boundaries(ready2, "MES")
            assert bounds2["train"]         == ("2026-08-01", "2026-08-03"), bounds2
            assert bounds2["validation"]    == ("2026-08-04", "2026-08-04"), bounds2
            assert bounds2["out_of_sample"] == ("2026-08-05", "2026-08-06"), bounds2

            # No learner run yet -> honest "no_learner_run", not a fabricated number
            no_run = run_out_of_sample_eval(db2_path, hist2_dir, symbols=["MES"])
            assert no_run["MES"]["status"] == "no_learner_run", no_run["MES"]

            # Manually seed a CONVERGED learner recommendation + its backing combo score
            # (mirrors what a real train-split run converging would leave behind --
            # cl_algo_learner.py's own self-test seeds cl_algo_score_history the same way).
            with get_db(db2_path) as con:
                con.execute("""
                    INSERT INTO cl_algo_learner_runs
                        (run_at, symbol, iteration, recommended_tp_ticks, recommended_sl_ticks,
                         convergence_status, reasoning)
                    VALUES ('2026-08-04T00:00:00Z', 'MES', 5, '[4]', '[4]',
                            'converged', 'test fixture')
                """)
                con.execute("""
                    INSERT INTO cl_algo_combo_scores
                        (scored_at, symbol, algo_type, tp_ticks, sl_ticks,
                         direction_filter, strength_max, rank, data_status)
                    VALUES ('2026-08-04T00:00:00Z', 'MES', 'BOUNCE', 4, 4, 'ALL', 3, 1, 'ok')
                """)

            oos = run_out_of_sample_eval(db2_path, hist2_dir, symbols=["MES"], verbose=False)
            assert oos["MES"]["status"] == "ok", oos["MES"]
            assert oos["MES"]["combo"]["algo_type"] == "BOUNCE"
            assert oos["MES"]["oos_range"] == ("2026-08-05", "2026-08-06")
            assert oos["MES"]["n_sims"] > 0, "OOS eval should have simulated something"

            with get_db(db2_path) as con:
                touched = {r[0] for r in con.execute(
                    "SELECT DISTINCT date FROM cl_algo_sim_results WHERE symbol='MES'"
                    " AND split='out_of_sample'"
                ).fetchall()}
                leaked = con.execute(
                    "SELECT COUNT(*) FROM cl_algo_sim_results WHERE symbol='MES'"
                    " AND split='out_of_sample' AND (date < '2026-08-05' OR date > '2026-08-06')"
                ).fetchone()[0]
            assert touched, "OOS eval wrote no rows"
            assert touched <= {"2026-08-05", "2026-08-06"}, \
                f"OOS eval touched dates outside its range: {touched}"
            assert leaked == 0, f"OOS eval leaked {leaked} row(s) outside its date range"

        # bug 6: cfg.paths.cl_algo_history must resolve to a real dir with tick CSVs
        # (this is what __main__ actually reads -- the synthetic run above uses its
        # own tmp hist_dir and never exercises the config path).
        from lib.config_loader import get_config
        cfg = get_config()
        real_hist_dir = Path(cfg.paths.cl_algo_history)
        assert real_hist_dir.exists(), (
            f"cfg.paths.cl_algo_history does not exist: {real_hist_dir}"
        )
        assert any(real_hist_dir.glob("*.csv")), (
            f"cfg.paths.cl_algo_history has no *.csv files: {real_hist_dir}"
        )

        print("PASS -- pipeline: all 4 stages complete, idempotent re-run verified, "
              "split scoping verified (train never writes out_of_sample rows, "
              "OOS eval never touches non-OOS dates), "
              "cfg.paths.cl_algo_history resolves to real tick data")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CL Algo Pipeline")
    parser.add_argument("--symbol", nargs="*")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--verbose",  action="store_true")
    parser.add_argument("--split", default="train",
                         help="train|validation|out_of_sample (default: train)")
    parser.add_argument("--oos-eval", action="store_true",
                         help="Run the honest out-of-sample read instead of the "
                              "train/tune pipeline (uses the learner's last "
                              "converged recommendation).")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg      = get_config()
    db_path  = Path(cfg.paths.db)
    # bug 6: real tick CSVs live at cfg.paths.cl_algo_history, NOT db_path.parent/"history"
    # (which doesn't exist -- that path silently resolved to zero ready days).
    hist_dir = Path(cfg.paths.cl_algo_history)

    if args.oos_eval:
        results = run_out_of_sample_eval(
            db_path=db_path, history_dir=hist_dir,
            symbols=args.symbol, verbose=args.verbose,
        )
        for sym, r in results.items():
            print(f"[{sym}] {r}")
    else:
        results = run_pipeline(
            db_path    = db_path,
            history_dir = hist_dir,
            symbols    = args.symbol,
            dry_run    = args.dry_run,
            verbose    = args.verbose,
            split      = args.split,
        )
