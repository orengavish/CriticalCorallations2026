"""
lib/algo_lab.py
"Algo Lab" — submits many parameter combinations of the Claude-designed
critical-line strategies (lib.algo_engine: BOUNCE/BREAKOUT/DIRECTIONAL/FADE/BOTH,
asymmetric TP/SL) as paper trades in one batch, tagged so P&L can later be
broken down per exact combo (see lib.algo_pnl).

This module owns none of the trading logic itself -- it is a thin batch/grid
layer on top of lib.algo_engine:
  - build_param_grid(): cartesian product of configured params, deterministically
    capped so a big grid can't runaway-submit thousands of resting orders.
  - submit_grid(): for every (symbol x combo), generate PENDING commands for all
    armed critical_lines, skipping any (line, direction, combo) that already has
    an unresolved command in flight (same dedup pattern as trader/decider.py's
    2026-07-17 425-stale-order fix, just keyed on the exact param combo too so
    different combos on the same line don't block each other).

Every row this module inserts is tagged source='algo_lab' -- paper trading only,
this module never talks to IB, it only ever writes PENDING rows to `commands`.
trader/broker.py is source-agnostic and picks them up from whatever paper
gateway is configured (port 4002).

Usage:
    from lib.algo_lab import build_param_grid, submit_grid
    combos = build_param_grid(cfg.algo_lab)
    result = submit_grid(["MES", "MNQ"], date_str,
                          current_prices={"MES": 6500.0, "MNQ": 19800.0},
                          tick_sizes={"MES": 0.25, "MNQ": 0.25},
                          combos=combos, db_path=db_path)

Self-test:
    python -m lib.algo_lab --self-test
"""

import sys
import json
import argparse
from itertools import product

from lib.algo_engine import AlgoParams, _build_cmds, preview_cl_commands


def build_param_grid(algo_lab_cfg, max_combos: int = None) -> list:
    """
    Cartesian product of algo_lab_cfg.{strategies,tp_ticks,sl_ticks,
    direction_filters,strength_max}. If the full grid exceeds the cap
    (max_combos, or algo_lab_cfg.max_param_combos if not given), deterministically
    downsample so repeated calls return the same subset -- otherwise "algo X
    params Y" would mean a different random subset of commands each run, making
    the P&L-by-params breakdown meaningless over time.
    Returns a list of plain JSON-serializable dicts (not AlgoParams instances).
    """
    strategies  = list(algo_lab_cfg.strategies)
    tp_grid     = list(algo_lab_cfg.tp_ticks)
    sl_grid     = list(algo_lab_cfg.sl_ticks)
    dir_filters = list(algo_lab_cfg.direction_filters)
    strengths   = list(algo_lab_cfg.strength_max)
    cap = max_combos if max_combos is not None else algo_lab_cfg.max_param_combos

    all_combos = [
        {"algo_type": a, "tp_ticks": tp, "sl_ticks": sl,
         "direction_filter": df, "strength_max": sm}
        for a, tp, sl, df, sm in product(strategies, tp_grid, sl_grid, dir_filters, strengths)
    ]

    if cap is None or len(all_combos) <= cap:
        return all_combos

    all_combos.sort(key=combo_params_json)
    step = len(all_combos) / cap
    picked, i = [], 0.0
    while len(picked) < cap and int(i) < len(all_combos):
        picked.append(all_combos[int(i)])
        i += step
    return picked


def combo_params_json(combo: dict) -> str:
    """Stable JSON key for a param combo -- used for sampling order and as the
    exact value stored in commands.params_json for later GROUP BY attribution."""
    return json.dumps(combo, sort_keys=True)


def _in_flight_combo_keys(con, symbol: str) -> set:
    """
    (critical_line_id, direction, algo_type, tp_ticks, sl_ticks) tuples that
    already have an unresolved algo_lab command for this symbol.
    """
    rows = con.execute(
        "SELECT critical_line_id, direction, algo_type, params_json FROM commands"
        " WHERE symbol=? AND source='algo_lab'"
        " AND status IN ('PENDING','SUBMITTING','SUBMITTED')",
        (symbol,)
    ).fetchall()
    keys = set()
    for r in rows:
        try:
            p = json.loads(r["params_json"]) if r["params_json"] else {}
        except (TypeError, ValueError):
            p = {}
        keys.add((r["critical_line_id"], r["direction"], r["algo_type"],
                  p.get("tp_ticks"), p.get("sl_ticks")))
    return keys


