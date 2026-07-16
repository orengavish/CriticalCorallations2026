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
import threading
from pathlib import Path
from datetime import datetime, date, timezone, timedelta

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests
from flask import Flask, jsonify, request, render_template_string

from lib.db import get_db
from lib.price_profile import ensure_profile as _ensure_price_profile, get_price_profile

# ── Constants ──────────────────────────────────────────────────────────────────

ALL_SYMBOLS      = ["MES", "MNQ", "MYM", "M2K"]
TICKS            = {"MES": 0.25, "MNQ": 0.25, "MYM": 1.0, "M2K": 0.10}
DEFAULT_BRACKETS = [2.0, 4.0, 10.0]   # points

_TRADER_URL = "http://127.0.0.1:5001"
_HIST_DIR   = Path(r"C:\Projects\Galgo2026\june\trader\data\history")

SOURCE_COLORS = {
    "ohlc":      "#4e79a7",
    "pivot":     "#f28e2b",
    "overnight": "#59a14f",
    "manual":    "#e15759",
    "orb":       "#1abc9c",
    "vwap":      "#9b59b6",
    "volume":    "#e67e22",
    "round":     "#7f8c8d",
}
SOURCE_LABELS = {
    "ohlc":      "OHLC",
    "pivot":     "Pivot",
    "overnight": "Overnight",
    "manual":    "Manual",
    "orb":       "ORB",
    "vwap":      "VWAP",
    "volume":    "Volume",
    "round":     "Round",
}

ALL_ALGO_TYPES = [
    "PDH", "PDL", "PDC", "PDO",
    "PIVOT_P", "PIVOT_R1", "PIVOT_S1", "PIVOT_R2", "PIVOT_S2", "PIVOT_R3", "PIVOT_S3",
    "OVERNIGHT_H", "OVERNIGHT_L",
    "ORB15_H", "ORB15_L", "ORB30_H", "ORB30_L",
    "VWAP",
    "POC", "VAH", "VAL",
    "ROUND_BIG", "ROUND_MED", "ROUND_SML",
    "MANUAL",
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
    "ORB15_H":     "ORB 15-Min High",
    "ORB15_L":     "ORB 15-Min Low",
    "ORB30_H":     "ORB 30-Min High",
    "ORB30_L":     "ORB 30-Min Low",
    "VWAP":        "VWAP (RTH Mean)",
    "POC":         "Point of Control",
    "VAH":         "Value Area High",
    "VAL":         "Value Area Low",
    "ROUND_BIG":   "Round Level (Major)",
    "ROUND_MED":   "Round Level (Medium)",
    "ROUND_SML":   "Round Level (Minor)",
    "MANUAL":      "Manual",
}

# Round number intervals (pts) and strengths per symbol
_ROUND_LEVELS = {
    "MES": [(100, 7), (50, 5), (25, 3)],
    "MNQ": [(1000, 7), (500, 5), (100, 3)],
    "MYM": [(1000, 7), (500, 5), (100, 3)],
    "M2K": [(100, 7), (50, 5), (25, 3)],
}

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

_DB_OVERRIDE: Path | None = None

_build_progress: dict = {
    "running": False, "total": 0, "done": 0,
    "current": "", "log": [],
}
_build_lock = threading.Lock()


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
    """Add source / algo_type / note / confidence to critical_lines if absent (idempotent)."""
    with get_db(db_path) as con:
        existing = {r[1] for r in con.execute("PRAGMA table_info(critical_lines)").fetchall()}
        if "source" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN source TEXT DEFAULT 'manual'")
        if "algo_type" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN algo_type TEXT DEFAULT 'MANUAL'")
        if "note" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN note TEXT")
        if "confidence" not in existing:
            con.execute("ALTER TABLE critical_lines ADD COLUMN confidence TEXT DEFAULT ''")


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


def _ohlcv_bars(ticks: list, interval_min: float = 5) -> list:
    bars: dict       = {}
    is_sub_min       = interval_min < 1
    interval_sec     = max(1, int(round(interval_min * 60)))
    interval_min_int = max(1, int(interval_min))
    for (t_min, price, iso) in ticks:
        date_part = iso[:10]                                    # "YYYY-MM-DD" from CT timestamp
        if is_sub_min:
            try:
                ts        = iso[11:19]                          # "HH:MM:SS"
                t_abs_sec = int(ts[0:2]) * 3600 + int(ts[3:5]) * 60 + int(ts[6:8])
            except (ValueError, IndexError):
                t_abs_sec = t_min * 60
            bucket_sec = (t_abs_sec // interval_sec) * interval_sec
        else:
            bucket_sec = (t_min // interval_min_int) * interval_min_int * 60
        key = (date_part, bucket_sec)
        if key not in bars:
            bars[key] = {"date": date_part, "t_sec": bucket_sec, "iso": iso,
                         "open": price, "high": price, "low": price, "close": price, "vol": 0}
        b = bars[key]
        b["high"]  = max(b["high"], price)
        b["low"]   = min(b["low"],  price)
        b["close"] = price
        b["vol"]  += 1
    return sorted(bars.values(), key=lambda x: (x["date"], x["t_sec"]))


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

    # Opening Range Breakout (ORB)
    ORB15_END = RTH_START + 15   # 09:45
    ORB30_END = RTH_START + 30   # 10:00
    orb15_p = [p for (t, p, _) in ticks if RTH_START <= t < ORB15_END]
    orb30_p = [p for (t, p, _) in ticks if RTH_START <= t < ORB30_END]
    if orb15_p:
        orb15_h, orb15_l = max(orb15_p), min(orb15_p)
        inp15 = f"09:30–09:45  {len(orb15_p)} ticks"
        add(orb15_h, "RESISTANCE", "orb", "ORB15_H", 7,
            f"ORB 15-min High = {orb15_h:.2f}", inp15)
        add(orb15_l, "SUPPORT",    "orb", "ORB15_L", 7,
            f"ORB 15-min Low = {orb15_l:.2f}",  inp15)
    if orb30_p:
        orb30_h, orb30_l = max(orb30_p), min(orb30_p)
        inp30 = f"09:30–10:00  {len(orb30_p)} ticks"
        add(orb30_h, "RESISTANCE", "orb", "ORB30_H", 6,
            f"ORB 30-min High = {orb30_h:.2f}", inp30)
        add(orb30_l, "SUPPORT",    "orb", "ORB30_L", 6,
            f"ORB 30-min Low = {orb30_l:.2f}",  inp30)

    # VWAP — equal-weighted arithmetic mean of RTH ticks
    if rth_p:
        vwap = sum(rth_p) / len(rth_p)
        add(vwap, "RESISTANCE" if vwap >= mid else "SUPPORT",
            "vwap", "VWAP", 8,
            f"VWAP = {vwap:.4f}  (RTH mean, n={len(rth_p)} ticks)",
            f"RTH n={len(rth_p)}")

    # Volume Profile — POC / VAH / VAL from RTH tick histogram
    if rth_p:
        t_sz = TICKS.get(symbol, 0.25)
        counts: dict = {}
        for p in rth_p:
            bkt = round(round(p / t_sz) * t_sz, 10)
            counts[bkt] = counts.get(bkt, 0) + 1
        sp = sorted(counts)
        poc = max(counts, key=lambda k: counts[k])
        total_t = len(rth_p)
        va_target = total_t * 0.70
        poc_i = sp.index(poc)
        lo_i, hi_i = poc_i, poc_i
        running = counts[poc]
        while running < va_target:
            lo_c = counts[sp[lo_i - 1]] if lo_i > 0 else -1
            hi_c = counts[sp[hi_i + 1]] if hi_i < len(sp) - 1 else -1
            if lo_c < 0 and hi_c < 0:
                break
            if lo_c >= hi_c and lo_i > 0:
                lo_i -= 1; running += counts[sp[lo_i]]
            elif hi_i < len(sp) - 1:
                hi_i += 1; running += counts[sp[hi_i]]
            else:
                break
        vah, val = sp[hi_i], sp[lo_i]
        vol_inp = f"RTH ticks={total_t}  POC count={counts[poc]}"
        add(poc, "RESISTANCE" if poc >= mid else "SUPPORT", "volume", "POC", 9,
            f"Point of Control = {poc:.2f} ({counts[poc]} ticks)", vol_inp)
        if vah != poc:
            add(vah, "RESISTANCE", "volume", "VAH", 7,
                f"Value Area High = {vah:.2f} (70% VA top)", vol_inp)
        if val != poc:
            add(val, "SUPPORT",    "volume", "VAL", 7,
                f"Value Area Low = {val:.2f} (70% VA bottom)", vol_inp)

    # Round psychological levels
    _rl_map = {"ROUND_BIG": 7, "ROUND_MED": 5, "ROUND_SML": 3}
    for interval, strength in _ROUND_LEVELS.get(symbol, [(100, 7), (50, 5), (25, 3)]):
        akey = next(k for k, v in _rl_map.items() if v == strength)
        lo_bkt = int(L / interval) * interval
        n = lo_bkt
        while n <= H + interval:
            if L <= n <= H:
                add(float(n), "RESISTANCE" if n >= mid else "SUPPORT",
                    "round", akey, strength,
                    f"Round {interval}pt level = {n}",
                    f"range {L:.2f}–{H:.2f}")
            n += interval

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

        # Build price profile in background — fast if cached, ~5s on first build
        threading.Thread(
            target=_ensure_price_profile,
            args=(sym, used_date.isoformat()),
            daemon=True,
            name=f"profile-{sym}-{used_date}",
        ).start()

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
    symbols_str  = request.args.get("symbols", "")
    min_strength = int(request.args.get("min_strength", 1))

    # Date range — support single date OR from/to
    req_date  = request.args.get("date", "")
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    if req_date:
        date_from = date_to = req_date
    if not date_from:
        date_from = date_to = date.today().isoformat()
    if not date_to:
        date_to = date_from

    q_base = (
        "SELECT id, symbol, date, price, line_type, strength,"
        " COALESCE(source,'manual') AS source,"
        " COALESCE(algo_type,'MANUAL') AS algo_type,"
        " note, COALESCE(armed,1) AS armed"
        " FROM critical_lines WHERE date>=? AND date<=? AND strength>=?"
    )
    params: list = [date_from, date_to, min_strength]

    syms_list: list[str] = []
    if symbol:
        syms_list = [symbol]
    elif symbols_str:
        syms_list = [s.strip() for s in symbols_str.split(",") if s.strip()]
    if syms_list:
        ph = ",".join("?" * len(syms_list))
        q_base += f" AND symbol IN ({ph})"
        params.extend(syms_list)

    q_base += " ORDER BY date DESC, symbol, strength DESC, price"
    with get_db(db_path) as con:
        rows = con.execute(q_base, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/lines/manual", methods=["POST"])
def api_lines_manual():
    body    = request.get_json(silent=True) or {}
    db_path = _resolve_db()
    _ensure_columns(db_path)
    # batch mode: body has "lines" array
    lines_in = body.get("lines")
    if lines_in is not None:
        saved = 0
        with get_db(db_path) as con:
            for ln in lines_in:
                lbl   = ln.get("label", "")
                note  = json.dumps({"label": lbl, "formula": "manual", "inputs": "", "from_date": ln.get("date", ""), "merged": []})
                con.execute(
                    "INSERT INTO critical_lines"
                    " (symbol, date, line_type, price, strength, armed, source, algo_type, note)"
                    " VALUES (?,?,?,?,?,1,'manual','MANUAL',?)",
                    (ln.get("symbol","MES"), ln.get("date", date.today().isoformat()),
                     ln.get("line_type","SUPPORT").upper(), float(ln.get("price",0)),
                     int(ln.get("strength", 8)), note)
                )
                saved += 1
        return jsonify({"saved": saved, "ok": True})
    # single line (legacy)
    symbol   = body.get("symbol", "MES")
    price    = float(body.get("price", 0))
    ltype    = body.get("line_type", "SUPPORT").upper()
    strength = int(body.get("strength", 8))
    today    = date.today().isoformat()
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


# ── Sandbox manual lines (source='D') ──────────────────────────────────────────

@app.route("/api/sandbox/lines/<symbol>/<date_str>")
def api_sandbox_lines_get(symbol: str, date_str: str):
    db_path = _resolve_db()
    _ensure_columns(db_path)
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT id, price, line_type, COALESCE(confidence,'') AS confidence"
            " FROM critical_lines"
            " WHERE symbol=? AND date=? AND source='D'"
            " ORDER BY price",
            (symbol.upper(), date_str)
        ).fetchall()
    return jsonify({"lines": [dict(r) for r in rows]})


@app.route("/api/sandbox/line", methods=["POST"])
def api_sandbox_line_create():
    body     = request.get_json(force=True) or {}
    symbol   = body.get("symbol", "MES").upper()
    date_str = body.get("date", date.today().isoformat())
    price    = float(body.get("price", 0))
    ltype    = body.get("line_type", "SUPPORT").upper()
    conf     = body.get("confidence", "")
    strength = {"!": 1, "": 2, "?": 3}.get(conf, 2)
    db_path  = _resolve_db()
    _ensure_columns(db_path)
    with get_db(db_path) as con:
        cur = con.execute(
            "INSERT INTO critical_lines"
            " (symbol, date, line_type, price, strength, armed, source, algo_type, confidence)"
            " VALUES (?,?,?,?,?,1,'D','MANUAL',?)",
            (symbol, date_str, ltype, price, strength, conf)
        )
    return jsonify({"id": cur.lastrowid, "ok": True})


@app.route("/api/sandbox/line/<int:line_id>", methods=["PATCH"])
def api_sandbox_line_patch(line_id: int):
    body   = request.get_json(force=True) or {}
    ltype  = body.get("line_type", "").upper() or None
    conf   = body.get("confidence")  # may be '' which is falsy but valid
    db_path = _resolve_db()
    _ensure_columns(db_path)
    with get_db(db_path) as con:
        if ltype:
            con.execute("UPDATE critical_lines SET line_type=? WHERE id=? AND source='D'",
                        (ltype, line_id))
        if conf is not None:
            strength = {"!": 1, "": 2, "?": 3}.get(conf, 2)
            con.execute("UPDATE critical_lines SET confidence=?, strength=? WHERE id=? AND source='D'",
                        (conf, strength, line_id))
    return jsonify({"ok": True})


def _build_lines_for(sym: str, target: date, algo_types: set,
                     merge_threshold: float, db_path: Path, force: bool) -> dict:
    """Generate and store lines for one (symbol, date).

    Returns {"action": "skip"|"done"|"no_csv"|"no_rth", "count": N}.
    force=False → skip if non-manual lines already exist.
    force=True  → always regenerate (delete existing non-manual first).
    """
    target_str = target.isoformat()

    if not force:
        with get_db(db_path) as con:
            existing = con.execute(
                "SELECT COUNT(*) FROM critical_lines"
                " WHERE symbol=? AND date=? AND (source IS NULL OR source != 'manual')",
                (sym, target_str),
            ).fetchone()[0]
        if existing > 0:
            return {"action": "skip", "count": existing}

    ticks = _load_ticks(sym, target)
    if not ticks:
        return {"action": "no_csv", "count": 0}
    if not any(_RTH_START_MIN <= t < _RTH_END_MIN for (t, _, _) in ticks):
        return {"action": "no_rth", "count": 0}

    raw_lines = _generate_lines(sym, ticks, filter_types=algo_types)
    kept: list = []
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
            (sym, target_str),
        )
        for ln in kept:
            tip = {
                "label":     _ALGO_LABEL.get(ln["algo_type"], ln["algo_type"]),
                "formula":   ln["_tip"]["formula"],
                "inputs":    ln["_tip"]["inputs"],
                "from_date": target_str,
                "merged":    ln.get("merged", []),
            }
            con.execute(
                "INSERT INTO critical_lines"
                " (symbol, date, line_type, price, strength, armed, source, algo_type, note)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (sym, target_str, ln["line_type"], ln["price"],
                 ln["strength"], ln["source"], ln["algo_type"], json.dumps(tip)),
            )
    return {"action": "done", "count": len(kept)}


