"""
back-trading/trading_dashboard.py
Trading Dashboard — Flask on port 5003.

Tabs: Lines | Graph | Create Trades | Submitted
Accessible at http://0.0.0.0:5003  (LAN: http://192.168.1.132:5003)

Usage:
    python back-trading/trading_dashboard.py
    python back-trading/trading_dashboard.py --port 5003
"""

import sys
import csv
import json
import socket
import argparse
from pathlib import Path
from datetime import datetime, date, timezone, timedelta

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests
from flask import Flask, jsonify, request, render_template_string

from lib.db import get_db

# ── Constants ──────────────────────────────────────────────────────────────────

ALL_SYMBOLS      = ["MES", "MNQ", "MYM", "M2K"]
TICKS            = {"MES": 0.25, "MNQ": 0.25, "MYM": 1.0, "M2K": 0.10}
DEFAULT_BRACKETS = [2.0, 4.0, 10.0]   # points

_TRADER_URL = "http://127.0.0.1:5001"
_HIST_DIR   = _ROOT / "june" / "trader" / "data" / "history"

SOURCE_COLORS = {
    "ohlc":      "#4e79a7",
    "pivot":     "#f28e2b",
    "overnight": "#59a14f",
    "manual":    "#e15759",
}
SOURCE_LABELS = {
    "ohlc":      "OHLC",
    "pivot":     "Pivot",
    "overnight": "Overnight",
    "manual":    "Manual",
}

ALL_ALGO_TYPES = [
    "PDH", "PDL", "PDC", "PDO",
    "PIVOT_P", "PIVOT_R1", "PIVOT_S1", "PIVOT_R2", "PIVOT_S2", "PIVOT_R3", "PIVOT_S3",
    "OVERNIGHT_H", "OVERNIGHT_L", "MANUAL",
]
_ALGO_LABEL = {
    "PDH":         "Previous Day High",
    "PDL":         "Previous Day Low",
    "PDC":         "Previous Day Close",
    "PDO":         "Previous Day Open",
    "PIVOT_P":     "Pivot Point",
    "PIVOT_R1":    "Resistance 1",
    "PIVOT_S1":    "Support 1",
    "PIVOT_R2":    "Resistance 2",
    "PIVOT_S2":    "Support 2",
    "PIVOT_R3":    "Resistance 3",
    "PIVOT_S3":    "Support 3",
    "OVERNIGHT_H": "Overnight High",
    "OVERNIGHT_L": "Overnight Low",
    "MANUAL":      "Manual",
}

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

_DB_OVERRIDE: Path | None = None


def _resolve_db() -> Path:
    if _DB_OVERRIDE:
        return _DB_OVERRIDE
    cfg = _ROOT / "trader" / "config.yaml"
    if cfg.exists():
        try:
            import yaml
            with open(cfg) as f:
                d = yaml.safe_load(f)
            rel = d.get("paths", {}).get("db", "data/galao.db")
            return (cfg.parent / rel).resolve()
        except Exception:
            pass
    return (_ROOT / "trader" / "data" / "galao.db").resolve()


def _ensure_columns(db_path: Path) -> None:
    """Add source / algo_type / note to critical_lines if absent (idempotent)."""
    with get_db(db_path) as con:
        existing = {r[1] for r in con.execute("PRAGMA table_info(critical_lines)").fetchall()}
        if "source" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN source TEXT DEFAULT 'manual'")
        if "algo_type" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN algo_type TEXT DEFAULT 'MANUAL'")
        if "note" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN note TEXT")


# ── History helpers ────────────────────────────────────────────────────────────

def _prev_trading_day(from_date: date | None = None) -> date | None:
    d = from_date or date.today()
    for _ in range(20):
        d -= timedelta(days=1)
        if d.weekday() < 5:
            return d
    return None


_RTH_START_MIN, _RTH_END_MIN = 9 * 60 + 30, 16 * 60   # 09:30–16:00 CT

def _find_csv(symbol: str, d: date) -> Path | None:
    p = _HIST_DIR / f"{symbol}_trades_{d.strftime('%Y%m%d')}.csv"
    return p if p.exists() else None