def preview_grid(symbols: list, date_str: str, current_prices: dict,
                 tick_sizes: dict, combos: list, db_path) -> dict:
    """
    Non-destructive dry-run count of what submit_grid() would generate --
    a fast upper-bound estimate (does NOT apply the in-flight dedup check,
    so the real submit may insert fewer than this if some combos are already
    resting from a previous submit).
    """
    per_symbol = {}
    total = 0
    for symbol in symbols:
        price = current_prices.get(symbol)
        tick  = tick_sizes.get(symbol, 0.25)
        if price is None:
            per_symbol[symbol] = {"error": "no current price for symbol"}
            continue

        per_symbol[symbol] = {}
        for combo in combos:
            params = AlgoParams(
                algo_type=combo["algo_type"], tp_ticks=combo["tp_ticks"],
                sl_ticks=combo["sl_ticks"], direction_filter=combo["direction_filter"],
                strength_max=combo["strength_max"],
            )
            n = preview_cl_commands(symbol, date_str, price, params, db_path, tick)
            per_symbol[symbol][combo_params_json(combo)] = n
            total += n

    return {"per_symbol": per_symbol, "total_estimate": total}


def submit_grid(symbols: list, date_str: str, current_prices: dict,
                tick_sizes: dict, combos: list, db_path,
                quantity: int = 1, max_commands_total: int = None) -> dict:
    """
    For every (symbol x combo), insert PENDING commands for all armed
    critical_lines, tagged source='algo_lab'. Skips any (line, direction, combo)
    already in flight. Respects an optional hard cap on total commands inserted
    in this one call (safety valve against a misconfigured huge grid).

    Returns:
        {"per_symbol": {symbol: {combo_json: n_inserted, ...}, ...},
         "total_submitted": int, "capped": bool}
    """
    from lib.db import get_db

    per_symbol = {}
    total = 0
    capped = False

    for symbol in symbols:
        price = current_prices.get(symbol)
        tick  = tick_sizes.get(symbol, 0.25)
        if price is None:
            per_symbol[symbol] = {"error": "no current price for symbol"}
            continue

        with get_db(db_path) as con:
            in_flight = _in_flight_combo_keys(con, symbol)
            lines = [dict(r) for r in con.execute(
                "SELECT * FROM critical_lines WHERE symbol=? AND date=? AND armed=1",
                (symbol, date_str)
            ).fetchall()]

        per_symbol[symbol] = {}
        for combo in combos:
            if max_commands_total is not None and total >= max_commands_total:
                capped = True
                break

            params = AlgoParams(
                algo_type=combo["algo_type"], tp_ticks=combo["tp_ticks"],
                sl_ticks=combo["sl_ticks"], direction_filter=combo["direction_filter"],
                strength_max=combo["strength_max"],
            )
            pjson = combo_params_json(combo)
            n_inserted = 0

            for line in lines:
                cmds = _build_cmds(line, params, price, tick)
                if not cmds:
                    continue
                with get_db(db_path) as con:
                    for cmd in cmds:
                        key = (line["id"], cmd["direction"], combo["algo_type"],
                               combo["tp_ticks"], combo["sl_ticks"])
                        if key in in_flight:
                            continue
                        con.execute("""
                            INSERT INTO commands
                                (symbol, line_price, line_type, line_strength,
                                 direction, entry_type, entry_price, tp_price, sl_price,
                                 bracket_size, source, critical_line_id, algo_type,
                                 params_json, quantity, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'algo_lab', ?, ?, ?, ?, 'PENDING')
                        """, (
                            symbol, cmd["line_price"], cmd["line_type"], cmd["line_strength"],
                            cmd["direction"], cmd["entry_type"],
                            cmd["entry_price"], cmd["tp_price"], cmd["sl_price"],
                            cmd["bracket_size"], line["id"], combo["algo_type"],
                            pjson, quantity,
                        ))
                        in_flight.add(key)
                        n_inserted += 1
                        total += 1
                        if max_commands_total is not None and total >= max_commands_total:
                            capped = True
                            break
                if max_commands_total is not None and total >= max_commands_total:
                    break

            per_symbol[symbol][pjson] = n_inserted
            if capped:
                break

    return {"per_symbol": per_symbol, "total_submitted": total, "capped": capped}


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    from pathlib import Path
    try:
        from lib.db import init_db, get_db
        from types import SimpleNamespace

        # 1. build_param_grid: full grid, no cap
        cfg = SimpleNamespace(
            strategies=["BOUNCE", "FADE"], tp_ticks=[4, 8], sl_ticks=[4],
            direction_filters=["ALL"], strength_max=[3],
            max_param_combos=100,
        )
        grid = build_param_grid(cfg)
        assert len(grid) == 4, f"Expected 4 combos (2x2x1x1x1), got {len(grid)}"

        # 2. build_param_grid: capped, deterministic across repeated calls
        cfg_big = SimpleNamespace(
            strategies=["BOUNCE", "BREAKOUT", "DIRECTIONAL", "FADE", "BOTH"],
            tp_ticks=[2, 4, 6, 8, 12], sl_ticks=[2, 4, 6, 8, 12],
            direction_filters=["ALL", "BUY_ONLY", "SELL_ONLY"],
            strength_max=[1, 2, 3],
            max_param_combos=20,
        )
        grid_big1 = build_param_grid(cfg_big)
        grid_big2 = build_param_grid(cfg_big)
        assert len(grid_big1) == 20, f"Expected cap of 20, got {len(grid_big1)}"
        assert grid_big1 == grid_big2, "Sampling must be deterministic across calls"

        # 3. submit_grid: dedup + multi-combo + multi-symbol
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)
            today = "2026-04-07"
            with get_db(db_path) as con:
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                    " VALUES ('MES', ?, 'SUPPORT', 6490.0, 2, 1)", (today,)
                )
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                    " VALUES ('MYM', ?, 'RESISTANCE', 40500.0, 1, 1)", (today,)
                )

            combos = build_param_grid(cfg)  # 4 combos: BOUNCE/FADE x tp4/tp8

            # preview_grid gives the same count submit_grid would (nothing in
            # flight yet, so no dedup skips to cause a mismatch here)
            preview = preview_grid(
                ["MES", "MYM"], today,
                current_prices={"MES": 6500.0, "MYM": 40000.0},
                tick_sizes={"MES": 0.25, "MYM": 1.0},
                combos=combos, db_path=db_path,
            )
            assert preview["total_estimate"] == 8, \
                f"Expected preview estimate 8, got {preview['total_estimate']}"

            result = submit_grid(
                ["MES", "MYM"], today,
                current_prices={"MES": 6500.0, "MYM": 40000.0},
                tick_sizes={"MES": 0.25, "MYM": 1.0},
                combos=combos, db_path=db_path,
            )
            # BOUNCE + FADE each generate exactly 1 order per line (LMT bounce/fade)
            # x 2 tp values x 2 symbols (1 line each) = 8 commands total
            assert result["total_submitted"] == 8, \
                f"Expected 8 commands, got {result['total_submitted']}"
            assert not result["capped"]

            with get_db(db_path) as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM commands WHERE source='algo_lab'"
                ).fetchall()]
            assert len(rows) == 8
            assert all(r["algo_type"] in ("BOUNCE", "FADE") for r in rows)
            assert all(r["params_json"] for r in rows)
            # MYM tick=1.0 must have been used, not the 0.25 default
            mym_rows = [r for r in rows if r["symbol"] == "MYM"]
            assert all(r["tp_price"] % 1.0 == 0 for r in mym_rows), \
                "MYM prices should land on whole-point ticks"

            # 4. Re-submitting the same grid must not duplicate (dedup guard)
            result2 = submit_grid(
                ["MES", "MYM"], today,
                current_prices={"MES": 6500.0, "MYM": 40000.0},
                tick_sizes={"MES": 0.25, "MYM": 1.0},
                combos=combos, db_path=db_path,
            )
            assert result2["total_submitted"] == 0, \
                f"Expected 0 new (all in flight), got {result2['total_submitted']}"
            with get_db(db_path) as con:
                total_after = con.execute(
                    "SELECT COUNT(*) FROM commands WHERE source='algo_lab'"
                ).fetchone()[0]
            assert total_after == 8, f"Dedup guard failed, total={total_after}"

            # 5. max_commands_total safety valve
            with get_db(db_path) as con:
                con.execute("DELETE FROM commands WHERE source='algo_lab'")
            result3 = submit_grid(
                ["MES", "MYM"], today,
                current_prices={"MES": 6500.0, "MYM": 40000.0},
                tick_sizes={"MES": 0.25, "MYM": 1.0},
                combos=combos, db_path=db_path, max_commands_total=3,
            )
            assert result3["total_submitted"] == 3, \
                f"Expected cap at 3, got {result3['total_submitted']}"
            assert result3["capped"]

        print("[self-test] algo_lab: PASS")
        return True

    except Exception as e:
        print(f"[self-test] algo_lab: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("algo_lab — run --self-test to verify logic")
