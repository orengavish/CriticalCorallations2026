"""
trader/scripts/run_research_lines_daily.py
Daily automation for the two-winning-reasons research experiment (2026-09-10).

Previously prep_research_lines.py (futures) and prep_research_lines_stocks.py (stocks)
were both run by hand each morning -- in practice only the stocks one reliably got run,
so futures algorithm 1/2 classification silently went stale most days. This runs both,
for today, and is meant to be driven by a scheduled task (register_research_lines_task.ps1)
each morning before the market opens rather than run manually.

Usage:
    python trader/scripts/run_research_lines_daily.py              # today
    python trader/scripts/run_research_lines_daily.py --date 2026-09-10

Self-test:
    python trader/scripts/run_research_lines_daily.py --self-test
"""

import sys
import subprocess
import argparse
from pathlib import Path
from datetime import date as ddate

sys.path.insert(0, str(Path(__file__).parent))
import prep_research_lines as futures_mod
import prep_research_lines_stocks as stocks_mod

_MST_ROOT = Path(r"C:\Projects\MultiSymbolTrader")


def _ensure_mst_data(as_of: str) -> dict:
    """
    prep_research_lines_stocks.py's own prerequisite (see its docstring): MultiSymbolTrader
    must have fetched today's daily bars and computed its own morning lines BEFORE our
    stock script has anything to read. Discovered missing 2026-09-10 -- the stock script
    silently found "0 qualifying lines" for a whole trading day, not because nothing
    qualified, but because there was no data behind it at all. This runs that prerequisite
    itself so the daily automation doesn't depend on someone remembering to run it by hand.
    """
    steps = [
        ("fetch_bars", [sys.executable, "mst_data/daily_bars.py", "--fetch-all"]),
        ("morning_lines", [sys.executable, "scheduler/morning_lines.py", "--date", as_of]),
    ]
    result = {}
    for label, cmd in steps:
        try:
            proc = subprocess.run(cmd, cwd=str(_MST_ROOT), capture_output=True,
                                   text=True, timeout=1200)
            result[label] = {"ok": proc.returncode == 0, "returncode": proc.returncode,
                              "stdout_tail": proc.stdout[-500:], "stderr_tail": proc.stderr[-500:]}
            if proc.returncode != 0:
                print(f"[mst:{label}] FAILED (rc={proc.returncode}): {proc.stderr[-300:]}")
                break  # morning_lines depends on fetch_bars having worked -- no point continuing
            print(f"[mst:{label}] ok")
        except Exception as e:
            result[label] = {"ok": False, "returncode": None, "error": str(e)}
            print(f"[mst:{label}] FAILED -- {e}")
            break
    return result


def run(as_of: str, dry_run: bool = False, ensure_mst: bool = True) -> dict:
    result = {}

    if ensure_mst:
        result["mst_pipeline"] = _ensure_mst_data(as_of)

    for label, mod in (("futures", futures_mod), ("stocks", stocks_mod)):
        try:
            pairs = mod.find_qualifying_lines(as_of)
            n = mod.insert_pairs(pairs, dry_run=dry_run) if pairs else 0
            result[label] = {"qualifying_lines": len(pairs), "inserted": n, "error": None}
            print(f"[{label}] {as_of}: {len(pairs)} qualifying line(s), inserted {n} row(s)")
        except Exception as e:
            result[label] = {"qualifying_lines": 0, "inserted": 0, "error": str(e)}
            print(f"[{label}] {as_of}: FAILED -- {e}")
    return result


def self_test() -> bool:
    try:
        # Both underlying modules have their own self-tests covering the real logic --
        # this only needs to verify the two are actually wired together and that a
        # failure in one doesn't stop the other from running. ensure_mst=False -- the
        # real MultiSymbolTrader pipeline hits IB and takes minutes, not a self-test's job.
        result = run("2026-09-07", dry_run=True, ensure_mst=False)
        assert set(result.keys()) == {"futures", "stocks"}
        for label, r in result.items():
            assert r["error"] is None, f"{label} failed on a known-good fixture date: {r['error']}"

        class _BoomModule:
            @staticmethod
            def find_qualifying_lines(as_of):
                raise RuntimeError("boom")

        # Patch the CURRENT module object (sys.modules[__name__]), not a fresh import
        # under a different name -- when this file runs as __main__, importing it by
        # its own filename re-executes it as a second, separate module object whose
        # globals `run()` above never actually reads from.
        self_mod = sys.modules[__name__]
        original = self_mod.futures_mod
        self_mod.futures_mod = _BoomModule
        try:
            result2 = run("2026-09-07", dry_run=True, ensure_mst=False)
            assert result2["futures"]["error"] == "boom"
            assert result2["stocks"]["error"] is None, \
                "one module failing must not prevent the other from running"
        finally:
            self_mod.futures_mod = original

        # _ensure_mst_data must stop after fetch_bars fails -- morning_lines depends on
        # it, running it anyway would just compute lines from stale/missing bar data.
        calls = []
        def _fake_run(cmd, **kw):
            calls.append(cmd)
            import types
            return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
        original_run = subprocess.run
        subprocess.run = _fake_run
        try:
            mst_result = _ensure_mst_data("2026-09-07")
        finally:
            subprocess.run = original_run
        assert len(calls) == 1, f"must not call morning_lines after fetch_bars fails: {len(calls)} calls made"
        assert mst_result["fetch_bars"]["ok"] is False

        print("[self-test] run_research_lines_daily: PASS")
        return True
    except Exception as e:
        print(f"[self-test] run_research_lines_daily: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default: today")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)

    as_of = args.date or ddate.today().isoformat()
    result = run(as_of, dry_run=args.dry_run)
    sys.exit(1 if any(r["error"] for r in result.values()) else 0)