def _csv_has_rth(symbol: str, d: date) -> bool:
    """Return True only if the CSV contains at least one tick inside RTH (09:30–16:00 CT)."""
    p = _find_csv(symbol, d)
    if not p:
        return False
    with open(p, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t_part = row["time_ct"].split("T")[1][:5]
                hh, mm = int(t_part[:2]), int(t_part[3:5])
                if _RTH_START_MIN <= hh * 60 + mm < _RTH_END_MIN:
                    return True
            except (ValueError, IndexError, KeyError):
                continue
    return False


def _load_ticks(symbol: str, d: date) -> list | None:
    """Return list of (minutes_from_midnight_ct, price, iso_str) or None."""
    p = _find_csv(symbol, d)
    if not p:
        return None
    rows = []
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            try:
                price = float(row["price"])
                tc    = row["time_ct"]
                t_part = tc.split("T")[1][:5]          # "HH:MM"
                hh, mm = int(t_part[:2]), int(t_part[3:5])
                rows.append((hh * 60 + mm, price, tc))
            except (ValueError, IndexError, KeyError):
                continue
    return rows or None


def _ohlcv_bars(ticks: list, interval_min: int = 5) -> list:
    bars: dict = {}
    for (t_min, price, iso) in ticks:
        date_part  = iso[:10]                                  # "YYYY-MM-DD" from the CT timestamp
        bucket_min = (t_min // interval_min) * interval_min
        key = (date_part, bucket_min)
        if key not in bars:
            bars[key] = {"date": date_part, "t_min": bucket_min, "iso": iso,
                         "open": price, "high": price, "low": price, "close": price, "vol": 0}
        b = bars[key]
        b["high"]  = max(b["high"], price)
        b["low"]   = min(b["low"],  price)
        b["close"] = price
        b["vol"]  += 1
    return sorted(bars.values(), key=lambda x: (x["date"], x["t_min"]))


# ── Line generation ────────────────────────────────────────────────────────────

def _generate_lines(symbol: str, ticks: list, filter_types: set | None = None) -> list[dict]:
    tick   = TICKS.get(symbol, 0.25)
    rt     = lambda p: round(round(p / tick) * tick, 10)

    RTH_START  = 9 * 60 + 30    # 09:30
    RTH_END    = 16 * 60         # 16:00
    GLOB_START = 17 * 60         # 17:00

    all_p   = [p for (_, p, _) in ticks]
    rth_p   = [p for (t, p, _) in ticks if RTH_START <= t < RTH_END]
    glob_p  = [p for (t, p, _) in ticks if t >= GLOB_START or t < RTH_START]

    if not all_p:
        return []

    H, L    = max(all_p), min(all_p)
    mid     = (H + L) / 2.0

    rth_open  = rth_p[0]  if rth_p else None
    rth_close = rth_p[-1] if rth_p else None
    glob_h    = max(glob_p) if glob_p else None
    glob_l    = min(glob_p) if glob_p else None

    lines = []

    def add(price, line_type, source, algo_type, strength, formula="", inputs=""):
        if filter_types is not None and algo_type not in filter_types:
            return
        lines.append({"price": rt(price), "line_type": line_type,
                      "source": source, "algo_type": algo_type, "strength": strength,
                      "_tip": {"formula": formula, "inputs": inputs}})

    ohlc_inp = (f"H={H:.2f}  L={L:.2f}"
                + (f"  O={rth_open:.2f}"  if rth_open  is not None else "")
                + (f"  C={rth_close:.2f}" if rth_close is not None else ""))

    # Full-session H / L
    add(H, "RESISTANCE", "ohlc", "PDH", 10,
        "max(all session prices)", ohlc_inp)
    add(L, "SUPPORT",    "ohlc", "PDL", 10,
        "min(all session prices)", ohlc_inp)

    # RTH close / open — classify by side of midpoint
    if rth_close is not None:
        add(rth_close,
            "RESISTANCE" if rth_close >= mid else "SUPPORT",
            "ohlc", "PDC", 9,
            f"last RTH price = {rth_close:.2f}", ohlc_inp)
    if rth_open is not None:
        add(rth_open,
            "RESISTANCE" if rth_open >= mid else "SUPPORT",
            "ohlc", "PDO", 8,
            f"first RTH price = {rth_open:.2f}", ohlc_inp)

    # Pivot points (use RTH H/L/C when available)
    ph = max(rth_p) if rth_p else H
    pl = min(rth_p) if rth_p else L
    pc = rth_close or all_p[-1]
    P  = (ph + pl + pc) / 3.0
    piv_inp = f"RTH H={ph:.2f}  L={pl:.2f}  C={pc:.2f}  P={P:.2f}"
    add(P,               "RESISTANCE", "pivot", "PIVOT_P",  8,
        f"(H+L+C)/3 = ({ph:.2f}+{pl:.2f}+{pc:.2f})/3 = {P:.2f}", piv_inp)
    add(2*P - pl,        "RESISTANCE", "pivot", "PIVOT_R1", 7,
        f"2xP - L = 2x{P:.2f} - {pl:.2f} = {2*P-pl:.2f}", piv_inp)
    add(2*P - ph,        "SUPPORT",    "pivot", "PIVOT_S1", 7,
        f"2xP - H = 2x{P:.2f} - {ph:.2f} = {2*P-ph:.2f}", piv_inp)
    add(P + (ph - pl),   "RESISTANCE", "pivot", "PIVOT_R2", 6,
        f"P + (H-L) = {P:.2f} + ({ph:.2f}-{pl:.2f}) = {P+(ph-pl):.2f}", piv_inp)
    add(P - (ph - pl),   "SUPPORT",    "pivot", "PIVOT_S2", 6,
        f"P - (H-L) = {P:.2f} - ({ph:.2f}-{pl:.2f}) = {P-(ph-pl):.2f}", piv_inp)
    add(ph + 2*(P - pl), "RESISTANCE", "pivot", "PIVOT_R3", 5,
        f"H + 2x(P-L) = {ph:.2f} + 2x({P:.2f}-{pl:.2f}) = {ph+2*(P-pl):.2f}", piv_inp)
    add(pl - 2*(ph - P), "SUPPORT",    "pivot", "PIVOT_S3", 5,
        f"L - 2x(H-P) = {pl:.2f} - 2x({ph:.2f}-{P:.2f}) = {pl-2*(ph-P):.2f}", piv_inp)

    # Overnight / Globex
    on_inp = ((f"GLX H={glob_h:.2f}" if glob_h is not None else "")
              + ("  " if glob_h is not None and glob_l is not None else "")
              + (f"L={glob_l:.2f}" if glob_l is not None else ""))
    if glob_h is not None:
        add(glob_h, "RESISTANCE", "overnight", "OVERNIGHT_H", 5,
            f"max(Globex 17:00-09:30 CT) = {glob_h:.2f}", on_inp)
    if glob_l is not None:
        add(glob_l, "SUPPORT",    "overnight", "OVERNIGHT_L", 5,
            f"min(Globex 17:00-09:30 CT) = {glob_l:.2f}", on_inp)

    # Deduplicate by tick bucket (keep highest-strength per bucket)
    seen: set = set()
    unique = []
    for ln in sorted(lines, key=lambda x: -x["strength"]):
        key = round(ln["price"] / tick)
        if key not in seen:
            seen.add(key)
            unique.append(ln)
    return unique


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/prices")
def api_prices():
    out = {}
    for sym in ALL_SYMBOLS:
        try:
            r = requests.get(f"{_TRADER_URL}/api/price", params={"symbol": sym}, timeout=1.5)
            out[sym] = r.json().get("price") if r.ok else None
        except Exception:
            out[sym] = None
    return jsonify(out)


@app.route("/api/lines/create", methods=["POST"])
def api_lines_create():
    body    = request.get_json(silent=True) or {}
    symbols         = body.get("symbols", ALL_SYMBOLS)
    algo_types      = set(body.get("algo_types", ALL_ALGO_TYPES))
    merge_threshold = float(body.get("merge_threshold", 16.0))
    hist_date_str   = body.get("history_date")
    hist_start      = date.fromisoformat(hist_date_str) if hist_date_str else date.today()
    today   = date.today().isoformat()
    db_path = _resolve_db()
    _ensure_columns(db_path)

    results: dict   = {}
    mock_date: str | None = None

    for sym in symbols:
        # Walk back up to 20 calendar days from hist_start to find history
        ticks, used_date = None, None
        search = hist_start + timedelta(days=1)
        for _ in range(20):
            search -= timedelta(days=1)
            if search.weekday() >= 5:
                continue
            if not _csv_has_rth(sym, search):
                continue
            t = _load_ticks(sym, search)
            if t:
                ticks, used_date = t, search
                break

        if not ticks or used_date is None:
            results[sym] = {"lines": 0, "from_date": None, "error": "no history CSV found"}
            continue

        if used_date != hist_start:
            mock_date = used_date.isoformat()

        raw_lines = _generate_lines(sym, ticks, filter_types=algo_types)

        # Apply merge threshold: sort by strength DESC; suppress lines within threshold of a stronger one
        kept = []
        for ln in sorted(raw_lines, key=lambda x: -x["strength"]):
            dominated = False
            for k in kept:
                if abs(k["price"] - ln["price"]) <= merge_threshold:
                    k.setdefault("merged", []).append({
                        "algo_type": ln["algo_type"],
                        "price":     ln["price"],
                        "strength":  ln["strength"],
                    })
                    dominated = True
                    break
            if not dominated:
                kept.append(ln)

        with get_db(db_path) as con:
            con.execute(
                "DELETE FROM critical_lines"
                " WHERE symbol=? AND date=? AND (source IS NULL OR source != 'manual')",
                (sym, today)
            )
            for ln in kept:
                tip = {
                    "label":     _ALGO_LABEL.get(ln["algo_type"], ln["algo_type"]),
                    "formula":   ln["_tip"]["formula"],
                    "inputs":    ln["_tip"]["inputs"],
                    "from_date": used_date.isoformat(),
                    "merged":    ln.get("merged", []),
                }
                con.execute(
                    "INSERT INTO critical_lines"
                    " (symbol, date, line_type, price, strength, armed, source, algo_type, note)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (sym, today, ln["line_type"], ln["price"],
                     ln["strength"], ln["source"], ln["algo_type"], json.dumps(tip))
                )

        results[sym] = {
            "lines":     len(kept),
            "from_date": used_date.isoformat(),
            "mock":      used_date.isoformat() if used_date != hist_start else None,
        }

    return jsonify({"results": results, "mock_date": mock_date, "today": today})


@app.route("/api/lines")
def api_lines():
    db_path      = _resolve_db()
    _ensure_columns(db_path)
    symbol       = request.args.get("symbol", "")
    min_strength = int(request.args.get("min_strength", 1))
    today        = date.today().isoformat()

    q_base = (
        "SELECT id, symbol, price, line_type, strength,"
        " COALESCE(source,'manual') AS source,"
        " COALESCE(algo_type,'MANUAL') AS algo_type,"
        " note, COALESCE(armed,1) AS armed"
        " FROM critical_lines WHERE date=? AND strength>=?"
    )
    with get_db(db_path) as con:
        if symbol:
            rows = con.execute(q_base + " AND symbol=? ORDER BY symbol, strength DESC, price",
                               (today, min_strength, symbol)).fetchall()
        else:
            rows = con.execute(q_base + " ORDER BY symbol, strength DESC, price",
                               (today, min_strength)).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/lines/manual", methods=["POST"])
def api_lines_manual():
    body     = request.get_json(silent=True) or {}
    symbol   = body.get("symbol", "MES")
    price    = float(body.get("price", 0))
    ltype    = body.get("line_type", "SUPPORT").upper()
    strength = int(body.get("strength", 8))
    today    = date.today().isoformat()
    db_path  = _resolve_db()
    _ensure_columns(db_path)

    with get_db(db_path) as con:
        cur = con.execute(
            "INSERT INTO critical_lines"
            " (symbol, date, line_type, price, strength, armed, source, algo_type)"
            " VALUES (?, ?, ?, ?, ?, 1, 'manual', 'MANUAL')",
            (symbol, today, ltype, price, strength)
        )
    return jsonify({"id": cur.lastrowid, "ok": True})


@app.route("/api/lines/<int:line_id>", methods=["PATCH"])
def api_lines_patch(line_id: int):
    body  = request.get_json(force=True) or {}
    armed = int(bool(body.get("armed", True)))
    with get_db(_resolve_db()) as con:
        con.execute("UPDATE critical_lines SET armed=? WHERE id=?", (armed, line_id))
    return jsonify({"ok": True})


@app.route("/api/lines/<int:line_id>", methods=["DELETE"])
def api_lines_delete(line_id: int):
    with get_db(_resolve_db()) as con:
        con.execute("DELETE FROM critical_lines WHERE id=?", (line_id,))
    return jsonify({"ok": True})


@app.route("/api/last_data_date")
def api_last_data_date():
    """Walk back from yesterday and return the most recent date any symbol has RTH data."""
    search = date.today()
    for _ in range(30):
        search -= timedelta(days=1)
        if search.weekday() >= 5:
            continue
        if any(_csv_has_rth(sym, search) for sym in ALL_SYMBOLS):
            return jsonify({"date": search.isoformat()})
    return jsonify({"date": None})


@app.route("/api/history/<symbol>")
def api_history(symbol: str):
    req_date_str = request.args.get("date")
    interval     = int(request.args.get("interval", 5))
    start        = date.fromisoformat(req_date_str) if req_date_str else (date.today() - timedelta(days=1))

    ticks, used_date = None, None
    search = start + timedelta(days=1)
    for _ in range(20):
        search -= timedelta(days=1)
        if search.weekday() >= 5:
            continue
        if not _csv_has_rth(symbol, search):
            continue
        t = _load_ticks(symbol, search)
        if t:
            ticks, used_date = t, search
            break

    if not ticks:
        return jsonify({"bars": [], "date": None, "symbol": symbol, "error": "no data"})

    rth_bars = [b for b in _ohlcv_bars(ticks, interval)
                if _RTH_START_MIN <= b["t_min"] < _RTH_END_MIN]
    bars = []
    for b in rth_bars:
        hh, mm = b["t_min"] // 60, b["t_min"] % 60
        bars.append({"t": f"{b['date']}T{hh:02d}:{mm:02d}:00",
                     "open": b["open"], "high": b["high"],
                     "low":  b["low"],  "close": b["close"], "vol": b["vol"]})

    mock = used_date.isoformat() if used_date != start else None
    return jsonify({"bars": bars, "date": used_date.isoformat(),
                    "symbol": symbol, "mock_date": mock})


@app.route("/api/trades/create", methods=["POST"])
def api_trades_create():
    body         = request.get_json(silent=True) or {}
    symbols      = body.get("symbols", ALL_SYMBOLS)
    brackets     = [float(b) for b in body.get("brackets", DEFAULT_BRACKETS)]
    min_strength = int(body.get("min_strength", 1))
    today        = date.today().isoformat()
    db_path      = _resolve_db()
    _ensure_columns(db_path)

    prices: dict = {}
    for sym in symbols:
        try:
            r = requests.get(f"{_TRADER_URL}/api/price", params={"symbol": sym}, timeout=1.5)
            prices[sym] = r.json().get("price") if r.ok else None
        except Exception:
            prices[sym] = None

    ph = ",".join("?" * len(symbols))
    with get_db(db_path) as con:
        lines = [dict(r) for r in con.execute(
            f"SELECT id, symbol, price, line_type, strength,"
            f" COALESCE(source,'manual') AS source,"
            f" COALESCE(algo_type,'MANUAL') AS algo_type"
            f" FROM critical_lines WHERE date=? AND symbol IN ({ph})"
            f" AND strength>=? AND armed=1 ORDER BY strength DESC",
            [today, *symbols, min_strength]
        ).fetchall()]

    candidates = []
    total_raw  = 0

    for ln in lines:
        sym, lp, ltype = ln["symbol"], ln["price"], ln["line_type"]
        strength, source, algo = ln["strength"], ln["source"], ln["algo_type"]
        tick = TICKS.get(sym, 0.25)
        live = prices.get(sym)
        rt   = lambda p: round(round(p / tick) * tick, 10)

        for bkt in brackets:
            if ltype == "SUPPORT":
                orders = [
                    ("BUY",  "LMT", rt(lp),         rt(lp + bkt),        rt(lp - tick)),
                    ("SELL", "STP", rt(lp - tick),   rt(lp - bkt - tick), rt(lp)),
                ]
            else:  # RESISTANCE
                orders = [
                    ("SELL", "LMT", rt(lp),         rt(lp - bkt),        rt(lp + tick)),
                    ("BUY",  "STP", rt(lp + tick),  rt(lp + bkt + tick), rt(lp)),
                ]

            for (direction, etype, entry, tp, sl) in orders:
                total_raw += 1
                if live is not None:
                    if etype == "LMT" and direction == "BUY"  and live <= entry: continue
                    if etype == "LMT" and direction == "SELL" and live >= entry: continue
                    if etype == "STP" and direction == "BUY"  and live >= entry: continue
                    if etype == "STP" and direction == "SELL" and live <= entry: continue

                candidates.append({
                    "symbol":      sym,
                    "direction":   direction,
                    "entry_type":  etype,
                    "entry_price": entry,
                    "tp_price":    tp,
                    "sl_price":    sl,
                    "bracket":     bkt,
                    "strength":    strength,
                    "algo_type":   algo,
                    "source":      source,
                    "line_type":   ltype,
                    "line_price":  lp,
                    "live_price":  live,
                    "prox":        abs(live - entry) if live is not None else 999,
                })

    candidates.sort(key=lambda c: (-c["strength"], c["prox"]))
    top = candidates[:200]

    return jsonify({
        "candidates":      top,
        "total":           total_raw,
        "passed":          len(candidates),
        "filtered":        total_raw - len(candidates),
        "symbols_covered": list({c["symbol"] for c in top}),
    })


@app.route("/api/trades/submit", methods=["POST"])
def api_trades_submit():
    body    = request.get_json(silent=True) or {}
    cands   = body.get("candidates", [])
    db_path = _resolve_db()
    now     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count   = 0

    with get_db(db_path) as con:
        for c in cands:
            con.execute(
                "INSERT INTO commands"
                " (symbol, line_price, line_type, line_strength, direction, entry_type,"
                "  entry_price, tp_price, sl_price, bracket_size, source, quantity,"
                "  status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'trading_dashboard', 1, 'PENDING', ?, ?)",
                (c["symbol"], c["line_price"], c["line_type"], c["strength"],
                 c["direction"], c["entry_type"], c["entry_price"],
                 c["tp_price"], c["sl_price"], c["bracket"], now, now)
            )
            count += 1

    return jsonify({"submitted": count})


@app.route("/api/submitted")
def api_submitted():
    with get_db(_resolve_db()) as con:
        rows = con.execute(
            "SELECT id, symbol, direction, entry_type, entry_price, tp_price, sl_price,"
            " bracket_size AS bracket, status, fill_price, updated_at"
            " FROM commands WHERE source='trading_dashboard' ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<title>Trading Dashboard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%23212529'/%3E%3Crect x='4' y='20' width='5' height='9' rx='1' fill='%23198754'/%3E%3Cline x1='6.5' y1='12' x2='6.5' y2='20' stroke='%23198754' stroke-width='1.5'/%3E%3Crect x='4' y='12' width='5' height='4' rx='1' fill='%23198754' opacity='.4'/%3E%3Crect x='13' y='8' width='5' height='21' rx='1' fill='%230d6efd'/%3E%3Cline x1='15.5' y1='4' x2='15.5' y2='8' stroke='%230d6efd' stroke-width='1.5'/%3E%3Crect x='13' y='4' width='5' height='5' rx='1' fill='%230d6efd' opacity='.4'/%3E%3Crect x='22' y='14' width='5' height='15' rx='1' fill='%23dc3545'/%3E%3Cline x1='24.5' y1='7' x2='24.5' y2='14' stroke='%23dc3545' stroke-width='1.5'/%3E%3Crect x='22' y='7' width='5' height='5' rx='1' fill='%23dc3545' opacity='.4'/%3E%3C/svg%3E">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
body{font-size:.85rem;}
.source-ohlc      {background:#4e79a7;color:#fff;}
.source-pivot     {background:#f28e2b;color:#fff;}
.source-overnight {background:#59a14f;color:#fff;}
.source-manual    {background:#e15759;color:#fff;}
.row-ohlc      {background:rgba(78,121,167,.12)!important;}
.row-pivot     {background:rgba(242,142,43,.12)!important;}
.row-overnight {background:rgba(89,161,79,.12)!important;}
.row-manual    {background:rgba(225,87,89,.12)!important;}
.price-chip{font-family:monospace;font-size:.8rem;padding:2px 8px;border-radius:4px;}
#mock-banner-graph{display:none;}
</style>
</head>
<body>

<nav class="navbar navbar-dark bg-dark border-bottom px-3 py-1">
  <span class="navbar-brand mb-0 fw-bold">Trading Dashboard</span>
  <div class="d-flex gap-2 align-items-center flex-wrap">
    <span class="price-chip bg-secondary" id="chip-MES">MES —</span>
    <span class="price-chip bg-secondary" id="chip-MNQ">MNQ —</span>
    <span class="price-chip bg-secondary" id="chip-MYM">MYM —</span>
    <span class="price-chip bg-secondary" id="chip-M2K">M2K —</span>
  </div>
  <span class="badge bg-info text-dark">:5003</span>
  <span class="badge bg-secondary">v1.7</span>
</nav>

<div class="container-fluid py-2">
<ul class="nav nav-tabs mb-2" id="mainTab" role="tablist">
  <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-lines">Lines</button></li>
  <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-graph" id="btn-graph-tab">Graph</button></li>
  <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-trades">Create Trades</button></li>
  <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-submitted" id="btn-sub-tab">Submitted</button></li>
  <li class="nav-item ms-auto d-flex align-items-center pe-1">
    <span class="badge bg-secondary">v1.7</span>
  </li>
</ul>
<div class="d-flex align-items-center gap-2 mb-2">
  <label class="small text-muted mb-0">Date</label>
  <input type="date" id="shared-date-input" class="form-control form-control-sm" style="width:148px"
         onchange="onSharedDateChange()">
</div>

<div class="tab-content">

<!-- ══════════════════════ LINES ══════════════════════ -->
<div class="tab-pane fade show active" id="tab-lines">
  <!-- Row 1: Symbols + merge threshold + Create -->
  <div class="d-flex flex-wrap gap-2 align-items-center mb-1">
    <span class="text-muted small">Symbols:</span>
    <div id="sym-lines" class="d-flex gap-2">
      <label class="small"><input class="form-check-input" type="checkbox" value="MES" checked> MES</label>
      <label class="small"><input class="form-check-input" type="checkbox" value="MNQ" checked> MNQ</label>
      <label class="small"><input class="form-check-input" type="checkbox" value="MYM" checked> MYM</label>
      <label class="small"><input class="form-check-input" type="checkbox" value="M2K" checked> M2K</label>
    </div>
    <span class="text-muted small ms-2">Merge ≤</span>
    <div class="d-flex gap-2">
      <label class="small"><input class="form-check-input" type="radio" name="merge-thr" value="4"> 4pt</label>
      <label class="small"><input class="form-check-input" type="radio" name="merge-thr" value="8"> 8pt</label>
      <label class="small"><input class="form-check-input" type="radio" name="merge-thr" value="16" checked> 16pt</label>
    </div>
    <button class="btn btn-sm btn-outline-info" onclick="loadLastDay()">Last Day</button>
    <button class="btn btn-sm btn-primary" onclick="createLines()">Create Lines</button>
    <span id="lines-msg" class="small text-muted ms-1"></span>
  </div>
  <!-- Row 2: Algo type checkboxes (all 14 preselected) -->
  <div class="d-flex flex-wrap gap-1 align-items-center mb-1 small border rounded px-2 py-1 bg-body-tertiary">
    <span class="fw-semibold text-muted me-1">Algos</span>
    <a href="#" class="text-muted" style="font-size:.75rem" onclick="setAllAlgos(true);return false">All</a>
    <a href="#" class="text-muted ms-1 me-2" style="font-size:.75rem" onclick="setAllAlgos(false);return false">Clear</a>
    <span class="vr me-2"></span>
    <span class="text-muted me-1">OHLC:</span>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PDH" checked> PDH</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PDL" checked> PDL</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PDC" checked> PDC</label>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="PDO" checked> PDO</label>
    <span class="vr me-2"></span>
    <span class="text-muted me-1">Pivot:</span>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_P" checked> P</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_R1" checked> R1</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_S1" checked> S1</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_R2" checked> R2</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_S2" checked> S2</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_R3" checked> R3</label>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="PIVOT_S3" checked> S3</label>
    <span class="vr me-2"></span>
    <span class="text-muted me-1">Overnight:</span>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="OVERNIGHT_H" checked> ONH</label>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="OVERNIGHT_L" checked> ONL</label>
    <span class="vr me-2"></span>
    <label><input class="form-check-input algo-chk" type="checkbox" value="MANUAL" checked> Manual</label>
  </div>
  <!-- Row 3: Strength filter + refresh -->
  <div class="d-flex gap-2 align-items-center mb-2">
    <label class="small">Strength ≥
      <input type="number" id="min-str-lines" class="form-control form-control-sm d-inline-block"
             style="width:55px" min="1" max="10" value="1" onchange="refreshLines()">
    </label>
    <button class="btn btn-sm btn-outline-secondary" onclick="refreshLines()">Refresh</button>
  </div>


  <table class="table table-sm table-hover table-bordered mb-1">
    <thead class="table-dark">
      <tr><th>ID</th><th>Sym</th><th>Price</th><th>Type</th><th>Algo</th><th>Str</th><th>Source</th><th></th></tr>
    </thead>
    <tbody id="lines-tbody"></tbody>
  </table>

  <hr class="my-2">
  <div class="d-flex gap-2 align-items-end flex-wrap">
    <div>
      <label class="form-label small mb-0">Symbol</label>
      <select class="form-select form-select-sm" id="m-sym">
        <option>MES</option><option>MNQ</option><option>MYM</option><option>M2K</option>
      </select>
    </div>
    <div>
      <label class="form-label small mb-0">Price</label>
      <input type="number" id="m-price" class="form-control form-control-sm" step="0.25" style="width:100px">
    </div>
    <div>
      <label class="form-label small mb-0">Type</label>
      <select class="form-select form-select-sm" id="m-type">
        <option value="SUPPORT">SUPPORT</option>
        <option value="RESISTANCE">RESISTANCE</option>
      </select>
    </div>
    <div>
      <label class="form-label small mb-0">Strength</label>
      <input type="number" id="m-str" class="form-control form-control-sm" min="1" max="10" value="8" style="width:55px">
    </div>
    <button class="btn btn-sm btn-success" onclick="addManualLine()">Add Line</button>
    <span id="manual-msg" class="small text-muted"></span>
  </div>
</div>

<!-- ══════════════════════ GRAPH ══════════════════════ -->
<div class="tab-pane fade" id="tab-graph">
  <div class="d-flex align-items-center flex-wrap gap-2 mb-1">
    <ul class="nav nav-pills mb-0" id="sym-pill-tabs">
      <li class="nav-item"><button class="nav-link active" onclick="selectSym('MES',this)">MES</button></li>
      <li class="nav-item"><button class="nav-link" onclick="selectSym('MNQ',this)">MNQ</button></li>
      <li class="nav-item"><button class="nav-link" onclick="selectSym('MYM',this)">MYM</button></li>
      <li class="nav-item"><button class="nav-link" onclick="selectSym('M2K',this)">M2K</button></li>
    </ul>
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-mode-candle" class="btn btn-outline-secondary active" onclick="setGraphMode('candle',this)">Candle</button>
      <button id="btn-mode-line"   class="btn btn-outline-secondary"        onclick="setGraphMode('line',this)">Line</button>
    </div>
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-range-all" class="btn btn-outline-secondary active" onclick="setGraphRange('all',this)">All Day</button>
      <button id="btn-range-4h"  class="btn btn-outline-secondary"        onclick="setGraphRange('4h',this)">Last 4h</button>
      <button id="btn-range-1h"  class="btn btn-outline-secondary"        onclick="setGraphRange('1h',this)">Last 1h</button>
    </div>
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-int-1"  class="btn btn-outline-secondary"        onclick="setGraphInterval(1,this)">1m</button>
      <button id="btn-int-5"  class="btn btn-outline-secondary active" onclick="setGraphInterval(5,this)">5m</button>
      <button id="btn-int-15" class="btn btn-outline-secondary"        onclick="setGraphInterval(15,this)">15m</button>
      <button id="btn-int-30" class="btn btn-outline-secondary"        onclick="setGraphInterval(30,this)">30m</button>
    </div>
    <span id="bar-count" class="text-muted small ms-1"></span>
    <button class="btn btn-sm btn-outline-warning ms-auto" onclick="createGraphTrades()">Create Trades</button>
    <span id="graph-trades-msg" class="small ms-1"></span>
  </div>
  <div id="mock-banner-graph" class="alert alert-warning py-1 px-2 mb-1 small">
    ⚠ No data for selected date — loaded <strong id="mock-date-graph"></strong>
  </div>
  <div id="chart" style="width:100%;height:460px;background:#1a1a2e;border-radius:4px;"></div>
  <div class="d-flex gap-3 mt-2 flex-wrap small">
    <label><input type="checkbox" id="tog-ohlc"      checked onchange="redrawLines()">
      <span class="badge source-ohlc">OHLC</span></label>
    <label><input type="checkbox" id="tog-pivot"     checked onchange="redrawLines()">
      <span class="badge source-pivot">Pivot</span></label>
    <label><input type="checkbox" id="tog-overnight" checked onchange="redrawLines()">
      <span class="badge source-overnight">Overnight</span></label>
    <label><input type="checkbox" id="tog-manual"    checked onchange="redrawLines()">
      <span class="badge source-manual">Manual</span></label>
  </div>
</div>

<!-- ══════════════════════ CREATE TRADES ══════════════════════ -->
<div class="tab-pane fade" id="tab-trades">
  <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
    <span class="text-muted small">Symbols:</span>
    <div id="sym-trades" class="d-flex gap-2">
      <label class="small"><input class="form-check-input" type="checkbox" value="MES" checked> MES</label>
      <label class="small"><input class="form-check-input" type="checkbox" value="MNQ" checked> MNQ</label>
      <label class="small"><input class="form-check-input" type="checkbox" value="MYM" checked> MYM</label>
      <label class="small"><input class="form-check-input" type="checkbox" value="M2K" checked> M2K</label>
    </div>
    <span class="text-muted small ms-2">Brackets (pts):</span>
    <label class="small"><input class="form-check-input bkt-chk" type="checkbox" value="2"  checked> 2</label>
    <label class="small"><input class="form-check-input bkt-chk" type="checkbox" value="4"  checked> 4</label>
    <label class="small"><input class="form-check-input bkt-chk" type="checkbox" value="10" checked> 10</label>
    <label class="small ms-2">Strength ≥
      <input type="number" id="min-str-trades" class="form-control form-control-sm d-inline-block"
             style="width:55px" min="1" max="10" value="1">
    </label>
    <button class="btn btn-sm btn-primary" onclick="createTrades()">Create Trades</button>
  </div>

  <div class="d-flex gap-2 mb-2">
    <span class="badge bg-secondary" id="ctr-total">Total: 0</span>
    <span class="badge bg-success"   id="ctr-passed">Passed: 0</span>
    <span class="badge bg-warning text-dark" id="ctr-filtered">Filtered: 0</span>
    <span class="badge bg-info text-dark"    id="ctr-syms">Symbols: —</span>
  </div>

  <div class="mb-2">
    <button class="btn btn-sm btn-success" id="btn-submit" disabled onclick="submitTrades()">
      Submit 0 Trades
    </button>
    <span id="trades-msg" class="small text-muted ms-2"></span>
  </div>

  <table class="table table-sm table-hover table-bordered">
    <thead class="table-dark">
      <tr><th>#</th><th>Sym</th><th>Algo</th><th>Dir</th><th>ET</th>
          <th>Entry</th><th>TP</th><th>SL</th><th>Bkt</th><th>Str</th><th>Source</th></tr>
    </thead>
    <tbody id="trades-tbody"></tbody>
  </table>
</div>

<!-- ══════════════════════ SUBMITTED ══════════════════════ -->
<div class="tab-pane fade" id="tab-submitted">
  <div class="d-flex gap-2 align-items-center mb-2">
    <button class="btn btn-sm btn-outline-secondary" onclick="loadSubmitted()">Refresh</button>
    <label class="small ms-2"><input type="checkbox" id="auto-ref" onchange="toggleAutoRef()"> Auto-refresh (5s)</label>
  </div>
  <table class="table table-sm table-hover table-bordered">
    <thead class="table-dark">
      <tr><th>ID</th><th>Sym</th><th>Dir</th><th>Type</th>
          <th>Entry</th><th>TP</th><th>SL</th><th>Bkt</th><th>Status</th><th>Fill</th><th>Updated</th></tr>
    </thead>
    <tbody id="sub-tbody"></tbody>
  </table>
</div>

</div><!-- tab-content -->
</div><!-- container -->

<script>
// ── price polling ────────────────────────────────────────────────────────────
const SOURCE_COLORS = {ohlc:'#4e79a7',pivot:'#f28e2b',overnight:'#59a14f',manual:'#e15759'};
const STATUS_CLS    = {PENDING:'secondary',SUBMITTED:'primary',SUBMITTING:'info',
                       FILLED:'warning',CLOSED:'success',CANCELLED:'dark',ERROR:'danger'};

async function pollPrices(){
  try{
    const d = await (await fetch('/api/prices')).json();
    for(const [s,p] of Object.entries(d)){
      const el = document.getElementById('chip-'+s);
      if(el) el.textContent = s+' '+(p!=null?p.toFixed(2):'—');
    }
  }catch(e){}
}
pollPrices(); setInterval(pollPrices,5000);

// ── helpers ──────────────────────────────────────────────────────────────────
function checkedVals(containerId){
  return [...document.querySelectorAll('#'+containerId+' input[type=checkbox]:checked')].map(e=>e.value);
}
function strengthColor(s){
  const g=['#555','#666','#777','#888','#999','#aaa','#f0a','#f60','#f80','#f00'];
  return g[Math.max(0,Math.min(9,s-1))];
}
function fmt(v){ return v!=null?v.toFixed(2):'—'; }

// ── LINES ────────────────────────────────────────────────────────────────────
function setAllAlgos(checked){
  document.querySelectorAll('.algo-chk').forEach(e=>e.checked=checked);
}

async function createLines(){
  const syms      = checkedVals('sym-lines');
  const algoTypes = [...document.querySelectorAll('.algo-chk:checked')].map(e=>e.value);
  const mergeThr  = parseFloat(document.querySelector('input[name="merge-thr"]:checked')?.value||'16');
  const histDate  = document.getElementById('shared-date-input').value||'';
  document.getElementById('lines-msg').textContent='Creating…';
  try{
    const payload = {symbols:syms,algo_types:algoTypes,merge_threshold:mergeThr};
    if(histDate) payload.history_date = histDate;
    const d = await (await fetch('/api/lines/create',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)})).json();
    const ok     = Object.entries(d.results).filter(([,v]) => !v.error);
    const failed = Object.entries(d.results).filter(([,v]) =>  v.error);
    const okMsg  = ok.map(([s,v]) => `${s}:${v.lines}`).join(' ');
    const noMsg  = failed.map(([s]) => s).join(' ');
    const mockSyms = ok.filter(([,v]) => v.mock).map(([s,v]) => `${s}→${v.mock}`).join(' ');

    const linesMsg = document.getElementById('lines-msg');
    if(ok.length === 0){
      linesMsg.className = 'small text-danger ms-1';
      linesMsg.textContent = 'No data found for: ' + noMsg;
    } else if(failed.length > 0){
      linesMsg.className = 'small text-warning ms-1';
      linesMsg.textContent = `Created ${okMsg}${mockSyms?' ('+mockSyms+')':''} | No data: ${noMsg}`;
    } else {
      linesMsg.className = 'small text-success ms-1';
      linesMsg.textContent = `Created ${okMsg}${mockSyms?' ('+mockSyms+')':''}`;
    }

    if(ok.length > 0) refreshLines();
  }catch(e){ document.getElementById('lines-msg').textContent='Error: '+e; }
}

async function refreshLines(){
  const ms = parseInt(document.getElementById('min-str-lines').value)||1;
  try{
    const rows = await (await fetch(`/api/lines?min_strength=${ms}`)).json();
    const tb = document.getElementById('lines-tbody');
    // Dispose existing tooltips before clearing
    tb.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el=>{
      bootstrap.Tooltip.getInstance(el)?.dispose();
    });
    tb.innerHTML='';
    for(const r of rows){
      const sc = SOURCE_COLORS[r.source]||'#888';
      const sl = r.source.charAt(0).toUpperCase()+r.source.slice(1);
      const tr = document.createElement('tr');
      // Build tooltip from stored note JSON
      let tipHtml = '';
      if(r.note){
        try{
          const n = typeof r.note==='string'?JSON.parse(r.note):r.note;
          const mergedPart = n.merged&&n.merged.length
            ? '<hr style="border-color:#555;margin:3px 0"><span class="text-warning">Absorbed: '
              +n.merged.map(m=>`${m.algo_type}@${m.price.toFixed(2)}`).join(', ')+'</span>'
            : '';
          tipHtml = `<b>${n.label}</b><br><small>${n.formula}</small><br>`
            +`<small class="text-muted">${n.inputs}</small><br>`
            +`<small class="text-muted">From: ${n.from_date}</small>${mergedPart}`;
        }catch(_){}
      }
      tr.innerHTML=`<td>${r.id}</td><td><b>${r.symbol}</b></td>
        <td class="font-monospace">${r.price.toFixed(2)}</td>
        <td>${r.line_type==='SUPPORT'?'<span class="text-success">SUPP</span>':'<span class="text-danger">RESI</span>'}</td>
        <td><small>${r.algo_type}</small></td>
        <td><span class="badge" style="background:${strengthColor(r.strength)}">${r.strength}</span></td>
        <td><span class="badge" style="background:${sc}">${sl}</span></td>
        <td><button class="btn btn-sm btn-outline-danger py-0 px-1" style="font-size:.7rem"
            onclick="delLine(${r.id})">✕</button></td>`;
      if(tipHtml){
        tr.setAttribute('data-bs-toggle','tooltip');
        tr.setAttribute('data-bs-html','true');
        tr.setAttribute('data-bs-placement','auto');
        tr.setAttribute('title', tipHtml);
        new bootstrap.Tooltip(tr, {html:true, boundary:'document'});
      }
      tb.appendChild(tr);
    }
  }catch(e){}
}

async function delLine(id){
  await fetch('/api/lines/'+id,{method:'DELETE'});
  refreshLines();
}

async function addManualLine(){
  const sym   = document.getElementById('m-sym').value;
  const price = parseFloat(document.getElementById('m-price').value);
  const ltype = document.getElementById('m-type').value;
  const str   = parseInt(document.getElementById('m-str').value)||8;
  if(!price){ document.getElementById('manual-msg').textContent='Enter price'; return; }
  await fetch('/api/lines/manual',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym,price,line_type:ltype,strength:str})});
  document.getElementById('manual-msg').textContent='Added ✓';
  setTimeout(()=>document.getElementById('manual-msg').textContent='',2000);
  refreshLines();
}