@app.route("/api/analyze_all", methods=["POST"])
def api_analyze_all():
    """Batch-generate lines for every available RTH date for requested symbols."""
    body            = request.get_json(silent=True) or {}
    symbols         = body.get("symbols", ALL_SYMBOLS)
    algo_types      = set(body.get("algo_types", ALL_ALGO_TYPES))
    merge_threshold = float(body.get("merge_threshold", 16.0))

    sym_dates: dict[str, list[date]] = {}
    for sym in symbols:
        sym_dates[sym] = []
        for p in _HIST_DIR.glob(f"{sym}_trades_????????.csv"):
            d_str = p.stem.split("_")[-1]
            try:
                d = date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
            except ValueError:
                continue
            if d.weekday() < 5 and _csv_has_rth(sym, d):
                sym_dates[sym].append(d)

    all_dates = sorted({d for dates in sym_dates.values() for d in dates})
    db_path   = _resolve_db()
    _ensure_columns(db_path)
    analyzed: list[str] = []

    for target in all_dates:
        any_written = False
        for sym in symbols:
            if target not in sym_dates.get(sym, []):
                continue
            r = _build_lines_for(sym, target, algo_types, merge_threshold, db_path, force=True)
            if r["action"] == "done":
                any_written = True
        if any_written:
            analyzed.append(target.isoformat())

    return jsonify({"dates": analyzed, "count": len(analyzed)})


def _build_db_thread(force: bool, algo_types: set | None = None,
                     merge_threshold: float = 16.0, weeks_back: int = 2) -> None:
    global _build_progress
    db_path = _resolve_db()
    _ensure_columns(db_path)
    if algo_types is None:
        algo_types = set(ALL_ALGO_TYPES) - {"MANUAL"}

    today       = date.today()
    market_days = []
    for i in range(1, weeks_back * 7 + 1):
        d = today - timedelta(days=i)
        if d.weekday() < 5:
            market_days.append(d)
    market_days = sorted(market_days, reverse=True)[:10]

    jobs = [(d, sym) for d in market_days for sym in ALL_SYMBOLS]
    with _build_lock:
        _build_progress["total"]   = len(jobs)
        _build_progress["done"]    = 0
        _build_progress["log"]     = []
        _build_progress["current"] = ""

    for (d, sym) in jobs:
        with _build_lock:
            _build_progress["current"] = f"{d.isoformat()} {sym}"
        result = _build_lines_for(sym, d, algo_types, merge_threshold, db_path, force)
        with _build_lock:
            _build_progress["log"].append({
                "date": d.isoformat(), "symbol": sym,
                "action": result["action"], "count": result["count"],
            })
            _build_progress["done"] += 1

    with _build_lock:
        _build_progress["running"] = False
        _build_progress["current"] = ""


@app.route("/api/build_db", methods=["POST"])
def api_build_db():
    global _build_progress
    with _build_lock:
        if _build_progress["running"]:
            return jsonify({"error": "already running"})
        _build_progress["running"] = True
    body          = request.get_json(silent=True) or {}
    force         = bool(body.get("force", False))
    algos         = body.get("algo_types")
    algo_types    = set(algos) if algos else None
    merge_thr     = float(body.get("merge_threshold", 16.0))
    weeks_back    = int(body.get("weeks_back", 2))
    t = threading.Thread(
        target=_build_db_thread,
        args=(force, algo_types, merge_thr, weeks_back),
        daemon=True,
    )
    t.start()
    return jsonify({"started": True})


@app.route("/api/build_db/status")
def api_build_db_status():
    with _build_lock:
        return jsonify(dict(_build_progress))


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
    interval     = float(request.args.get("interval", 5))
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
                if _RTH_START_MIN * 60 <= b["t_sec"] < _RTH_END_MIN * 60]
    bars = []
    for b in rth_bars:
        t  = b["t_sec"]
        hh, mm, ss = t // 3600, (t % 3600) // 60, t % 60
        bars.append({"t": f"{b['date']}T{hh:02d}:{mm:02d}:{ss:02d}",
                     "open": b["open"], "high": b["high"],
                     "low":  b["low"],  "close": b["close"], "vol": b["vol"]})

    total_ticks = sum(b["vol"] for b in rth_bars)
    mock = used_date.isoformat() if used_date != start else None
    return jsonify({"bars": bars, "date": used_date.isoformat(),
                    "symbol": symbol, "mock_date": mock,
                    "total_ticks": total_ticks})


@app.route("/api/volume_profile/<symbol>")
def api_volume_profile(symbol: str):
    req_date_str = request.args.get("date")
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
        return jsonify({"profile": [], "date": None, "symbol": symbol, "error": "no data"})

    t_sz  = TICKS.get(symbol, 0.25)
    rth_p = [p for (t_min, p, _) in ticks if _RTH_START_MIN <= t_min < _RTH_END_MIN]
    counts: dict = {}
    for p in rth_p:
        bkt = round(round(p / t_sz) * t_sz, 10)
        counts[bkt] = counts.get(bkt, 0) + 1

    profile = [{"price": p, "count": c} for p, c in sorted(counts.items())]
    mock    = used_date.isoformat() if used_date != start else None
    return jsonify({"profile": profile, "date": used_date.isoformat(),
                    "symbol": symbol, "mock_date": mock, "tick_size": t_sz})


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


@app.route("/api/available_dates")
def api_available_dates():
    """Dates that have CSV data for at least one requested symbol in the given range."""
    symbols_str  = request.args.get("symbols", ",".join(ALL_SYMBOLS))
    syms         = [s.strip() for s in symbols_str.split(",") if s.strip()] or ALL_SYMBOLS
    today        = date.today()
    date_from_s  = request.args.get("date_from", (today - timedelta(days=14)).isoformat())
    date_to_s    = request.args.get("date_to",   (today - timedelta(days=1)).isoformat())
    date_from    = date.fromisoformat(date_from_s)
    date_to      = date.fromisoformat(date_to_s)

    available: set[str] = set()
    cur = date_from
    while cur <= date_to:
        if cur.weekday() < 5:
            for sym in syms:
                if _find_csv(sym, cur):
                    available.add(cur.isoformat())
                    break
        cur += timedelta(days=1)
    return jsonify({"dates": sorted(available)})


