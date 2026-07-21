"""
lib/algo_pnl.py
P&L breakdown by source algo and exact parameter combo.

Reads lib.db's `verified_trades` view (already a clean, arithmetic-consistent,
lineage-aware view over completed_trades JOIN commands) and groups it by:
  - symbol
  - source            (algo_lab | geva_extract | critical_line | trading_dashboard
                        | cl_algo | random_mkt/lmt/stp | test | ...)
  - algo_type         (trade strategy, set only when source='algo_lab':
                        BOUNCE|BREAKOUT|DIRECTIONAL|FADE|BOTH)
  - params_json       (full param combo, set only when source='algo_lab')
  - line_detect_algo  (the ORIGINATING critical_line's own algo_type -- which
                        S/R-detection method produced the line this trade came
                        from: ohlc|pivot|overnight|orb|vwap|volume|round|manual|D)

That last dimension is what lets "half-baked" S/R lines be judged on real P&L
instead of guesswork -- e.g. "pivot-detected lines traded with BOUNCE/tp8-sl4
are +$340 over 42 trades; overnight-range lines with the same strategy are
-$120" is a concrete, actionable signal.

Paper trading only -- this module is read-only, it never writes to `commands`.

Usage:
    from lib.algo_pnl import get_breakdown, rollup_by_source
    rows = get_breakdown(db_path)
    summary = rollup_by_source(rows)

Self-test:
    python -m lib.algo_pnl --self-test
"""

import sys
import json
import argparse

# CME official contract multipliers ($ per full point). Tick value = multiplier * tick_size.
SYMBOL_MULTIPLIERS = {"MES": 5.0, "MNQ": 2.0, "MYM": 0.5, "M2K": 5.0}


def get_breakdown(db_path, date_from: str = None, date_to: str = None) -> list:
    """
    Returns one dict per (symbol, source, algo_type, params_json, line_detect_algo)
    group, sorted by total_pnl_dollars descending. Each dict:
        symbol, source, algo_type, params (parsed dict or None), params_json,
        line_detect_algo, line_detect_source,
        n_trades, wins, losses, win_rate,
        total_pnl_points, avg_pnl_points,
        total_pnl_dollars, avg_pnl_dollars, profit_factor
    """
    from lib.db import get_db

    with get_db(db_path) as con:
        q = "SELECT * FROM verified_trades WHERE 1=1"
        params: list = []
        if date_from:
            q += " AND exit_time >= ?"
            params.append(date_from)
        if date_to:
            q += " AND exit_time <= ?"
            params.append(date_to if "T" in date_to else date_to + "T23:59:59Z")
        rows = [dict(r) for r in con.execute(q, params).fetchall()]

        line_ids = {r["root_critical_line_id"] for r in rows
                   if r["root_critical_line_id"] is not None}
        line_info = {}
        if line_ids:
            ph = ",".join("?" * len(line_ids))
            for lr in con.execute(
                f"SELECT id, algo_type, source FROM critical_lines WHERE id IN ({ph})",
                list(line_ids)
            ).fetchall():
                line_info[lr["id"]] = {"algo_type": lr["algo_type"], "source": lr["source"]}

    groups: dict = {}
    for r in rows:
        li = line_info.get(r["root_critical_line_id"], {})
        key = (
            r["symbol"], r["source"] or "unknown",
            r["algo_type"] or "", r["params_json"] or "",
            li.get("algo_type") or "", li.get("source") or "",
        )
        g = groups.get(key)
        if g is None:
            g = {
                "symbol": r["symbol"], "source": r["source"] or "unknown",
                "algo_type": r["algo_type"],
                "params": json.loads(r["params_json"]) if r["params_json"] else None,
                "params_json": r["params_json"],
                "line_detect_algo": li.get("algo_type"),
                "line_detect_source": li.get("source"),
                "n_trades": 0, "wins": 0, "losses": 0,
                "total_pnl_points": 0.0, "total_pnl_dollars": 0.0,
                "gross_win_dollars": 0.0, "gross_loss_dollars": 0.0,
            }
            groups[key] = g

        mult = SYMBOL_MULTIPLIERS.get(r["symbol"], 1.0)
        dollars = r["pnl_points"] * mult
        g["n_trades"] += 1
        if r["pnl_points"] > 0:
            g["wins"] += 1
            g["gross_win_dollars"] += dollars
        elif r["pnl_points"] < 0:
            g["losses"] += 1
            g["gross_loss_dollars"] += -dollars
        g["total_pnl_points"]  += r["pnl_points"]
        g["total_pnl_dollars"] += dollars

    out = []
    for g in groups.values():
        n = g["n_trades"]
        g["win_rate"]        = g["wins"] / n if n else 0.0
        g["avg_pnl_points"]  = g["total_pnl_points"] / n if n else 0.0
        g["avg_pnl_dollars"] = g["total_pnl_dollars"] / n if n else 0.0
        if g["gross_loss_dollars"] > 0:
            g["profit_factor"] = g["gross_win_dollars"] / g["gross_loss_dollars"]
        else:
            g["profit_factor"] = float("inf") if g["gross_win_dollars"] > 0 else 0.0
        del g["gross_win_dollars"], g["gross_loss_dollars"]
        out.append(g)

    out.sort(key=lambda g: -g["total_pnl_dollars"])
    return out