// ── GRAPH ────────────────────────────────────────────────────────────────────
let _graphSym='MES', _chartBars=[], _chartLines=[], _graphMode='candle', _graphRange='all', _graphInterval=5;
let _visibleLines=[];  // mirrors line traces 1:1 (trace index 0 = bar, 1..N = lines)

function setGraphMode(mode, btn){
  _graphMode = mode;
  document.querySelectorAll('#btn-mode-candle,#btn-mode-line').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  drawChart();
}

function setGraphRange(range, btn){
  _graphRange = range;
  document.querySelectorAll('#btn-range-all,#btn-range-4h,#btn-range-1h').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  drawChart();
}

function setGraphInterval(min, btn){
  _graphInterval = min;
  document.querySelectorAll('#btn-int-1,#btn-int-5,#btn-int-15,#btn-int-30').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadGraph();
}

function selectSym(sym,btn){
  _graphSym=sym;
  document.querySelectorAll('#sym-pill-tabs .nav-link').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadGraph();
}

function filteredBars(){
  if(_graphRange==='all') return _chartBars;
  const bph = Math.round(60 / _graphInterval);
  const n = _graphRange==='1h' ? bph : bph * 4;
  return _chartBars.slice(-n);
}

async function loadGraph(){
  const reqDate = document.getElementById('shared-date-input').value||'';
  const base = `/api/history/${_graphSym}?interval=${_graphInterval}`;
  const url = reqDate ? `${base}&date=${reqDate}` : base;
  try{
    const br = await (await fetch(url)).json();
    _chartBars = br.bars||[];
    const mock = br.mock_date;
    document.getElementById('mock-banner-graph').style.display=mock?'block':'none';
    if(mock) document.getElementById('mock-date-graph').textContent=mock;

    const ms = parseInt(document.getElementById('min-str-lines').value)||1;
    _chartLines = await (await fetch(`/api/lines?symbol=${_graphSym}&min_strength=${ms}`)).json();
    drawChart();
  }catch(e){ console.error(e); }
}