@app.route("/api/sandbox/profile/<symbol>/<date_str>")
def api_sandbox_profile(symbol: str, date_str: str):
    """Return price-level profile for (symbol, date). Builds if not yet cached."""
    if symbol not in ALL_SYMBOLS:
        return jsonify({"error": "unknown symbol"}), 400
    try:
        date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    db_path = _resolve_db()
    _ensure_price_profile(symbol, date_str, db_path)
    rows = get_price_profile(symbol, date_str, db_path)
    if not rows:
        return jsonify({"error": "no data", "symbol": symbol, "date": date_str, "rows": []}), 404
    return jsonify({"symbol": symbol, "date": date_str, "rows": rows})


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
.source-orb       {background:#1abc9c;color:#fff;}
.source-vwap      {background:#9b59b6;color:#fff;}
.source-volume    {background:#e67e22;color:#fff;}
.source-round     {background:#7f8c8d;color:#fff;}
.row-ohlc      {background:rgba(78,121,167,.12)!important;}
.row-pivot     {background:rgba(242,142,43,.12)!important;}
.row-overnight {background:rgba(89,161,79,.12)!important;}
.row-manual    {background:rgba(225,87,89,.12)!important;}
.row-orb       {background:rgba(26,188,156,.12)!important;}
.row-vwap      {background:rgba(155,89,182,.12)!important;}
.row-volume    {background:rgba(230,126,34,.12)!important;}
.row-round     {background:rgba(127,140,141,.12)!important;}
.price-chip{font-family:monospace;font-size:.8rem;padding:2px 8px;border-radius:4px;}
body.busy-wait{cursor:wait!important;}
body.busy-wait *{pointer-events:none!important;}
body.busy-wait button,body.busy-wait input,body.busy-wait select{opacity:.55;}
#top-bar{background:#161b22;height:40px;border-bottom:1px solid #30363d;display:flex;align-items:center;padding:0 8px;gap:0;overflow:hidden}
.top-tab{border:none!important;border-radius:0!important;background:transparent!important;color:#8b949e;font-size:.8rem;height:40px;border-bottom:2px solid transparent!important;display:flex;align-items:center;padding:0 12px;white-space:nowrap}
.top-tab:hover{color:#ccc;background:rgba(255,255,255,.05)!important}
.top-tab.active{color:#fff!important;border-bottom-color:#0d6efd!important}
/* Sandbox tab */
#sb-col-checks label{cursor:pointer;gap:4px;}
#sb-transpose-btn.active{background:rgba(13,110,253,.25);border-color:#0d6efd;color:#6ea8fe;}
#sb-popup{display:none;position:fixed;z-index:1050;min-width:215px;background:#1e2530;border:1px solid #30363d;border-radius:6px;padding:8px 10px;box-shadow:0 4px 20px rgba(0,0,0,.7);}
#sb-popup table td{padding:2px 6px;}
#sb-chart{cursor:crosshair;}
</style>
</head>
<body>

<div id="top-bar">
  <ul class="nav mb-0 flex-shrink-0" id="mainTab" role="tablist" style="height:40px;gap:0;list-style:none;padding:0;margin:0;display:flex">
    <li class="nav-item"><button class="nav-link top-tab active" data-bs-toggle="tab" data-bs-target="#tab-lines">Lines</button></li>
    <li class="nav-item"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-graph" id="btn-graph-tab">Graph</button></li>
    <li class="nav-item"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-sandbox" id="btn-sandbox-tab">Sandbox</button></li>
    <li class="nav-item"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-all" id="btn-all-tab">All</button></li>
    <li class="nav-item"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-test" id="btn-test-tab">Test</button></li>
    <li class="nav-item"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-trades">Trades</button></li>
    <li class="nav-item"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-submitted" id="btn-sub-tab">Sub</button></li>
  </ul>
  <div class="vr mx-2 flex-shrink-0" style="height:20px;background:#30363d"></div>
  <div class="d-flex align-items-center gap-2 small flex-shrink-0">
    <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="day" checked onchange="onDateRangeChange()"> 1D</label>
    <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="week" onchange="onDateRangeChange()"> 1W</label>
    <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="2weeks" onchange="onDateRangeChange()"> 2W</label>
    <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="custom" onchange="onDateRangeChange()"> Custom</label>
    <input type="date" id="range-from" class="form-control form-control-sm py-0" style="width:120px;display:none;font-size:.75rem;height:24px" onchange="onDateRangeChange()">
    <span id="range-sep" style="display:none" class="text-muted">–</span>
    <input type="date" id="range-to"   class="form-control form-control-sm py-0" style="width:120px;display:none;font-size:.75rem;height:24px" onchange="onDateRangeChange()">
  </div>
  <div class="d-flex align-items-center gap-2 ms-auto flex-shrink-0">
    <span class="price-chip bg-secondary" id="chip-MES">MES —</span>
    <span class="price-chip bg-secondary" id="chip-MNQ">MNQ —</span>
    <span class="price-chip bg-secondary" id="chip-MYM">MYM —</span>
    <span class="price-chip bg-secondary" id="chip-M2K">M2K —</span>
    <span class="text-muted ms-1" style="font-size:.75rem">Trading Dashboard</span>
    <span class="badge bg-info text-dark">:5003</span>
    <span class="badge bg-secondary">v4.09</span>
  </div>
</div>

<!-- Line detail modal -->
<div class="modal fade" id="lineModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg">
    <div class="modal-content bg-dark border-secondary">
      <div class="modal-header border-secondary py-2">
        <h6 class="modal-title font-monospace" id="lm-title"></h6>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body small" id="lm-body"></div>
      <div class="modal-footer border-secondary py-2">
        <button class="btn btn-sm btn-outline-warning" id="lm-toggle-btn" onclick="toggleCurrentLine()">Enable/Disable</button>
        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Close</button>
      </div>
    </div>
  </div>
</div>

<!-- Manual line naming modal -->
<div class="modal fade" id="manualLineModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-sm">
    <div class="modal-content bg-dark border-warning">
      <div class="modal-header border-warning py-2">
        <h6 class="modal-title text-warning font-monospace">&#9998; <span id="ml-price-display"></span></h6>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body small">
        <input id="ml-name-input" class="form-control form-control-sm bg-dark text-light border-secondary mb-3"
               placeholder="Label (optional)" maxlength="60">
        <div class="d-flex gap-2">
          <button class="btn btn-success flex-fill fw-bold py-2" onclick="pickAndSave('SUPPORT')">&#9650; Support</button>
          <button class="btn btn-danger  flex-fill fw-bold py-2" onclick="pickAndSave('RESISTANCE')">&#9660; Resistance</button>
        </div>
      </div>
      <div class="modal-footer border-warning py-1">
        <button class="btn btn-sm btn-outline-danger" onclick="removeCurrentManualLine()">Remove</button>
        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- Unsaved manual lines guard modal -->
<div class="modal fade" id="unsavedModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-sm">
    <div class="modal-content bg-dark border-danger">
      <div class="modal-header border-danger py-2">
        <h6 class="modal-title text-danger">Unsaved manual lines</h6>
      </div>
      <div class="modal-body small text-muted">You have unsaved manual lines. Save to DB or discard?</div>
      <div class="modal-footer border-danger py-2 gap-1">
        <button class="btn btn-sm btn-success" onclick="_unsavedSave()">Save &amp; Leave</button>
        <button class="btn btn-sm btn-outline-danger" onclick="_unsavedDiscard()">Discard</button>
        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
      </div>
    </div>
  </div>
</div>

<div class="container-fluid py-2">

<div class="tab-content">

<!-- ══════════════════════ LINES ══════════════════════ -->
<div class="tab-pane fade show active" id="tab-lines">
  <!-- Algo types row -->
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
    <span class="text-muted me-1">ORB:</span>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="ORB15_H" checked> 15H</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="ORB15_L" checked> 15L</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="ORB30_H" checked> 30H</label>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="ORB30_L" checked> 30L</label>
    <span class="vr me-2"></span>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="VWAP" checked> VWAP</label>
    <span class="vr me-2"></span>
    <span class="text-muted me-1">Vol:</span>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="POC" checked> POC</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="VAH" checked> VAH</label>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="VAL" checked> VAL</label>
    <span class="vr me-2"></span>
    <span class="text-muted me-1">Round:</span>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="ROUND_BIG" checked> Big</label>
    <label class="me-1"><input class="form-check-input algo-chk" type="checkbox" value="ROUND_MED" checked> Med</label>
    <label class="me-2"><input class="form-check-input algo-chk" type="checkbox" value="ROUND_SML"> Sml</label>
    <span class="vr me-2"></span>
    <label><input class="form-check-input algo-chk" type="checkbox" value="MANUAL" checked> Manual</label>
  </div>
  <!-- Merge + strength + refresh -->
  <div class="d-flex gap-3 align-items-center mb-2 flex-wrap">
    <span class="text-muted small">Merge ≤</span>
    <div class="d-flex gap-2">
      <label class="small"><input class="form-check-input" type="radio" name="merge-thr" value="4"> 4pt</label>
      <label class="small"><input class="form-check-input" type="radio" name="merge-thr" value="8"> 8pt</label>
      <label class="small"><input class="form-check-input" type="radio" name="merge-thr" value="16" checked> 16pt</label>
    </div>
    <label class="small">Strength ≥
      <input type="number" id="min-str-lines" class="form-control form-control-sm d-inline-block"
             style="width:55px" min="1" max="10" value="1" onchange="refreshLines()">
    </label>
    <button class="btn btn-sm btn-outline-secondary" onclick="refreshLines()">Refresh</button>
  </div>
  <!-- Create buttons -->
  <div class="d-flex gap-2 align-items-center mb-2">
    <button class="btn btn-sm btn-primary" onclick="buildLinesDB(false)">Create</button>
    <button class="btn btn-sm btn-outline-danger" onclick="buildLinesDB(true)">Force Create</button>
    <span id="build-db-msg" class="small text-muted ms-1"></span>
  </div>
  <!-- Build progress panel -->
  <div id="build-db-panel" style="display:none;margin-bottom:10px">
    <div class="progress mb-2" style="height:5px">
      <div id="build-db-bar" class="progress-bar bg-primary" role="progressbar" style="width:0%"></div>
    </div>
    <div style="max-height:200px;overflow-y:auto">
      <table class="table table-sm table-bordered mb-0 small">
        <thead class="table-dark sticky-top">
          <tr><th>Date</th><th>MES</th><th>MNQ</th><th>MYM</th><th>M2K</th></tr>
        </thead>
        <tbody id="build-db-tbody"></tbody>
      </table>
    </div>
  </div>
  <!-- Lines table with Date column -->
  <table class="table table-sm table-hover table-bordered mb-1">
    <thead class="table-dark">
      <tr><th>ID</th><th>Sym</th><th>Date</th><th>Price</th><th>Type</th><th>Algo</th><th>Str</th><th>Source</th><th>Armed</th><th></th></tr>
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
  <!-- Row 1: Sym pills | Auto/Draw | mode | interval | Reset Zoom -->
  <div class="d-flex align-items-center flex-wrap gap-2 mb-1">
    <ul class="nav nav-pills mb-0" id="sym-pill-tabs">
      <li class="nav-item"><button class="nav-link active" onclick="selectSym('MES',this)">MES</button></li>
      <li class="nav-item"><button class="nav-link" onclick="selectSym('MNQ',this)">MNQ</button></li>
      <li class="nav-item"><button class="nav-link" onclick="selectSym('MYM',this)">MYM</button></li>
      <li class="nav-item"><button class="nav-link" onclick="selectSym('M2K',this)">M2K</button></li>
    </ul>
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-drawmode-auto" class="btn btn-outline-info active" onclick="setDrawMode('auto',this)">Auto</button>
      <button id="btn-drawmode-draw" class="btn btn-outline-warning"     onclick="setDrawMode('draw',this)">Draw</button>
    </div>
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-mode-candle" class="btn btn-outline-secondary active" onclick="setGraphMode('candle',this)">Candle</button>
      <button id="btn-mode-line"   class="btn btn-outline-secondary"        onclick="setGraphMode('line',this)">Line</button>
      <button id="btn-mode-bars"   class="btn btn-outline-secondary"        onclick="setGraphMode('bars',this)">Bars</button>
    </div>
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-int-30s" class="btn btn-outline-secondary"        onclick="setGraphInterval(0.5,this)">30s</button>
      <button id="btn-int-1"   class="btn btn-outline-secondary"        onclick="setGraphInterval(1,this)">1m</button>
      <button id="btn-int-5"   class="btn btn-outline-secondary active" onclick="setGraphInterval(5,this)">5m</button>
      <button id="btn-int-15"  class="btn btn-outline-secondary"        onclick="setGraphInterval(15,this)">15m</button>
      <button id="btn-int-30"  class="btn btn-outline-secondary"        onclick="setGraphInterval(30,this)">30m</button>
    </div>
    <button class="btn btn-sm btn-outline-secondary ms-auto" onclick="resetZoom()">Reset Zoom</button>
    <span id="bar-count" class="text-muted small ms-1"></span>
  </div>
  <!-- Row 2: Time range | nav -->
  <div class="d-flex align-items-center flex-wrap gap-2 mb-1">
    <div class="btn-group btn-group-sm" role="group">
      <button id="btn-range-all" class="btn btn-outline-secondary active" onclick="setGraphRange('all',this)">All Day</button>
      <button id="btn-range-4h"  class="btn btn-outline-secondary"        onclick="setGraphRange('4h',this)">Last 4h</button>
      <button id="btn-range-1h"  class="btn btn-outline-secondary"        onclick="setGraphRange('1h',this)">Last 1h</button>
    </div>
    <span class="vr"></span>
    <div class="d-flex align-items-center gap-1">
      <button class="btn btn-sm btn-outline-secondary px-2" title="Prev symbol" onclick="navSym(-1)">&#9664;S</button>
      <button class="btn btn-sm btn-outline-secondary px-2" title="Next symbol" onclick="navSym(1)">S&#9654;</button>
      <span class="vr mx-1"></span>
      <button class="btn btn-sm btn-outline-secondary px-2" title="Prev day" onclick="navDay(-1)">&#9664;D</button>
      <span id="day-info" class="small text-muted px-1" style="min-width:90px;text-align:center">&#8212;</span>
      <button class="btn btn-sm btn-outline-secondary px-2" title="Next day" onclick="navDay(1)">D&#9654;</button>
    </div>
  </div>
  <!-- Draw mode controls (hidden in Auto mode) -->
  <div id="draw-controls" class="d-flex align-items-center gap-2 mb-1 px-2 py-1 rounded" style="display:none;background:rgba(255,193,7,.07);border:1px solid rgba(255,193,7,.25)">
    <span class="text-warning small">&#9998; Draw — dbl-click to add/remove</span>
    <label class="small mb-0 ms-2 user-select-none"><input type="checkbox" id="tog-auto-gray" onchange="drawChart()"> Auto (gray)</label>
    <span id="draw-dirty-dot" class="text-warning ms-1" style="display:none" title="Unsaved changes">&#9679;</span>
    <button class="btn btn-sm btn-success ms-auto" onclick="saveManualLines()">Save All</button>
    <button class="btn btn-sm btn-outline-primary" onclick="sendManualLines()">Send</button>
  </div>
  <!-- Mock banner -->
  <div id="mock-banner-graph" class="alert alert-warning py-1 px-2 mb-1 small" style="display:none">
    &#9888; No data for <strong id="mock-req-graph"></strong> &#8212; showing <strong id="mock-date-graph"></strong>
  </div>
  <!-- Sanity bar -->
  <div class="d-flex gap-4 align-items-center px-1 mb-1 small text-muted">
    <span>Trades&nbsp;<b id="sb-trades" class="text-info">&#8212;</b></span>
    <span>Bars&nbsp;<b id="sb-bars" class="text-light">&#8212;</b></span>
    <span>Low&nbsp;<b id="sb-low" class="text-success">&#8212;</b></span>
    <span>High&nbsp;<b id="sb-high" class="text-danger fw-bold">&#8212;</b></span>
    <span id="graph-date-label" class="ms-auto text-muted small"></span>
  </div>
  <!-- Chart -->
  <div id="chart" style="width:100%;height:460px;background:#1a1a2e;border-radius:4px;"></div>
  <!-- Source toggles -->
  <div class="d-flex gap-3 mt-2 flex-wrap small">
    <label><input type="checkbox" id="tog-ohlc"      checked onchange="redrawLines()"><span class="badge source-ohlc">OHLC</span></label>
    <label><input type="checkbox" id="tog-pivot"     checked onchange="redrawLines()"><span class="badge source-pivot">Pivot</span></label>
    <label><input type="checkbox" id="tog-overnight" checked onchange="redrawLines()"><span class="badge source-overnight">Overnight</span></label>
    <label><input type="checkbox" id="tog-orb"       checked onchange="redrawLines()"><span class="badge source-orb">ORB</span></label>
    <label><input type="checkbox" id="tog-vwap"      checked onchange="redrawLines()"><span class="badge source-vwap">VWAP</span></label>
    <label><input type="checkbox" id="tog-volume"    checked onchange="redrawLines()"><span class="badge source-volume">Volume</span></label>
    <label><input type="checkbox" id="tog-round"     checked onchange="redrawLines()"><span class="badge source-round">Round</span></label>
    <label><input type="checkbox" id="tog-manual"    checked onchange="redrawLines()"><span class="badge source-manual">Manual</span></label>
  </div>
</div>

<!-- ══════════════════════ SANDBOX ══════════════════════ -->
<div class="tab-pane fade" id="tab-sandbox">
  <div class="d-flex align-items-center gap-2 mb-1 flex-wrap">
    <ul class="nav nav-pills mb-0" id="sb-sym-pills">
      <li class="nav-item"><button class="nav-link active" onclick="sbSelectSym('MES',this)">MES</button></li>
      <li class="nav-item"><button class="nav-link" onclick="sbSelectSym('MNQ',this)">MNQ</button></li>
      <li class="nav-item"><button class="nav-link" onclick="sbSelectSym('MYM',this)">MYM</button></li>
      <li class="nav-item"><button class="nav-link" onclick="sbSelectSym('M2K',this)">M2K</button></li>
    </ul>
    <input type="date" id="sb-date" class="form-control form-control-sm py-0" style="width:130px;font-size:.8rem;height:28px">
    <button class="btn btn-sm btn-primary" onclick="sbLoad()">Load</button>
    <button class="btn btn-sm btn-outline-secondary" id="sb-transpose-btn" onclick="sbTranspose()">⟲ Transpose</button>
    <button class="btn btn-sm btn-outline-secondary" onclick="sbResetZoom()">⊙ Reset</button>
    <span class="vr"></span>
    <input type="number" id="sb-add-price" class="form-control form-control-sm" placeholder="Price" step="0.25" style="width:82px;height:26px;font-size:.78rem">
    <label class="d-flex align-items-center gap-1 text-success mb-0 user-select-none" style="font-size:.75rem;cursor:pointer">
      <input type="radio" name="sb-add-type" value="SUPPORT" checked> S
    </label>
    <label class="d-flex align-items-center gap-1 text-danger mb-0 user-select-none" style="font-size:.75rem;cursor:pointer">
      <input type="radio" name="sb-add-type" value="RESISTANCE"> R
    </label>
    <span class="text-muted" style="font-size:.7rem">|</span>
    <label class="d-flex align-items-center gap-1 text-muted mb-0 user-select-none" style="font-size:.75rem;cursor:pointer">
      <input type="radio" name="sb-add-conf" value="" checked> ●
    </label>
    <label class="d-flex align-items-center gap-1 text-warning mb-0 user-select-none" style="font-size:.75rem;cursor:pointer">
      <input type="radio" name="sb-add-conf" value="!"> !
    </label>
    <label class="d-flex align-items-center gap-1 text-muted mb-0 user-select-none" style="font-size:.75rem;cursor:pointer">
      <input type="radio" name="sb-add-conf" value="?"> ?
    </label>
    <button class="btn btn-sm btn-outline-secondary" onclick="sbAddManualLine()" style="height:26px;padding:0 8px;font-size:.75rem">+ Line</button>
    <span id="sb-status" class="text-muted small"></span>
    <span id="sb-spinner" class="spinner-border spinner-border-sm text-info" style="display:none"></span>
    <div class="ms-auto d-flex align-items-center gap-3 small text-muted">
      <span id="sb-has-bidask"></span>
      <span>Levels: <b id="sb-level-count">—</b></span>
    </div>
  </div>
  <div id="sb-chart" style="width:100%;height:460px;background:#1a1a2e;border-radius:4px;"></div>
  <div class="d-flex gap-3 mt-1 flex-wrap small" id="sb-col-checks"></div>
</div>

<!-- Sandbox line popup (shared, outside tab-pane so it survives DOM reflow) -->
<div id="sb-popup">
  <div class="d-flex align-items-center justify-content-between mb-1">
    <b id="sb-popup-price" class="font-monospace text-info" style="font-size:.9rem"></b>
    <button onclick="sbClosePopup()" style="background:none;border:none;color:#8b949e;cursor:pointer;line-height:1;padding:0 2px;font-size:.95rem">✕</button>
  </div>
  <div class="d-flex gap-3 mb-1" style="font-size:.75rem">
    <label class="d-flex align-items-center gap-1 text-success user-select-none" style="cursor:pointer">
      <input type="radio" name="sb-popup-type" value="SUPPORT" onchange="sbSetPopupType('SUPPORT')"> Support
    </label>
    <label class="d-flex align-items-center gap-1 text-danger user-select-none" style="cursor:pointer">
      <input type="radio" name="sb-popup-type" value="RESISTANCE" onchange="sbSetPopupType('RESISTANCE')"> Resistance
    </label>
  </div>
  <div class="d-flex gap-3 mb-2" style="font-size:.75rem">
    <label class="d-flex align-items-center gap-1 text-muted user-select-none" style="cursor:pointer">
      <input type="radio" name="sb-popup-conf" value="" onchange="sbSetPopupConf('')"> Normal
    </label>
    <label class="d-flex align-items-center gap-1 text-warning user-select-none" style="cursor:pointer">
      <input type="radio" name="sb-popup-conf" value="!" onchange="sbSetPopupConf('!')"> !
    </label>
    <label class="d-flex align-items-center gap-1 text-muted user-select-none" style="cursor:pointer">
      <input type="radio" name="sb-popup-conf" value="?" onchange="sbSetPopupConf('?')"> ?
    </label>
  </div>
  <div id="sb-popup-body"></div>
  <div class="d-flex gap-1 mt-2">
    <button class="btn btn-sm btn-outline-secondary px-2" onclick="sbMovePopupLine(1)" title="Up 1 tick">▲</button>
    <button class="btn btn-sm btn-outline-secondary px-2" onclick="sbMovePopupLine(-1)" title="Down 1 tick">▼</button>
    <button class="btn btn-sm btn-outline-danger ms-auto" onclick="sbDeletePopupLine()">Delete</button>
  </div>
</div>

<!-- ══════════════════════ ALL SYMBOLS ══════════════════════ -->
<div class="tab-pane fade" id="tab-all">
  <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
    <div class="btn-group btn-group-sm" role="group">
      <button class="btn btn-outline-secondary" onclick="setAllInterval(1,this)">1m</button>
      <button id="btn-all-int-5" class="btn btn-outline-secondary active" onclick="setAllInterval(5,this)">5m</button>
      <button class="btn btn-outline-secondary" onclick="setAllInterval(15,this)">15m</button>
      <button class="btn btn-outline-secondary" onclick="setAllInterval(30,this)">30m</button>
    </div>
    <span class="vr"></span>
    <button class="btn btn-sm btn-outline-secondary px-2" onclick="navAllDay(-1)">&#9664;D</button>
    <span id="all-day-info" class="small text-muted px-1" style="min-width:100px;text-align:center">&#8212;</span>
    <button class="btn btn-sm btn-outline-secondary px-2" onclick="navAllDay(1)">D&#9654;</button>
    <span class="vr"></span>
    <button class="btn btn-sm btn-outline-info" id="all-overlay-btn" onclick="toggleAllOverlay()">&#8853; Overlay</button>
  </div>
  <div id="chart-all-overlay" style="display:none;height:590px;background:#1a1a2e;border-radius:4px"></div>
  <div id="all-grid" class="row g-2">
    <div class="col-6">
      <div class="text-center small text-muted mb-1">MES</div>
      <div id="chart-all-MES" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
    <div class="col-6">
      <div class="text-center small text-muted mb-1">MNQ</div>
      <div id="chart-all-MNQ" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
    <div class="col-6">
      <div class="text-center small text-muted mb-1">MYM</div>
      <div id="chart-all-MYM" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
    <div class="col-6">
      <div class="text-center small text-muted mb-1">M2K</div>
      <div id="chart-all-M2K" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
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
    <label class="small ms-2">Strength &#8805;
      <input type="number" id="min-str-trades" class="form-control form-control-sm d-inline-block"
             style="width:55px" min="1" max="10" value="1">
    </label>
    <button class="btn btn-sm btn-primary" onclick="createTrades()">Create Trades</button>
  </div>
  <div class="d-flex gap-2 mb-2">
    <span class="badge bg-secondary" id="ctr-total">Total: 0</span>
    <span class="badge bg-success"   id="ctr-passed">Passed: 0</span>
    <span class="badge bg-warning text-dark" id="ctr-filtered">Filtered: 0</span>
    <span class="badge bg-info text-dark"    id="ctr-syms">Symbols: &#8212;</span>
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

<!-- ══════════════════════ TEST ══════════════════════ -->
<div class="tab-pane fade" id="tab-test">
  <div class="d-flex align-items-center flex-wrap gap-2 mb-2">
    <span class="fw-bold text-info">MES · 5-Day · 15m</span>
    <span class="text-muted small">dbl-click line to hide · dbl-click chart to add</span>
    <button class="btn btn-sm btn-outline-success ms-auto" onclick="testShowAll()">Show All</button>
    <button class="btn btn-sm btn-outline-secondary" onclick="testResetZoom()">&#8857; Reset</button>
  </div>
  <div id="test-chart" style="width:100%;height:520px;background:#1a1a2e;border-radius:4px;"></div>
  <div class="d-flex gap-3 mt-2 flex-wrap" style="font-size:.75rem">
    <span style="color:#32ba64">&#9135; Support</span>
    <span style="color:#00ff99">&#9135; Strong (!)</span>
    <span style="color:rgba(50,186,100,0.65)">&#9135;&#9135; Uncertain (?)</span>
    <span style="color:rgba(255,165,0,0.8)">&#9135;&#9135; Day low</span>
    <span style="color:#aaa">&#9135;&#9135; Day sep.</span>
    <span style="color:#fff">&#9135; Manual</span>
  </div>
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
// ── Constants ─────────────────────────────────────────────────────────────────
const SOURCE_COLORS={
  ohlc:'#4e79a7',pivot:'#f28e2b',overnight:'#59a14f',manual:'#e15759',
  orb:'#1abc9c',vwap:'#9b59b6',volume:'#e67e22',round:'#7f8c8d'
};

// ── Busy state ────────────────────────────────────────────────────────────────
let _busyCount=0,_busyDisabled=[];
function _enterBusy(){
  if(++_busyCount===1){
    document.body.classList.add('busy-wait');
    _busyDisabled=[];
    document.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled)').forEach(el=>{
      _busyDisabled.push(el);el.disabled=true;
    });
  }
}
function _exitBusy(){
  if(--_busyCount<=0){
    _busyCount=0;
    document.body.classList.remove('busy-wait');
    _busyDisabled.forEach(el=>el.disabled=false);
    _busyDisabled=[];
  }
}
const STATUS_CLS={PENDING:'secondary',SUBMITTED:'primary',SUBMITTING:'info',
                  FILLED:'warning',CLOSED:'success',CANCELLED:'dark',ERROR:'danger'};

// ── Price polling ─────────────────────────────────────────────────────────────
async function pollPrices(){
  try{
    const d=await (await fetch('/api/prices')).json();
    for(const [s,p] of Object.entries(d)){
      const el=document.getElementById('chip-'+s);
      if(el) el.textContent=s+' '+(p!=null?p.toFixed(2):'--');
    }
  }catch(e){}
}
pollPrices();setInterval(pollPrices,5000);

// ── Helpers ───────────────────────────────────────────────────────────────────
function checkedVals(id){
  return[...document.querySelectorAll('#'+id+' input[type=checkbox]:checked')].map(e=>e.value);
}
function strengthColor(s){
  const g=['#555','#666','#777','#888','#999','#aaa','#f0a','#f60','#f80','#f00'];
  return g[Math.max(0,Math.min(9,s-1))];
}
function fmt(v){return v!=null?v.toFixed(2):'--';}

function _lastWeekday(){
  const d=new Date();d.setDate(d.getDate()-1);
  while(d.getDay()===0||d.getDay()===6)d.setDate(d.getDate()-1);
  return d.toISOString().split('T')[0];
}
function _nWeekdaysBack(n){
  const d=new Date();let c=0;d.setDate(d.getDate()-1);
  while(true){
    if(d.getDay()!==0&&d.getDay()!==6)c++;
    if(c===n)return d.toISOString().split('T')[0];
    d.setDate(d.getDate()-1);
  }
}

// ── Shared Controls ───────────────────────────────────────────────────────────
function _sharedSyms(){return['MES','MNQ','MYM','M2K'];}

function _getDateRange(){
  const v=document.querySelector('input[name="date-range"]:checked')?.value||'day';
  const lw=_lastWeekday();
  if(v==='day')    return{from:lw,to:lw};
  if(v==='week')   return{from:_nWeekdaysBack(5),to:lw};
  if(v==='2weeks') return{from:_nWeekdaysBack(10),to:lw};
  return{from:document.getElementById('range-from').value||lw,
         to:document.getElementById('range-to').value||lw};
}

function onDateRangeChange(){
  const isCustom=document.querySelector('input[name="date-range"]:checked')?.value==='custom';
  document.getElementById('range-from').style.display=isCustom?'':'none';
  document.getElementById('range-sep').style.display=isCustom?'':'none';
  document.getElementById('range-to').style.display=isCustom?'':'none';
  const tgt=document.querySelector('#mainTab .nav-link.active')?.dataset?.bsTarget;
  if(tgt==='#tab-lines')refreshLines();
  else if(tgt==='#tab-graph')initGraphAndLoad();
}

// ── LINES ─────────────────────────────────────────────────────────────────────
function setAllAlgos(checked){
  document.querySelectorAll('.algo-chk').forEach(e=>e.checked=checked);
}

async function buildLinesDB(force){
  _enterBusy();
  const msg=document.getElementById('build-db-msg');
  const algos=[...document.querySelectorAll('.algo-chk:checked')].map(e=>e.value);
  const mergeThr=parseFloat(document.querySelector('input[name="merge-thr"]:checked')?.value||'16');
  msg.className='small text-warning ms-1';
  msg.textContent=force?'Force creating...':'Creating missing...';
  try{
    const r=await (await fetch('/api/build_db',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({force,algo_types:algos,merge_threshold:mergeThr,weeks_back:2})
    })).json();
    if(r.error){msg.className='small text-danger ms-1';msg.textContent=''+r.error;return;}
    document.getElementById('build-db-panel').style.display='block';
    document.getElementById('build-db-bar').style.width='0%';
    document.getElementById('build-db-tbody').innerHTML='';
    msg.textContent='Starting...';
    if(_buildPollTimer)clearInterval(_buildPollTimer);
    _buildPollTimer=setInterval(_pollBuildStatus,800);
  }catch(e){msg.className='small text-danger ms-1';msg.textContent='Error: '+e;}
  finally{_exitBusy();}
}

let _buildPollTimer=null;
async function _pollBuildStatus(){
  try{
    const s=await (await fetch('/api/build_db/status')).json();
    const pct=s.total>0?Math.round(s.done/s.total*100):0;
    document.getElementById('build-db-bar').style.width=pct+'%';
    const msg=document.getElementById('build-db-msg');
    msg.className=s.running?'small text-warning ms-1':'small text-success ms-1';
    msg.textContent=s.running
      ?(s.current?s.current+' ('+s.done+'/'+s.total+')':'Running...')
      :('Done -- '+s.done+'/'+s.total+' processed');
    _renderBuildTable(s.log);
    if(!s.running&&_buildPollTimer){clearInterval(_buildPollTimer);_buildPollTimer=null;refreshLines();}
  }catch(e){}
}

function _renderBuildTable(log){
  const SYMS=['MES','MNQ','MYM','M2K'];
  const HOLIDAYS=new Set([
    '2026-01-01','2026-01-19','2026-02-16','2026-04-03',
    '2026-05-25','2026-07-03','2026-09-07','2026-11-26','2026-12-25',
    '2027-01-01','2027-01-18','2027-02-15','2027-04-02',
    '2027-05-31','2027-07-05','2027-09-06','2027-11-25','2027-12-24'
  ]);
  const byDate={};
  for(const e of log){if(!byDate[e.date])byDate[e.date]={};byDate[e.date][e.symbol]=e;}
  const rows=[];
  const now=new Date();
  for(let i=1;i<=14;i++){
    const d=new Date(now);d.setDate(now.getDate()-i);
    const y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),dd=String(d.getDate()).padStart(2,'0');
    rows.push(`${y}-${m}-${dd}`);
  }
  const html=rows.map(d=>{
    const dow=new Date(d+'T12:00:00Z').getDay();
    const isWeekend=dow===0||dow===6;
    const isHoliday=HOLIDAYS.has(d);
    if(isWeekend||isHoliday){
      const tag=dow===6?'SAT':dow===0?'SUN':'HOL';
      return`<tr style="opacity:0.25"><td class="text-muted">${d} <span class="badge bg-secondary" style="font-size:9px">${tag}</span></td>${SYMS.map(()=>'<td></td>').join('')}</tr>`;
    }
    const data=byDate[d];
    const allNoCsv=data&&SYMS.every(s=>!data[s]||data[s].action==='no_csv');
    const rowStyle=allNoCsv?'opacity:0.35':'';
    const cells=SYMS.map(sym=>{
      const e=data?.[sym];
      if(!e)return'<td class="text-muted text-center small">—</td>';
      if(e.action==='done')  return`<td class="text-success text-center">+ ${e.count}</td>`;
      if(e.action==='skip')  return`<td class="text-muted text-center">~ ${e.count}</td>`;
      if(e.action==='no_csv')return`<td class="text-center" style="color:#5a5020;opacity:0.6">no csv</td>`;
      if(e.action==='no_rth')return`<td class="text-warning text-center">no rth</td>`;
      return'<td class="text-danger text-center">?</td>';
    }).join('');
    return`<tr style="${rowStyle}"><td>${d}</td>${cells}</tr>`;
  }).join('');
  document.getElementById('build-db-tbody').innerHTML=html;
}

async function refreshLines(){
  const syms=_sharedSyms();
  const{from,to}=_getDateRange();
  const ms=parseInt(document.getElementById('min-str-lines').value)||1;
  let url=`/api/lines?min_strength=${ms}&date_from=${from}&date_to=${to}`;
  if(syms.length>0&&syms.length<4)url+='&symbols='+syms.join(',');
  try{
    const rows=await (await fetch(url)).json();
    const tb=document.getElementById('lines-tbody');
    tb.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el=>{
      bootstrap.Tooltip.getInstance(el)?.dispose();
    });
    tb.innerHTML='';
    for(const r of rows){
      const sc=SOURCE_COLORS[r.source]||'#888';
      const sl=r.source.charAt(0).toUpperCase()+r.source.slice(1);
      const armedBadge=r.armed
        ?'<span class="badge bg-success">ON</span>'
        :'<span class="badge bg-secondary">off</span>';
      const tr=document.createElement('tr');
      let tipHtml='';
      if(r.note){
        try{
          const n=typeof r.note==='string'?JSON.parse(r.note):r.note;
          const mp=n.merged&&n.merged.length
            ?'<hr style="border-color:#555;margin:3px 0"><span class="text-warning">Absorbed: '
              +n.merged.map(m=>`${m.algo_type}@${m.price.toFixed(2)}`).join(', ')+'</span>':'';
          tipHtml=`<b>${n.label}</b><br><small>${n.formula}</small><br>`
            +`<small class="text-muted">${n.inputs}</small><br>`
            +`<small class="text-muted">From: ${n.from_date}</small>${mp}`;
        }catch(_){}
      }
      tr.innerHTML=`<td>${r.id}</td><td><b>${r.symbol}</b></td>
        <td class="text-muted small">${r.date||'--'}</td>
        <td class="font-monospace">${r.price.toFixed(2)}</td>
        <td>${r.line_type==='SUPPORT'?'<span class="text-success">SUPP</span>':'<span class="text-danger">RESI</span>'}</td>
        <td><small>${r.algo_type}</small></td>
        <td><span class="badge" style="background:${strengthColor(r.strength)}">${r.strength}</span></td>
        <td><span class="badge" style="background:${sc}">${sl}</span></td>
        <td>${armedBadge}</td>
        <td><button class="btn btn-sm btn-outline-danger py-0 px-1" style="font-size:.7rem"
            onclick="delLine(${r.id})">x</button></td>`;
      if(tipHtml){
        tr.setAttribute('data-bs-toggle','tooltip');
        tr.setAttribute('data-bs-html','true');
        tr.setAttribute('data-bs-placement','auto');
        tr.setAttribute('title',tipHtml);
        new bootstrap.Tooltip(tr,{html:true,boundary:'document'});
      }
      tb.appendChild(tr);
    }
  }catch(e){console.error(e);}
}

async function delLine(id){
  await fetch('/api/lines/'+id,{method:'DELETE'});
  refreshLines();
}

async function addManualLine(){
  const sym=document.getElementById('m-sym').value;
  const price=parseFloat(document.getElementById('m-price').value);
  const ltype=document.getElementById('m-type').value;
  const str=parseInt(document.getElementById('m-str').value)||8;
  if(!price){document.getElementById('manual-msg').textContent='Enter price';return;}
  await fetch('/api/lines/manual',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym,price,line_type:ltype,strength:str})});
  document.getElementById('manual-msg').textContent='Added';
  setTimeout(()=>document.getElementById('manual-msg').textContent='',2000);
  refreshLines();
}

