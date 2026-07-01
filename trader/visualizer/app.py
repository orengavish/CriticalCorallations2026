"""
visualizer/app.py
Flask web app for Galao monitoring.

Routes:
  GET /            -> dashboard
  GET /active      -> active orders (PENDING/SUBMITTING/SUBMITTED/FILLED)
  GET /positions   -> open positions with unrealized P&L
  GET /orders      -> all commands
  GET /ib-trace    -> IB events log
  GET /logs        -> log file viewer
  GET /preflight   -> last preflight results
  GET /release-notes -> release notes

  API (JSON):
  GET /api/price           -> {price, age_s, stale}
  GET /api/session-state   -> {session}
  GET /api/stats           -> status counts + unrealized_pnl
  GET /api/nearby          -> {price, lines: [{line, commands}]}
  GET /api/commands        -> commands (?status_in=A,B&status=X&symbol=Y&limit=N)
  GET /api/positions       -> open positions
  GET /api/ib-events       -> IB events (?limit=N)
  GET /api/preflight       -> last preflight results from system_state
  GET /api/logs            -> log tail (?component=broker&level=ERROR&limit=N)
  GET /api/release-notes   -> release notes (?program=X)
  GET /api/state           -> system_state rows

Usage:
    python visualizer/app.py                # run server
    python visualizer/app.py --self-test    # headless test

Self-test:
    python visualizer/app.py --self-test
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

_HERE = Path(__file__).parent
_TRADER = _HERE.parent          # trader/visualizer -> trader
_ROOT = _TRADER.parent          # trader -> galgo2026
for _p in (str(_ROOT), str(_TRADER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import re
from flask import Flask, jsonify, request, render_template
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db

log = get_logger("visualizer")

app = Flask(__name__, template_folder="templates", static_folder="static")
_cfg = None
_db_path = None
_fetch_proc = None        # tracked fetcher process
_fetch_rate_state = {}    # "SYM_DTYPE" -> {ts, records} for server-side rate


def _get_cfg():
    global _cfg
    if _cfg is None:
        _cfg = get_config()
    return _cfg


def _get_db_path():
    global _db_path
    if _db_path is None:
        _db_path = Path(_get_cfg().paths.db)
    return _db_path


def _rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ── Price API ─────────────────────────────────────────────────────────────────

@app.route("/api/price")
def api_price():
    try:
        from visualizer.price_feed import get_latest
        price, ts = get_latest()
    except Exception:
        price, ts = None, None

    if price is None:
        return jsonify({"price": None, "age_s": None, "stale": True})

    now = datetime.now(timezone.utc)
    age_s = int((now - ts).total_seconds()) if ts else None
    stale = age_s is not None and age_s > 30
    return jsonify({"price": price, "age_s": age_s, "stale": stale})


@app.route("/api/session-state")
def api_session_state():
    try:
        with get_db(_get_db_path()) as con:
            row = con.execute(
                "SELECT value FROM system_state WHERE key='SESSION'"
            ).fetchone()
        return jsonify({"session": row["value"] if row else None})
    except Exception:
        return jsonify({"session": None})


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db(_get_db_path()) as con:
        status_rows = con.execute(
            "SELECT status, COUNT(*) as cnt FROM commands GROUP BY status"
        ).fetchall()
        closed_today = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
            " AND date(updated_at)=?", (today,)
        ).fetchone()[0]
        errors = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status IN ('ERROR','RECONCILE_REQUIRED')"
        ).fetchone()[0]

        # Unrealized P&L — read from FILLED commands (positions table not populated)
        filled_cmds = _rows_to_list(con.execute(
            "SELECT direction, fill_price, quantity FROM commands WHERE status='FILLED'"
        ).fetchall())

        # Realized P&L today from completed_trades
        pnl_today_rows = _rows_to_list(con.execute(
            "SELECT pnl_points FROM completed_trades WHERE date(exit_time)=?", (today,)
        ).fetchall())

        # Replenish stats
        rp_rows = con.execute(
            "SELECT status, COUNT(*) as cnt FROM commands"
            " WHERE parent_command_id IS NOT NULL GROUP BY status"
        ).fetchall()
        rp_total = con.execute(
            "SELECT COUNT(*) FROM commands WHERE parent_command_id IS NOT NULL"
        ).fetchone()[0]
        rp_closed_pnl = con.execute(
            "SELECT SUM(ct.pnl_points) FROM completed_trades ct"
            " JOIN commands c ON c.id = ct.command_id"
            " WHERE c.parent_command_id IS NOT NULL"
        ).fetchone()[0]

    counts = {r["status"]: r["cnt"] for r in status_rows}
    counts["CLOSED"] = closed_today
    counts["ERROR"]  = errors

    # ── Unrealized P&L ────────────────────────────────────────────────────
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    unrealized = None
    if price and filled_cmds:
        unrealized = sum(
            (price - c["fill_price"]) * c["quantity"] if c["direction"] == "BUY"
            else (c["fill_price"] - price) * c["quantity"]
            for c in filled_cmds if c["fill_price"] is not None
        )
    counts["unrealized_pnl"] = round(unrealized, 2) if unrealized is not None else None

    # ── Realized P&L today breakdown ──────────────────────────────────────
    wins   = [r["pnl_points"] for r in pnl_today_rows if r["pnl_points"] > 0]
    losses = [r["pnl_points"] for r in pnl_today_rows if r["pnl_points"] <= 0]
    counts["pnl_today"] = {
        "wins":       len(wins),
        "gains":      round(sum(wins), 2)   if wins   else 0.0,
        "losses":     len(losses),
        "loss_total": round(sum(losses), 2) if losses else 0.0,
        "net":        round(sum(r["pnl_points"] for r in pnl_today_rows), 2) if pnl_today_rows else 0.0,
    }

    # ── Replenish stats ───────────────────────────────────────────────────
    rp_by_status = {r["status"]: r["cnt"] for r in rp_rows}
    counts["replenish"] = {
        "total":     rp_total,
        "pending":   rp_by_status.get("PENDING", 0),
        "submitted": rp_by_status.get("SUBMITTED", 0) + rp_by_status.get("SUBMITTING", 0),
        "filled":    rp_by_status.get("FILLED", 0),
        "closed":    rp_by_status.get("CLOSED", 0),
        "error":     rp_by_status.get("ERROR", 0),
        "pnl":       round(rp_closed_pnl, 2) if rp_closed_pnl is not None else None,
    }

    return jsonify(counts)


# ── Nearby lines API ──────────────────────────────────────────────────────────

@app.route("/api/nearby")
def api_nearby():
    """Return the 8 critical lines nearest to current price, with attached commands."""
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db(_get_db_path()) as con:
        lines = _rows_to_list(con.execute(
            "SELECT * FROM critical_lines WHERE date=? AND armed=1 ORDER BY price",
            (today,)
        ).fetchall())

        active_cmds = _rows_to_list(con.execute(
            "SELECT * FROM commands WHERE status IN"
            " ('PENDING','SUBMITTING','SUBMITTED','FILLED')"
        ).fetchall())

    if not price or not lines:
        return jsonify({"price": price, "lines": []})

    # Sort by distance from price, take nearest 8
    lines.sort(key=lambda l: abs(l["price"] - price))
    nearest = lines[:8]

    # Attach commands to each line
    result = []
    for ln in nearest:
        cmds = [c for c in active_cmds if abs(c["line_price"] - ln["price"]) < 0.01]
        result.append({"line": ln, "commands": cmds})

    # Sort by price descending for display (resistance above, support below)
    result.sort(key=lambda x: x["line"]["price"], reverse=True)
    return jsonify({"price": price, "lines": result})


# ── Commands API ──────────────────────────────────────────────────────────────

@app.route("/api/commands")
def api_commands():
    status_in = request.args.get("status_in")   # "PENDING,SUBMITTED,FILLED"
    status    = request.args.get("status")       # single status (legacy)
    symbol    = request.args.get("symbol")
    limit     = int(request.args.get("limit", 200))

    query  = "SELECT * FROM commands WHERE 1=1"
    params = []
    if status_in:
        placeholders = ",".join("?" for _ in status_in.split(","))
        query += f" AND status IN ({placeholders})"
        params.extend(status_in.split(","))
    elif status:
        query += " AND status=?"
        params.append(status)
    if symbol:
        query += " AND symbol=?"
        params.append(symbol)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with get_db(_get_db_path()) as con:
        rows = con.execute(query, params).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Positions API ─────────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            """SELECT p.*,
                      c.tp_price, c.sl_price
               FROM positions p
               LEFT JOIN commands c ON c.id = p.command_id
               WHERE p.status='OPEN'
               ORDER BY p.id DESC"""
        ).fetchall()
    return jsonify(_rows_to_list(rows))


# ── IB Events API ─────────────────────────────────────────────────────────────

@app.route("/api/ib-events")
def api_ib_events():
    limit = int(request.args.get("limit", 500))
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT * FROM ib_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Preflight API ─────────────────────────────────────────────────────────────

@app.route("/api/preflight")
def api_preflight():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT * FROM system_state WHERE key LIKE 'preflight_%' ORDER BY key"
        ).fetchall()
    results = {}
    for row in rows:
        parts = row["value"].split("|", 2)
        results[row["key"]] = {
            "status": parts[0] if len(parts) > 0 else "",
            "time":   parts[1] if len(parts) > 1 else "",
            "detail": parts[2] if len(parts) > 2 else "",
        }
    return jsonify(results)


# ── Logs API ──────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    component = request.args.get("component", "broker")
    level     = request.args.get("level", "").upper()
    limit     = int(request.args.get("limit", 500))

    cfg = _get_cfg()
    log_path = Path(cfg.paths.logs) / f"{component}.log"
    if not log_path.exists():
        return jsonify({"lines": [], "error": f"Not found: {log_path.name}"})

    with open(log_path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    if level:
        all_lines = [l for l in all_lines if f"| {level}" in l]

    lines = all_lines[-limit:]
    return jsonify({"component": component, "level": level,
                    "count": len(lines), "lines": lines})


# ── Release Notes API ─────────────────────────────────────────────────────────

@app.route("/api/release-notes")
def api_release_notes():
    program = request.args.get("program")
    query   = "SELECT * FROM release_notes WHERE 1=1"
    params  = []
    if program:
        query += " AND program=?"
        params.append(program)
    query += " ORDER BY id DESC"
    with get_db(_get_db_path()) as con:
        rows = con.execute(query, params).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Generate API ─────────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    """
    POST /api/generate
    Body JSON:
      mode         str    — "random" (default) or "critical_line"
      bracket      float  — bracket size in points (default 8.0)
      types        list   — entry types ["MKT","LMT","STP"]
      count        int    — trades per line or total (default 10, max 200)
      max_offset   int    — max entry offset from anchor price in ticks (default 2)

      -- critical_line mode only --
      line_ids     list   — IDs from critical_lines table (DB-sourced, fully traceable)
      line_text    str    — free-form text to parse (e.g. "support: 7150!, 7140")

    Returns {count, price, commands: [...]}
    """
    import random as _rnd

    body       = request.get_json(force=True) or {}
    mode       = body.get("mode", "random")
    bracket    = float(body.get("bracket", 8.0))
    types      = [t.upper() for t in body.get("types", ["MKT", "LMT", "STP"])]
    count      = min(int(body.get("count", 10)), 200)
    max_offset = int(body.get("max_offset", 2))

    if not types:
        return jsonify({"error": "At least one entry type required"}), 400

    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    if price is None:
        return jsonify({"error": "No price available — price feed not running"}), 400

    cfg  = _get_cfg()
    tick = cfg.orders.tick_size
    gen_symbols = _fetch_symbols() or ["MES", "MNQ", "MYM", "M2K"]
    sym  = (cfg.symbols or gen_symbols)[0]  # for critical_line mode

    def rt(p):
        return round(round(p / tick) * tick, 10)

    def _make_cmd(anchor_price, direction, entry_type, source, symbol=None,
                  critical_line_id=None, line_type=None, strength=1):
        offset = _rnd.randint(1, max(1, max_offset)) * tick
        if entry_type == "MKT":
            ep = rt(anchor_price)
        elif entry_type == "LMT":
            ep = rt(anchor_price - offset) if direction == "BUY" else rt(anchor_price + offset)
        else:  # STP
            ep = rt(anchor_price + offset) if direction == "BUY" else rt(anchor_price - offset)
        tp = rt(ep + bracket) if direction == "BUY" else rt(ep - bracket)
        sl = rt(ep - bracket) if direction == "BUY" else rt(ep + bracket)
        return {
            "symbol":           symbol or sym,
            "line_price":       anchor_price,
            "line_type":        line_type or ("SUPPORT" if direction == "BUY" else "RESISTANCE"),
            "line_strength":    strength,
            "direction":        direction,
            "entry_type":       entry_type,
            "entry_price":      ep,
            "tp_price":         tp,
            "sl_price":         sl,
            "bracket_size":     bracket,
            "source":           source,
            "critical_line_id": critical_line_id,
            "quantity":         cfg.orders.quantity,
        }

    def _insert(trade):
        with get_db(_get_db_path()) as con:
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength,
                     direction, entry_type, entry_price, tp_price, sl_price,
                     bracket_size, source, critical_line_id, quantity, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                trade["symbol"], trade["line_price"], trade["line_type"], trade["line_strength"],
                trade["direction"], trade["entry_type"],
                trade["entry_price"], trade["tp_price"], trade["sl_price"],
                trade["bracket_size"], trade["source"], trade["critical_line_id"],
                trade["quantity"],
            ))
            return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    inserted = []

    if mode == "critical_line":
        # ── Resolve lines from DB IDs and/or parsed text ──────────────────────
        lines_to_use = []

        line_ids = body.get("line_ids", [])
        if line_ids:
            with get_db(_get_db_path()) as con:
                for lid in line_ids:
                    row = con.execute(
                        "SELECT * FROM critical_lines WHERE id=?", (lid,)
                    ).fetchone()
                    if row:
                        lines_to_use.append({
                            "id":        row["id"],
                            "price":     row["price"],
                            "line_type": row["line_type"],
                            "strength":  row["strength"],
                            "from_db":   True,
                        })

        line_text = body.get("line_text", "").strip()
        if line_text:
            parsed = _parse_lines_text(line_text)
            today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Save parsed lines to DB so they're traceable, then use their IDs
            with get_db(_get_db_path()) as con:
                for pl in parsed:
                    con.execute(
                        "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                        " VALUES (?, ?, ?, ?, ?, 1)",
                        (sym, today, pl["line_type"], pl["price"], pl["strength"])
                    )
                    new_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                    lines_to_use.append({
                        "id":        new_id,
                        "price":     pl["price"],
                        "line_type": pl["line_type"],
                        "strength":  pl["strength"],
                        "from_db":   False,
                    })

        if not lines_to_use:
            return jsonify({"error": "No critical lines found — select DB lines or paste line text"}), 400

        # Generate `count` trades per line; direction fixed by line_type
        for ln in lines_to_use:
            direction = "BUY" if ln["line_type"] == "SUPPORT" else "SELL"
            for _ in range(count):
                entry_type = _rnd.choice(types)
                trade = _make_cmd(
                    anchor_price     = ln["price"],
                    direction        = direction,
                    entry_type       = entry_type,
                    source           = "critical_line",
                    critical_line_id = ln["id"],
                    line_type        = ln["line_type"],
                    strength         = ln["strength"],
                )
                cmd_id = _insert(trade)
                inserted.append({
                    "id": cmd_id, "direction": direction, "entry_type": entry_type,
                    "entry_price": trade["entry_price"],
                    "critical_line_id": ln["id"], "line_price": ln["price"],
                })

        log.info(f"[generate] critical_line: {len(inserted)} cmds across {len(lines_to_use)} lines "
                 f"bracket={bracket}")

    else:
        # ── Random mode — generate `count` commands per symbol ────────────────
        for s in gen_symbols:
            for _ in range(count):
                entry_type = _rnd.choice(types)
                direction  = _rnd.choice(["BUY", "SELL"])
                trade = _make_cmd(
                    anchor_price = price,
                    direction    = direction,
                    entry_type   = entry_type,
                    source       = f"random_{entry_type.lower()}",
                    symbol       = s,
                )
                cmd_id = _insert(trade)
                inserted.append({
                    "id": cmd_id, "symbol": s, "direction": direction,
                    "entry_type": entry_type, "entry_price": trade["entry_price"],
                })

        log.info(f"[generate] random: {len(inserted)} cmds ({len(gen_symbols)} syms × {count}) "
                 f"near price={price} bracket={bracket}")

    return jsonify({"count": len(inserted), "price": price, "commands": inserted})


# ── Test Trades API ───────────────────────────────────────────────────────────

@app.route("/api/test-trades", methods=["POST"])
def api_test_trades():
    """Insert ~10 test PENDING commands near current price for end-to-end testing."""
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    if price is None:
        return jsonify({"error": "No price available — price feed not running"}), 400

    tick    = 0.25
    bracket = 2.0
    symbol  = _get_cfg().symbols[0] if _get_cfg().symbols else "MES"

    def rt(p):
        return round(round(p / tick) * tick, 10)

    # 10 test orders: MKT long+short, aggressive LMT, STP already-triggered, near LMT/STP
    specs = [
        # Market orders (fill immediately)
        ("BUY",  "MKT", rt(price),        rt(price + bracket),       rt(price - bracket)),
        ("SELL", "MKT", rt(price),        rt(price - bracket),       rt(price + bracket)),
        # Aggressive LMT (limit far past market → fills at market price)
        ("BUY",  "LMT", rt(price + 2.0),  rt(price + 2.0 + bracket), rt(price + 2.0 - bracket)),
        ("SELL", "LMT", rt(price - 2.0),  rt(price - 2.0 - bracket), rt(price - 2.0 + bracket)),
        # STP already past market (triggers immediately → MKT fill)
        ("BUY",  "STP", rt(price - tick), rt(price - tick + bracket), rt(price - tick - bracket)),
        ("SELL", "STP", rt(price + tick), rt(price + tick - bracket), rt(price + tick + bracket)),
        # Near LMT (just inside spread — fills quickly)
        ("BUY",  "LMT", rt(price + 0.5),  rt(price + 0.5 + bracket), rt(price + 0.5 - bracket)),
        ("SELL", "LMT", rt(price - 0.5),  rt(price - 0.5 - bracket), rt(price - 0.5 + bracket)),
        # Near STP (one tick move triggers)
        ("BUY",  "STP", rt(price + 0.5),  rt(price + 0.5 + bracket), rt(price + 0.5 - bracket)),
        ("SELL", "STP", rt(price - 0.5),  rt(price - 0.5 - bracket), rt(price - 0.5 + bracket)),
    ]

    inserted = []
    with get_db(_get_db_path()) as con:
        for direction, entry_type, entry_price, tp_price, sl_price in specs:
            line_type = "SUPPORT" if direction == "BUY" else "RESISTANCE"
            con.execute(
                "INSERT INTO commands"
                " (symbol, line_price, line_type, line_strength,"
                "  direction, entry_type, entry_price, tp_price, sl_price, bracket_size, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test')",
                (symbol, rt(price), line_type, 1,
                 direction, entry_type, entry_price, tp_price, sl_price, bracket)
            )
            cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            inserted.append({
                "id": cmd_id, "symbol": symbol,
                "direction": direction, "entry_type": entry_type,
                "entry_price": entry_price, "tp_price": tp_price, "sl_price": sl_price,
            })

    log.info(f"[test-trades] Inserted {len(inserted)} test commands near price={price}")
    return jsonify({"count": len(inserted), "price": price, "commands": inserted})


# ── IB Gateway Log API ────────────────────────────────────────────────────────

@app.route("/api/ib-gateway-log")
def api_ib_gateway_log():
    """
    GET /api/ib-gateway-log?limit=500
    Reads the most recent IB Gateway system log file from ib.gateway_log_dir.
    Returns {lines, file, error}.
    """
    limit = int(request.args.get("limit", 500))
    cfg   = _get_cfg()
    log_dir = getattr(cfg.ib, "gateway_log_dir", "") if hasattr(cfg, "ib") else ""

    if not log_dir:
        return jsonify({"lines": [], "file": None,
                        "error": "ib.gateway_log_dir not set in config.yaml"})

    log_dir_path = Path(log_dir)
    if not log_dir_path.exists():
        return jsonify({"lines": [], "file": None,
                        "error": f"Directory not found: {log_dir}"})

    # Find the most recent *.log or any log file in the dir (or subdirs by date)
    candidates = sorted(log_dir_path.glob("**/*.log"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    if not candidates:
        # Also try .txt files (IB Gateway sometimes uses different extensions)
        candidates = sorted(log_dir_path.glob("**/*.txt"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
    if not candidates:
        return jsonify({"lines": [], "file": None,
                        "error": f"No log files found in {log_dir}"})

    latest = candidates[0]
    try:
        with open(latest, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-limit:]
        return jsonify({"lines": lines, "file": str(latest), "error": None})
    except Exception as e:
        return jsonify({"lines": [], "file": str(latest), "error": str(e)})


# ── Replenish toggle API ──────────────────────────────────────────────────────

@app.route("/api/replenish", methods=["GET"])
def api_replenish_get():
    with get_db(_get_db_path()) as con:
        from lib.db import get_system_state
        val = get_system_state(con, "REPLENISH_ENABLED")
    return jsonify({"enabled": val == "1"})


@app.route("/api/replenish", methods=["POST"])
def api_replenish_set():
    body    = request.get_json(force=True) or {}
    enabled = bool(body.get("enabled", False))
    with get_db(_get_db_path()) as con:
        from lib.db import set_system_state
        set_system_state(con, "REPLENISH_ENABLED", "1" if enabled else "0")
    log.info(f"[replenish] {'ENABLED' if enabled else 'DISABLED'} via API")
    return jsonify({"enabled": enabled})


# ── System State API ──────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    with get_db(_get_db_path()) as con:
        rows = con.execute("SELECT * FROM system_state ORDER BY key").fetchall()
    return jsonify(_rows_to_list(rows))


# ── Lines input API ───────────────────────────────────────────────────────────

_STRENGTH_LABELS = {1: "strong (!)", 2: "medium (?)", 3: "weak (no suffix)"}

def _parse_lines_text(text: str) -> list[dict]:
    """
    Parse Hebrew/free-form critical lines input.

    Recognised section headers (case-insensitive, Hebrew or English):
      קווי תמיכה / support
      קווי התנגדות / resistance

    Per-price suffixes:
      !  → strength 1 (strong)
      ?  → strength 2 (medium)
      (none) → strength 3 (weak)

    Ranges  A - B  inside a comma-separated list add both endpoints separately.
    """
    results = []

    support_text    = ""
    resistance_text = ""

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if "תמיכה" in line or "support" in low:
            idx = line.find(":")
            if idx >= 0:
                support_text = line[idx + 1:].strip()
        elif "התנגדות" in line or "resistance" in low:
            idx = line.find(":")
            if idx >= 0:
                resistance_text = line[idx + 1:].strip()

    def _parse_section(raw: str, line_type: str):
        items = []
        raw = re.sub(r'\s*\([^)]*\)\s*', ' ', raw)  # strip (parenthetical comments)
        for token in raw.split(","):
            for sub in re.split(r'\s+-\s+', token):  # split on " - " zone separator
                sub = sub.strip()
                if not sub:
                    continue
                m = re.match(r'(\d+(?:\.\d+)?)(.*)', sub)
                if not m:
                    continue
                price_str, suffix = m.group(1), m.group(2)
                # * and ! both mean strong; ? means medium; bare = weak
                if '!' in suffix or '*' in suffix:
                    strength = 3
                elif '?' in suffix:
                    strength = 2
                else:
                    strength = 1
                try:
                    items.append({"line_type": line_type,
                                  "price": float(price_str), "strength": strength})
                except ValueError:
                    pass
        return items

    if support_text:
        results.extend(_parse_section(support_text, "SUPPORT"))
    if resistance_text:
        results.extend(_parse_section(resistance_text, "RESISTANCE"))

    return results


@app.route("/api/lines", methods=["GET", "POST"])
def api_lines():
    """
    GET  /api/lines?date=YYYY-MM-DD&symbol=MES
         Returns current critical_lines rows for that date+symbol.

    POST /api/lines
         Body JSON: {text, date, symbol}
         Parses the text and upserts into critical_lines.
         Returns {count, lines} on success or {error} on failure.
    """
    if request.method == "GET":
        date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        symbol   = request.args.get("symbol", (_get_cfg().symbols or ["MES"])[0])
        with get_db(_get_db_path()) as con:
            rows = _rows_to_list(con.execute(
                "SELECT * FROM critical_lines WHERE symbol=? AND date=? ORDER BY price",
                (symbol, date_str)
            ).fetchall())
        return jsonify(rows)

    # POST
    body   = request.get_json(force=True) or {}
    text   = body.get("text", "").strip()
    date_str = body.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    symbol   = body.get("symbol", (_get_cfg().symbols or ["MES"])[0])

    if not text:
        return jsonify({"error": "No text provided"}), 400

    lines = _parse_lines_text(text)
    if not lines:
        return jsonify({"error": "No valid lines found in input"}), 400
    if len(lines) > 20:
        return jsonify({"error": f"Too many lines ({len(lines)}), max is 20"}), 400

    with get_db(_get_db_path()) as con:
        con.execute(
            "DELETE FROM critical_lines WHERE symbol=? AND date=?",
            (symbol, date_str)
        )
        for ln in lines:
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (symbol, date_str, ln["line_type"], ln["price"], ln["strength"])
            )

    log.info(f"Lines input: saved {len(lines)} lines for {symbol} {date_str}")
    return jsonify({"count": len(lines), "lines": lines})


# ── Cancel-all / Flatten API ──────────────────────────────────────────────────

@app.route("/api/cancel-all", methods=["POST"])
def api_cancel_all():
    """
    POST /api/cancel-all
    Cancels all pending IB orders (reqGlobalCancel) and flattens all filled
    positions by placing reverse MKT orders via PAPER.
    Updates DB: PENDING/SUBMITTING/SUBMITTED → CANCELLED; FILLED → CLOSED.
    Does NOT delete any records.
    Returns {ib_cancel, ib_flatten, db_cancelled, db_flattened, errors}.
    """
    from lib.ib_client import IBClient
    from ib_insync import MarketOrder
    now    = datetime.now(timezone.utc).isoformat()
    result = {"ib_cancel": "skipped", "ib_flatten": 0,
              "db_cancelled": 0, "db_flattened": 0, "errors": []}
    ibc = None
    try:
        cfg = _get_cfg()
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=True)

        # Cancel all open IB orders on both connections
        for label, ib_conn in [("LIVE", ibc.live), ("PAPER", ibc.paper)]:
            if ib_conn and ib_conn.isConnected():
                try:
                    ib_conn.reqGlobalCancel()
                    log.info(f"[cancel-all] reqGlobalCancel → {label}")
                except Exception as e:
                    result["errors"].append(f"reqGlobalCancel {label}: {e}")
        result["ib_cancel"] = "ok"

        # Flatten open positions via PAPER
        if ibc.paper and ibc.paper.isConnected():
            try:
                ibc.paper.reqPositions()
                ibc.paper.sleep(1.5)
                for pos in ibc.paper.positions():
                    qty = pos.position
                    if qty == 0:
                        continue
                    action = "SELL" if qty > 0 else "BUY"
                    try:
                        ibc.place_order(pos.contract,
                                        MarketOrder(action, abs(qty)))
                        result["ib_flatten"] += 1
                        log.info(f"[cancel-all] flatten {pos.contract.symbol}"
                                 f" {qty:+} → {action} MKT")
                    except Exception as e:
                        result["errors"].append(
                            f"close {pos.contract.symbol}: {e}")
            except Exception as e:
                result["errors"].append(f"positions: {e}")

    except Exception as e:
        result["errors"].append(f"IB connect: {e}")
        result["ib_cancel"] = "failed"
        log.warning(f"[cancel-all] IB error: {e}")
    finally:
        if ibc:
            try:
                ibc.disconnect()
            except Exception:
                pass

    # DB: update statuses — no records deleted
    try:
        with get_db(_get_db_path()) as con:
            r1 = con.execute(
                "UPDATE commands SET status='CANCELLED', updated_at=?"
                " WHERE status IN ('PENDING','SUBMITTING','SUBMITTED')",
                (now,)
            )
            result["db_cancelled"] = r1.rowcount
            r2 = con.execute(
                "UPDATE commands SET status='CLOSED',"
                " exit_reason='manual_flatten', exit_time=?, updated_at=?"
                " WHERE status='FILLED'",
                (now, now)
            )
            result["db_flattened"] = r2.rowcount
    except Exception as e:
        result["errors"].append(f"DB: {e}")
        log.error(f"[cancel-all] DB update failed: {e}")

    log.info(f"[cancel-all] {result}")
    return jsonify(result)


# ── Reset API ─────────────────────────────────────────────────────────────────

_WIPE_TABLES = ("commands", "positions", "ib_events", "system_state")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """
    POST /api/reset
    Body JSON: {cancel_ib: bool, wipe_db: bool}
    Defaults: both true.

    1. Optionally call reqGlobalCancel on LIVE and PAPER.
    2. Optionally wipe operational DB tables (keeps critical_lines, release_notes).
    Returns {ib_cancel: "ok"|"failed"|"skipped", wipe: {table: count}, errors: [...]}
    """
    body      = request.get_json(force=True) or {}
    do_ib     = body.get("cancel_ib", True)
    do_wipe   = body.get("wipe_db",   True)

    result = {"ib_cancel": "skipped", "wipe": {}, "errors": []}

    # ── 1. IB cancel ──────────────────────────────────────────────────────────
    if do_ib:
        try:
            from lib.ib_client import IBClient
            cfg = _get_cfg()
            ibc = IBClient(cfg)
            ibc.connect(live=True, paper=True)
            try:
                ibc.live.reqGlobalCancel()
                log.info("[reset] reqGlobalCancel sent to LIVE")
            except Exception as e:
                result["errors"].append(f"LIVE cancel: {e}")
            try:
                ibc.paper.reqGlobalCancel()
                log.info("[reset] reqGlobalCancel sent to PAPER")
            except Exception as e:
                result["errors"].append(f"PAPER cancel: {e}")
            ibc.disconnect()
            result["ib_cancel"] = "ok"
        except Exception as e:
            result["ib_cancel"] = "failed"
            result["errors"].append(f"IB connect: {e}")
            log.warning(f"[reset] IB cancel failed: {e}")

    # ── 2. DB wipe ────────────────────────────────────────────────────────────
    if do_wipe:
        try:
            with get_db(_get_db_path()) as con:
                for tbl in _WIPE_TABLES:
                    cnt = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    con.execute(f"DELETE FROM {tbl}")
                    result["wipe"][tbl] = cnt
            log.info(f"[reset] DB wiped: {result['wipe']}")
        except Exception as e:
            result["errors"].append(f"DB wipe: {e}")
            log.error(f"[reset] DB wipe failed: {e}")

    return jsonify(result)


# ── Report API ────────────────────────────────────────────────────────────────

@app.route("/api/report")
def api_report():
    """
    GET /api/report?source=X&symbol=Y&limit=N
    Reads from completed_trades — guaranteed no duplicates, no missing data.
      trades       — list of completed trades (newest first)
      by_source    — per-source stats
      by_exit      — per-exit-reason stats
      summary      — overall totals
      equity_curve — [{time, pnl, cumulative}] sorted by exit_time
    """
    source  = request.args.get("source")
    symbol  = request.args.get("symbol")
    limit   = int(request.args.get("limit", 5000))

    query  = "SELECT * FROM completed_trades WHERE 1=1"
    params = []
    if source:
        query += " AND source=?"
        params.append(source)
    if symbol:
        query += " AND symbol=?"
        params.append(symbol)
    query += " ORDER BY exit_time ASC LIMIT ?"
    params.append(limit)

    with get_db(_get_db_path()) as con:
        rows = _rows_to_list(con.execute(query, params).fetchall())

    trades_with_pnl = rows  # completed_trades always has pnl_points (NOT NULL)

    # ── by_source ──
    src_map: dict = {}
    for r in rows:
        src = r["source"] or "unknown"
        if src not in src_map:
            src_map[src] = {"source": src, "count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        src_map[src]["count"] += 1
        pnl = r.get("pnl_points")
        if pnl is not None:
            src_map[src]["total_pnl"] += pnl
            if pnl > 0:
                src_map[src]["wins"] += 1
            else:
                src_map[src]["losses"] += 1
    for s in src_map.values():
        n = s["wins"] + s["losses"]
        s["win_rate"]  = round(100.0 * s["wins"] / n, 1) if n else None
        s["avg_pnl"]   = round(s["total_pnl"] / n, 4)    if n else None
        s["total_pnl"] = round(s["total_pnl"], 4)
    by_source = sorted(src_map.values(), key=lambda x: x["total_pnl"], reverse=True)

    # ── by_exit ──
    exit_map: dict = {}
    for r in rows:
        reason = r["exit_reason"] or "unknown"
        exit_map[reason] = exit_map.get(reason, 0) + 1
    by_exit = [{"reason": k, "count": v} for k, v in sorted(exit_map.items())]

    # ── summary ──
    pnl_vals = [r["pnl_points"] for r in trades_with_pnl]
    total_pnl = sum(pnl_vals) if pnl_vals else 0.0
    wins = sum(1 for p in pnl_vals if p > 0)
    summary = {
        "total_trades": len(rows),
        "trades_with_pnl": len(pnl_vals),
        "total_pnl":    round(total_pnl, 4),
        "win_rate":     round(100.0 * wins / len(pnl_vals), 1) if pnl_vals else None,
        "avg_pnl":      round(total_pnl / len(pnl_vals), 4)    if pnl_vals else None,
        "best_trade":   round(max(pnl_vals), 4)                 if pnl_vals else None,
        "worst_trade":  round(min(pnl_vals), 4)                 if pnl_vals else None,
    }

    # ── equity_curve ──
    equity_curve = []
    cumulative = 0.0
    for r in trades_with_pnl:
        cumulative += r["pnl_points"]
        equity_curve.append({
            "time":       r["exit_time"],
            "pnl":        round(r["pnl_points"], 4),
            "cumulative": round(cumulative, 4),
            "source":     r["source"] or "unknown",
        })

    return jsonify({
        "trades":       list(reversed(rows)),   # newest first for table display
        "by_source":    by_source,
        "by_exit":      by_exit,
        "summary":      summary,
        "equity_curve": equity_curve,
    })


# ── HTML routes ───────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/active")
def active_page():
    return render_template("active.html", active="active")


@app.route("/positions")
def positions_page():
    return render_template("positions.html", active="positions")


@app.route("/orders")
def orders_page():
    return render_template("orders.html", active="orders")


@app.route("/ib-trace")
def ib_trace_page():
    return render_template("ib_trace.html", active="ib_trace")


@app.route("/logs")
def logs_page():
    cfg = _get_cfg()
    log_dir = Path(cfg.paths.logs)
    components = sorted([p.stem for p in log_dir.glob("*.log")]) if log_dir.exists() else ["broker"]
    return render_template("logs.html", active="logs", components=components)


@app.route("/preflight")
def preflight_page():
    return render_template("preflight.html", active="preflight")


@app.route("/release-notes")
def release_notes_page():
    return render_template("release_notes.html", active="release_notes")


@app.route("/db")
def db_view_page():
    return render_template("db_view.html", active="db_view")


@app.route("/report")
def report_page():
    return render_template("report.html", active="report")


@app.route("/lines")
def lines_page():
    cfg = _get_cfg()
    return render_template("lines.html", active="lines",
                           symbols=cfg.symbols or ["MES"])


# ── Fetch Status ──────────────────────────────────────────────────────────────

_FETCH_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def _is_trading_day(d) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in _FETCH_HOLIDAYS


def _fetch_symbols():
    try:
        cfg = _get_cfg()
        override = getattr(getattr(cfg, "fetcher", None), "symbols_override", None)
        return list(override) if override else list(cfg.symbols)
    except Exception:
        return ["MES"]


@app.route("/api/fetch-status")
def api_fetch_status():
    """Fetch status grid from june project's fetch_progress.db + history CSVs."""
    import sqlite3 as _sq3
    from datetime import date as date_cls, datetime, timedelta, timezone

    today = date_cls.today()
    days = []
    d = today
    while len(days) < 45:
        if _is_trading_day(d):
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)

    symbols = _fetch_symbols() or ["MES", "MNQ", "MYM", "M2K"]

    # Build lookup from june fetch_progress.db
    lookup: dict = {}  # (sym, date, dtype) -> {status, rows, pct, rate_ks, age_s}
    june_db   = _june_fetch_db()
    hist_dir  = _june_history_dir()
    now_utc   = datetime.now(timezone.utc)

    # Scan CSV files in history dir for completed files
    csv_present: set = set()
    csv_rows: dict   = {}
    if hist_dir.exists():
        for f in hist_dir.glob("*.csv"):
            if f.stat().st_size < 100:
                continue
            parts = f.stem.split("_")
            if len(parts) >= 3:
                sym  = parts[0]
                ftyp = parts[1]      # "trades" or "bidask"
                dc   = parts[2]
                ds   = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                csv_present.add((sym, ds, ftyp))

    if june_db.exists():
        try:
            con = _sq3.connect(str(june_db))
            con.row_factory = _sq3.Row
            rows = con.execute(
                "SELECT symbol, date, data_type, records_fetched, finished, updated_at"
                " FROM fetch_progress"
            ).fetchall()
            con.close()
            for r in rows:
                sym   = r["symbol"]
                ds    = r["date"]
                dtype = r["data_type"].lower()   # TRADES -> trades
                rec   = r["records_fetched"] or 0
                done  = bool(r["finished"])
                age_s = None
                try:
                    upd = r["updated_at"]
                    if upd:
                        dt_obj = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                        if dt_obj.tzinfo is None:
                            dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                        age_s = round((now_utc - dt_obj).total_seconds())
                except Exception:
                    pass

                is_csv = (sym, ds, dtype) in csv_present
                if done or is_csv:
                    status = "ok"
                elif rec > 0 and age_s is not None and age_s < 90:
                    status = "active"
                elif rec > 0:
                    status = "stale"
                else:
                    status = "missing"

                key = (sym, ds, dtype)
                lookup[key] = {
                    "status": status,
                    "rows": rec,
                    "age_s": age_s,
                    "error": None,
                }
        except Exception as exc:
            log.warning("fetch-status DB error: %s", exc)

    # Also mark CSVs present but not in DB as ok
    for (sym, ds, dtype) in csv_present:
        if (sym, ds, dtype) not in lookup:
            lookup[(sym, ds, dtype)] = {"status": "ok", "rows": None, "age_s": None, "error": None}

    result: dict = {}
    for ds in days:
        result[ds] = {}
        for sym in symbols:
            result[ds][sym] = {
                "trades": lookup.get((sym, ds, "trades")),
                "bidask": lookup.get((sym, ds, "bidask")),
            }

    return jsonify({"days": days, "symbols": symbols, "grid": result})