function enabledSources(){
  const s=new Set();
  if(document.getElementById('tog-ohlc').checked)      s.add('ohlc');
  if(document.getElementById('tog-pivot').checked)     s.add('pivot');
  if(document.getElementById('tog-overnight').checked) s.add('overnight');
  if(document.getElementById('tog-manual').checked)    s.add('manual');
  return s;
}

function buildLineTraces(bars){
  if(!bars.length) return [];
  const x0 = bars[0].t, x1 = bars[bars.length-1].t;
  const en  = enabledSources();
  _visibleLines = _chartLines.filter(l => en.has(l.source));
  return _visibleLines.map(l => {
    const armed = l._armed !== undefined ? l._armed : !!l.armed;
    const col   = armed ? (SOURCE_COLORS[l.source]||'#888') : 'rgba(128,128,128,0.35)';
    return {
      type:'scatter', mode:'lines',
      x:[x0, x1], y:[l.price, l.price],
      line:{color:col, width:armed?3:1, dash:armed?'solid':'dot'},
      name:l.algo_type,
      hovertemplate:`<b>${l.algo_type}</b> ${l.price.toFixed(2)}<extra></extra>`,
      showlegend:false
    };
  });
}

function buildAnnotations(){
  const en = enabledSources();
  return _chartLines.filter(l => en.has(l.source)).map(l => {
    const armed = l._armed !== undefined ? l._armed : !!l.armed;
    const col   = armed ? (SOURCE_COLORS[l.source]||'#888') : 'rgba(128,128,128,0.45)';
    return {xref:'paper',yref:'y',x:1,y:l.price,
      text:`${l.algo_type} ${l.price}`,showarrow:false,
      xanchor:'right',font:{size:9,color:col}};
  });
}