// ── GRAPH ─────────────────────────────────────────────────────────────────────
let _graphSym='MES',_chartBars=[],_chartLines=[],_barsProfile=[];
let _graphMode='candle',_graphRange='all',_graphInterval=5;
let _visibleLines=[];
let _graphDates=[],_graphDateIdx=0,_graphCurrentDate=null;
let _graphZoomState=null;
let _graphNaturalYRange=null,_graphNaturalXRange=null;
// ── Draw mode ─────────────────────────────────────────────────────────────────
let _drawMode='auto'; // 'auto'|'draw'
let _manualLines=[],_manualNextId=0,_manualDirty=false;
let _currentManualLine=null;
let _pendingTabTarget=null,_unsavedOnSave=null,_unsavedOnDiscard=null;

async function initGraphAndLoad(){
  const syms=_sharedSyms();
  const today=new Date().toISOString().slice(0,10);
  const from14=new Date(Date.now()-14*86400000).toISOString().slice(0,10);
  const url=`/api/available_dates?date_from=${from14}&date_to=${today}&symbols=${syms.join(',')}`;
  try{
    const d=await (await fetch(url)).json();
    _graphDates=d.dates||[];
    if(_graphCurrentDate&&_graphDates.includes(_graphCurrentDate)){
      _graphDateIdx=_graphDates.indexOf(_graphCurrentDate);
    }else{
      _graphDateIdx=_graphDates.length-1;
    }
    _updateDayInfo();
  }catch(e){console.error(e);}
  if(_graphDates.length){
    await loadGraph();
  }else{
    Plotly.purge('chart');
    document.getElementById('chart').innerHTML=
      '<div class="d-flex align-items-center justify-content-center h-100 text-muted">No data in selected date range</div>';
    document.getElementById('day-info').textContent='--';
  }
}

function _updateDayInfo(){
  const total=_graphDates.length;
  const d=_graphDates[_graphDateIdx]||'';
  document.getElementById('day-info').textContent=
    total>0?`${_graphDateIdx+1}/${total}  ${d}`:'--';
}

async function navDay(delta){
  if(!_graphDates.length)return;
  const newIdx=Math.max(0,Math.min(_graphDates.length-1,_graphDateIdx+delta));
  if(newIdx===_graphDateIdx)return;
  _graphDateIdx=newIdx;
  _graphZoomState=null;
  _updateDayInfo();
  await loadGraph();
}

function navSym(delta){
  const syms=_sharedSyms().length?_sharedSyms():['MES','MNQ','MYM','M2K'];
  const idx=syms.indexOf(_graphSym);
  const next=syms[(idx+delta+syms.length)%syms.length];
  document.querySelectorAll('#sym-pill-tabs .nav-link').forEach(b=>{
    if(b.textContent.trim()===next)selectSym(next,b);
  });
}

function selectSym(sym,btn){
  if(_graphSym!==sym)_graphZoomState=null;
  _graphSym=sym;
  document.querySelectorAll('#sym-pill-tabs .nav-link').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadGraph();
}

function setGraphMode(mode,btn){
  _graphMode=mode;
  document.querySelectorAll('#btn-mode-candle,#btn-mode-line,#btn-mode-bars').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  drawChart();
}

function setGraphRange(range,btn){
  _graphRange=range;
  document.querySelectorAll('#btn-range-all,#btn-range-4h,#btn-range-1h').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(_graphMode!=='bars')drawChart();
}

function setGraphInterval(min,btn){
  _graphInterval=min;
  document.querySelectorAll('#btn-int-30s,#btn-int-1,#btn-int-5,#btn-int-15,#btn-int-30').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadGraph();
}