@app.route("/api/fetch-now", methods=["POST"])
def api_fetch_now():
    import subprocess
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", "").upper()
    date_str = data.get("date", "")
    if not symbol or not date_str:
        return jsonify({"error": "symbol and date required"}), 400

    scheduler = Path(__file__).parent.parent / "fetch_scheduler.py"
    subprocess.Popen(
        [sys.executable, str(scheduler), "--symbol", symbol, "--date", date_str],
        cwd=str(Path(__file__).parent.parent)
    )
    return jsonify({"started": True, "symbol": symbol, "date": date_str})


def _june_fetch_db():
    return Path(__file__).parent.parent.parent / "june" / "trader" / "data" / "fetch_progress.db"


def _june_history_dir():
    return Path(__file__).parent.parent.parent / "june" / "trader" / "data" / "history"


@app.route("/api/fetch-live")
def api_fetch_live():
    """
    Per-slot live progress: track the most recently active row for each
    (symbol, data_type) so rate reflects the currently downloading date.
    """
    import sqlite3 as _sq3, time as _time
    from datetime import datetime, timezone

    symbols = _fetch_symbols() or ["MES", "MNQ", "MYM", "M2K"]
    dtypes  = ["TRADES", "BID_ASK"]
    result  = {sym: {} for sym in symbols}

    june_db = _june_fetch_db()
    if not june_db.exists():
        for sym in symbols:
            for dtype in dtypes:
                result[sym][dtype] = {"status": "missing", "records": 0, "pct": None,
                                      "finished": False, "rate_ks": None, "age_s": None}
        return jsonify({"data": result, "symbols": symbols})

    now_mono = _time.monotonic()
    now_utc  = datetime.now(timezone.utc)
    _FALLBACK = {"TRADES": 400_000, "BID_ASK": 600_000}

    try:
        con = _sq3.connect(str(june_db))
        con.row_factory = _sq3.Row
        all_rows = con.execute(
            "SELECT symbol, date, data_type, records_fetched, finished, updated_at"
            " FROM fetch_progress"
        ).fetchall()
        con.close()

        def _age(upd):
            try:
                dt_obj = datetime.fromisoformat((upd or "").replace("Z", "+00:00"))
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                return round((now_utc - dt_obj).total_seconds())
            except Exception:
                return None

        # Group by (symbol, data_type)
        from collections import defaultdict
        by_slot = defaultdict(list)
        for r in all_rows:
            by_slot[(r["symbol"], r["data_type"])].append(r)

        for sym in symbols:
            for dtype in dtypes:
                rows = by_slot.get((sym, dtype), [])
                n_total = len(rows)
                n_done  = sum(1 for r in rows if r["finished"])

                # Find the most recently updated unfinished row (what's being fetched now)
                active_rows = [r for r in rows if not r["finished"]]
                active_rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
                active_row = active_rows[0] if active_rows else None

                # Per-date target from median of finished rows
                finished_counts = [r["records_fetched"] for r in rows
                                   if r["finished"] and r["records_fetched"] > 1000]
                if finished_counts:
                    finished_counts.sort()
                    target = finished_counts[len(finished_counts) // 2]
                else:
                    target = _FALLBACK.get(dtype, 400_000)

                if active_row:
                    rec   = active_row["records_fetched"] or 0
                    age_s = _age(active_row["updated_at"])
                    pct   = min(100, round(rec / target * 100)) if target > 0 else 0

                    # Rate: rolling 90-second history so bursts between polls are captured
                    key = f"{sym}_{dtype}"
                    if key not in _fetch_rate_state:
                        _fetch_rate_state[key] = []
                    hist = _fetch_rate_state[key]
                    if isinstance(hist, dict):          # migrate old format
                        hist = []
                        _fetch_rate_state[key] = hist
                    cur_date = active_row["date"]
                    # Reset history if fetcher moved to a new date
                    if hist and hist[-1].get("date") != cur_date:
                        hist.clear()
                    hist.append({"ts": now_mono, "records": rec, "date": cur_date})
                    # Keep last 90 seconds
                    cutoff = now_mono - 90
                    while len(hist) > 1 and hist[0]["ts"] < cutoff:
                        hist.pop(0)
                    rate_ks = None
                    if len(hist) >= 2:
                        dt_sec = hist[-1]["ts"] - hist[0]["ts"]
                        dr     = hist[-1]["records"] - hist[0]["records"]
                        if dt_sec > 1.0:
                            rate_ks = round(max(0.0, dr / dt_sec / 1000), 2)

                    is_active = age_s is not None and age_s < 90
                    status    = "active" if is_active else "stale"
                    result[sym][dtype] = {
                        "status":   status,
                        "records":  rec,
                        "pct":      pct,
                        "finished": False,
                        "dates":    f"{n_done}/{n_total}",
                        "cur_date": active_row["date"],
                        "rate_ks":  rate_ks,
                        "age_s":    age_s,
                    }
                elif n_done > 0:
                    result[sym][dtype] = {
                        "status":  "done",
                        "records": sum(r["records_fetched"] for r in rows),
                        "pct":     100,
                        "finished": True,
                        "dates":   f"{n_done}/{n_total}",
                        "rate_ks": None, "age_s": None,
                    }
                else:
                    result[sym][dtype] = {
                        "status": "missing", "records": 0, "pct": None,
                        "finished": False, "rate_ks": None, "age_s": None,
                    }

    except Exception as exc:
        log.warning("fetch-live error: %s", exc)

    for sym in symbols:
        for dtype in dtypes:
            if dtype not in result[sym]:
                result[sym][dtype] = {"status": "missing", "records": 0, "pct": None,
                                      "finished": False, "rate_ks": None, "age_s": None}

    return jsonify({"data": result, "symbols": symbols})


@app.route("/api/fetch-start", methods=["POST"])
def api_fetch_start():
    global _fetch_proc
    import subprocess as _sp
    if _fetch_proc and _fetch_proc.poll() is None:
        return jsonify({"status": "already_running", "pid": _fetch_proc.pid})
    june_trader = Path(__file__).parent.parent.parent / "june" / "trader"
    scheduler = june_trader / "fetch_scheduler.py"
    if not scheduler.exists():
        return jsonify({"error": "fetch_scheduler.py not found"}), 404
    _fetch_proc = _sp.Popen(
        [sys.executable, str(scheduler), "--backfill"],
        cwd=str(june_trader),
    )
    return jsonify({"status": "started", "pid": _fetch_proc.pid})


@app.route("/api/fetch-stop", methods=["POST"])
def api_fetch_stop():
    global _fetch_proc
    if _fetch_proc is None or _fetch_proc.poll() is not None:
        return jsonify({"status": "not_running"})
    _fetch_proc.terminate()
    try:
        _fetch_proc.wait(timeout=5)
    except Exception:
        _fetch_proc.kill()
    pid = _fetch_proc.pid
    _fetch_proc = None
    return jsonify({"status": "stopped", "pid": pid})


@app.route("/api/fetch-proc-status")
def api_fetch_proc_status():
    global _fetch_proc
    if _fetch_proc is None:
        return jsonify({"running": False})
    running = _fetch_proc.poll() is None
    return jsonify({"running": running, "pid": _fetch_proc.pid if running else None})


@app.route("/fetch-status")
def fetch_status_page():
    return render_template("fetch_status.html", active="fetch_status")


# ── Traceback ─────────────────────────────────────────────────────────────────

@app.route("/api/traceback-stats")
def api_traceback_stats():
    """
    Returns verification pipeline stats:
      - closed_commands: all CLOSED commands
      - recorded: rows in completed_trades
      - verified: rows passing verified_trades view filters
      - drop_reasons: breakdown of why trades were not verified
      - daily: per-day {date, closed, recorded, verified, rate}
      - recent_verified: last 50 verified trades (from verified_trades view)
    """
    with get_db(_get_db_path()) as con:
        closed = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
        ).fetchone()[0]

        recorded = con.execute(
            "SELECT COUNT(*) FROM completed_trades"
        ).fetchone()[0]

        verified = con.execute(
            "SELECT COUNT(*) FROM verified_trades"
        ).fetchone()[0]

        # ── Drop reason breakdown ──────────────────────────────────────────
        # Tier 1: CLOSED but not recorded (incomplete data from broker)
        incomplete = con.execute("""
            SELECT COUNT(*) FROM commands
            WHERE status='CLOSED'
            AND id NOT IN (SELECT command_id FROM completed_trades)
        """).fetchone()[0]

        # Tier 2: recorded but filtered by view
        shutdown_exits = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE exit_reason='SHUTDOWN'"
        ).fetchone()[0]

        instant_exits = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE fill_time=exit_time"
        ).fetchone()[0]

        null_source = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE source IS NULL"
        ).fetchone()[0]

        test_source = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE source='test'"
        ).fetchone()[0]

        drop_reasons = [
            {"reason": "INCOMPLETE_DATA",  "count": incomplete,
             "note": "CLOSED command missing fill/exit/pnl"},
            {"reason": "SHUTDOWN_EXIT",    "count": shutdown_exits,
             "note": "Position closed by session shutdown"},
            {"reason": "INSTANT_EXIT",     "count": instant_exits,
             "note": "fill_time = exit_time (reconnect artifact)"},
            {"reason": "NULL_SOURCE",      "count": null_source,
             "note": "No source tag on command"},
            {"reason": "TEST_SOURCE",      "count": test_source,
             "note": "Test command excluded by design"},
        ]

        # ── Per-day breakdown ──────────────────────────────────────────────
        daily_rows = _rows_to_list(con.execute("""
            SELECT
                date(exit_time) as day,
                COUNT(*) as recorded,
                SUM(CASE WHEN exit_reason != 'SHUTDOWN'
                         AND fill_time != exit_time
                         AND source IS NOT NULL
                         AND source != 'test'
                    THEN 1 ELSE 0 END) as verified
            FROM completed_trades
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
        """).fetchall())

        # ── Recent verified trades ─────────────────────────────────────────
        recent = _rows_to_list(con.execute("""
            SELECT command_id, symbol, direction, entry_type,
                   fill_price, fill_time, exit_price, exit_time,
                   exit_reason, pnl_points, bracket_size, source,
                   chain_depth
            FROM verified_trades
            ORDER BY fill_time DESC
            LIMIT 50
        """).fetchall())

    rate = round(100.0 * verified / closed, 1) if closed else None

    return jsonify({
        "closed":       closed,
        "recorded":     recorded,
        "verified":     verified,
        "rate":         rate,
        "drop_reasons": drop_reasons,
        "daily":        daily_rows,
        "recent":       recent,
    })