function drawChart(){
  if(!_chartBars.length){
    Plotly.purge('chart');
    document.getElementById('chart').innerHTML=
      `<div class="d-flex align-items-center justify-content-center h-100 text-muted">No history data for ${_graphSym}</div>`;
    return;
  }
  const bars = filteredBars();
  document.getElementById('bar-count').textContent = bars.length + ' bars';
  let barTrace;
  if(_graphMode==='line'){
    barTrace = {type:'scatter',mode:'lines',x:bars.map(b=>b.t),y:bars.map(b=>b.close),
      name:_graphSym,line:{color:'#7db3d8',width:1.5},showlegend:false};
  } else {
    barTrace = {type:'candlestick',x:bars.map(b=>b.t),
      open:bars.map(b=>b.open),high:bars.map(b=>b.high),
      low:bars.map(b=>b.low),close:bars.map(b=>b.close),
      name:_graphSym,
      increasing:{line:{color:'#26a69a'}},decreasing:{line:{color:'#ef5350'}},
      showlegend:false};
  }
  const lineTraces = buildLineTraces(bars);
  const layout={
    paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
    font:{color:'#ccc'},margin:{l:55,r:10,t:10,b:40},
    xaxis:{rangeslider:{visible:false},gridcolor:'#333'},
    yaxis:{gridcolor:'#333'},
    annotations:buildAnnotations(),
    showlegend:false};
  Plotly.newPlot('chart',[barTrace,...lineTraces],layout,{responsive:true,displayModeBar:false});

  document.getElementById('chart').on('plotly_click', function(evtData){
    const cn = evtData.points[0].curveNumber;
    if(cn === 0) return;
    const line = _visibleLines[cn - 1];
    if(!line) return;
    const wasArmed = line._armed !== undefined ? line._armed : !!line.armed;
    line._armed = !wasArmed;
    fetch(`/api/lines/${line.id}`, {method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({armed: line._armed ? 1 : 0})
    });
    drawChart();
  });
}