function filteredBars(){
  if(_graphRange==='all')return _chartBars;
  const bph=Math.round(60/_graphInterval);
  const n=_graphRange==='1h'?bph:bph*4;
  return _chartBars.slice(-n);
}

async function loadGraph(){
  const reqDate=_graphDates[_graphDateIdx];
  if(!reqDate)return;
  if(reqDate!==_graphCurrentDate)_graphZoomState=null;
  _graphCurrentDate=reqDate;
  _enterBusy();
  try{
    const br=await (await fetch(`/api/history/${_graphSym}?interval=${_graphInterval}&date=${reqDate}`)).json();
    _chartBars=br.bars||[];
    const mock=br.mock_date;
    document.getElementById('mock-banner-graph').style.display=mock?'block':'none';
    if(mock){
      document.getElementById('mock-req-graph').textContent=reqDate;
      document.getElementById('mock-date-graph').textContent=mock;
    }
    document.getElementById('sb-trades').textContent=br.total_ticks!=null?br.total_ticks.toLocaleString():'--';
    document.getElementById('graph-date-label').textContent=br.date||reqDate;
    const ms=parseInt(document.getElementById('min-str-lines').value)||1;
    const lineDate=br.date||reqDate;
    _chartLines=await (await fetch(`/api/lines?symbol=${_graphSym}&min_strength=${ms}&date=${lineDate}`)).json();
    const vp=await (await fetch(`/api/volume_profile/${_graphSym}?date=${reqDate}`)).json();
    _barsProfile=vp.profile||[];
    drawChart();
  }catch(e){console.error(e);}
  finally{_exitBusy();}
}

function enabledSources(){
  const s=new Set();
  ['ohlc','pivot','overnight','orb','vwap','volume','round','manual'].forEach(src=>{
    const el=document.getElementById('tog-'+src);
    if(el&&el.checked)s.add(src);
  });
  return s;
}

// ── Line Popup ────────────────────────────────────────────────────────────────
let _currentPopupLine=null;

function openLinePopup(line){
  _currentPopupLine=line;
  const note=(()=>{
    try{return typeof line.note==='string'?JSON.parse(line.note):(line.note||{});}
    catch(_){return{};}
  })();
  const armed=line._armed!==undefined?line._armed:!!line.armed;
  document.getElementById('lm-title').textContent=
    `${line.algo_type} @ ${line.price.toFixed(2)}  --  ${line.line_type}`;
  const mh=note.merged&&note.merged.length
    ?`<div class="mt-2 pt-2 border-top border-secondary"><small class="text-warning">Absorbed: ${note.merged.map(m=>`${m.algo_type}@${m.price.toFixed(2)}`).join(', ')}</small></div>`:'';
  document.getElementById('lm-body').innerHTML=`
    <div class="row g-1 small">
      <div class="col-3 text-muted">Symbol</div><div class="col-9">${line.symbol||'--'}</div>
      <div class="col-3 text-muted">Price</div><div class="col-9 font-monospace fw-bold">${line.price.toFixed(2)}</div>
      <div class="col-3 text-muted">Type</div><div class="col-9">${line.line_type||'--'}</div>
      <div class="col-3 text-muted">Strength</div><div class="col-9">${line.strength}</div>
      <div class="col-3 text-muted">Algo</div><div class="col-9">${note.label||line.algo_type||'--'}</div>
      ${note.formula?`<div class="col-3 text-muted">Formula</div><div class="col-9 text-light">${note.formula}</div>`:''}
      ${note.inputs?`<div class="col-3 text-muted">Inputs</div><div class="col-9 text-muted small">${note.inputs}</div>`:''}
      ${note.from_date?`<div class="col-3 text-muted">From</div><div class="col-9">${note.from_date}</div>`:''}
      <div class="col-3 text-muted">Status</div><div class="col-9">${armed?'<span class="text-success">Enabled</span>':'<span class="text-secondary">Disabled (dotted)</span>'}</div>
    </div>${mh}`;
  const btn=document.getElementById('lm-toggle-btn');
  btn.textContent=armed?'Disable':'Enable';
  btn.className=armed?'btn btn-sm btn-outline-warning':'btn btn-sm btn-outline-success';
  new bootstrap.Modal(document.getElementById('lineModal')).show();
}