@app.route("/traceback")
def traceback_page():
    return render_template("traceback.html", active="traceback")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import init_db, set_system_state

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "broker.log").write_text(
                "2026-04-07T10:00:00 | INFO | broker | started\n"
                "2026-04-07T10:00:01 | ERROR | broker | test error\n"
            )

            init_db(db_path)

            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
                con.execute(
                    "INSERT INTO release_notes (program, version, summary)"
                    " VALUES ('test_prog', '0.1.0', 'Test note')"
                )
                con.execute(
                    "INSERT INTO system_state (key, value) VALUES"
                    " ('preflight_db', 'PASS|2026-04-07T10:00:00Z|')"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                con.execute(
                    "INSERT INTO ib_events (event_type, component, message)"
                    " VALUES ('ERROR', 'broker', 'test IB error')"
                )
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength)"
                    " VALUES ('MES', '2026-04-07', 'SUPPORT', 5500.0, 1)"
                )

            global _db_path, _cfg
            _db_path = db_path

            # Patch config paths for log dir
            class _FakePaths:
                db   = str(db_path)
                logs = str(log_dir)
            class _FakeCfg:
                paths = _FakePaths()
            _cfg = _FakeCfg()

            app.config["TESTING"] = True
            client = app.test_client()

            routes_200 = [
                "/",
                "/active",
                "/positions",
                "/orders",
                "/ib-trace",
                "/preflight",
                "/release-notes",
                "/api/price",
                "/api/session-state",
                "/api/stats",
                "/api/nearby",
                "/api/commands",
                "/api/commands?status_in=PENDING,SUBMITTED",
                "/api/positions",
                "/api/ib-events",
                "/api/preflight",
                "/api/logs?component=broker",
                "/api/logs?component=broker&level=ERROR",
                "/api/release-notes",
                "/api/state",
            ]

            for route in routes_200:
                resp = client.get(route)
                assert resp.status_code == 200, \
                    f"Route {route}: expected 200, got {resp.status_code}"

            # /logs page needs components list — test separately
            resp = client.get("/logs")
            assert resp.status_code == 200, f"/logs failed"

            # Validate shapes
            pf = client.get("/api/preflight").get_json()
            assert "preflight_db" in pf
            assert pf["preflight_db"]["status"] == "PASS"

            rn = client.get("/api/release-notes").get_json()
            assert isinstance(rn, list) and len(rn) >= 1
            assert rn[0]["program"] == "test_prog"

            stats = client.get("/api/stats").get_json()
            assert isinstance(stats, dict)

            cmds_all = client.get("/api/commands").get_json()
            assert isinstance(cmds_all, list)

            logs_data = client.get("/api/logs?component=broker").get_json()
            assert isinstance(logs_data["lines"], list)
            assert len(logs_data["lines"]) == 2

            logs_err = client.get("/api/logs?component=broker&level=ERROR").get_json()
            assert len(logs_err["lines"]) == 1, \
                f"Expected 1 ERROR line, got {len(logs_err['lines'])}"

            ib = client.get("/api/ib-events").get_json()
            assert isinstance(ib, list) and len(ib) >= 1
            assert ib[0]["event_type"] == "ERROR"

            _db_path = None
            _cfg = None
            reset_loggers()

        print("[self-test] visualizer: PASS")
        return True

    except Exception as e:
        print(f"[self-test] visualizer: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


def _cmdline_running(script_path: str) -> bool:
    """Return True if a python process with script_path in its command line is running."""
    import subprocess as _sp
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            try:
                if any(script_path in (c or "") for c in (p.info["cmdline"] or [])):
                    return True
            except Exception:
                pass
        return False
    except ImportError:
        pass
    try:
        r = _sp.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            capture_output=True, text=True, timeout=8,
        )
        return script_path in (r.stdout or "")
    except Exception:
        return False