function redrawLines(){
  if(!_chartBars.length) return;
  drawChart();
}

async function createGraphTrades(){
  const msg = document.getElementById('graph-trades-msg');
  msg.className='small ms-1 text-warning';
  msg.textContent='Generating…';
  try{
    const d = await (await fetch('/api/trades/create',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:[_graphSym], brackets:[4,8], min_strength:1})
    })).json();
    const cands = d.candidates||[];
    if(!cands.length){
      msg.className='small ms-1 text-muted';
      msg.textContent='No armed lines';
      return;
    }
    const s = await (await fetch('/api/trades/submit',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({candidates:cands})
    })).json();
    msg.className='small ms-1 text-success';
    msg.textContent=`${s.submitted} trades submitted`;
  }catch(e){
    msg.className='small ms-1 text-danger';
    msg.textContent='Error: '+e;
  }
}

function onSharedDateChange(){
  const active = document.querySelector('#mainTab .nav-link.active');
  if(active?.dataset?.bsTarget === '#tab-graph') loadGraph();
}

document.getElementById('btn-graph-tab').addEventListener('click', function(){
  document.getElementById('shared-date-input').readOnly = true;
  loadGraph();
});
document.querySelectorAll('#mainTab .nav-link:not(#btn-graph-tab)').forEach(function(btn){
  btn.addEventListener('click', function(){
    document.getElementById('shared-date-input').readOnly = false;
  });
});