function toggleCurrentLine(){
  const line=_currentPopupLine;
  if(!line)return;
  const wasArmed=line._armed!==undefined?line._armed:!!line.armed;
  line._armed=!wasArmed;
  fetch(`/api/lines/${line.id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({armed:line._armed?1:0})});
  bootstrap.Modal.getInstance(document.getElementById('lineModal'))?.hide();
  drawChart();
}

// ── Chart ─────────────────────────────────────────────────────────────────────
function buildLineTraces(bars){
  if(!bars.length)return[];
  const x0=bars[0].t,x1=bars[bars.length-1].t;
  const en=enabledSources();
  _visibleLines=_chartLines.filter(l=>en.has(l.source));
  return _visibleLines.map(l=>{
    const armed=l._armed!==undefined?l._armed:!!l.armed;
    const col=armed?(SOURCE_COLORS[l.source]||'#888'):'rgba(128,128,128,0.35)';
    return{type:'scatter',mode:'lines',x:[x0,x1],y:[l.price,l.price],
      line:{color:col,width:armed?3:1,dash:armed?'solid':'dot'},
      name:l.algo_type,hovertemplate:`<b>${l.algo_type}</b> ${l.price.toFixed(2)}<extra></extra>`,
      showlegend:false};
  });
}

function buildAnnotations(bars){
  const en=enabledSources();
  return _chartLines.filter(l=>en.has(l.source)).map(l=>{
    const armed=l._armed!==undefined?l._armed:!!l.armed;
    const col=armed?(SOURCE_COLORS[l.source]||'#888'):'rgba(128,128,128,0.45)';
    return{xref:'paper',yref:'y',x:1,y:l.price,text:`${l.algo_type} ${l.price}`,
      showarrow:false,xanchor:'right',font:{size:9,color:col}};
  });
}

function buildBarsLineTraces(){
  if(!_barsProfile.length)return[];
  const en=enabledSources();
  _visibleLines=_chartLines.filter(l=>en.has(l.source));
  const maxCount=Math.max(..._barsProfile.map(p=>p.count),1);
  return _visibleLines.map(l=>{
    const armed=l._armed!==undefined?l._armed:!!l.armed;
    const col=armed?(SOURCE_COLORS[l.source]||'#888'):'rgba(128,128,128,0.35)';
    return{type:'scatter',mode:'lines',x:[0,maxCount],y:[l.price,l.price],
      line:{color:col,width:armed?3:1,dash:armed?'solid':'dot'},
      name:l.algo_type,showlegend:false,
      hovertemplate:`<b>${l.algo_type}</b> ${l.price.toFixed(2)}<extra></extra>`};
  });
}

function buildBarsAnnotations(){
  const en=enabledSources();
  return _chartLines.filter(l=>en.has(l.source)).map(l=>{
    const armed=l._armed!==undefined?l._armed:!!l.armed;
    const col=armed?(SOURCE_COLORS[l.source]||'#888'):'rgba(128,128,128,0.45)';
    return{xref:'paper',yref:'y',x:1,y:l.price,text:l.algo_type,
      showarrow:false,xanchor:'right',font:{size:9,color:col}};
  });
}

function _attachChartHandlers(){
  const el=document.getElementById('chart');
  el.on('plotly_click',function(evtData){
    if(!evtData?.points?.length)return;
    const pt=evtData.points[0];
    if(_drawMode==='draw'){
      const nm=pt.data?.name||'';
      if(nm.startsWith('ml_')){
        const mlId=parseInt(nm.replace('ml_',''));
        const ml=_manualLines.find(l=>l.id===mlId);
        if(ml)_showManualNamePopup(ml);
      }
      return;
    }
    const cn=pt.curveNumber;
    if(cn===0)return;
    const line=_visibleLines[cn-1];
    if(line)openLinePopup(line);
  });
  el.on('plotly_relayout',function(evt){
    if(evt['xaxis.range[0]']!=null){
      _graphZoomState={
        x:[evt['xaxis.range[0]'],evt['xaxis.range[1]']],
        y:evt['yaxis.range[0]']!=null?[evt['yaxis.range[0]'],evt['yaxis.range[1]']]:null
      };
    }else if(evt['xaxis.autorange']){
      _graphZoomState=null;
    }
  });
  _attachDblClick();
}

function _applyZoom(layout){
  if(!_graphZoomState)return;
  if(_graphZoomState.x){layout.xaxis.range=_graphZoomState.x;delete layout.xaxis.autorange;}
  if(_graphZoomState.y){layout.yaxis.range=_graphZoomState.y;delete layout.yaxis.autorange;}
}

function drawChart(){
  if(_graphMode==='bars'){drawBarsMode();return;}
  if(!_chartBars.length){
    Plotly.purge('chart');
    document.getElementById('chart').innerHTML=
      `<div class="d-flex align-items-center justify-content-center h-100 text-muted">No history data for ${_graphSym}</div>`;
    return;
  }
  const bars=filteredBars();
  document.getElementById('bar-count').textContent=bars.length+' bars';
  const yLow=Math.min(...bars.map(b=>b.low));
  const yHigh=Math.max(...bars.map(b=>b.high));
  const yPad=(yHigh-yLow)*0.07;
  _graphNaturalYRange=[yLow-yPad,yHigh+yPad];
  document.getElementById('sb-bars').textContent=bars.length;
  document.getElementById('sb-low').textContent=yLow.toFixed(2);
  document.getElementById('sb-high').textContent=yHigh.toFixed(2);
  let barTrace;
  if(_graphMode==='line'){
    barTrace={type:'scatter',mode:'lines',x:bars.map(b=>b.t),y:bars.map(b=>b.close),
      name:_graphSym,line:{color:'#7db3d8',width:1.5},showlegend:false};
  }else{
    barTrace={type:'candlestick',x:bars.map(b=>b.t),
      open:bars.map(b=>b.open),high:bars.map(b=>b.high),
      low:bars.map(b=>b.low),close:bars.map(b=>b.close),name:_graphSym,
      increasing:{line:{color:'#26a69a'}},decreasing:{line:{color:'#ef5350'}},showlegend:false};
  }
  const lineTraces=_drawMode==='draw'
    ?[...buildAutoGrayTraces(bars),...buildManualTraces(bars)]
    :buildLineTraces(bars);
  const annotations=_drawMode==='draw'?[]:buildAnnotations(bars);
  const layout={paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
    font:{color:'#ccc'},margin:{l:55,r:10,t:10,b:40},
    xaxis:{rangeslider:{visible:false},gridcolor:'#333'},
    yaxis:{range:[yLow-yPad,yHigh+yPad],gridcolor:'#333'},
    annotations,showlegend:false,dragmode:'zoom'};
  _applyZoom(layout);
  Plotly.newPlot('chart',[barTrace,...lineTraces],layout,{responsive:true,displayModeBar:false,doubleClick:false});
  _attachChartHandlers();
}

function drawBarsMode(){
  const chartEl=document.getElementById('chart');
  if(!_barsProfile.length){
    Plotly.purge('chart');
    chartEl.innerHTML=`<div class="d-flex align-items-center justify-content-center h-100 text-muted">No volume data for ${_graphSym}</div>`;
    return;
  }
  document.getElementById('bar-count').textContent=_barsProfile.length+' price levels';
  const prices=_barsProfile.map(p=>p.price);
  const counts=_barsProfile.map(p=>p.count);
  const priceMin=Math.min(...prices);
  const priceMax=Math.max(...prices);
  const pricePad=(priceMax-priceMin)*0.05||1;
  const countMax=Math.max(...counts,1);
  const countPad=countMax*0.05;
  _graphNaturalXRange=[0,countMax+countPad];
  _graphNaturalYRange=[priceMin-pricePad,priceMax+pricePad];
  const barTrace={type:'bar',orientation:'h',x:counts,y:prices,
    marker:{color:'#4e79a7',opacity:0.75},showlegend:false,
    hovertemplate:'%{y:.2f}: %{x} ticks<extra></extra>'};
  const lineTraces=_drawMode==='draw'
    ?[...buildAutoGrayTraces(null),...buildManualTraces(null)]
    :buildBarsLineTraces();
  const annotations=_drawMode==='draw'?[]:buildBarsAnnotations();
  const layout={paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
    font:{color:'#ccc'},margin:{l:55,r:10,t:10,b:40},bargap:0.05,
    xaxis:{range:_graphNaturalXRange,gridcolor:'#333',title:{text:'Ticks',font:{size:10}}},
    yaxis:{range:_graphNaturalYRange,gridcolor:'#333',title:{text:'Price',font:{size:10}}},
    annotations,showlegend:false,dragmode:'zoom'};
  _applyZoom(layout);
  Plotly.newPlot('chart',[barTrace,...lineTraces],layout,{responsive:true,displayModeBar:false,doubleClick:false});
  _attachChartHandlers();
}

function resetZoom(){
  _graphZoomState=null;
  const update={};
  if(_graphMode==='bars'){
    if(_graphNaturalXRange)update['xaxis.range']=_graphNaturalXRange;
    else update['xaxis.autorange']=true;
    update['yaxis.autorange']=true;
  }else{
    update['xaxis.autorange']=true;
    if(_graphNaturalYRange)update['yaxis.range']=_graphNaturalYRange;
    else update['yaxis.autorange']=true;
  }
  try{Plotly.relayout('chart',update);}catch(_){}
}

function redrawLines(){
  if(_graphMode==='bars'){if(_barsProfile.length)drawBarsMode();}
  else{if(_chartBars.length)drawChart();}
}

document.getElementById('btn-graph-tab').addEventListener('click',function(){
  initGraphAndLoad();
});

// ── ALL SYMBOLS ───────────────────────────────────────────────────────────────
let _allInterval=5, _allOverlay=false;

function setAllInterval(v,btn){
  _allInterval=v;
  document.querySelectorAll('#tab-all .btn-group-sm .btn').forEach(b=>b.classList.remove('active'));
  if(btn)btn.classList.add('active');
  loadAllSymbols();
}

function toggleAllOverlay(){
  _allOverlay=!_allOverlay;
  document.getElementById('all-overlay-btn').classList.toggle('active',_allOverlay);
  document.getElementById('chart-all-overlay').style.display=_allOverlay?'block':'none';
  document.getElementById('all-grid').style.display=_allOverlay?'none':'flex';
  // Wait one frame so browser reflows the newly-visible div before Plotly queries its dimensions
  requestAnimationFrame(()=>loadAllSymbols());
}

async function navAllDay(delta){
  if(!_graphDates.length)return;
  const newIdx=Math.max(0,Math.min(_graphDates.length-1,_graphDateIdx+delta));
  if(newIdx===_graphDateIdx)return;
  _graphDateIdx=newIdx;
  _updateDayInfo();
  await loadAllSymbols();
}

async function loadAllSymbols(){
  if(!_graphDates.length){
    await initGraphAndLoad();
    if(!_graphDates.length)return;
  }
  const reqDate=_graphDates[_graphDateIdx]||'';
  if(!reqDate)return;
  document.getElementById('all-day-info').textContent=
    `${_graphDateIdx+1}/${_graphDates.length}  ${reqDate}`;
  if(_allOverlay) await _loadOverlayAll(reqDate);
  else await Promise.all(['MES','MNQ','MYM','M2K'].map(sym=>_loadOneSymAll(sym,reqDate)));
}

async function _loadOverlayAll(reqDate){
  const el=document.getElementById('chart-all-overlay');
  const SYM_COLORS={MES:'#5B8DD9',MNQ:'#E8A838',MYM:'#32BA64',M2K:'#D25050'};
  const SYMS=['MES','MNQ','MYM','M2K'];
  let data;
  try{
    data=await Promise.all(SYMS.map(s=>
      fetch(`/api/history/${s}?interval=${_allInterval}&date=${reqDate}`).then(r=>r.json())
    ));
  }catch(e){el.innerHTML=`<div class="text-danger small p-2">${e}</div>`;return;}
  const traces=[];
  for(let i=0;i<SYMS.length;i++){
    const sym=SYMS[i],bars=data[i].bars||[];
    if(!bars.length)continue;
    const base=bars[0].close??bars[0].open;
    const tick=SB_TICKS[sym]||0.25;
    traces.push({
      name:sym,type:'scatter',mode:'lines',
      x:bars.map(b=>b.t),
      y:bars.map(b=>Math.round((b.close-base)/tick)),
      line:{color:SYM_COLORS[sym],width:1.5},
      hovertemplate:`<b>${sym}</b><br>%{x}<br>%{y} ticks<extra></extra>`,
    });
  }
  if(!traces.length){el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No data</div>';return;}
  Plotly.newPlot(el,traces,{
    paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
    font:{color:'#ccc',size:10},margin:{t:8,b:30,l:50,r:8},
    xaxis:{gridcolor:'#252535',zeroline:false,rangeslider:{visible:false}},
    yaxis:{gridcolor:'#252535',zeroline:true,zerolinecolor:'#555',title:'Ticks from open'},
    showlegend:true,legend:{x:0,y:1,bgcolor:'rgba(0,0,0,0)',font:{size:11}},
    dragmode:'zoom',
  },{responsive:true,displayModeBar:false,scrollZoom:true});
}

async function _loadOneSymAll(sym,reqDate){
  const el=document.getElementById(`chart-all-${sym}`);
  try{
    const [hd,ld]=await Promise.all([
      fetch(`/api/history/${sym}?interval=${_allInterval}&date=${reqDate}`).then(r=>r.json()),
      fetch(`/api/lines?symbol=${sym}&date=${reqDate}`).then(r=>r.json())
    ]);
    const bars=hd.bars||[];
    if(!bars.length){
      Plotly.purge(el);
      el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No data</div>';
      return;
    }
    const yLow=Math.min(...bars.map(b=>b.low));
    const yHigh=Math.max(...bars.map(b=>b.high));
    const yPad=(yHigh-yLow)*0.07;
    const x0=bars[0].t,x1=bars[bars.length-1].t;
    const candleTrace={type:'candlestick',x:bars.map(b=>b.t),
      open:bars.map(b=>b.open),high:bars.map(b=>b.high),
      low:bars.map(b=>b.low),close:bars.map(b=>b.close),name:sym,
      increasing:{line:{color:'#26a69a'}},decreasing:{line:{color:'#ef5350'}},
      showlegend:false};
    const lineTraces=(Array.isArray(ld)?ld:[]).map(l=>{
      const col=SOURCE_COLORS[l.source]||'#888';
      return{type:'scatter',mode:'lines',x:[x0,x1],y:[l.price,l.price],
        line:{color:col,width:2,dash:l.armed?'solid':'dot'},
        hovertemplate:`<b>${l.algo_type}</b> ${l.price.toFixed(2)}<extra></extra>`,
        showlegend:false};
    });
    const layout={paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
      font:{color:'#ccc',size:9},margin:{l:50,r:5,t:5,b:30},
      xaxis:{rangeslider:{visible:false},gridcolor:'#333'},
      yaxis:{range:[yLow-yPad,yHigh+yPad],gridcolor:'#333'},
      showlegend:false,dragmode:'zoom'};
    Plotly.newPlot(el,[candleTrace,...lineTraces],layout,{responsive:true,displayModeBar:false});
  }catch(e){
    el.innerHTML=`<div class="text-danger small p-2">${e}</div>`;
  }
}

document.getElementById('btn-all-tab').addEventListener('click',function(){
  loadAllSymbols();
});

// ── DRAW MODE ─────────────────────────────────────────────────────────────────
function setDrawMode(mode,btn){
  if(_manualDirty&&_drawMode==='draw'&&mode==='auto'){
    _pendingTabTarget=null;
    _unsavedOnSave=async()=>{await saveManualLines();_applyDrawMode('auto',btn);};
    _unsavedOnDiscard=()=>{_manualLines=[];_manualDirty=false;_applyDrawMode('auto',btn);};
    new bootstrap.Modal(document.getElementById('unsavedModal')).show();
    return;
  }
  _applyDrawMode(mode,btn);
}
function _applyDrawMode(mode,btn){
  _drawMode=mode;
  document.querySelectorAll('[id^="btn-drawmode-"]').forEach(b=>b.classList.remove('active'));
  if(btn)btn.classList.add('active');
  const dc=document.getElementById('draw-controls');
  dc.style.display=mode==='draw'?'flex':'none';
  if(mode==='draw'){_manualLines=[];_manualDirty=false;_updateDirtyDot();}
  drawChart();
}
function _updateDirtyDot(){
  document.getElementById('draw-dirty-dot').style.display=_manualDirty?'inline':'none';
}

function _pixelToPrice(offsetY){
  const gd=document.getElementById('chart');
  const fl=gd._fullLayout;
  if(!fl)return null;
  const ya=fl.yaxis;
  if(!ya||!ya.range)return null;
  const[yMin,yMax]=ya.range;
  const top=(ya._offset!=null&&ya._offset>0)?ya._offset:(fl.margin?.t||10);
  const h  =(ya._length >0                )?ya._length :(gd.offsetHeight-(fl.margin?.t||10)-(fl.margin?.b||40));
  if(h<=0)return null;
  return yMax-((offsetY-top)/h)*(yMax-yMin);
}
function _pxTolerance(){
  const gd=document.getElementById('chart');
  if(!gd._fullLayout)return 1;
  const ya=gd._fullLayout.yaxis;
  const[yMin,yMax]=(ya&&ya.range)||[0,1];
  const h=ya._length||gd.offsetHeight;
  return 2*(yMax-yMin)/h;
}

function _attachDblClick(){
  const el=document.getElementById('chart');
  if(el._dblHandler)el.removeEventListener('click',el._dblHandler,true);
  let _dblT=0,_dblY=0;
  el._dblHandler=function(e){
    const now=Date.now();
    if(now-_dblT<400&&Math.abs(e.offsetY-_dblY)<12){
      // second click = double-click: add/remove line
      e.stopPropagation();e.preventDefault();
      _dblT=0;
      // auto-enter draw mode if needed
      if(_drawMode!=='draw')_applyDrawMode('draw',document.getElementById('btn-drawmode-draw'));
      const price=_pixelToPrice(e.offsetY);
      if(price===null)return;
      const tol=_pxTolerance();
      const idx=_manualLines.findIndex(ml=>Math.abs(ml.price-price)<=tol);
      if(idx>=0){
        // double-click on existing line → open popup to edit/remove
        _showManualNamePopup(_manualLines[idx]);
      }else{
        const mid=_graphNaturalYRange?(_graphNaturalYRange[0]+_graphNaturalYRange[1])/2:price;
        const ml={id:_manualNextId++,price,label:'',type:price>=mid?'RESISTANCE':'SUPPORT'};
        _manualLines.push(ml);
        _manualDirty=true;_updateDirtyDot();
        drawChart();
        _showManualNamePopup(ml);
      }
    }else{
      _dblT=now;_dblY=e.offsetY;
    }
  };
  el.addEventListener('click',el._dblHandler,true);
}

function _mlColor(ml){
  return ml.type==='SUPPORT'?'#2ecc71':ml.type==='RESISTANCE'?'#e74c3c':'#f1c40f';
}
function buildManualTraces(bars){
  if(!_manualLines.length)return[];
  if(bars&&bars.length){
    const x0=bars[0].t,x1=bars[bars.length-1].t;
    return _manualLines.map(ml=>({
      type:'scatter',mode:'lines',
      x:[x0,x1],y:[ml.price,ml.price],
      line:{color:_mlColor(ml),width:2.5,dash:'solid'},
      name:`ml_${ml.id}`,
      hovertemplate:`<b>${ml.label||ml.type}</b> ${ml.price.toFixed(2)}<extra></extra>`,
      showlegend:false
    }));
  }
  const xr=_graphNaturalXRange||[0,1];
  return _manualLines.map(ml=>({
    type:'scatter',mode:'lines',
    x:[xr[0],xr[1]],y:[ml.price,ml.price],
    line:{color:_mlColor(ml),width:2.5,dash:'solid'},
    name:`ml_${ml.id}`,
    hovertemplate:`<b>${ml.label||ml.type}</b> ${ml.price.toFixed(2)}<extra></extra>`,
    showlegend:false
  }));
}
function buildAutoGrayTraces(bars){
  if(!document.getElementById('tog-auto-gray')?.checked)return[];
  if(!_chartLines.length)return[];
  if(bars&&bars.length){
    const x0=bars[0].t,x1=bars[bars.length-1].t;
    return _chartLines.map(l=>({
      type:'scatter',mode:'lines',
      x:[x0,x1],y:[l.price,l.price],
      line:{color:'rgba(160,160,160,0.3)',width:1,dash:'dot'},
      hovertemplate:`${l.algo_type} ${l.price.toFixed(2)}<extra></extra>`,
      showlegend:false
    }));
  }
  const xr=_graphNaturalXRange||[0,1];
  return _chartLines.map(l=>({
    type:'scatter',mode:'lines',
    x:[xr[0],xr[1]],y:[l.price,l.price],
    line:{color:'rgba(160,160,160,0.3)',width:1,dash:'dot'},
    hovertemplate:`${l.algo_type} ${l.price.toFixed(2)}<extra></extra>`,
    showlegend:false
  }));
}

function _showManualNamePopup(ml){
  _currentManualLine=ml;
  document.getElementById('ml-price-display').textContent=ml.price.toFixed(2);
  document.getElementById('ml-name-input').value=ml.label||'';
  document.getElementById('ml-type-select').value=ml.type||'SUPPORT';
  new bootstrap.Modal(document.getElementById('manualLineModal')).show();
}
function pickAndSave(type){
  if(!_currentManualLine)return;
  _currentManualLine.label=document.getElementById('ml-name-input').value;
  _currentManualLine.type=type;
  _manualDirty=true;_updateDirtyDot();
  bootstrap.Modal.getInstance(document.getElementById('manualLineModal')).hide();
  drawChart();
}
function saveManualLineName(){
  if(!_currentManualLine)return;
  _currentManualLine.label=document.getElementById('ml-name-input').value;
  _manualDirty=true;_updateDirtyDot();
  bootstrap.Modal.getInstance(document.getElementById('manualLineModal')).hide();
  drawChart();
}
function removeCurrentManualLine(){
  if(!_currentManualLine)return;
  _manualLines=_manualLines.filter(ml=>ml.id!==_currentManualLine.id);
  _manualDirty=true;_updateDirtyDot();
  bootstrap.Modal.getInstance(document.getElementById('manualLineModal')).hide();
  drawChart();
}

async function saveManualLines(){
  const reqDate=_graphDates[_graphDateIdx]||'';
  if(!reqDate||!_manualLines.length){_manualDirty=false;_updateDirtyDot();return;}
  const payload=_manualLines.map(ml=>({
    symbol:_graphSym,date:reqDate,price:ml.price,
    line_type:ml.type||'SUPPORT',label:ml.label||'',strength:8
  }));
  try{
    await fetch('/api/lines/manual',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lines:payload})});
    _manualDirty=false;_updateDirtyDot();
    refreshLines();
  }catch(e){console.error(e);}
}
async function sendManualLines(){
  await saveManualLines();
  document.querySelector('[data-bs-target="#tab-trades"]').click();
}

function _unsavedSave(){
  bootstrap.Modal.getInstance(document.getElementById('unsavedModal'))?.hide();
  if(_unsavedOnSave)_unsavedOnSave();
}
function _unsavedDiscard(){
  bootstrap.Modal.getInstance(document.getElementById('unsavedModal'))?.hide();
  if(_unsavedOnDiscard)_unsavedOnDiscard();
}

// Tab-switch guard
document.querySelectorAll('#mainTab .top-tab').forEach(btn=>{
  btn.addEventListener('click',function(e){
    if(!_manualDirty||_drawMode!=='draw')return;
    const activeTarget=document.querySelector('#mainTab .top-tab.active')?.dataset?.bsTarget;
    if(activeTarget!=='#tab-graph')return;
    if(btn.dataset?.bsTarget==='#tab-graph')return;
    e.preventDefault();e.stopImmediatePropagation();
    const target=btn;
    _unsavedOnSave=async()=>{await saveManualLines();target.click();};
    _unsavedOnDiscard=()=>{_manualLines=[];_manualDirty=false;_updateDirtyDot();target.click();};
    new bootstrap.Modal(document.getElementById('unsavedModal')).show();
  },true);
});

// ── CREATE TRADES ─────────────────────────────────────────────────────────────
let _candidates=[];

async function createTrades(){
  _enterBusy();
  const syms=checkedVals('sym-trades');
  const brackets=[...document.querySelectorAll('.bkt-chk:checked')].map(e=>parseFloat(e.value));
  const ms=parseInt(document.getElementById('min-str-trades').value)||1;
  document.getElementById('trades-msg').textContent='Generating...';
  try{
    const d=await (await fetch('/api/trades/create',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:syms,brackets,min_strength:ms})})).json();
    _candidates=d.candidates||[];
    document.getElementById('ctr-total').textContent='Total: '+d.total;
    document.getElementById('ctr-passed').textContent='Passed: '+d.passed;
    document.getElementById('ctr-filtered').textContent='Filtered: '+d.filtered;
    document.getElementById('ctr-syms').textContent='Symbols: '+(d.symbols_covered||[]).join(', ');
    const btn=document.getElementById('btn-submit');
    btn.textContent=`Submit ${_candidates.length} Trades`;
    btn.disabled=_candidates.length===0;
    document.getElementById('trades-msg').textContent='';
    renderTrades(_candidates);
  }catch(e){document.getElementById('trades-msg').textContent='Error: '+e;}
  finally{_exitBusy();}
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
  if(!_candidates.length)return;
  _enterBusy();
  const btn=document.getElementById('btn-submit');
  btn.disabled=true;
  try{
    const d=await (await fetch('/api/trades/submit',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({candidates:_candidates})})).json();
    document.getElementById('trades-msg').textContent=`Submitted ${d.submitted}`;
    _candidates=[];btn.textContent='Submit 0 Trades';
    document.getElementById('trades-tbody').innerHTML='';
  }catch(e){document.getElementById('trades-msg').textContent='Error: '+e;btn.disabled=false;}
  finally{_exitBusy();}
}

// ── SUBMITTED ─────────────────────────────────────────────────────────────────
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
        <td>${r.bracket||'--'}</td>
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

// ── Sandbox ───────────────────────────────────────────────────────────────────
// Line shape helpers — Support=green, Resistance=red; !=solid bright, blank=solid, ?=dashed dim
function _sbLineColor(type, conf){
  if(type==='SUPPORT'){
    return conf==='?' ? 'rgba(50,186,100,.45)' : '#32ba64';
  }
  return conf==='?' ? 'rgba(210,80,80,.45)' : '#d25050';
}
function _sbLineDash(conf){ return conf==='?' ? 'dash' : 'solid'; }
function _sbLineWidth(conf){ return conf==='!' ? 3 : 2; }

const SB_COLS=[
  {key:'total_volume', label:'Volume',   color:'rgba(91,141,217,.65)',  on:true,  group:'blue'},
  {key:'visits',       label:'Visits',   color:'rgba(160,191,224,.55)', on:false, group:'blue'},
  {key:'price_change', label:'Change #', color:'rgba(130,130,130,.55)', on:false, group:'blue'},
  {key:'change_vol',   label:'Chg Vol',  color:'rgba(170,170,170,.50)', on:false, group:'blue'},
  {key:'delta',        label:'Delta',    color:'rgba(60,205,200,.70)',  on:true,  group:'blue'},
  {key:'total_ask',    label:'Ask Liq',  color:'rgba(210,140,60,.65)',  on:false, group:'blue'},
  {key:'total_bid',    label:'Bid Liq',  color:'rgba(150,100,210,.65)', on:false, group:'blue'},
  {key:'price_up',     label:'Up #',     color:'rgba(50,186,100,.70)',  on:true,  group:'green'},
  {key:'up_vol',       label:'Up Vol',   color:'rgba(100,220,140,.65)', on:true,  group:'green'},
  {key:'price_down',   label:'Down #',   color:'rgba(210,80,80,.70)',   on:true,  group:'red'},
  {key:'down_vol',     label:'Dn Vol',   color:'rgba(220,110,110,.65)', on:true,  group:'red'},
];

const SB_TICKS={MES:0.25,MNQ:0.25,MYM:1.0,M2K:0.10};

let _sbSym='MES', _sbRows=[], _sbTransposed=false, _sbChecksBuilt=false;
let _sbPriceRange=null; // [min, max] of current data prices
let _sbLines=[];  // [{id, price, type:'SUPPORT'|'RESISTANCE', confidence:'|'?'|'!'}]
let _sbPopupPrice=null;
let _sbLastClick={t:0,price:null};

function sbSelectSym(sym,btn){
  _sbSym=sym;
  document.querySelectorAll('#sb-sym-pills .nav-link').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
}

function _sbNorm(values,isDelta){
  const absMax=isDelta
    ?Math.max(...values.map(v=>Math.abs(v??0)))
    :Math.max(...values.map(v=>v??0));
  if(absMax===0)return values.map(()=>0);
  return values.map(v=>(v??0)/absMax);
}

async function sbLoad(){
  const dt=document.getElementById('sb-date').value;
  if(!dt){document.getElementById('sb-status').textContent='Pick a date';return;}
  document.getElementById('sb-spinner').style.display='inline-block';
  document.getElementById('sb-status').textContent='Building profile…';
  try{
    const [pr, lr]=await Promise.all([
      fetch(`/api/sandbox/profile/${_sbSym}/${dt}`),
      fetch(`/api/sandbox/lines/${_sbSym}/${dt}`),
    ]);
    const [pj, lj]=await Promise.all([pr.json(), lr.json()]);
    if(!pr.ok){document.getElementById('sb-status').textContent=pj.error||'no data';return;}
    _sbRows=pj.rows;
    _sbLines=(lj.lines||[]).map(l=>({id:l.id, price:l.price, type:l.line_type, confidence:l.confidence||''}));
    sbPlot();
    const hasBa=_sbRows.some(r=>r.total_ask!==null||r.total_bid!==null);
    document.getElementById('sb-has-bidask').innerHTML=hasBa
      ?'<span class="badge bg-success">Bid/Ask ✓</span>'
      :'<span class="badge bg-secondary">No Bid/Ask</span>';
    document.getElementById('sb-level-count').textContent=_sbRows.length;
    document.getElementById('sb-status').textContent='';
  }catch(e){document.getElementById('sb-status').textContent='Error: '+e;}
  finally{document.getElementById('sb-spinner').style.display='none';}
}

function sbTranspose(){
  _sbTransposed=!_sbTransposed;
  document.getElementById('sb-transpose-btn').classList.toggle('active',_sbTransposed);
  if(_sbRows.length)sbPlot();
}

function sbResetZoom(){
  if(!_sbPriceRange)return;
  try{
    if(_sbTransposed){
      Plotly.relayout('sb-chart',{'yaxis.range':_sbPriceRange,'xaxis.range':[0,1.05]});
    }else{
      Plotly.relayout('sb-chart',{'xaxis.range':_sbPriceRange,'yaxis.range':[0,1.05]});
    }
  }catch(_){}
}

function _sbShapes(){
  return _sbLines.map(l=>{
    const col=_sbLineColor(l.type,l.confidence);
    const ln={color:col,width:_sbLineWidth(l.confidence),dash:_sbLineDash(l.confidence)};
    return _sbTransposed
      ?{type:'line',y0:l.price,y1:l.price,x0:0,x1:1,xref:'paper',yref:'y',line:ln}
      :{type:'line',x0:l.price,x1:l.price,y0:0,y1:1,xref:'x',yref:'paper',line:ln};
  });
}

function sbPlot(){
  if(!_sbRows.length)return;
  const prices=_sbRows.map(r=>r.price);
  const tick=SB_TICKS[_sbSym]||0.25;
  const pmin=Math.min(...prices)-tick, pmax=Math.max(...prices)+tick;
  _sbPriceRange=[pmin,pmax];
  const bg='#1a1a2e',grid='#252535';
  const axC={gridcolor:grid,zeroline:false,tickfont:{size:9}};
  const traces=[];
  for(const col of SB_COLS){
    const chk=document.getElementById('sbchk-'+col.key);
    if(chk&&!chk.checked)continue;
    const norm=_sbNorm(_sbRows.map(r=>r[col.key]),col.key==='delta');
    const ht=`<b>${col.label}</b><br>Price: %{${_sbTransposed?'y':'x'}}<br>Norm: %{${_sbTransposed?'x':'y'}:.3f}<extra></extra>`;
    const tr={name:col.label,type:'bar',marker:{color:col.color},opacity:1,hovertemplate:ht};
    if(_sbTransposed){tr.y=prices;tr.x=norm;tr.orientation='h';}
    else             {tr.x=prices;tr.y=norm;tr.orientation='v';}
    traces.push(tr);
  }
  const layout={
    paper_bgcolor:bg,plot_bgcolor:bg,font:{color:'#ccc',size:10},
    margin:_sbTransposed?{t:8,b:40,l:60,r:6}:{t:8,b:30,l:44,r:6},
    barmode:'overlay',showlegend:false,bargap:0,
    xaxis:{...axC,
      title:_sbTransposed?'Normalized (0-1)':'Price',
      ..._sbTransposed?{range:[0,1.05],fixedrange:true}:{range:_sbPriceRange}},
    yaxis:{...axC,
      title:_sbTransposed?'Price':'Normalized (0-1)',
      ..._sbTransposed?{range:_sbPriceRange}:{range:[0,1.05],fixedrange:true}},
    shapes:_sbShapes(),
  };
  Plotly.newPlot('sb-chart',traces,layout,{responsive:true,displayModeBar:false,doubleClick:false,scrollZoom:true})
    .then(()=>{
      const el=document.getElementById('sb-chart');
      if(el._sbClickH){el.removeEventListener('click',el._sbClickH);}
      el._sbClickH=_sbNativeClick;
      el.addEventListener('click',el._sbClickH);
    });
}

// Convert mouse clientX/Y to price coordinate using Plotly internals
function _sbPxToPrice(e){
  const gd=document.getElementById('sb-chart');
  if(!gd._fullLayout)return null;
  const rect=gd.getBoundingClientRect();
  if(_sbTransposed){
    const ya=gd._fullLayout.yaxis;
    if(!ya||!ya.range||!ya._length)return null;
    const py=(e.clientY-rect.top)-ya._offset;
    const frac=py/ya._length;
    return ya.range[1]-frac*(ya.range[1]-ya.range[0]);
  }else{
    const xa=gd._fullLayout.xaxis;
    if(!xa||!xa.range||!xa._length)return null;
    const px=(e.clientX-rect.left)-xa._offset;
    const frac=px/xa._length;
    return xa.range[0]+frac*(xa.range[1]-xa.range[0]);
  }
}

function _sbNativeClick(e){
  if(e.target.closest('#sb-popup'))return;
  const raw=_sbPxToPrice(e);
  if(raw===null)return;
  const tick=SB_TICKS[_sbSym]||0.25;
  const hitLine=_sbLines.find(l=>Math.abs(l.price-raw)<=tick*2);
  const now=Date.now();
  if(now-_sbLastClick.t<350&&Math.abs((_sbLastClick.raw??raw)-raw)<=tick*3){
    _sbLastClick={t:0,raw:null};
    _sbToggleLine(_sbSnapPrice(raw));
  }else{
    _sbLastClick={t:now,raw};
    if(hitLine)sbShowPopup(hitLine.price,e.clientX,e.clientY);
  }
}

function _sbSnapPrice(raw){
  if(!_sbRows.length)return raw;
  return _sbRows.reduce((b,r)=>Math.abs(r.price-raw)<Math.abs(b-raw)?r.price:b,_sbRows[0].price);
}

async function _sbToggleLine(price){
  const idx=_sbLines.findIndex(l=>Math.abs(l.price-price)<0.001);
  if(idx>=0){
    const lineId=_sbLines[idx].id;
    if(lineId)await fetch(`/api/lines/${lineId}`,{method:'DELETE'});
    _sbLines.splice(idx,1);
    sbClosePopup();
  }else{
    const ltype=document.querySelector('input[name="sb-add-type"]:checked')?.value||'SUPPORT';
    const conf=document.querySelector('input[name="sb-add-conf"]:checked')?.value||'';
    const dt=document.getElementById('sb-date').value;
    const res=await fetch('/api/sandbox/line',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbol:_sbSym,date:dt,price,line_type:ltype,confidence:conf})});
    const j=await res.json();
    _sbLines.push({id:j.id,price,type:ltype,confidence:conf});
  }
  Plotly.relayout('sb-chart',{shapes:_sbShapes()});
}

async function sbAddManualLine(){
  const priceEl=document.getElementById('sb-add-price');
  const price=parseFloat(priceEl.value);
  if(isNaN(price)||price<=0)return;
  if(_sbLines.find(l=>Math.abs(l.price-price)<0.001))return;
  const ltype=document.querySelector('input[name="sb-add-type"]:checked')?.value||'SUPPORT';
  const conf=document.querySelector('input[name="sb-add-conf"]:checked')?.value||'';
  const dt=document.getElementById('sb-date').value;
  if(!dt)return;
  const res=await fetch('/api/sandbox/line',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:_sbSym,date:dt,price,line_type:ltype,confidence:conf})});
  const j=await res.json();
  _sbLines.push({id:j.id,price,type:ltype,confidence:conf});
  Plotly.relayout('sb-chart',{shapes:_sbShapes()});
  priceEl.value='';
}

function sbShowPopup(price,cx,cy){
  _sbPopupPrice=price;
  const line=_sbLines.find(l=>Math.abs(l.price-price)<0.001);
  const row=_sbRows.find(r=>Math.abs(r.price-price)<0.001);
  const fmt=(v,d=0)=>v===null||v===undefined?'—':Number(v).toFixed(d);
  document.getElementById('sb-popup-price').textContent=price.toFixed(2);
  // Set type radio
  if(line){
    const tr=document.querySelector(`input[name="sb-popup-type"][value="${line.type}"]`);
    if(tr)tr.checked=true;
    const cr=document.querySelector(`input[name="sb-popup-conf"][value="${line.confidence}"]`);
    if(cr)cr.checked=true;
  }
  document.getElementById('sb-popup-body').innerHTML=row?`
    <table class="table table-dark table-sm table-borderless mb-0" style="font-size:.75rem">
      <tr><td class="text-muted pe-3">Volume</td><td>${fmt(row.total_volume,0)}</td></tr>
      <tr><td class="text-muted">Visits</td><td>${fmt(row.visits,0)}</td></tr>
      <tr><td class="text-muted">Up #</td><td class="text-success">${fmt(row.price_up,0)}</td></tr>
      <tr><td class="text-muted">Dn #</td><td class="text-danger">${fmt(row.price_down,0)}</td></tr>
      <tr><td class="text-muted">Up Vol</td><td class="text-success">${fmt(row.up_vol,0)}</td></tr>
      <tr><td class="text-muted">Dn Vol</td><td class="text-danger">${fmt(row.down_vol,0)}</td></tr>
      <tr><td class="text-muted">Change #</td><td>${fmt(row.price_change,0)}</td></tr>
      <tr><td class="text-muted">Delta</td><td>${fmt(row.delta,0)}</td></tr>
      <tr><td class="text-muted">Ask Liq</td><td>${fmt(row.total_ask,0)}</td></tr>
      <tr><td class="text-muted">Bid Liq</td><td>${fmt(row.total_bid,0)}</td></tr>
    </table>`
    :'<span class="text-muted small">No profile data at this price</span>';
  const pop=document.getElementById('sb-popup');
  pop.style.display='block';
  const pw=pop.offsetWidth||240,ph=pop.offsetHeight||320;
  let left=cx+14,top=cy-20;
  if(left+pw>window.innerWidth-10)left=cx-pw-14;
  if(top+ph>window.innerHeight-10)top=window.innerHeight-ph-10;
  if(top<10)top=10;
  pop.style.left=left+'px';pop.style.top=top+'px';
}

async function sbSetPopupType(ltype){
  if(_sbPopupPrice===null)return;
  const idx=_sbLines.findIndex(l=>Math.abs(l.price-_sbPopupPrice)<0.001);
  if(idx<0)return;
  _sbLines[idx].type=ltype;
  const lineId=_sbLines[idx].id;
  if(lineId)await fetch(`/api/sandbox/line/${lineId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({line_type:ltype})});
  Plotly.relayout('sb-chart',{shapes:_sbShapes()});
}