def rollup_by_source(breakdown: list) -> list:
    """Collapse a get_breakdown() result up to one row per (symbol, source),
    ignoring algo_type/params/line-detection -- a coarse top-line summary."""
    groups: dict = {}
    for g in breakdown:
        key = (g["symbol"], g["source"])
        s = groups.get(key)
        if s is None:
            s = {"symbol": g["symbol"], "source": g["source"],
                 "n_trades": 0, "wins": 0, "losses": 0,
                 "total_pnl_points": 0.0, "total_pnl_dollars": 0.0}
            groups[key] = s
        s["n_trades"]          += g["n_trades"]
        s["wins"]              += g["wins"]
        s["losses"]            += g["losses"]
        s["total_pnl_points"]  += g["total_pnl_points"]
        s["total_pnl_dollars"] += g["total_pnl_dollars"]

    out = list(groups.values())
    for s in out:
        n = s["n_trades"]
        s["win_rate"] = s["wins"] / n if n else 0.0
    out.sort(key=lambda s: -s["total_pnl_dollars"])
    return out


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    from pathlib import Path
    try:
        from lib.db import init_db, get_db, update_command_status, record_completed_trade

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            with get_db(db_path) as con:
                # A pivot-detected critical line, traded twice by algo_lab/BOUNCE
                # with two different tp/sl combos -- one win, one loss.
                cur = con.execute(
                    "INSERT INTO critical_lines"
                    " (symbol, date, line_type, price, strength, armed, source, algo_type)"
                    " VALUES ('MES', '2026-04-07', 'SUPPORT', 6490.0, 2, 1, 'auto', 'pivot')"
                )
                line_id = cur.lastrowid

            def _insert_closed(symbol, direction, entry, tp, sl, exit_price,
                               source, algo_type, params_json, critical_line_id=None):
                with get_db(db_path) as con:
                    cur = con.execute("""
                        INSERT INTO commands
                            (symbol, line_price, line_type, line_strength,
                             direction, entry_type, entry_price, tp_price, sl_price,
                             bracket_size, source, algo_type, params_json, critical_line_id)
                        VALUES (?, 6490.0, 'SUPPORT', 2, ?, 'LMT', ?, ?, ?, 2.0, ?, ?, ?, ?)
                    """, (symbol, direction, entry, tp, sl, source, algo_type,
                          params_json, critical_line_id))
                    cmd_id = cur.lastrowid
                    update_command_status(
                        con, cmd_id, "CLOSED",
                        fill_price=entry, fill_time="2026-04-07T10:00:00Z",
                        exit_price=exit_price, exit_time="2026-04-07T10:05:00Z",
                        exit_reason="TP" if exit_price != sl else "SL",
                        pnl_points=round(exit_price - entry, 4)
                                   if direction == "BUY" else round(entry - exit_price, 4),
                    )
                    record_completed_trade(con, cmd_id)
                return cmd_id

            # Win: BOUNCE tp8/sl4 on the pivot line, BUY, +2.0 pts
            _insert_closed("MES", "BUY", 6490.0, 6492.0, 6489.0, 6492.0,
                           "algo_lab", "BOUNCE", '{"algo_type":"BOUNCE","tp_ticks":8,"sl_ticks":4}',
                           critical_line_id=line_id)
            # Loss: BOUNCE tp4/sl8 on the same pivot line, BUY, -2.0 pts
            _insert_closed("MES", "BUY", 6490.0, 6491.0, 6488.0, 6488.0,
                           "algo_lab", "BOUNCE", '{"algo_type":"BOUNCE","tp_ticks":4,"sl_ticks":8}',
                           critical_line_id=line_id)
            # A geva_extract (manual FB) trade, no algo_type/params, +1.0 pt, MNQ
            _insert_closed("MNQ", "BUY", 19800.0, 19801.0, 19799.0, 19801.0,
                           "geva_extract", None, None)

            breakdown = get_breakdown(db_path)
            assert len(breakdown) == 3, f"Expected 3 groups, got {len(breakdown)}"

            win_group = next(g for g in breakdown
                             if g["source"] == "algo_lab" and g["params"]["tp_ticks"] == 8)
            assert win_group["n_trades"] == 1
            assert win_group["wins"] == 1 and win_group["losses"] == 0
            assert win_group["total_pnl_points"] == 2.0
            assert win_group["total_pnl_dollars"] == 2.0 * SYMBOL_MULTIPLIERS["MES"]
            assert win_group["line_detect_algo"] == "pivot"
            assert win_group["line_detect_source"] == "auto"

            loss_group = next(g for g in breakdown
                              if g["source"] == "algo_lab" and g["params"]["tp_ticks"] == 4)
            assert loss_group["total_pnl_points"] == -2.0
            assert loss_group["win_rate"] == 0.0

            geva_group = next(g for g in breakdown if g["source"] == "geva_extract")
            assert geva_group["algo_type"] is None
            assert geva_group["params"] is None
            assert geva_group["total_pnl_dollars"] == 1.0 * SYMBOL_MULTIPLIERS["MNQ"]

            # rollup_by_source collapses the two algo_lab groups into one
            summary = rollup_by_source(breakdown)
            assert len(summary) == 2, f"Expected 2 source rows, got {len(summary)}"
            algo_lab_summary = next(s for s in summary if s["source"] == "algo_lab")
            assert algo_lab_summary["n_trades"] == 2
            assert algo_lab_summary["total_pnl_points"] == 0.0  # +2 and -2 cancel out

            # date_from/date_to filtering
            future_only = get_breakdown(db_path, date_from="2099-01-01")
            assert future_only == [], "date_from filter should exclude all 2026 trades"

        print("[self-test] algo_pnl: PASS")
        return True

    except Exception as e:
        print(f"[self-test] algo_pnl: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("algo_pnl — run --self-test to verify logic")