// ── CREATE TRADES ────────────────────────────────────────────────────────────
let _candidates=[];

async function createTrades(){
  const syms     = checkedVals('sym-trades');
  const brackets = [...document.querySelectorAll('.bkt-chk:checked')].map(e=>parseFloat(e.value));
  const ms       = parseInt(document.getElementById('min-str-trades').value)||1;
  document.getElementById('trades-msg').textContent='Generating…';
  try{
    const d = await (await fetch('/api/trades/create',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:syms,brackets,min_strength:ms})})).json();
    _candidates=d.candidates||[];
    document.getElementById('ctr-total').textContent   ='Total: '+d.total;
    document.getElementById('ctr-passed').textContent  ='Passed: '+d.passed;
    document.getElementById('ctr-filtered').textContent='Filtered: '+d.filtered;
    document.getElementById('ctr-syms').textContent    ='Symbols: '+(d.symbols_covered||[]).join(', ');
    const btn=document.getElementById('btn-submit');
    btn.textContent=`Submit ${_candidates.length} Trades`;
    btn.disabled=_candidates.length===0;
    document.getElementById('trades-msg').textContent='';
    renderTrades(_candidates);
  }catch(e){ document.getElementById('trades-msg').textContent='Error: '+e; }
}

function renderTrades(cands){
  const tb=document.getElementById('trades-tbody');
  tb.innerHTML='';
  cands.forEach((c,i)=>{
    const sc=SOURCE_COLORS[c.source]||'#888';
    const sl=c.source.charAt(0).toUpperCase()+c.source.slice(1);
    const tr=document.createElement('tr');
    tr.className='row-'+(c.source||'manual');
    tr.innerHTML=`<td>${i+1}</td><td><b>${c.symbol}</b></td>
      <td><small>${c.algo_type}</small></td>
      <td>${c.direction==='BUY'?'<span class="text-success fw-bold">BUY</span>':'<span class="text-danger fw-bold">SELL</span>'}</td>
      <td>${c.entry_type}</td>
      <td class="font-monospace">${fmt(c.entry_price)}</td>
      <td class="font-monospace">${fmt(c.tp_price)}</td>
      <td class="font-monospace">${fmt(c.sl_price)}</td>
      <td>${c.bracket}</td>
      <td><span class="badge" style="background:${strengthColor(c.strength)}">${c.strength}</span></td>
      <td><span class="badge" style="background:${sc}">${sl}</span></td>`;
    tb.appendChild(tr);
  });
}

