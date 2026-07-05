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
from lib.data_availability import get_ready_days, summarise


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
                 verbose: bool = False) -> dict:
    """
    Full pipeline: availability → half-duplex backtest + full-duplex → score → learn.
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

        # Stage 2a: half-duplex backtest (Cartesian combos)
        _print(f"[{sym}] Stage 2a/5 — Half-duplex backtester (combo matrix)...")
        bt_result = bt.run(
            db_path=db_path,
            history_dir=history_dir,
            symbols=[sym],
            dry_run=dry_run,
            verbose=verbose,
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
        )
        _print(f"[{sym}] Full-duplex: written={fd_result.get('written',0)}"
               f" skipped={fd_result.get('skipped',0)}"
               f" errors={fd_result.get('errors',0)}"
               f" elapsed={fd_result.get('elapsed_s','?')}s")

        # Stage 3: score (half-duplex combos)
        _print(f"[{sym}] Stage 3/5 — Scoring combos...")
        score_result = sc.score(db_path, sym, verbose=verbose)
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
                WHERE symbol=? AND entry_fill_price IS NOT NULL
            """, (sym,)).fetchone()
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
            "bt":              bt_result,
            "fd":              fd_result,
            "score":           score_result,
            "learn":           learn_result,
        }

    _print("Pipeline complete.")
    return all_results


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

            # Seed critical lines
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                    VALUES('MES','2026-06-30','SUPPORT',5500.0,1,1)
                """)
                con.execute("""
                    INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)
                    VALUES('MES','2026-06-30','RESISTANCE',5550.0,2,1)
                """)

            # Write 200-row synthetic CSVs
            base = datetime(2026, 6, 30, 13, 30, 0, tzinfo=UTC)
            prices = [round(5525.0 + 30.0 * math.sin(i / 40.0), 2) for i in range(200)]

            t_path = hist_dir / "MES_trades_20260630.csv"
            b_path = hist_dir / "MES_bid_ask_20260630.csv"

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

            # Run full pipeline (NOT dry-run — we want real writes for the integration test)
            results = run_pipeline(db_path, hist_dir, symbols=["MES"], dry_run=False)

            assert "MES" in results, f"MES missing from results: {results}"
            mes = results["MES"]
            assert mes["bt"]["written"] > 0,        "Backtest should have written rows"
            assert mes["score"]["n_combos"] > 0,    "Scorer should have scored combos"
            assert mes["learn"]["iteration"] >= 1,  "Learner should have run"

            # Re-run: no new backtest rows (idempotent)
            results2 = run_pipeline(db_path, hist_dir, symbols=["MES"], dry_run=False)
            assert results2["MES"]["bt"]["written"] == 0, "Re-run must not add rows"

        print("PASS -- pipeline: all 4 stages complete, idempotent re-run verified")
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
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg      = get_config()
    db_path  = Path(cfg.paths.db)
    hist_dir = db_path.parent / "history"

    results = run_pipeline(
        db_path    = db_path,
        history_dir = hist_dir,
        symbols    = args.symbol,
        dry_run    = args.dry_run,
        verbose    = args.verbose,
    )