def _auto_start_bg():
    """Start this project's broker + decider if not already running."""
    import subprocess as _sp
    trader_dir = Path(__file__).parent.parent  # C:\Projects\Galgo2026\trader

    for script in ("broker.py", "decider.py"):
        path = trader_dir / script
        if not path.exists():
            log.warning("auto-start: %s not found", path)
            continue
        if _cmdline_running(str(path)):
            log.info("auto-start: %s already running", script)
            continue
        try:
            _sp.Popen(
                [sys.executable, str(path)],
                cwd=str(trader_dir),
                creationflags=_sp.CREATE_NEW_CONSOLE,
            )
            log.info("auto-start: launched %s", script)
        except Exception as exc:
            log.warning("auto-start: could not launch %s: %s", script, exc)


def _auto_start_fetcher():
    """Start backfill fetcher if not already running. Tracks process in _fetch_proc."""
    global _fetch_proc
    import subprocess as _sp
    june_trader = Path(__file__).parent.parent.parent / "june" / "trader"
    scheduler   = june_trader / "fetch_scheduler.py"
    if not scheduler.exists():
        log.warning("auto-start fetcher: fetch_scheduler.py not found")
        return

    # Already running (tracked process or external)
    if _fetch_proc and _fetch_proc.poll() is None:
        log.info("auto-start fetcher: already tracked (pid %s)", _fetch_proc.pid)
        return
    if _cmdline_running(str(scheduler)):
        log.info("auto-start fetcher: already running (external process)")
        return

    try:
        _fetch_proc = _sp.Popen(
            [sys.executable, str(scheduler), "--backfill"],
            cwd=str(june_trader),
        )
        log.info("auto-start fetcher: launched --backfill (pid %s)", _fetch_proc.pid)
    except Exception as exc:
        log.warning("auto-start fetcher: could not launch: %s", exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao visualizer")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-price-feed", action="store_true",
                        help="Skip IB price feed thread (useful for testing)")
    parser.add_argument("--no-auto-start", action="store_true",
                        help="Skip auto-starting broker/decider")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    init_db(Path(cfg.paths.db))

    if not args.no_auto_start:
        _auto_start_bg()
        _auto_start_fetcher()

    if not args.no_price_feed:
        import atexit
        from visualizer.price_feed import start as start_price_feed, stop as stop_price_feed
        symbol = cfg.symbols[0] if cfg.symbols else "MES"
        start_price_feed(cfg, symbol=symbol, interval=5)
        atexit.register(stop_price_feed)
        log.info(f"Price feed started for {symbol}")

    log.info(f"Visualizer starting on {cfg.visualizer.host}:{cfg.visualizer.port}")
    app.run(host=cfg.visualizer.host, port=cfg.visualizer.port, debug=False)