async function submitTrades(){
  if(!_candidates.length) return;
  const btn=document.getElementById('btn-submit');
  btn.disabled=true;
  try{
    const d=await (await fetch('/api/trades/submit',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({candidates:_candidates})})).json();
    document.getElementById('trades-msg').textContent=`Submitted ${d.submitted} ✓`;
    _candidates=[];
    btn.textContent='Submit 0 Trades';
    document.getElementById('trades-tbody').innerHTML='';
  }catch(e){ document.getElementById('trades-msg').textContent='Error: '+e; btn.disabled=false; }
}

// ── SUBMITTED ────────────────────────────────────────────────────────────────
let _autoRefTimer=null;

async function loadSubmitted(){
  try{
    const rows=await (await fetch('/api/submitted')).json();
    const tb=document.getElementById('sub-tbody');
    tb.innerHTML='';
    for(const r of rows){
      const bc=STATUS_CLS[r.status]||'secondary';
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${r.id}</td><td><b>${r.symbol}</b></td>
        <td>${r.direction==='BUY'?'<span class="text-success">BUY</span>':'<span class="text-danger">SELL</span>'}</td>
        <td>${r.entry_type}</td>
        <td class="font-monospace">${fmt(r.entry_price)}</td>
        <td class="font-monospace">${fmt(r.tp_price)}</td>
        <td class="font-monospace">${fmt(r.sl_price)}</td>
        <td>${r.bracket||'—'}</td>
        <td><span class="badge bg-${bc}">${r.status}</span></td>
        <td class="font-monospace">${fmt(r.fill_price)}</td>
        <td class="text-muted small">${(r.updated_at||'').slice(11,16)}</td>`;
      tb.appendChild(tr);
    }
  }catch(e){}
}

function toggleAutoRef(){
  clearInterval(_autoRefTimer);
  if(document.getElementById('auto-ref').checked)
    _autoRefTimer=setInterval(loadSubmitted,5000);
}

document.getElementById('btn-sub-tab').addEventListener('click',loadSubmitted);

function _lastWeekday(){
  const d = new Date();
  d.setDate(d.getDate() - 1);
  while(d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return d.toISOString().split('T')[0];
}

async function loadLastDay(){
  try{
    const d = await (await fetch('/api/last_data_date')).json();
    document.getElementById('shared-date-input').value = d.date || _lastWeekday();
  }catch(e){
    document.getElementById('shared-date-input').value = _lastWeekday();
  }
  createLines();
}

// Initial load — set shared date picker to last actual data date, max = today
(async function(){
  const today = new Date().toISOString().split('T')[0];
  const el = document.getElementById('shared-date-input');
  el.max = today;
  try{
    const d = await (await fetch('/api/last_data_date')).json();
    el.value = d.date || _lastWeekday();
  }catch(e){
    el.value = _lastWeekday();
  }
  refreshLines();
})();
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trading Dashboard — port 5003")
    parser.add_argument("--port",  type=int, default=5003)
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"[trading_dashboard] port {args.port} already in use — exiting"); sys.exit(0)
    print(f"Trading Dashboard -> http://{args.host}:{args.port}")
    print(f"LAN access        -> http://192.168.1.132:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