async function sbSetPopupConf(conf){
  if(_sbPopupPrice===null)return;
  const idx=_sbLines.findIndex(l=>Math.abs(l.price-_sbPopupPrice)<0.001);
  if(idx<0)return;
  _sbLines[idx].confidence=conf;
  const lineId=_sbLines[idx].id;
  if(lineId)await fetch(`/api/sandbox/line/${lineId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({confidence:conf})});
  Plotly.relayout('sb-chart',{shapes:_sbShapes()});
}

async function sbMovePopupLine(dir){
  if(_sbPopupPrice===null)return;
  const tick=SB_TICKS[_sbSym]||0.25;
  const np=Math.round((_sbPopupPrice+dir*tick)*10000)/10000;
  const idx=_sbLines.findIndex(l=>Math.abs(l.price-_sbPopupPrice)<0.001);
  if(idx<0)return;
  const lineId=_sbLines[idx].id;
  _sbLines[idx].price=np;
  _sbPopupPrice=np;
  // Re-insert at new price (delete + create, since price is part of the key)
  if(lineId){
    const line=_sbLines[idx];
    await fetch(`/api/lines/${lineId}`,{method:'DELETE'});
    const res=await fetch('/api/sandbox/line',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbol:_sbSym,date:document.getElementById('sb-date').value,
        price:np,line_type:line.type,confidence:line.confidence})});
    const j=await res.json();
    _sbLines[idx].id=j.id;
  }
  Plotly.relayout('sb-chart',{shapes:_sbShapes()});
  const pop=document.getElementById('sb-popup');
  document.getElementById('sb-popup-price').textContent=np.toFixed(2);
  sbShowPopup(np,parseFloat(pop.style.left),parseFloat(pop.style.top));
}

async function sbDeletePopupLine(){
  if(_sbPopupPrice===null)return;
  const idx=_sbLines.findIndex(l=>Math.abs(l.price-_sbPopupPrice)<0.001);
  if(idx>=0){
    const lineId=_sbLines[idx].id;
    if(lineId)await fetch(`/api/lines/${lineId}`,{method:'DELETE'});
    _sbLines.splice(idx,1);
  }
  Plotly.relayout('sb-chart',{shapes:_sbShapes()});
  sbClosePopup();
}

function sbClosePopup(){
  _sbPopupPrice=null;
  document.getElementById('sb-popup').style.display='none';
}

document.addEventListener('click',function(e){
  const pop=document.getElementById('sb-popup');
  if(pop.style.display!=='none'&&!pop.contains(e.target)&&!e.target.closest('#sb-chart'))
    sbClosePopup();
});

function sbToggleGroup(grp){
  const cols=SB_COLS.filter(c=>c.group===grp);
  const allOn=cols.every(c=>{const el=document.getElementById('sbchk-'+c.key);return el&&el.checked;});
  cols.forEach(c=>{const el=document.getElementById('sbchk-'+c.key);if(el)el.checked=!allOn;});
  if(_sbRows.length)sbPlot();
}

function sbBuildCheckboxes(){
  if(_sbChecksBuilt)return;
  _sbChecksBuilt=true;
  const wrap=document.getElementById('sb-col-checks');

  // Group toggle buttons
  const grpWrap=document.createElement('div');
  grpWrap.className='d-flex gap-1 me-2';
  [['Greens','green','#32ba64'],['Reds','red','#d25050'],['Blues','blue','#5b8dd9']].forEach(([lbl,grp,col])=>{
    const b=document.createElement('button');
    b.className='btn btn-sm btn-outline-secondary';
    b.style.cssText=`font-size:.7rem;padding:1px 8px;height:22px;border-color:${col};color:${col};`;
    b.textContent=lbl;
    b.onclick=()=>sbToggleGroup(grp);
    grpWrap.appendChild(b);
  });
  wrap.appendChild(grpWrap);

  // Individual checkboxes
  for(const col of SB_COLS){
    const lbl=document.createElement('label');
    lbl.className='d-flex align-items-center gap-1 user-select-none';
    lbl.style.cursor='pointer';
    const chk=document.createElement('input');
    chk.type='checkbox';chk.id='sbchk-'+col.key;chk.checked=col.on;
    chk.onchange=()=>{if(_sbRows.length)sbPlot();};
    const dot=document.createElement('span');
    dot.style.cssText=`display:inline-block;width:10px;height:10px;border-radius:2px;background:${col.color};flex-shrink:0`;
    const txt=document.createElement('span');
    txt.textContent=col.label;
    lbl.append(chk,dot,txt);
    wrap.appendChild(lbl);
  }

  // Clear All
  const btn=document.createElement('button');
  btn.className='btn btn-sm btn-outline-secondary ms-1';
  btn.style.cssText='font-size:.7rem;padding:1px 7px;height:22px;';
  btn.textContent='Clear All';
  btn.onclick=()=>{
    SB_COLS.forEach(c=>{const el=document.getElementById('sbchk-'+c.key);if(el)el.checked=false;});
    if(_sbRows.length)sbPlot();
  };
  wrap.appendChild(btn);
}

document.getElementById('btn-sandbox-tab').addEventListener('click',function(){
  if(!document.getElementById('sb-date').value)
    document.getElementById('sb-date').value=_lastWeekday();
  sbBuildCheckboxes();
});

// ── TEST TAB ──────────────────────────────────────────────────────────────────
const TEST_SYM='MES', TEST_TICK=0.25;
const TEST_SUPPORT=[
  {price:7568.25,conf:'?'},{price:7529.50,conf:'?'},{price:7507.75,conf:'?'},
  {price:7469.00,conf:''},{price:7398.50,conf:''},{price:7360.25,conf:'!'},
  {price:7304.75,conf:''},{price:7266.50,conf:''},
];
let _testAllBars=[],_testDates=[],_testDayLows={};
let _testHidden=new Set(),_testManual=[];

function _testSLineColor(conf){
  if(conf==='!')return '#00ff99';
  if(conf==='?')return 'rgba(50,186,100,0.42)';
  return '#32ba64';
}
function _testSLineWidth(conf){return conf==='!'?2.5:conf==='?'?1:1.5;}
function _testSLineDash(conf){return conf==='?'?'dash':'solid';}

async function loadTest(){
  const el=document.getElementById('test-chart');
  el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted">Loading 5 days…</div>';
  // Walk back up to 14 calendar days; collect 5 real trading days (skip holidays/mock)
  const candidates=[];
  let d=new Date('2026-07-10T12:00:00Z');
  for(let i=0;i<14;i++){
    const dow=d.getUTCDay();
    if(dow>0&&dow<6)candidates.unshift(d.toISOString().slice(0,10));
    d=new Date(d.getTime()-86400000);
  }
  _testAllBars=[];_testDates=[];_testDayLows={};
  for(const dt of candidates){
    if(_testDates.length>=5)break;
    try{
      const j=await (await fetch(`/api/history/${TEST_SYM}?interval=15&date=${dt}`)).json();
      if(j.mock_date)continue;          // holiday — API served a different day's data
      const bars=j.bars||[];
      if(!bars.length)continue;
      _testDates.push(dt);
      let dayLow=Infinity;
      for(const b of bars){_testAllBars.push({...b,date:dt});if(b.low<dayLow)dayLow=b.low;}
      _testDayLows[dt]=dayLow;
    }catch(_){}
  }
  testPlot();
}

function testPlot(){
  if(!_testAllBars.length)return;
  const n=_testAllBars.length;
  // Y range: fit to actual candle prices, not support lines
  const yLow=Math.min(..._testAllBars.map(b=>b.low));
  const yHigh=Math.max(..._testAllBars.map(b=>b.high));
  const yPad=(yHigh-yLow)*0.06;
  // Use formatted strings as category labels — eliminates overnight gaps
  const labels=_testAllBars.map(b=>{
    const md=b.t.slice(5,10).replace('-','/');  // "MM/DD"
    const hm=b.t.slice(11,16);                   // "HH:MM"
    return `${md} ${hm}`;
  });
  const shapes=[],annotations=[];
  // Day separators (float index puts line between bars)
  for(let i=1;i<n;i++){
    if(_testAllBars[i].date!==_testAllBars[i-1].date){
      shapes.push({type:'line',x0:i-0.5,x1:i-0.5,y0:0,y1:1,xref:'x',yref:'paper',
        line:{color:'rgba(180,180,255,0.35)',width:1.5,dash:'dot'}});
    }
  }
  // Date labels at first bar of each day
  for(const dt of _testDates){
    const fi=_testAllBars.findIndex(b=>b.date===dt);
    if(fi>=0)annotations.push({x:fi,y:0.99,xref:'x',yref:'paper',text:dt.slice(5),
      showarrow:false,xanchor:'left',yanchor:'top',font:{size:9,color:'rgba(180,180,255,0.7)'}});
  }
  // Support lines (paper x = full width)
  for(const sl of TEST_SUPPORT){
    if(_testHidden.has(sl.price))continue;
    shapes.push({type:'line',x0:0,x1:1,y0:sl.price,y1:sl.price,xref:'paper',yref:'y',
      line:{color:_testSLineColor(sl.conf),width:_testSLineWidth(sl.conf),dash:_testSLineDash(sl.conf)}});
    annotations.push({x:1,y:sl.price,xref:'paper',yref:'y',
      text:(sl.conf||' ')+sl.price.toFixed(2),showarrow:false,
      xanchor:'right',font:{size:8,color:_testSLineColor(sl.conf)}});
  }
  // Daily lows (span only that day's bar indices)
  for(const dt of _testDates){
    const low=_testDayLows[dt];
    if(low==null)continue;
    const idxs=_testAllBars.map((b,i)=>b.date===dt?i:-1).filter(i=>i>=0);
    if(!idxs.length)continue;
    shapes.push({type:'line',x0:idxs[0],x1:idxs[idxs.length-1],y0:low,y1:low,
      xref:'x',yref:'y',line:{color:'rgba(255,165,0,0.6)',width:1,dash:'dot'}});
    annotations.push({x:idxs[0],y:low,xref:'x',yref:'y',
      text:low.toFixed(2),showarrow:false,xanchor:'left',font:{size:7,color:'rgba(255,165,0,0.8)'}});
  }
  // Manual lines (paper x = full width)
  for(const ml of _testManual){
    shapes.push({type:'line',x0:0,x1:1,y0:ml.price,y1:ml.price,xref:'paper',yref:'y',
      line:{color:'rgba(255,255,255,0.8)',width:1.5,dash:'solid'}});
    annotations.push({x:0,y:ml.price,xref:'paper',yref:'y',
      text:ml.price.toFixed(2),showarrow:false,xanchor:'left',font:{size:8,color:'rgba(255,255,255,0.75)'}});
  }
  const bg='#1a1a2e',grid='#252535';
  const layout={
    paper_bgcolor:bg,plot_bgcolor:bg,font:{color:'#ccc'},
    margin:{l:55,r:55,t:8,b:65},dragmode:'zoom',showlegend:false,
    xaxis:{type:'category',gridcolor:grid,rangeslider:{visible:false},
      tickangle:-45,tickfont:{size:8},nticks:20},
    yaxis:{gridcolor:grid,range:[yLow-yPad,yHigh+yPad]},
    shapes,annotations
  };
  const trace={type:'candlestick',
    x:labels,
    open:_testAllBars.map(b=>b.open),high:_testAllBars.map(b=>b.high),
    low:_testAllBars.map(b=>b.low),close:_testAllBars.map(b=>b.close),
    name:TEST_SYM,showlegend:false,
    increasing:{line:{color:'#26a69a'}},decreasing:{line:{color:'#ef5350'}}
  };
  Plotly.newPlot('test-chart',[trace],layout,
    {responsive:true,displayModeBar:false,scrollZoom:true,doubleClick:false})
    .then(()=>_attachTestDblClick());
}

function testResetZoom(){
  try{Plotly.relayout('test-chart',{'xaxis.autorange':true,'yaxis.autorange':true});}catch(_){}
}

function testShowAll(){
  _testHidden.clear();
  testPlot();
}

function _testPxToPrice(e){
  const gd=document.getElementById('test-chart');
  if(!gd._fullLayout)return null;
  const ya=gd._fullLayout.yaxis;
  if(!ya||!ya.range||!ya._length)return null;
  const rect=gd.getBoundingClientRect();
  const py=(e.clientY-rect.top)-ya._offset;
  return ya.range[1]-py/ya._length*(ya.range[1]-ya.range[0]);
}

function _attachTestDblClick(){
  const el=document.getElementById('test-chart');
  if(el._testDblH)el.removeEventListener('click',el._testDblH,true);
  let _dt=0,_dy=0;
  el._testDblH=function(e){
    const now=Date.now();
    if(now-_dt<400&&Math.abs(e.clientY-_dy)<14){
      _dt=0;e.stopPropagation();e.preventDefault();
      const price=_testPxToPrice(e);
      if(price===null)return;
      const tol=TEST_TICK*4;
      // Near a support line → toggle hide
      const sl=TEST_SUPPORT.find(s=>!_testHidden.has(s.price)&&Math.abs(s.price-price)<=tol);
      if(sl){_testHidden.add(sl.price);testPlot();return;}
      // Near a manual line → remove
      const mi=_testManual.findIndex(m=>Math.abs(m.price-price)<=tol);
      if(mi>=0){_testManual.splice(mi,1);testPlot();return;}
      // Add new manual line (snapped to tick)
      const snapped=Math.round(price/TEST_TICK)*TEST_TICK;
      _testManual.push({price:snapped});
      testPlot();
    }else{_dt=now;_dy=e.clientY;}
  };
  el.addEventListener('click',el._testDblH,true);
}

document.getElementById('btn-test-tab').addEventListener('click',function(){
  if(!_testAllBars.length)loadTest();
});

// ── Init ──────────────────────────────────────────────────────────────────────
(function(){
  const lw=_lastWeekday();
  document.getElementById('range-from').value=lw;
  document.getElementById('range-to').value=lw;
  refreshLines();
  _renderBuildTable([]);
  fetch('/api/build_db/status').then(r=>r.json()).then(s=>{
    if(s.log&&s.log.length)_renderBuildTable(s.log);
  }).catch(()=>{});
})();
</script>
</body>
</html>"""


# ── Release notes ─────────────────────────────────────────────────────────────

_RELEASE_NOTES = [
    ("v3.0",  "Full dashboard redesign — Lines/Graph/Trades/Submitted tabs", None),
    ("v3.2",  "Fix day/sym nav, bars x-axis range, reset zoom squeeze", None),
    ("v3.3",  "All Symbols tab + nav/zoom/bars fixes", None),
    ("v3.4",  "Add lines to All Symbols tab charts", None),
    ("v3.5",  "Fix Graph day nav (always use 14-day window)", None),
    ("v3.6",  "Enable threaded Flask server", None),
    ("v3.7",  "Gray out weekends/holidays and zero-data days in Lines table", None),
    ("v3.8",  "Collapse header to single 40px bar", None),
    ("v3.9",  "Draw mode wired — dblclick add/remove, click to name, Auto gray toggle, Save/Send", None),
    ("v3.10", "Transpose bars mode — price on Y axis, ticks on X, lines align with other graphs", None),
    ("v3.11", "Fix Draw mode — remove !important, timed dblclick, robust _pixelToPrice fallback", None),
    ("v3.12", "Draw mode popup on dblclick — Support/Resistance color buttons, green/red lines", None),
    ("v4.07", "Test tab: MES 5-day 15m candlestick, support lines, daily lows, dbl-click hide/add",
              "New Test tab — MES only, 5 working days back from 2026-07-10, 15m candles. "
              "8 support lines (normal/!/?) with distinct colors. Day separators. Daily low per day. "
              "Dbl-click support line to hide it; Show All to restore. "
              "Dbl-click chart to add/remove white manual lines. Zoom + Reset."),
    ("v4.06", "Graph: double-click adds manual line from Auto mode (auto-enters Draw mode)",
              "Double-clicking the chart no longer requires switching to Draw mode first. "
              "A dblclick in Auto mode auto-switches to Draw mode and adds the line."),
    ("v4.05", "Sandbox: zoom locked to price axis only; Reset Zoom snaps to data price range",
              "scrollZoom now only zooms the price axis (normalized 0-1 axis is fixedrange). "
              "Price range computed from data min/max ± 1 tick. "
              "Reset Zoom restores exactly that range on the price axis."),
    ("v4.04", "Sandbox: manual lines saved to DB with source='D'; type/confidence stored; GEVA lines inserted",
              "Sandbox lines (click to add/remove) are persisted in critical_lines with source='D'. "
              "Each line stores line_type (SUPPORT/RESISTANCE) and confidence ('', '!', '?'). "
              "Toolbar radios select type+confidence before adding a line. "
              "Popup shows type+confidence radios to change them (PATCH to DB). "
              "Move Up/Down re-inserts the line at the new price (delete+create). "
              "Delete removes from DB. Line color/dash/width varies by type+confidence. "
              "16 GEVA support/resistance lines inserted for MES 2026-07-10."),
    ("v4.03", "Sandbox: single chart, bargap=0, native click anywhere, group buttons; All: overlay mode",
              "Sandbox: reverted to single chart (3-panel removed). bargap:0 = no space between bars. "
              "Click detection uses native mouse listener + Plotly._fullLayout coordinate conversion so "
              "double-click/single-click works anywhere in the chart area (not just on bar data). "
              "Added Greens/Reds/Blues group toggle buttons. Reset Zoom button. "
              "All tab: Overlay toggle button shows all 4 symbols on one chart as ticks-from-open lines."),
    ("v4.02", "Sandbox: 3-panel layout (General/Up/Down) + 2px bars + white-line placement + click popup",
              "Split Sandbox into 3 Plotly subplots (General top 32%, Up mid 30%, Down bot 32%). "
              "Bar width set to 2px (bargap:0.90). Double-click places/removes 3px white line. "
              "Single click on line opens floating popup showing all price-level stats. "
              "Popup has Move Up/Down (by 1 tick) and Delete buttons. Panel column routing via SB_COLS.panel."),
    ("v4.01", "Sandbox: 1px bar width + Clear All checkbox button", None),
    ("v4.00", "Sandbox tab — price-level microstructure bar chart",
              "New Sandbox tab: per-price market profile built from tick + bid/ask CSVs. "
              "Columns: total_volume, visits, price_up/down/change (count), up_vol/down_vol/change_vol "
              "(volume-weighted), total_ask, total_bid, delta. "
              "Normalized overlay bar chart (price on X), Transpose button (price on Y). "
              "11 toggleable columns with color swatches. DB-controlled rebuild when bidask arrives. "
              "New lib/price_profile.py module + price_profile DB table."),
]


def _write_release_notes():
    """Upsert all release notes on startup. Idempotent."""
    db_path = _resolve_db()
    from lib.db import init_db
    init_db(db_path)
    with get_db(db_path) as con:
        for version, summary, details in _RELEASE_NOTES:
            exists = con.execute(
                "SELECT id FROM release_notes WHERE program='trading_dashboard' AND version=?",
                (version,)
            ).fetchone()
            if not exists:
                con.execute(
                    "INSERT INTO release_notes (program, version, summary, details)"
                    " VALUES (?, ?, ?, ?)",
                    ("trading_dashboard", version, summary, details)
                )


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trading Dashboard -- port 5003")
    parser.add_argument("--port",  type=int, default=5003)
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"[trading_dashboard] port {args.port} already in use -- exiting"); sys.exit(0)
    _write_release_notes()
    print(f"Trading Dashboard -> http://{args.host}:{args.port}")
    print(f"LAN access        -> http://192.168.1.132:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
