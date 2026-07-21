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
from trader.session import get_session_manager
from lib import algo_lab, algo_pnl, correlation_lab

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


def _trader_config() -> dict:
    """
    Load trader/config.yaml directly as a raw dict -- deliberately bypasses
    lib.config_loader's global process-wide cache (documented footgun, see
    CC2026 handoff docs Rule 7): that cache is keyed on whichever config.yaml
    some module loads first via a bare get_config(), and back-trading/ has its
    own separate config.yaml for the backtest engine, so a bare get_config()
    call from this file is not reliable. _resolve_db() above already works
    around the same footgun for paths.db this same way.
    """
    cfg_path = _ROOT / "trader" / "config.yaml"
    try:
        import yaml
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _ns(d: dict):
    """dict -> SimpleNamespace (recursively), for attribute-style config access."""
    from types import SimpleNamespace
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_ns(v) for v in d]
    return d


_ALGO_LAB_DEFAULTS = {
    "enabled": True, "symbols": ALL_SYMBOLS, "quantity": 1,
    "strategies": ["BOUNCE", "BREAKOUT", "DIRECTIONAL", "FADE", "BOTH"],
    "tp_ticks": [4, 8, 16], "sl_ticks": [4, 8, 16],
    "direction_filters": ["ALL", "BUY_ONLY", "SELL_ONLY"],
    "strength_max": [1, 2, 3],
    "max_param_combos": 24, "max_commands_per_submit": 500,
}

_CORRELATION_DEFAULTS = {
    "symbols": ALL_SYMBOLS, "windows": [20, 50, 100],
    "default_window": 50, "max_series_points": 500,
}


def _algo_lab_cfg():
    merged = {**_ALGO_LAB_DEFAULTS, **(_trader_config().get("algo_lab") or {})}
    return _ns(merged)


def _correlation_cfg():
    merged = {**_CORRELATION_DEFAULTS, **(_trader_config().get("correlation") or {})}
    return _ns(merged)


def _bars_db_path() -> str:
    rel = (_trader_config().get("paths") or {}).get("bars", "data/bars.db")
    return str((_ROOT / "trader" / rel).resolve())


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


@app.route("/api/session/status")
def api_session_status():
    return jsonify(get_session_manager().status())


@app.route("/api/session/start", methods=["POST"])
def api_session_start():
    try:
        return jsonify(get_session_manager().start())
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/session/stop", methods=["POST"])
def api_session_stop():
    return jsonify(get_session_manager().stop())


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
    days         = max(1, min(10, int(request.args.get("days", 1))))
    start        = date.fromisoformat(req_date_str) if req_date_str else (date.today() - timedelta(days=1))

    # Walk backward from `start` collecting up to `days` trading days that have
    # RTH data, same single-day fallback logic as before when days=1. Ticks from
    # all collected days go into one _ohlcv_bars() call — it buckets by its own
    # (date, time) key per tick, so multi-day concatenation order doesn't matter.
    all_ticks: list = []
    used_dates: list = []
    search  = start + timedelta(days=1)
    scanned = 0
    while len(used_dates) < days and scanned < 40:
        search  -= timedelta(days=1)
        scanned += 1
        if search.weekday() >= 5:
            continue
        if not _csv_has_rth(symbol, search):
            continue
        t = _load_ticks(symbol, search)
        if t:
            all_ticks.extend(t)
            used_dates.append(search)

    if not all_ticks:
        return jsonify({"bars": [], "date": None, "symbol": symbol, "error": "no data"})

    rth_bars = [b for b in _ohlcv_bars(all_ticks, interval)
                if _RTH_START_MIN * 60 <= b["t_sec"] < _RTH_END_MIN * 60]
    bars = []
    for b in rth_bars:
        t  = b["t_sec"]
        hh, mm, ss = t // 3600, (t % 3600) // 60, t % 60
        bars.append({"t": f"{b['date']}T{hh:02d}:{mm:02d}:{ss:02d}",
                     "open": b["open"], "high": b["high"],
                     "low":  b["low"],  "close": b["close"], "vol": b["vol"]})

    used_dates.sort()
    newest = used_dates[-1]
    total_ticks = sum(b["vol"] for b in rth_bars)
    mock = newest.isoformat() if newest != start else None
    return jsonify({"bars": bars, "date": newest.isoformat(),
                    "date_range": [used_dates[0].isoformat(), newest.isoformat()],
                    "symbol": symbol, "mock_date": mock,
                    "total_ticks": total_ticks})


_BARS_DB = Path(r"C:\Projects\CriticalCorallations2026\trader\data\bars.db")

_RESAMPLE_FREQ = {"30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}

@app.route("/api/bars-long")
def api_bars_long():
    """
    GET /api/bars-long?symbol=MES&days=365&resolution=30m
    GET /api/bars-long?pair=MES-MYM&days=90&resolution=1h
    Reads from trader/data/bars.db (pre-backfilled by scripts/backfill_bars.py).
    """
    import sqlite3 as _sq
    import pandas as _pd

    if not _BARS_DB.exists():
        return jsonify({"error": "bars.db not found — run scripts/backfill_bars.py first"}), 404

    days       = min(int(request.args.get("days", 365)), 365)
    resolution = request.args.get("resolution", "30m")
    pair       = request.args.get("pair", "")
    symbol     = request.args.get("symbol", "MES").upper()
    freq       = _RESAMPLE_FREQ.get(resolution, "30min")

    con = _sq.connect(f"file:{_BARS_DB}?mode=ro", uri=True)
    try:
        cutoff = (_pd.Timestamp.utcnow() - _pd.Timedelta(days=days)).isoformat()

        if pair:
            parts = pair.upper().split("-")
            if len(parts) != 2:
                return jsonify({"error": "pair must be SYM_A-SYM_B"}), 400
            sa, sb = parts

            if freq == "30min":
                # Native bars_30m granularity -- read the precomputed,
                # sanity-checked normalized diff straight from
                # bars_30m_diffs_normalized (scripts/build_bars_diffs.py)
                # instead of recomputing normalize+diff on every request.
                df = _pd.read_sql(
                    "SELECT ts, close_norm_a, close_norm_b, diff_norm FROM bars_30m_diffs_normalized "
                    "WHERE pair=? AND ts>=? ORDER BY ts",
                    con, params=(f"{sa}-{sb}", cutoff), parse_dates=["ts"]
                )
                if df.empty:
                    return jsonify({"error": f"No precomputed diff data for {sa}-{sb}"}), 404
                return jsonify({
                    "pair": pair,
                    "ts":     df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
                    "spread": df["diff_norm"].round(6).tolist(),
                    "sym_a":  df["close_norm_a"].round(6).tolist(),
                    "sym_b":  df["close_norm_b"].round(6).tolist(),
                })

            def _load(sym):
                df = _pd.read_sql(
                    "SELECT ts, close FROM bars_30m WHERE symbol=? AND ts>=? ORDER BY ts",
                    con, params=(sym, cutoff), parse_dates=["ts"]
                ).set_index("ts").rename(columns={"close": sym})
                return df

            merged = _load(sa).join(_load(sb), how="inner")
            if merged.empty:
                return jsonify({"error": f"No overlapping data for {sa}/{sb}"}), 404
            if freq != "30min":
                merged = merged.resample(freq).agg("last").dropna()
            # normalise to first bar
            merged[sa] = merged[sa] / merged[sa].iloc[0]
            merged[sb] = merged[sb] / merged[sb].iloc[0]
            spread = (merged[sa] - merged[sb]).round(6)
            return jsonify({
                "pair": pair,
                "ts":     merged.index.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
                "spread": spread.tolist(),
                "sym_a":  merged[sa].round(6).tolist(),
                "sym_b":  merged[sb].round(6).tolist(),
            })
        else:
            df = _pd.read_sql(
                "SELECT ts, open, high, low, close, volume FROM bars_30m "
                "WHERE symbol=? AND ts>=? ORDER BY ts",
                con, params=(symbol, cutoff), parse_dates=["ts"]
            ).set_index("ts")
            if df.empty:
                return jsonify({"error": f"No data for {symbol} — run backfill first"}), 404
            if freq != "30min":
                df = df.resample(freq).agg(
                    {"open": "first", "high": "max", "low": "min",
                     "close": "last", "volume": "sum"}
                ).dropna()
            return jsonify({
                "symbol": symbol,
                "ts":     df.index.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
                "open":   df["open"].round(4).tolist(),
                "high":   df["high"].round(4).tolist(),
                "low":    df["low"].round(4).tolist(),
                "close":  df["close"].round(4).tolist(),
                "volume": df["volume"].round(0).tolist(),
            })
    finally:
        con.close()


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


# ── Algo Lab ("Claude-designed algo for sup/res") ─────────────────────────────
# Submits many (strategy x tp/sl x direction_filter x strength_max) paper
# trades in one batch over whatever critical_lines already exist (any
# detection method), tagged source='algo_lab' for later P&L attribution.
# Paper only -- writes PENDING rows into the same `commands` table trader/
# broker.py already polls; this route never talks to IB for order submission,
# only (briefly, on-demand) for a current-price snapshot used to evaluate the
# toggle rule. See lib/algo_lab.py and lib/algo_pnl.py docstrings for detail.

def _fetch_live_prices(symbols: list) -> dict:
    """
    Ephemeral IB LIVE-data-only connection (paper=False -- data only, no order
    capability) to snapshot current prices for the requested symbols. Connects,
    fetches, disconnects -- never held open between requests, unlike broker.py/
    decider.py's long-lived connections. Uses the same live_client_ids pool;
    IBClient's connect-retry already shuffles through free IDs so this can't
    collide with an ID broker/decider currently holds.
    """
    prices = {s: None for s in symbols}
    try:
        from lib.ib_client import IBClient
        from lib.config_loader import get_config
        cfg_path = _ROOT / "trader" / "config.yaml"
        ibc = IBClient(get_config(cfg_path))
        ibc.connect(live=True, paper=False)
        try:
            for sym in symbols:
                try:
                    prices[sym] = ibc.get_price(sym)
                except Exception:
                    prices[sym] = None
        finally:
            ibc.disconnect()
    except Exception as e:
        log_msg = f"Algo Lab: live price fetch unavailable ({e})"
        print(log_msg)
    return prices


@app.route("/api/algo-lab/config")
def api_algo_lab_config():
    cfg = _algo_lab_cfg()
    combos = algo_lab.build_param_grid(cfg)
    full_grid = (len(cfg.strategies) * len(cfg.tp_ticks) * len(cfg.sl_ticks)
                * len(cfg.direction_filters) * len(cfg.strength_max))
    return jsonify({
        "enabled": cfg.enabled, "symbols": list(cfg.symbols),
        "strategies": list(cfg.strategies), "tp_ticks": list(cfg.tp_ticks),
        "sl_ticks": list(cfg.sl_ticks), "direction_filters": list(cfg.direction_filters),
        "strength_max": list(cfg.strength_max), "quantity": cfg.quantity,
        "max_param_combos": cfg.max_param_combos,
        "full_grid_size": full_grid, "combo_count": len(combos),
    })


@app.route("/api/algo-lab/preview", methods=["POST"])
def api_algo_lab_preview():
    body    = request.get_json(silent=True) or {}
    cfg     = _algo_lab_cfg()
    symbols = body.get("symbols") or list(cfg.symbols)
    db_path = _resolve_db()
    _ensure_columns(db_path)
    today   = date.today().isoformat()

    combos = algo_lab.build_param_grid(cfg)
    prices = _fetch_live_prices(symbols)
    ticks  = {s: TICKS.get(s, 0.25) for s in symbols}

    result = algo_lab.preview_grid(symbols, today, prices, ticks, combos, db_path)
    return jsonify({**result, "combos_used": len(combos), "prices": prices})


@app.route("/api/algo-lab/submit", methods=["POST"])
def api_algo_lab_submit():
    body    = request.get_json(silent=True) or {}
    cfg     = _algo_lab_cfg()
    symbols = body.get("symbols") or list(cfg.symbols)
    db_path = _resolve_db()
    _ensure_columns(db_path)
    today   = date.today().isoformat()

    combos = algo_lab.build_param_grid(cfg)
    prices = _fetch_live_prices(symbols)
    ticks  = {s: TICKS.get(s, 0.25) for s in symbols}

    result = algo_lab.submit_grid(
        symbols, today, prices, ticks, combos, db_path,
        quantity=cfg.quantity, max_commands_total=cfg.max_commands_per_submit,
    )
    return jsonify({**result, "combos_used": len(combos), "prices": prices})


@app.route("/api/algo-lab/pnl")
def api_algo_lab_pnl():
    db_path   = _resolve_db()
    date_from = request.args.get("date_from") or None
    date_to   = request.args.get("date_to") or None
    breakdown = algo_pnl.get_breakdown(db_path, date_from, date_to)
    summary   = algo_pnl.rollup_by_source(breakdown)
    return jsonify({"summary": summary, "breakdown": breakdown})


# ── Sup/Res visualization data (feeds the Graph tab's line overlay) ───────────

@app.route("/api/srviz/<symbol>")
def api_srviz(symbol: str):
    """Critical lines for one symbol/date, with source/algo_type/note kept for
    color-coding + tooltips on the Graph tab overlay -- lets 'theoretical,
    half-baked' S/R lines be visually judged against real price action."""
    date_str = request.args.get("date", date.today().isoformat())
    db_path  = _resolve_db()
    _ensure_columns(db_path)
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT id, price, line_type, strength,"
            " COALESCE(source,'manual') AS source,"
            " COALESCE(algo_type,'MANUAL') AS algo_type, note, armed"
            " FROM critical_lines WHERE symbol=? AND date=? ORDER BY price",
            (symbol.upper(), date_str)
        ).fetchall()
    return jsonify({"symbol": symbol.upper(), "date": date_str,
                    "lines": [dict(r) for r in rows]})


# ── Correlation Lab ────────────────────────────────────────────────────────────
# Read-only exploration module -- no correlation-analysis code existed
# anywhere in this repo before this (verified by grep). Purely a
# visualization aid meant to surface ideas for a future correlation-based
# algo type; does not trade.

@app.route("/api/correlation/config")
def api_correlation_config():
    cfg = _correlation_cfg()
    missing = [s for s in cfg.symbols if not correlation_lab.has_data(_bars_db_path(), s)]
    return jsonify({"symbols": list(cfg.symbols), "windows": list(cfg.windows),
                    "default_window": cfg.default_window, "missing": missing})


@app.route("/api/correlation/matrix")
def api_correlation_matrix():
    cfg    = _correlation_cfg()
    window = int(request.args.get("window", cfg.default_window))
    return jsonify(correlation_lab.correlation_matrix(_bars_db_path(), list(cfg.symbols), window))


@app.route("/api/correlation/timeseries")
def api_correlation_timeseries():
    cfg    = _correlation_cfg()
    sym_a  = request.args.get("a", "MES").upper()
    sym_b  = request.args.get("b", "MYM").upper()
    window = int(request.args.get("window", cfg.default_window))
    series = correlation_lab.rolling_correlation_series(
        _bars_db_path(), sym_a, sym_b, window, max_points=cfg.max_series_points
    )
    return jsonify({"symbol_a": sym_a, "symbol_b": sym_b, "window": window, "series": series})


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
#top-bar{background:#161b22;height:40px;border-bottom:1px solid #30363d;display:flex;align-items:center;padding:0 8px;gap:0;overflow-x:auto;overflow-y:hidden;scrollbar-width:thin}
#top-bar::-webkit-scrollbar{height:4px}
#top-bar::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
.top-tab{border:none!important;border-radius:0!important;background:transparent!important;color:#8b949e;font-size:.8rem;height:40px;border-bottom:2px solid transparent!important;display:flex;align-items:center;padding:0 9px;white-space:nowrap}
.top-tab.icon-tab{padding:0 8px;font-size:.9rem}
#menu-links{min-width:11rem;position:fixed!important;top:auto;left:auto;z-index:2000}
.top-tab:hover{color:#ccc;background:rgba(255,255,255,.05)!important}
.top-tab.active{color:#fff!important;border-bottom-color:#0d6efd!important}
/* Sandbox tab */
#sb-col-checks label{cursor:pointer;gap:4px;}
#sb-transpose-btn.active{background:rgba(13,110,253,.25);border-color:#0d6efd;color:#6ea8fe;}
#sb-popup{display:none;position:fixed;z-index:1050;min-width:215px;background:#1e2530;border:1px solid #30363d;border-radius:6px;padding:8px 10px;box-shadow:0 4px 20px rgba(0,0,0,.7);}
#sb-popup table td{padding:2px 6px;}
#sb-chart{cursor:crosshair;}

/* ══════════════════════ v5.00: rail + header + tabstrip + filterbar ══════════ */
:root{
  --rail-w:64px;
  --gl-bg:#12141a; --gl-panel:#171a22; --gl-panel-2:#1f2330; --gl-border:#2c3140;
  --gl-ink:#eae7df; --gl-muted:#8b90a0; --gl-faint:#5b6070;
  --gl-accent:#d98d2b; --gl-accent-ink:#2a1a05; --gl-accent-dim:#8a5c22;
  --gl-good:#3fbb82; --gl-bad:#e05a5a;
  --gl-mono:"SF Mono","Cascadia Code","Consolas","Roboto Mono",ui-monospace,monospace;
}
html,body{height:100%}
body{background:var(--gl-bg)!important}
.app-shell{display:flex; height:100vh; overflow:hidden}

/* Left rail */
.rail{
  width:var(--rail-w); flex:none; background:var(--gl-panel); border-right:1px solid var(--gl-border);
  display:flex; flex-direction:column; align-items:stretch; padding:8px 0; overflow-y:auto;
}
.rail-mark{display:flex;align-items:center;justify-content:center;height:36px;margin-bottom:4px;
  font-family:var(--gl-mono);font-weight:700;color:var(--gl-accent);font-size:14px;letter-spacing:.5px}
.rail-item{
  display:flex;flex-direction:column;align-items:center;gap:3px;padding:9px 2px;margin:1px 6px;
  border-radius:6px;cursor:pointer;color:var(--gl-muted);border:1px solid transparent;background:none;
  font-family:inherit;position:relative;
}
.rail-item .ico{font-size:16px;line-height:1}
.rail-item .lbl{font-size:9px;text-transform:uppercase;letter-spacing:.05em;font-weight:600;text-align:center}
.rail-item:hover{background:var(--gl-panel-2);color:var(--gl-ink)}
.rail-item.active{background:var(--gl-panel-2);color:var(--gl-accent)}
.rail-item.active::before{content:"";position:absolute;left:-6px;top:8px;bottom:8px;width:3px;
  background:var(--gl-accent);border-radius:2px}
.rail-spacer{flex:1}

/* Main column */
.main-col{flex:1; min-width:0; display:flex; flex-direction:column; height:100vh}

/* App header (replaces the old single-row #top-bar branding area) */
.app-header{
  display:flex;align-items:center;gap:14px;padding:0 14px;height:44px;flex:none;
  background:var(--gl-panel);border-bottom:1px solid var(--gl-border);color:var(--gl-ink);
}
.app-header .brand{font-weight:700;font-size:13.5px;letter-spacing:.2px}
.app-header .verchip{font-family:var(--gl-mono);font-size:10px;color:var(--gl-muted);
  background:var(--gl-panel-2);padding:2px 6px;border-radius:4px}
.gl-pill{font-family:var(--gl-mono);font-size:10.5px;padding:3px 9px;border-radius:20px;
  display:flex;align-items:center;gap:5px;background:var(--gl-panel-2)}
.gl-ticker{display:flex;gap:12px;margin-left:4px}
.gl-tick{display:flex;flex-direction:column;align-items:flex-end;line-height:1.1;font-family:var(--gl-mono)}
.gl-tick .sym{font-size:8.5px;color:var(--gl-faint);letter-spacing:.04em}
.gl-tick .px{font-size:11.5px;font-variant-numeric:tabular-nums;color:var(--gl-ink)}
.app-header-spacer{flex:1}

/* Busy strip — unified hourglass (replaces full-screen #busy-overlay) */
.busy-strip{height:2.5px;flex:none;background:var(--gl-border);position:relative;overflow:hidden}
.busy-strip::after{content:"";position:absolute;inset:0;width:40%;
  background:linear-gradient(90deg,transparent,var(--gl-accent),transparent);
  animation:gl-sweep 1.3s linear infinite}
.busy-strip.idle::after{display:none}
@keyframes gl-sweep{from{transform:translateX(-120%)}to{transform:translateX(320%)}}
body:not(.busy-wait) .busy-strip{background:var(--gl-border)}

/* Tabstrip (repurposed #top-bar / #mainTab) */
#top-bar{background:var(--gl-panel-2)!important;height:36px!important;border-bottom:1px solid var(--gl-border)}
.rail-group-caption{font-size:9.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--gl-faint);
  padding:0 10px 0 4px;border-right:1px solid var(--gl-border);margin-right:4px;white-space:nowrap;flex-shrink:0}
.top-tab{color:var(--gl-muted)!important}
.top-tab.active{color:var(--gl-ink)!important;border-bottom-color:var(--gl-accent)!important}
.top-tab:hover{color:var(--gl-ink)!important}
.nav-item[data-group].gl-hidden{display:none!important}
/* .d-flex etc. carry !important, so plain el.style.display can't hide them -- use this instead */
.gl-force-hidden{display:none!important}

/* Consistent filter-bar treatment for simple single-row filter views */
.filterbar{
  display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:8px 10px;margin-bottom:10px;
  background:var(--gl-panel);border:1px solid var(--gl-border);border-radius:6px;
}
.filterbar .text-muted{color:var(--gl-faint)!important}

/* Session/action buttons restyled to the amber accent */
.btn-success,.btn-primary{background:var(--gl-accent)!important;border-color:var(--gl-accent)!important;color:var(--gl-accent-ink)!important}
.btn-outline-primary{color:var(--gl-accent)!important;border-color:var(--gl-accent-dim)!important}
.btn-outline-primary:hover{background:var(--gl-accent)!important;color:var(--gl-accent-ink)!important}

/* Overview tab stat cards */
.gl-card{background:var(--gl-panel);border:1px solid var(--gl-border);border-radius:8px;padding:14px}
.gl-card h6{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--gl-muted);font-weight:700;margin-bottom:10px}
.gl-stat-k{font-size:10px;color:var(--gl-faint);text-transform:uppercase;letter-spacing:.05em}
.gl-stat-v{font-family:var(--gl-mono);font-size:22px;font-variant-numeric:tabular-nums}
</style>
</head>
<body>

<div class="app-shell">

  <!-- ══════════════════════ LEFT RAIL ══════════════════════ -->
  <nav class="rail" id="rail">
    <div class="rail-mark">GL</div>
    <button class="rail-item" data-group="overview"><span class="ico">&#9671;</span><span class="lbl">Overview</span></button>
    <button class="rail-item" data-group="levels"><span class="ico">&#9638;</span><span class="lbl">Levels</span></button>
    <button class="rail-item" data-group="explore"><span class="ico">&#9678;</span><span class="lbl">Explore</span></button>
    <button class="rail-item" data-group="charts"><span class="ico">&#128200;</span><span class="lbl">Charts</span></button>
    <button class="rail-item" data-group="algolab"><span class="ico">&#9879;</span><span class="lbl">Algo Lab</span></button>
    <button class="rail-item" data-group="trading"><span class="ico">&#9635;</span><span class="lbl">Trading</span></button>
    <div class="rail-spacer"></div>
  </nav>

  <div class="main-col">

    <!-- Header -->
    <div class="app-header">
      <span class="brand">Galao</span>
      <span class="verchip">v5.00</span>
      <span class="gl-pill" id="session-broker-badge" style="color:var(--gl-muted)">Broker: —</span>
      <span class="gl-pill" id="session-decider-badge" style="color:var(--gl-muted)">Decider: —</span>
      <span class="text-muted" id="session-uptime" style="font-size:.7rem;min-width:3.5em"></span>
      <div class="gl-ticker">
        <div class="gl-tick"><span class="sym">MES</span><span class="px" id="chip-MES">—</span></div>
        <div class="gl-tick"><span class="sym">MNQ</span><span class="px" id="chip-MNQ">—</span></div>
        <div class="gl-tick"><span class="sym">MYM</span><span class="px" id="chip-MYM">—</span></div>
        <div class="gl-tick"><span class="sym">M2K</span><span class="px" id="chip-M2K">—</span></div>
      </div>
      <div class="app-header-spacer"></div>
      <button class="nav-link top-tab icon-tab" onclick="toggleCrossMenu(event)" title="Other dashboards" style="color:var(--gl-muted)">&#128279;</button>
      <button class="btn btn-sm btn-success" id="session-toggle-btn" onclick="toggleSession()">Start Session</button>
    </div>

    <!-- Unified busy indicator (replaces the old full-screen hourglass overlay) -->
    <div class="busy-strip idle" id="busy-strip"></div>

    <div id="top-bar">
      <span class="rail-group-caption" id="rail-group-caption">Overview</span>
      <ul class="nav mb-0 flex-shrink-0" id="mainTab" role="tablist" style="height:36px;gap:0;list-style:none;padding:0;margin:0;display:flex">
        <li class="nav-item" data-group="overview"><button class="nav-link top-tab active" data-bs-toggle="tab" data-bs-target="#tab-overview" id="btn-overview-tab">Overview</button></li>
        <li class="nav-item" data-group="levels"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-lines" id="btn-lines-tab">Lines</button></li>
        <li class="nav-item" data-group="levels"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-sandbox" id="btn-sandbox-tab">Sandbox</button></li>
        <li class="nav-item" data-group="explore"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-srviz" id="btn-srviz-tab">Sup/Res Viz</button></li>
        <li class="nav-item" data-group="explore"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-correlation" id="btn-correlation-tab">Correlation</button></li>
        <li class="nav-item" data-group="charts"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-graph" id="btn-graph-tab">Graph</button></li>
        <li class="nav-item" data-group="charts"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-all" id="btn-all-tab">All</button></li>
        <li class="nav-item" data-group="charts"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-test" id="btn-test-tab">Test</button></li>
        <li class="nav-item" data-group="algolab"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-algolab-grid" id="btn-algolab-grid-tab">Grid &amp; Submit</button></li>
        <li class="nav-item" data-group="algolab"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-algolab-pnl" id="btn-algolab-pnl-tab">P&amp;L Breakdown</button></li>
        <li class="nav-item" data-group="trading"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-trades" id="btn-trades-tab">Create Trades</button></li>
        <li class="nav-item" data-group="trading"><button class="nav-link top-tab" data-bs-toggle="tab" data-bs-target="#tab-submitted" id="btn-sub-tab">Submitted</button></li>
      </ul>
      <ul class="dropdown-menu dropdown-menu-dark" id="menu-links">
        <li><a class="dropdown-item" id="menu-link-cc2026"  target="_blank">CC2026 Dashboard (this)</a></li>
        <li><a class="dropdown-item" id="menu-link-fetcher" target="_blank">Fetcher2026</a></li>
        <li><a class="dropdown-item" id="menu-link-geva"    target="_blank">GevaExtract</a></li>
      </ul>
      <div class="d-flex align-items-center gap-2 small flex-shrink-0 ms-auto gl-force-hidden" id="date-range-wrap">
        <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="day" checked onchange="onDateRangeChange()"> 1D</label>
        <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="week" onchange="onDateRangeChange()"> 1W</label>
        <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="2weeks" onchange="onDateRangeChange()"> 2W</label>
        <label class="mb-0 user-select-none"><input type="radio" name="date-range" value="custom" onchange="onDateRangeChange()"> Custom</label>
        <input type="date" id="range-from" class="form-control form-control-sm py-0" style="width:120px;display:none;font-size:.75rem;height:24px" onchange="onDateRangeChange()">
        <span id="range-sep" style="display:none" class="text-muted">–</span>
        <input type="date" id="range-to"   class="form-control form-control-sm py-0" style="width:120px;display:none;font-size:.75rem;height:24px" onchange="onDateRangeChange()">
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

<!-- ══════════════════════ OVERVIEW ══════════════════════ -->
<div class="tab-pane fade show active" id="tab-overview">
  <div class="row g-3 mb-1">
    <div class="col-3"><div class="gl-card"><div class="gl-stat-k">Session Uptime</div>
      <div class="gl-stat-v" id="ov-uptime">—</div></div></div>
    <div class="col-3"><div class="gl-card"><div class="gl-stat-k">Broker / Decider</div>
      <div class="gl-stat-v" id="ov-session" style="font-size:16px">—</div></div></div>
    <div class="col-3"><div class="gl-card"><div class="gl-stat-k">Trades Tracked</div>
      <div class="gl-stat-v" id="ov-trades">—</div></div></div>
    <div class="col-3"><div class="gl-card"><div class="gl-stat-k">Net P&amp;L (tracked)</div>
      <div class="gl-stat-v" id="ov-pnl">—</div></div></div>
  </div>
  <div class="row g-3">
    <div class="col-6">
      <div class="gl-card">
        <h6>P&amp;L by Source</h6>
        <table class="table table-sm table-hover table-borderless mb-0">
          <thead><tr><th>Symbol</th><th>Source</th><th>Trades</th><th>Win%</th><th>$ P&amp;L</th></tr></thead>
          <tbody id="ov-summary-tbody"><tr><td colspan="5" class="text-muted small">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="col-6">
      <div class="gl-card">
        <h6>Quick Links</h6>
        <div class="d-flex flex-column gap-2">
          <a href="#" class="text-decoration-none" onclick="selectGroupTab('algolab','tab-algolab-grid');return false">&#9879; Algo Lab &rarr; Grid &amp; Submit</a>
          <a href="#" class="text-decoration-none" onclick="selectGroupTab('explore','tab-correlation');return false">&#9678; Explore &rarr; Correlation</a>
          <a href="#" class="text-decoration-none" onclick="selectGroupTab('explore','tab-srviz');return false">&#9678; Explore &rarr; Sup/Res Viz</a>
          <a href="#" class="text-decoration-none" onclick="selectGroupTab('trading','tab-submitted');return false">&#9635; Trading &rarr; Submitted</a>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════ LINES ══════════════════════ -->
<div class="tab-pane fade" id="tab-lines">
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
  <!-- ── Unified range preset: Day/Week -> short-range tick charts; Month+ -> Long View bars ── -->
  <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
    <div class="btn-group btn-group-sm" id="all-preset-group">
      <button class="btn btn-outline-light active" data-preset="day"   onclick="setAllPreset('day')">Day</button>
      <button class="btn btn-outline-light"        data-preset="week"  onclick="setAllPreset('week')">Week</button>
      <button class="btn btn-outline-light"        data-preset="month" onclick="setAllPreset('month')">Month</button>
      <button class="btn btn-outline-light"        data-preset="2mo"   onclick="setAllPreset('2mo')">2mo</button>
      <button class="btn btn-outline-light"        data-preset="6mo"   onclick="setAllPreset('6mo')">6mo</button>
      <button class="btn btn-outline-light"        data-preset="year"  onclick="setAllPreset('year')">Year</button>
    </div>
    <span id="all-preset-note" class="small text-muted"></span>
  </div>

  <!-- ── Short range (Day/Week) controls: tick-CSV based, fine intraday detail ── -->
  <div id="all-shortrange">
    <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
      <div class="btn-group btn-group-sm" role="group" id="all-interval-group">
        <button class="btn btn-outline-secondary" onclick="setAllInterval(0.5,this)">30s</button>
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
      <button class="btn btn-sm btn-outline-info" id="all-overlay-btn" onclick="toggleAllOverlay()" style="display:none">&#8853; Overlay</button>
      <button class="btn btn-sm btn-outline-warning active" id="all-auto-btn" onclick="toggleAllAutoZoom()"
              title="Auto-refine bar resolution when you zoom the overlay chart">&#9889; Auto</button>
    </div>
  </div>

  <!-- ── Long View (Month/2mo/6mo/Year) controls: pre-backfilled 30-min bars, coarser ── -->
  <div id="all-longview" style="display:none">
    <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
      <div class="btn-group btn-group-sm" id="lv-res-group">
        <button class="btn btn-outline-secondary" data-res="30m" onclick="setLVRes('30m',this)">30m</button>
        <button class="btn btn-outline-secondary" data-res="1h"  onclick="setLVRes('1h',this)">1h</button>
        <button class="btn btn-outline-secondary" data-res="4h"  onclick="setLVRes('4h',this)">4h</button>
        <button class="btn btn-outline-secondary" data-res="1d"  onclick="setLVRes('1d',this)">1d</button>
      </div>
      <span id="lv-status" class="small text-muted"></span>
    </div>
  </div>

  <!-- ── Shared overlay + diff panel: same visualization for every preset, just a -->
  <!-- different data source/span behind it (tick-CSV for Day/Week, bars.db beyond) -->
  <div id="chart-all-overlay-wrap" style="display:none">
    <div class="d-flex align-items-center gap-2 mb-1">
      <button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:.72rem" onclick="resetAllZoom()">&#8634; Reset Zoom</button>
      <span class="ms-2 small text-muted">Symbols:</span>
      <label class="mb-0"><input type="checkbox" id="all-sym-MES" checked onchange="_plotAllOverlay()"> MES</label>
      <label class="mb-0"><input type="checkbox" id="all-sym-MYM" checked onchange="_plotAllOverlay()"> MYM</label>
      <label class="mb-0"><input type="checkbox" id="all-sym-M2K" checked onchange="_plotAllOverlay()"> M2K</label>
    </div>
    <div id="chart-all-overlay" style="height:285px;background:#1a1a2e;border-radius:4px"></div>
    <div class="d-flex align-items-center gap-3 my-1 small text-muted">
      <span>Pairs:</span>
      <label class="mb-0"><input type="checkbox" id="all-pair-MES_MYM" checked onchange="_plotAllDiff()"> MES&minus;MYM</label>
      <label class="mb-0"><input type="checkbox" id="all-pair-MES_M2K" checked onchange="_plotAllDiff()"> MES&minus;M2K</label>
      <label class="mb-0"><input type="checkbox" id="all-pair-MYM_M2K" checked onchange="_plotAllDiff()"> MYM&minus;M2K</label>
      <span class="ms-auto" style="font-size:.7rem" id="all-diff-note">Lines are de-meaned (own avg over loaded period = 0). Dotted = &plusmn;2&sigma;</span>
    </div>
    <div id="chart-all-diff" style="height:285px;background:#1a1a2e;border-radius:4px"></div>
  </div>
  <div id="all-grid" class="row g-2">
    <div class="col-4">
      <div class="text-center small text-muted mb-1">MES</div>
      <div id="chart-all-MES" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
    <div class="col-4">
      <div class="text-center small text-muted mb-1">MYM</div>
      <div id="chart-all-MYM" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
    <div class="col-4">
      <div class="text-center small text-muted mb-1">M2K</div>
      <div id="chart-all-M2K" style="height:290px;background:#1a1a2e;border-radius:4px"></div>
    </div>
  </div>
</div>

<!-- ══════════════════════ CREATE TRADES ══════════════════════ -->
<div class="tab-pane fade" id="tab-trades">
  <div class="filterbar">
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
  <div class="filterbar">
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
  <div class="filterbar">
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

<!-- ══════════════════════ SUP/RES VIZ ══════════════════════ -->
<div class="tab-pane fade" id="tab-srviz">
  <p class="text-muted small mb-2">
    All critical lines for one symbol/date overlaid on real price action, color-coded by
    detection method — every S/R line here is theoretical/half-baked until judged against
    what price actually did. Hover a line for its formula/inputs; click a legend chip to
    toggle that source on/off.
  </p>
  <div class="filterbar">
    <span class="text-muted small">Symbol:</span>
    <select id="sv-sym" class="form-select form-select-sm d-inline-block" style="width:90px" onchange="loadSrViz()">
      <option>MES</option><option>MNQ</option><option>MYM</option><option>M2K</option>
    </select>
    <label class="small">Date <input type="date" id="sv-date" class="form-control form-control-sm d-inline-block" style="width:150px" onchange="loadSrViz()"></label>
    <button class="btn btn-sm btn-outline-secondary" onclick="loadSrViz()">Reload</button>
    <span id="sv-msg" class="small text-muted"></span>
  </div>
  <div id="sv-legend" class="d-flex gap-2 flex-wrap mb-2" style="font-size:.75rem"></div>
  <div id="sv-chart" style="width:100%;height:520px;background:#1a1a2e;border-radius:4px;"></div>
</div>

<!-- ══════════════════════ ALGO LAB — GRID & SUBMIT ══════════════════════ -->
<div class="tab-pane fade" id="tab-algolab-grid">
  <p class="text-muted small mb-2">
    Submits many parameter combinations of the critical-line strategies (BOUNCE/BREAKOUT/
    DIRECTIONAL/FADE/BOTH, asymmetric TP/SL) as paper trades in one batch, tagged so the
    P&amp;L Breakdown tab can attribute results per exact combo and per originating
    S/R-detection method. Paper trading only — writes PENDING rows the same broker already polls.
  </p>
  <div class="filterbar">
    <div class="filter-group"><span class="text-muted small">Symbols:</span>
    <div id="sym-algolab" class="d-flex gap-2">
      <label class="small"><input class="form-check-input al-sym-chk" type="checkbox" value="MES" checked> MES</label>
      <label class="small"><input class="form-check-input al-sym-chk" type="checkbox" value="MNQ" checked> MNQ</label>
      <label class="small"><input class="form-check-input al-sym-chk" type="checkbox" value="MYM" checked> MYM</label>
      <label class="small"><input class="form-check-input al-sym-chk" type="checkbox" value="M2K" checked> M2K</label>
    </div></div>
    <span class="badge bg-info text-dark" id="al-grid-badge">grid: — / —</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <button class="btn btn-sm btn-outline-primary" onclick="algoLabPreview()">Preview (dry-run)</button>
      <button class="btn btn-sm btn-success" onclick="algoLabSubmit()">Submit Grid (paper trades)</button>
    </div>
  </div>
  <div class="small text-muted mb-2" id="al-msg"></div>
  <div id="al-preview-wrap" class="mb-3" style="display:none">
    <table class="table table-sm table-bordered">
      <thead class="table-dark"><tr><th>Symbol</th><th>Combos w/ candidates</th><th>Est. commands</th></tr></thead>
      <tbody id="al-preview-tbody"></tbody>
    </table>
  </div>
</div>

<!-- ══════════════════════ ALGO LAB — P&L BREAKDOWN ══════════════════════ -->
<div class="tab-pane fade" id="tab-algolab-pnl">
  <div class="filterbar">
    <div class="filter-group"><label class="small mb-0">From <input type="date" id="al-pnl-from" class="form-control form-control-sm d-inline-block" style="width:150px"></label></div>
    <div class="filter-group"><label class="small mb-0">To <input type="date" id="al-pnl-to" class="form-control form-control-sm d-inline-block" style="width:150px"></label></div>
    <button class="btn btn-sm btn-outline-secondary ms-auto" onclick="loadAlgoPnl()">Refresh P&amp;L</button>
  </div>
  <div class="small text-muted mb-1">By source (coarse):</div>
  <table class="table table-sm table-hover table-bordered mb-3">
    <thead class="table-dark"><tr><th>Symbol</th><th>Source</th><th>Trades</th><th>Win%</th>
        <th>Pts</th><th>$ P&amp;L</th></tr></thead>
    <tbody id="al-summary-tbody"></tbody>
  </table>
  <div class="small text-muted mb-1">By exact algo + params + line-detection method:</div>
  <table class="table table-sm table-hover table-bordered">
    <thead class="table-dark"><tr><th>Symbol</th><th>Source</th><th>Strategy</th><th>Params</th>
        <th>Line detect</th><th>Trades</th><th>Win%</th><th>Pts</th><th>$ P&amp;L</th><th>PF</th></tr></thead>
    <tbody id="al-breakdown-tbody"></tbody>
  </table>
</div>

<!-- ══════════════════════ CORRELATION ══════════════════════ -->
<div class="tab-pane fade" id="tab-correlation">
  <p class="text-muted small mb-2">
    Rolling pairwise correlation across the 4 symbols, computed from 30-min bar log-returns
    (trader/data/bars.db). Exploration only — no trades. Meant to surface correlation ideas
    since none exist yet.
  </p>
  <div class="filterbar">
    <span class="text-muted small">Window (30m bars):</span>
    <select id="corr-window" class="form-select form-select-sm d-inline-block" style="width:90px" onchange="loadCorrMatrix()">
      <option value="20">20</option>
      <option value="50" selected>50</option>
      <option value="100">100</option>
    </select>
    <span id="corr-missing" class="badge bg-warning text-dark" style="display:none"></span>
  </div>
  <div id="corr-heatmap" style="width:100%;max-width:520px;height:420px;"></div>

  <hr>
  <h6 class="text-info">Rolling correlation over time</h6>
  <div class="d-flex align-items-center gap-2 flex-wrap mb-2">
    <label class="small">A
      <select id="corr-sym-a" class="form-select form-select-sm d-inline-block" style="width:90px">
        <option>MES</option><option>MNQ</option><option>MYM</option><option>M2K</option>
      </select>
    </label>
    <label class="small">B
      <select id="corr-sym-b" class="form-select form-select-sm d-inline-block" style="width:90px">
        <option>MES</option><option>MNQ</option><option selected>MYM</option><option>M2K</option>
      </select>
    </label>
    <button class="btn btn-sm btn-outline-primary" onclick="loadCorrSeries()">Load</button>
  </div>
  <div id="corr-series-chart" style="width:100%;height:340px;background:#1a1a2e;border-radius:4px;"></div>
</div>

</div><!-- tab-content -->
</div><!-- container -->

  </div><!-- main-col -->
</div><!-- app-shell -->

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
    document.getElementById('busy-strip')?.classList.remove('idle');
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
    document.getElementById('busy-strip')?.classList.add('idle');
    _busyDisabled.forEach(el=>el.disabled=false);
    _busyDisabled=[];
  }
}
const STATUS_CLS={PENDING:'secondary',SUBMITTED:'primary',SUBMITTING:'info',
                  FILLED:'warning',CLOSED:'success',CANCELLED:'dark',ERROR:'danger'};

// ── Price polling ─────────────────────────────────────────────────────────────
let _lastPrices={};
async function pollPrices(){
  try{
    const d=await (await fetch('/api/prices')).json();
    for(const [s,p] of Object.entries(d)){
      const el=document.getElementById('chip-'+s);
      if(!el) continue;
      el.textContent=p!=null?p.toFixed(2):'—';
      const prev=_lastPrices[s];
      el.classList.remove('text-success','text-danger');
      if(p!=null && prev!=null && p!==prev) el.classList.add(p>prev?'text-success':'text-danger');
      if(p!=null) _lastPrices[s]=p;
    }
  }catch(e){}
}
pollPrices();setInterval(pollPrices,5000);

// ── Cross-dashboard menu (localhost/LAN/VPN all work — same host, diff port) ──
// position:fixed + coordinates computed from the button's own screen rect,
// so it escapes #top-bar's overflow-y:hidden (a position:absolute Bootstrap
// dropdown got silently clipped there — button worked, popup was invisible).
(function(){
  const base=location.protocol+'//'+location.hostname;
  document.getElementById('menu-link-cc2026').href  = base+':5003';
  document.getElementById('menu-link-fetcher').href = base+':5050';
  document.getElementById('menu-link-geva').href    = base+':5005';
})();
function toggleCrossMenu(ev){
  ev.stopPropagation();
  const dd=document.getElementById('menu-links');
  const willShow=!dd.classList.contains('show');
  dd.classList.remove('show');
  if(willShow){
    dd.classList.add('show');  // make measurable before positioning
    const r=ev.currentTarget.getBoundingClientRect();
    dd.style.top=r.bottom+'px';
    dd.style.left=Math.max(4,r.right-dd.offsetWidth)+'px';
  }
}
document.addEventListener('click',(e)=>{
  const dd=document.getElementById('menu-links');
  if(dd.classList.contains('show') && !e.target.closest('#menu-links') && !e.target.closest('.icon-tab')){
    dd.classList.remove('show');
  }
});

// ── Session manager (broker + decider) ──────────────────────────────────────
let _sessionBusy=false;
function _sessionBadgeColor(state){
  return state==='running'?'#198754':state==='restarting'?'#fd7e14':'#6c757d';
}
async function pollSessionStatus(){
  try{
    const d=await (await fetch('/api/session/status')).json();
    const bB=document.getElementById('session-broker-badge');
    const bD=document.getElementById('session-decider-badge');
    bB.textContent='Broker: '+d.broker;   bB.style.background=_sessionBadgeColor(d.broker);
    bD.textContent='Decider: '+d.decider; bD.style.background=_sessionBadgeColor(d.decider);
    const up=d.uptime_seconds||0;
    document.getElementById('session-uptime').textContent=
      up>0?`${Math.floor(up/60)}m ${up%60}s`:'';
    if(!_sessionBusy){
      const anyAlive=d.broker!=='dead'||d.decider!=='dead';
      const btn=document.getElementById('session-toggle-btn');
      btn.textContent=anyAlive?'Stop Session':'Start Session';
      btn.className='btn btn-sm '+(anyAlive?'btn-danger':'btn-success');
    }
  }catch(e){}
}
async function toggleSession(){
  const btn=document.getElementById('session-toggle-btn');
  const starting=btn.textContent.trim()==='Start Session';
  _sessionBusy=true; btn.disabled=true;
  btn.textContent=starting?'Starting…':'Stopping…';
  try{
    const r=await fetch('/api/session/'+(starting?'start':'stop'),{method:'POST'});
    const d=await r.json();
    if(d.error) alert('Session '+(starting?'start':'stop')+' failed: '+d.error);
  }catch(e){
    alert('Session '+(starting?'start':'stop')+' failed: '+e);
  }finally{
    _sessionBusy=false; btn.disabled=false;
    pollSessionStatus();
  }
}
pollSessionStatus();setInterval(pollSessionStatus,5000);

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
let _allInterval=5, _allOverlay=false, _allAutoZoom=true, _allZoomTimer=null, _allDaysSpan=1;
let _allPreset='day';

// Day/Week -> short-range tick-CSV path (fine intraday detail, capped at 10
// days since that's roughly all the tick data reliably covers). Month+ ->
// Long View / bars.db path (pre-backfilled, coarser, actually goes back a
// year). Each preset also picks a sensible default Long View resolution;
// the resolution buttons stay as a user override on top of that default.
const ALL_PRESETS={
  day:   {days:1,   overlay:false, lvDays:null, lvRes:null},
  week:  {days:5,   overlay:true,  lvDays:null, lvRes:null},
  month: {days:null,overlay:null,  lvDays:30,   lvRes:'1h'},
  '2mo': {days:null,overlay:null,  lvDays:60,   lvRes:'4h'},
  '6mo': {days:null,overlay:null,  lvDays:180,  lvRes:'1d'},
  year:  {days:null,overlay:null,  lvDays:365,  lvRes:'1d'},
};
const ALL_PRESET_NOTES={
  day:   'Single trading day — fine intraday detail (tick data).',
  week:  '~5 trading days, correlation overlay — tick data.',
  month: '30 days @ hourly bars (pre-backfilled, updates via backfill_bars.py).',
  '2mo': '60 days @ 4h bars.',
  '6mo': '180 days @ daily bars.',
  year:  '365 days @ daily bars.',
};

function setAllPreset(preset){
  const cfg=ALL_PRESETS[preset];
  if(!cfg)return;
  _allPreset=preset;
  document.querySelectorAll('#all-preset-group .btn').forEach(b=>
    b.classList.toggle('active', b.dataset.preset===preset));
  document.getElementById('all-preset-note').textContent=ALL_PRESET_NOTES[preset]||'';

  const shortRange = (preset==='day' || preset==='week');
  document.getElementById('all-shortrange').style.display = shortRange ? 'block' : 'none';
  document.getElementById('all-longview').style.display   = shortRange ? 'none'  : 'block';
  // Overlay is a user choice only for Day (grid vs correlation lines); Week and
  // Month+ always show the overlay+diff panel, just fed by a different source.
  document.getElementById('all-overlay-btn').style.display = (preset==='day') ? '' : 'none';

  if(shortRange){
    _allDaysSpan=cfg.days;
    if(_allOverlay!==cfg.overlay){
      // toggleAllOverlay() flips _allOverlay and reloads itself
      toggleAllOverlay();
    }else{
      loadAllSymbols();
    }
  }else{
    _lvDays=cfg.lvDays;
    _lvRes=cfg.lvRes;
    document.querySelectorAll('#lv-res-group .btn').forEach(b=>
      b.classList.toggle('active', b.dataset.res===_lvRes));
    document.getElementById('all-grid').style.display='none';
    document.getElementById('chart-all-overlay-wrap').style.display='block';
    _loadLongOverlay();
  }
}

// Hides weekends + outside-RTH hours so a multi-day span isn't mostly blank gaps.
// Long View bars aren't RTH-restricted (near-continuous futures session), so
// only the weekend break applies there — the hour break would carve out real data.
const ALL_RANGEBREAKS=[
  {pattern:'day of week', bounds:[6,1]},
  {pattern:'hour', bounds:[16,9.5]},
];
const ALL_RANGEBREAKS_LONG=[
  {pattern:'day of week', bounds:[6,1]},
];
let _allDiffCache=null, _allSyncingZoom=false, _allDiffUnit='ticks';
let _allOverlayCache=null, _allOverlayUnit='ticks', _allOverlayRangebreaks=null;
const ALL_PAIRS=[['MES','MYM'],['MES','M2K'],['MYM','M2K']];
const ALL_PAIR_COLORS={MES_MYM:'#E8A838',MES_M2K:'#9B59B6',MYM_M2K:'#17A2B8'};
const ALL_SYMS=['MES','MYM','M2K'];
const ALL_SYM_COLORS={MES:'#5B8DD9',MYM:'#32BA64',M2K:'#D25050'};

// Auto-zoom bar resolution ladder: wider visible window -> coarser bars, so
// zooming in always reveals more bars instead of the same handful stretched out.
const _ALL_ZOOM_LADDER=[
  {maxSec:4*3600, interval:30},
  {maxSec:2*3600, interval:15},
  {maxSec:40*60,  interval:5},
  {maxSec:8*60,   interval:1},
  {maxSec:0,      interval:0.5},
];
function _allIntervalForWindow(sec){
  for(const {maxSec,interval} of _ALL_ZOOM_LADDER) if(sec>maxSec) return interval;
  return 0.5;
}

function setAllInterval(v,btn){
  _allInterval=v;
  document.querySelectorAll('#all-interval-group .btn').forEach(b=>b.classList.remove('active'));
  if(btn)btn.classList.add('active');
  loadAllSymbols();
}
function _syncAllIntervalBtn(){
  document.querySelectorAll('#all-interval-group .btn').forEach(b=>{
    b.classList.toggle('active', parseFloat(b.getAttribute('onclick').match(/setAllInterval\(([\d.]+)/)[1])===_allInterval);
  });
}

function toggleAllAutoZoom(){
  _allAutoZoom=!_allAutoZoom;
  document.getElementById('all-auto-btn').classList.toggle('active',_allAutoZoom);
}

// Resets both overlay charts to full-data view. Explicit on both divs rather
// than relying on the zoom-sync mirror — autorange relayout events don't
// carry the same 'xaxis.range[0]/[1]' keys that mirror listens for, and Long
// View charts (Month+) aren't zoom-synced at all.
function resetAllZoom(){
  clearTimeout(_allZoomTimer);
  for(const id of ['chart-all-overlay','chart-all-diff']){
    try{ Plotly.relayout(id,{'xaxis.autorange':true}); }catch(e){}
  }
}

function toggleAllOverlay(){
  _allOverlay=!_allOverlay;
  document.getElementById('all-overlay-btn').classList.toggle('active',_allOverlay);
  document.getElementById('chart-all-overlay-wrap').style.display=_allOverlay?'block':'none';
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
  _enterBusy();
  try{
    if(_allOverlay) await _loadOverlayAll(reqDate);
    else await Promise.all(['MES','MYM','M2K'].map(sym=>_loadOneSymAll(sym,reqDate)));
  }finally{_exitBusy();}
}

function _pairKey(a,b){return a+'_'+b;}

async function _loadOverlayAll(reqDate,forcedRange){
  const el=document.getElementById('chart-all-overlay');
  let data;
  try{
    data=await Promise.all(ALL_SYMS.map(s=>
      fetch(`/api/history/${s}?interval=${_allInterval}&date=${reqDate}&days=${_allDaysSpan}`).then(r=>r.json())
    ));
  }catch(e){el.innerHTML=`<div class="text-danger small p-2">${e}</div>`;return;}
  const spanRange=(data.find(d=>d.date_range)||{}).date_range;
  if(spanRange){
    document.getElementById('all-day-info').textContent=
      _allDaysSpan>1 ? `${spanRange[0]} → ${spanRange[1]}  (${_allDaysSpan}d)` : spanRange[1];
  }

  _allOverlayUnit='ticks';
  _allOverlayCache={};
  _allOverlayRangebreaks=_allDaysSpan>1 ? ALL_RANGEBREAKS : null;
  const tsToY={};  // per-symbol map: timestamp -> ticks-from-open (for the diff panel below)
  for(let i=0;i<ALL_SYMS.length;i++){
    const sym=ALL_SYMS[i],bars=data[i].bars||[];
    if(!bars.length)continue;
    const base=bars[0].close??bars[0].open;
    const tick=SB_TICKS[sym]||0.25;
    const ys=bars.map(b=>Math.round((b.close-base)/tick));
    tsToY[sym]=new Map(bars.map((b,j)=>[b.t,ys[j]]));
    _allOverlayCache[sym]={x:bars.map(b=>b.t), y:ys};
  }
  if(!Object.keys(_allOverlayCache).length){el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No data</div>';return;}
  await _plotAllOverlay(forcedRange);

  // Pairwise diffs for the opportunity panel below — only over timestamps
  // present in BOTH symbols of a pair (a data gap in one shouldn't silently
  // misalign the other). Session-wide mean/std gives a flat +-2sigma band:
  // "what's normal scatter for this pair today."
  _allDiffUnit='ticks';
  _allDiffCache={};
  for(const [a,b] of ALL_PAIRS){
    const key=_pairKey(a,b);
    if(!tsToY[a]||!tsToY[b]){_allDiffCache[key]=null;continue;}
    const xs=[],ys=[];
    for(const [t,ya] of tsToY[a]){
      if(!tsToY[b].has(t))continue;
      xs.push(t); ys.push(ya-tsToY[b].get(t));
    }
    if(ys.length<2){_allDiffCache[key]=null;continue;}
    const mean=ys.reduce((s,v)=>s+v,0)/ys.length;
    const std=Math.sqrt(ys.reduce((s,v)=>s+(v-mean)**2,0)/ys.length);
    _allDiffCache[key]={x:xs,y:ys,mean,std};
  }
  await _plotAllDiff(forcedRange);

  // Zoom handling, wired on both panels — mirror is shared with Long View
  // (_wireAllZoomMirror below); auto-refine is specific to the tick-CSV
  // interval ladder so it's passed in as a callback rather than baked into
  // the shared mirror function.
  const diffEl=document.getElementById('chart-all-diff');
  const onZoomSettled=(x0,x1)=>{
    if(!_allAutoZoom)return;
    clearTimeout(_allZoomTimer);
    _allZoomTimer=setTimeout(()=>{
      const windowSec=(new Date(x1)-new Date(x0))/1000;
      const next=_allIntervalForWindow(windowSec);
      if(next===_allInterval)return;
      _allInterval=next;
      _syncAllIntervalBtn();
      _loadOverlayAll(reqDate,[x0,x1]);
    },400);
  };
  _wireAllZoomMirror(el,diffEl,onZoomSettled);
  _wireAllZoomMirror(diffEl,el,onZoomSettled);
}

// Mirrors zoom between the overlay and diff charts, since they're two
// independent Plotly figures (not subplots) sharing one time axis rather
// than a single figure with linked axes. _allSyncingZoom guards against the
// mirrored relayout re-triggering itself (infinite ping-pong). onZoomSettled
// is optional — short-range passes the interval auto-refine logic; Long
// View just wants the mirror with no reload behavior attached.
function _wireAllZoomMirror(srcEl, mirrorEl, onZoomSettled){
  srcEl.removeAllListeners?.('plotly_relayout');
  srcEl.on('plotly_relayout',(ev)=>{
    if(_allSyncingZoom){_allSyncingZoom=false;return;}
    const x0=ev['xaxis.range[0]'], x1=ev['xaxis.range[1]'];
    if(x0===undefined||x1===undefined)return;  // pan/autorange/other relayout, not a zoom range
    _allSyncingZoom=true;
    Plotly.relayout(mirrorEl,{'xaxis.range':[x0,x1]});
    if(onZoomSettled) onZoomSettled(x0,x1);
  });
}

async function _plotAllOverlay(forcedRange){
  const el=document.getElementById('chart-all-overlay');
  if(!_allOverlayCache){el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No data</div>';return;}
  // Preserve the current zoom on checkbox-toggle redraws, same as _plotAllDiff —
  // and since only checked symbols' traces get passed to Plotly.newPlot below,
  // the y-axis autorange (no explicit yaxis.range set) rescales to fit only
  // those traces' valid points within the current x-window on every redraw.
  if(!forcedRange){
    const cur=el._fullLayout?.xaxis?.range;
    if(cur) forcedRange=[...cur];
  }
  const unitLabel = _allOverlayUnit==='pct' ? '%' : 'ticks';
  const traces=[];
  for(const sym of ALL_SYMS){
    const chk=document.getElementById('all-sym-'+sym);
    if(chk&&!chk.checked)continue;
    const d=_allOverlayCache[sym];
    if(!d||!d.y||!d.y.length)continue;
    traces.push({name:sym,type:'scatter',mode:'lines',x:d.x,y:d.y,
      line:{color:ALL_SYM_COLORS[sym],width:1.5},
      hovertemplate:`<b>${sym}</b><br>%{x}<br>%{y} ${unitLabel}<extra></extra>`});
  }
  if(!traces.length){el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No symbols selected</div>';return;}
  const xaxis={gridcolor:'#252535',zeroline:false,rangeslider:{visible:false}};
  if(_allOverlayRangebreaks) xaxis.rangebreaks=_allOverlayRangebreaks;
  if(forcedRange) xaxis.range=forcedRange;
  await Plotly.newPlot(el,traces,{
    paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
    font:{color:'#ccc',size:10},margin:{t:8,b:20,l:50,r:8},
    xaxis,
    yaxis:{gridcolor:'#252535',zeroline:true,zerolinecolor:'#555',
           title:_allOverlayUnit==='pct'?'% change':'Ticks from open'},
    showlegend:true,legend:{x:0,y:1,bgcolor:'rgba(0,0,0,0)',font:{size:11}},
    dragmode:'zoom',
  },{responsive:true,displayModeBar:false,scrollZoom:true});
}

async function _plotAllDiff(forcedRange){
  const el=document.getElementById('chart-all-diff');
  if(!_allDiffCache){el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No data</div>';return;}
  // Preserve the current zoom on checkbox-toggle redraws (no explicit range passed) —
  // otherwise ticking a pair on/off would silently reset the view to full-day.
  if(!forcedRange){
    const cur=el._fullLayout?.xaxis?.range;
    if(cur) forcedRange=[...cur];
  }
  const unitLabel = _allDiffUnit==='pct' ? '%' : (_allDiffUnit==='norm' ? 'norm' : 'ticks');
  const traces=[];
  for(const [a,b] of ALL_PAIRS){
    const key=_pairKey(a,b);
    const chk=document.getElementById('all-pair-'+key);
    if(chk&&!chk.checked)continue;
    const d=_allDiffCache[key];
    if(!d)continue;
    const col=ALL_PAIR_COLORS[key];
    const label=`${a}−${b}`;
    // De-meaned: each pair centers on its own average over the loaded window,
    // so all 3 sit around zero regardless of absolute drift level (e.g. a
    // pair that drifted -14% over a year no longer visually dwarfs one that
    // stayed near 0) -- the +-2sigma band is the residual wobble around that.
    // Note this deliberately hides the absolute drift direction/magnitude;
    // that's the explicit tradeoff that was asked for.
    const yDemeaned = d.y.map(v=>Math.round((v-d.mean)*100)/100);
    traces.push({name:label,type:'scatter',mode:'lines',x:d.x,y:yDemeaned,
      line:{color:col,width:1.5},
      hovertemplate:`<b>${label}</b><br>%{x}<br>%{y} ${unitLabel} (vs own avg)<extra></extra>`});
    traces.push({name:label+' +2σ',type:'scatter',mode:'lines',x:[d.x[0],d.x[d.x.length-1]],
      y:[2*d.std,2*d.std],line:{color:col,width:1,dash:'dot'},
      opacity:.6,hoverinfo:'skip',showlegend:false});
    traces.push({name:label+' -2σ',type:'scatter',mode:'lines',x:[d.x[0],d.x[d.x.length-1]],
      y:[-2*d.std,-2*d.std],line:{color:col,width:1,dash:'dot'},
      opacity:.6,hoverinfo:'skip',showlegend:false});
  }
  if(!traces.length){el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No pairs selected</div>';return;}
  const xaxis={gridcolor:'#252535',zeroline:false,rangeslider:{visible:false}};
  if(_allPreset==='week')                      xaxis.rangebreaks=ALL_RANGEBREAKS;
  else if(_allPreset!=='day')                  xaxis.rangebreaks=ALL_RANGEBREAKS_LONG;
  if(forcedRange) xaxis.range=forcedRange;
  await Plotly.newPlot(el,traces,{
    paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',
    font:{color:'#ccc',size:10},margin:{t:8,b:30,l:50,r:8},
    xaxis,
    yaxis:{gridcolor:'#252535',zeroline:true,zerolinecolor:'#777',title:`Diff (${unitLabel})`},
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
  setAllPreset(_allPreset);
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

// ── Long View — same overlay+diff visualization as short-range, fed by bars.db ──
let _lvRes='1h', _lvDays=180;

function setLVRes(v,btn){
  _lvRes=v;
  document.querySelectorAll('#lv-res-group .btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  _loadLongOverlay();
}

async function _loadLongOverlay(){
  const el=document.getElementById('chart-all-overlay');
  const st=document.getElementById('lv-status');
  if(st)st.textContent='Loading...';
  let data;
  try{
    data=await Promise.all(ALL_SYMS.map(s=>
      fetch(`/api/bars-long?symbol=${s}&days=${_lvDays}&resolution=${_lvRes}`).then(r=>r.json())
    ));
  }catch(e){el.innerHTML=`<div class="text-danger small p-2">${e}</div>`;return;}

  _allOverlayUnit='pct';
  _allOverlayCache={};
  _allOverlayRangebreaks=ALL_RANGEBREAKS_LONG;
  const tsToPct={};  // per-symbol map: timestamp -> % change from first bar
  for(let i=0;i<ALL_SYMS.length;i++){
    const sym=ALL_SYMS[i], d=data[i];
    if(d.error||!d.close||!d.close.length)continue;
    const base=d.close[0];
    const pct=d.close.map(c=>Math.round(((c/base)-1)*10000)/100);
    tsToPct[sym]=new Map(d.ts.map((t,j)=>[t,pct[j]]));
    _allOverlayCache[sym]={x:d.ts,y:pct};
  }
  if(!Object.keys(_allOverlayCache).length){
    el.innerHTML='<div class="d-flex align-items-center justify-content-center h-100 text-muted small">No data — run scripts/backfill_bars.py</div>';
    if(st)st.textContent='';
    return;
  }
  await _plotAllOverlay();

  // Pairwise-diff panel: at native 30m resolution, read the precomputed,
  // sanity-checked diff_norm straight from bars_30m_diffs_normalized (via
  // /api/bars-long?pair=...) instead of recomputing it client-side from the
  // % overlay lines above. 1h/4h/1d have no precomputed table at that
  // granularity, so they keep the original on-the-fly calc.
  if(_lvRes==='30m'){
    _allDiffUnit='norm';
    _allDiffCache={};
    let pairData;
    try{
      pairData=await Promise.all(ALL_PAIRS.map(([a,b])=>
        fetch(`/api/bars-long?pair=${a}-${b}&days=${_lvDays}&resolution=30m`).then(r=>r.json())
      ));
    }catch(e){pairData=ALL_PAIRS.map(()=>({error:String(e)}));}
    for(let i=0;i<ALL_PAIRS.length;i++){
      const [a,b]=ALL_PAIRS[i], key=_pairKey(a,b), d=pairData[i];
      if(d.error||!d.spread||d.spread.length<2){_allDiffCache[key]=null;continue;}
      const ys=d.spread;
      const mean=ys.reduce((s,v)=>s+v,0)/ys.length;
      const std=Math.sqrt(ys.reduce((s,v)=>s+(v-mean)**2,0)/ys.length);
      _allDiffCache[key]={x:d.ts,y:ys,mean,std};
    }
  } else {
    // Same pairwise-diff treatment as short-range, just in % instead of ticks —
    // reuses the same checkboxes and _plotAllDiff() renderer.
    _allDiffUnit='pct';
    _allDiffCache={};
    for(const [a,b] of ALL_PAIRS){
      const key=_pairKey(a,b);
      if(!tsToPct[a]||!tsToPct[b]){_allDiffCache[key]=null;continue;}
      const xs=[],ys=[];
      for(const [t,pa] of tsToPct[a]){
        if(!tsToPct[b].has(t))continue;
        xs.push(t); ys.push(Math.round((pa-tsToPct[b].get(t))*100)/100);
      }
      if(ys.length<2){_allDiffCache[key]=null;continue;}
      const mean=ys.reduce((s,v)=>s+v,0)/ys.length;
      const std=Math.sqrt(ys.reduce((s,v)=>s+(v-mean)**2,0)/ys.length);
      _allDiffCache[key]={x:xs,y:ys,mean,std};
    }
  }
  await _plotAllDiff();

  // Same zoom mirror as short-range (no auto-refine ladder here — Long View's
  // resolution buttons cover that role instead).
  const diffEl=document.getElementById('chart-all-diff');
  _wireAllZoomMirror(el,diffEl);
  _wireAllZoomMirror(diffEl,el);

  if(st)st.textContent=`Loaded ${_lvDays}d @ ${_lvRes}  ${new Date().toLocaleTimeString()}`;
}

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
    barmode:'overlay',showlegend:false,bargap:0.06,
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

// ── Sup/Res Viz ───────────────────────────────────────────────────────────────
let _svHidden=new Set();

function _svLastWeekday(){
  const d=new Date();
  d.setDate(d.getDate()-1);
  while(d.getDay()===0||d.getDay()===6)d.setDate(d.getDate()-1);
  return d.toISOString().slice(0,10);
}

async function loadSrViz(){
  const sym=document.getElementById('sv-sym').value;
  const dateEl=document.getElementById('sv-date');
  if(!dateEl.value)dateEl.value=_svLastWeekday();
  const dt=dateEl.value;
  document.getElementById('sv-msg').textContent='Loading…';
  _enterBusy();
  try{

  const [histResp,linesResp]=await Promise.all([
    fetch(`/api/history/${sym}?interval=15&date=${dt}&days=1`).then(r=>r.json()).catch(()=>({bars:[]})),
    fetch(`/api/srviz/${sym}?date=${dt}`).then(r=>r.json()).catch(()=>({lines:[]}))
  ]);

  const bars=histResp.bars||[];
  const lines=(linesResp.lines||[]).filter(l=>!_svHidden.has(l.source));

  const legendSrcs=[...new Set((linesResp.lines||[]).map(l=>l.source))];
  document.getElementById('sv-legend').innerHTML=legendSrcs.map(s=>{
    const color=SOURCE_COLORS[s]||'#888';
    const off=_svHidden.has(s);
    return `<span class="badge" style="background:${color};opacity:${off?0.3:1};cursor:pointer"
              onclick="_svToggleSource('${s}')">${s}</span>`;
  }).join('')||'<span class="text-muted">No lines for this symbol/date.</span>';

  if(!bars.length){
    document.getElementById('sv-chart').innerHTML=
      '<div class="d-flex align-items-center justify-content-center h-100 text-muted">No price history for this date.</div>';
    document.getElementById('sv-msg').textContent=histResp.error||'';
    return;
  }
  document.getElementById('sv-msg').textContent=
    `${bars.length} bars · ${lines.length} of ${(linesResp.lines||[]).length} lines shown`;

  const shapes=[],annotations=[];
  for(const ln of lines){
    const color=SOURCE_COLORS[ln.source]||'#fff';
    shapes.push({type:'line',x0:0,x1:1,y0:ln.price,y1:ln.price,xref:'paper',yref:'y',
      line:{color,width:ln.strength===1?2:1,dash:ln.strength===1?'solid':'dot'}});
    let tip=`${ln.line_type} ${ln.price.toFixed(2)} [${ln.source}]`;
    try{const note=ln.note?JSON.parse(ln.note):null;if(note?.formula)tip+=' — '+note.formula;}catch(e){}
    annotations.push({x:1,y:ln.price,xref:'paper',yref:'y',text:ln.price.toFixed(2),
      showarrow:false,xanchor:'right',font:{size:8,color},hovertext:tip});
  }

  Plotly.newPlot('sv-chart',[{
    type:'candlestick',
    x:bars.map(b=>b.t),open:bars.map(b=>b.open),high:bars.map(b=>b.high),
    low:bars.map(b=>b.low),close:bars.map(b=>b.close),
    name:sym,showlegend:false,
    increasing:{line:{color:'#26a69a'}},decreasing:{line:{color:'#ef5350'}}
  }],{
    paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',font:{color:'#ccc'},
    margin:{l:55,r:55,t:8,b:45},dragmode:'zoom',showlegend:false,
    xaxis:{gridcolor:'#252535',rangeslider:{visible:false}},
    yaxis:{gridcolor:'#252535'},
    shapes,annotations
  },{responsive:true,displayModeBar:false});
  }finally{_exitBusy();}
}

function _svToggleSource(src){
  if(_svHidden.has(src))_svHidden.delete(src);else _svHidden.add(src);
  loadSrViz();
}

document.getElementById('btn-srviz-tab').addEventListener('click',function(){
  if(!document.getElementById('sv-date').value)document.getElementById('sv-date').value=_svLastWeekday();
  loadSrViz();
});

// ── Algo Lab ──────────────────────────────────────────────────────────────────
function alSelectedSymbols(){
  return [...document.querySelectorAll('.al-sym-chk:checked')].map(el=>el.value);
}

async function algoLabLoadConfig(){
  try{
    const c=await (await fetch('/api/algo-lab/config')).json();
    document.getElementById('al-grid-badge').textContent=
      `grid: ${c.combo_count} of ${c.full_grid_size} combos/symbol (config-capped)`;
  }catch(e){}
}

function _renderComboMap(tbodyId,perSymbol,estKey){
  const tbody=document.getElementById(tbodyId);
  tbody.innerHTML='';
  for(const [sym,combos] of Object.entries(perSymbol)){
    if(combos.error){
      tbody.innerHTML+=`<tr><td>${sym}</td><td colspan="2" class="text-warning">${combos.error}</td></tr>`;
      continue;
    }
    const entries=Object.entries(combos);
    const withCandidates=entries.filter(([,n])=>n>0).length;
    const total=entries.reduce((a,[,n])=>a+n,0);
    tbody.innerHTML+=`<tr><td>${sym}</td><td>${withCandidates} / ${entries.length}</td><td>${total}</td></tr>`;
  }
}

async function algoLabPreview(){
  const syms=alSelectedSymbols();
  if(!syms.length){document.getElementById('al-msg').textContent='Select at least one symbol.';return;}
  document.getElementById('al-msg').textContent='Previewing…';
  _enterBusy();
  try{
    const r=await fetch('/api/algo-lab/preview',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:syms})});
    const d=await r.json();
    document.getElementById('al-preview-wrap').style.display='';
    _renderComboMap('al-preview-tbody',d.per_symbol);
    document.getElementById('al-msg').textContent=
      `Estimated ${d.total_estimate} commands across ${d.combos_used} combos (dry-run, nothing submitted).`;
  }catch(e){
    document.getElementById('al-msg').textContent='Preview failed: '+e;
  }finally{_exitBusy();}
}

async function algoLabSubmit(){
  const syms=alSelectedSymbols();
  if(!syms.length){document.getElementById('al-msg').textContent='Select at least one symbol.';return;}
  if(!confirm(`Submit paper trades for ${syms.join(', ')} across the configured param grid? `+
              'This inserts real PENDING orders that trader/broker.py will submit to the IB paper gateway.'))return;
  document.getElementById('al-msg').textContent='Submitting…';
  _enterBusy();
  try{
    const r=await fetch('/api/algo-lab/submit',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:syms})});
    const d=await r.json();
    document.getElementById('al-preview-wrap').style.display='';
    _renderComboMap('al-preview-tbody',d.per_symbol);
    document.getElementById('al-msg').textContent=
      `Submitted ${d.total_submitted} commands${d.capped?' (capped by max_commands_per_submit)':''}.`;
    loadAlgoPnl();
  }catch(e){
    document.getElementById('al-msg').textContent='Submit failed: '+e;
  }finally{_exitBusy();}
}

function _fmtPct(x){return (x*100).toFixed(1)+'%';}
function _fmtMoney(x){
  if(x===Infinity)return '∞';
  return (x<0?'-$':'$')+Math.abs(x).toFixed(2);
}

async function loadAlgoPnl(){
  const from=document.getElementById('al-pnl-from').value;
  const to=document.getElementById('al-pnl-to').value;
  const qs=new URLSearchParams();
  if(from)qs.set('date_from',from);
  if(to)qs.set('date_to',to);
  _enterBusy();
  try{
    const d=await (await fetch('/api/algo-lab/pnl?'+qs.toString())).json();
    const sTbody=document.getElementById('al-summary-tbody');
    sTbody.innerHTML=d.summary.map(s=>`<tr>
      <td>${s.symbol}</td><td>${s.source}</td><td>${s.n_trades}</td>
      <td>${_fmtPct(s.win_rate)}</td><td>${s.total_pnl_points.toFixed(2)}</td>
      <td class="${s.total_pnl_dollars>=0?'text-success':'text-danger'}">${_fmtMoney(s.total_pnl_dollars)}</td>
    </tr>`).join('')||'<tr><td colspan="6" class="text-muted">No completed trades yet.</td></tr>';

    const bTbody=document.getElementById('al-breakdown-tbody');
    bTbody.innerHTML=d.breakdown.map(g=>`<tr>
      <td>${g.symbol}</td><td>${g.source}</td><td>${g.algo_type||'—'}</td>
      <td><code style="font-size:.7rem">${g.params?JSON.stringify(g.params):'—'}</code></td>
      <td>${g.line_detect_algo||'—'}</td>
      <td>${g.n_trades}</td><td>${_fmtPct(g.win_rate)}</td>
      <td>${g.total_pnl_points.toFixed(2)}</td>
      <td class="${g.total_pnl_dollars>=0?'text-success':'text-danger'}">${_fmtMoney(g.total_pnl_dollars)}</td>
      <td>${g.profit_factor===Infinity?'∞':g.profit_factor.toFixed(2)}</td>
    </tr>`).join('')||'<tr><td colspan="10" class="text-muted">No completed trades yet.</td></tr>';
  }catch(e){}finally{_exitBusy();}
}

document.getElementById('btn-algolab-grid-tab').addEventListener('click',algoLabLoadConfig);
document.getElementById('btn-algolab-pnl-tab').addEventListener('click',loadAlgoPnl);

// ── Correlation ───────────────────────────────────────────────────────────────
async function loadCorrMatrix(){
  const window_=document.getElementById('corr-window').value;
  _enterBusy();
  try{
    const d=await (await fetch('/api/correlation/matrix?window='+window_)).json();
    const missEl=document.getElementById('corr-missing');
    if(d.missing.length){
      missEl.style.display='';
      missEl.textContent='No bars.db data for: '+d.missing.join(', ');
    }else{
      missEl.style.display='none';
    }
    const syms=d.symbols;
    const z=syms.map(a=>syms.map(b=>d.matrix[a][b]));
    Plotly.newPlot('corr-heatmap',[{
      z:z,x:syms,y:syms,type:'heatmap',zmin:-1,zmax:1,
      colorscale:[[0,'#d62728'],[0.5,'#1a1a2e'],[1,'#2ca02c']],
      text:z.map(row=>row.map(v=>v==null?'':v.toFixed(2))),texttemplate:'%{text}',
      hovertemplate:'%{x} vs %{y}: %{z:.3f}<extra></extra>'
    }],{
      paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',font:{color:'#ccc'},
      margin:{l:50,r:20,t:20,b:40}
    },{displayModeBar:false,responsive:true});
  }catch(e){}finally{_exitBusy();}
}

async function loadCorrSeries(){
  const a=document.getElementById('corr-sym-a').value;
  const b=document.getElementById('corr-sym-b').value;
  const window_=document.getElementById('corr-window').value;
  _enterBusy();
  try{
    const d=await (await fetch(`/api/correlation/timeseries?a=${a}&b=${b}&window=${window_}`)).json();
    if(!d.series.length){
      document.getElementById('corr-series-chart').innerHTML=
        '<div class="text-muted small p-3">Not enough overlapping bars.db history for this pair/window.</div>';
      return;
    }
    Plotly.newPlot('corr-series-chart',[{
      x:d.series.map(p=>p.ts),y:d.series.map(p=>p.corr),
      type:'scatter',mode:'lines',line:{color:'#4e79a7'},name:`${a} vs ${b}`
    }],{
      paper_bgcolor:'#1a1a2e',plot_bgcolor:'#1a1a2e',font:{color:'#ccc'},
      yaxis:{range:[-1,1],title:'correlation'},margin:{l:45,r:20,t:20,b:40}
    },{displayModeBar:false,responsive:true});
  }catch(e){}finally{_exitBusy();}
}

document.getElementById('btn-correlation-tab').addEventListener('click',function(){
  loadCorrMatrix();
  loadCorrSeries();
});

// ── Overview ──────────────────────────────────────────────────────────────────
async function loadOverview(){
  _enterBusy();
  try{
    const s=await (await fetch('/api/session/status')).json();
    document.getElementById('ov-session').textContent=`${s.broker} / ${s.decider}`;
    const up=s.uptime_seconds||0;
    document.getElementById('ov-uptime').textContent=
      up>0?`${Math.floor(up/3600)}h ${Math.floor(up%3600/60)}m`:'—';
  }catch(e){}
  try{
    const d=await (await fetch('/api/algo-lab/pnl')).json();
    const rows=d.summary||[];
    const tbody=document.getElementById('ov-summary-tbody');
    tbody.innerHTML=rows.map(r=>`<tr>
      <td>${r.symbol}</td><td>${r.source}</td><td>${r.n_trades}</td>
      <td>${(r.win_rate*100).toFixed(1)}%</td>
      <td class="${r.total_pnl_dollars>=0?'text-success':'text-danger'}">${_fmtMoney(r.total_pnl_dollars)}</td>
    </tr>`).join('')||'<tr><td colspan="5" class="text-muted small">No completed trades yet.</td></tr>';
    const totalTrades=rows.reduce((a,r)=>a+r.n_trades,0);
    const totalPnl=rows.reduce((a,r)=>a+r.total_pnl_dollars,0);
    document.getElementById('ov-trades').textContent=totalTrades;
    const pnlEl=document.getElementById('ov-pnl');
    pnlEl.textContent=_fmtMoney(totalPnl);
    pnlEl.className='gl-stat-v '+(totalPnl>=0?'text-success':'text-danger');
  }catch(e){}finally{_exitBusy();}
}
document.getElementById('btn-overview-tab').addEventListener('click',loadOverview);

// ── Rail navigation (groups) ─────────────────────────────────────────────────
const GROUP_LABELS={overview:'Overview',levels:'Levels',explore:'Explore',
  charts:'Charts',algolab:'Algo Lab',trading:'Trading'};

function setActiveRailGroup(group){
  document.querySelectorAll('.rail-item').forEach(b=>b.classList.toggle('active',b.dataset.group===group));
}

function updateDateRangeVisibility(){
  const activeTarget=document.querySelector('#mainTab .nav-link.active')?.dataset?.bsTarget;
  const show=(activeTarget==='#tab-lines'||activeTarget==='#tab-graph');
  document.getElementById('date-range-wrap').classList.toggle('gl-force-hidden',!show);
}

function showGroupTabs(group,autoClickFirst){
  document.querySelectorAll('#mainTab li[data-group]').forEach(li=>{
    li.classList.toggle('gl-hidden',li.dataset.group!==group);
  });
  const cap=document.getElementById('rail-group-caption');
  if(cap)cap.textContent=GROUP_LABELS[group]||group;
  if(autoClickFirst){
    const firstBtn=document.querySelector(`#mainTab li[data-group="${group}"] .top-tab`);
    if(firstBtn && !firstBtn.classList.contains('active'))firstBtn.click();
  }
  updateDateRangeVisibility();
}

// Programmatic navigation (Overview quick-links, cross-group redirects like
// sendManualLines() -> Trades): switches the rail group AND clicks the right
// tab button by its target pane id.
function selectGroupTab(groupId,paneId){
  setActiveRailGroup(groupId);
  showGroupTabs(groupId,false);
  const btn=document.querySelector(`#mainTab [data-bs-target="#${paneId}"]`);
  if(btn)btn.click();
}

document.querySelectorAll('.rail-item[data-group]').forEach(b=>{
  b.addEventListener('click',function(){
    setActiveRailGroup(b.dataset.group);
    showGroupTabs(b.dataset.group,true);
  });
});

// Single source of truth for "which tab is active now", regardless of how it
// got activated (rail click, direct top-tab click, tab-switch guard redirect,
// or a programmatic .click() from elsewhere) -- Bootstrap fires this on the
// tab trigger itself whenever its pane becomes the shown one.
document.addEventListener('shown.bs.tab',function(e){
  const li=e.target.closest('li[data-group]');
  if(!li)return;
  const group=li.dataset.group;
  showGroupTabs(group,false);
  setActiveRailGroup(group);
});

// ── Init ──────────────────────────────────────────────────────────────────────
(function(){
  setActiveRailGroup('overview');
  showGroupTabs('overview',false);
  loadOverview();
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
    ("v5.00", "Full navigation rebuild -- left rail + contextual top tabs + unified filter bar "
              "+ busy strip, replacing 12 flat top-level tabs",
              "Requested redesign: header (status/prices/session), a left rail of 6 groups "
              "(Overview/Levels/Explore/Charts/Algo Lab/Trading), and a top tab strip that shows "
              "only the active group's tabs -- validated first as a static no-op mockup (separate "
              "artifact), approved, then implemented for real with zero changes to any existing "
              "load/render function: every original tab-pane id, button id, and lazy-load "
              "addEventListener hook is untouched, Bootstrap's native data-bs-toggle=\"tab\" "
              "mechanism still does all pane-switching -- the rail only shows/hides which subset "
              "of #mainTab's <li> elements is visible and, on rail click, .click()s the first "
              "visible tab's button. A single delegated shown.bs.tab listener re-syncs the rail's "
              "active state and visible tab subset no matter how a pane was activated (rail click, "
              "direct top-tab click, the Graph draw-mode unsaved-changes guard, or "
              "sendManualLines()'s programmatic redirect to Trades) -- so no per-call-site changes "
              "were needed there either. New: a real Overview landing tab (session status + P&L-"
              "by-source rollup via the existing /api/algo-lab/pnl + quick links), and Algo Lab "
              "split into two tabs (Grid & Submit / P&L Breakdown) instead of one stacked pane. "
              "The old full-screen hourglass overlay (#busy-overlay, a flipping hourglass emoji "
              "dimming the whole page) is replaced by a slim amber strip under the header, wired "
              "into the exact same global _enterBusy()/_exitBusy() counters every fetch call "
              "already used -- so it lights up everywhere automatically. The global 1D/1W/2W/"
              "Custom date-range control (only ever consumed by Lines and Graph, per "
              "onDateRangeChange()) moved from always-visible in the header to the tabstrip row, "
              "shown only when one of those two tabs is active. Applied a consistent .filterbar "
              "visual treatment (background/border/padding) to the simple single-row filter views "
              "(Sup/Res Viz, Correlation, Create Trades, Test, Submitted, Algo Lab) -- Lines/Graph/"
              "Sandbox/All keep their existing bespoke multi-row control panels rather than being "
              "force-fit into a template that doesn't suit that much content."),
    ("v4.27", "Algo Lab, Sup/Res Viz, and Correlation tabs — parameterized paper-trade algo "
              "framework with P&L-by-params attribution",
              "New 'Claude-designed algo' layer on top of the existing critical-line strategies "
              "(lib/algo_engine.py: BOUNCE/BREAKOUT/DIRECTIONAL/FADE/BOTH, asymmetric TP/SL -- "
              "already built in a prior session but never wired to anything live). lib/algo_lab.py "
              "submits many (strategy x tp_ticks x sl_ticks x direction_filter x strength_max) "
              "param combos as paper trades in one batch, tagged source='algo_lab' with the exact "
              "combo stored in the new commands.algo_type/commands.params_json columns. "
              "lib/algo_pnl.py breaks P&L down by symbol/source/algo/params/originating "
              "line-detection-method (joins the existing verified_trades view + critical_lines) -- "
              "this already surfaces real findings against the live DB's 929 verified trades, e.g. "
              "random_mkt is -$109,468.75/486 trades and critical_line is -$797.50/14 trades with a "
              "0% win rate. lib/correlation_lab.py is a new read-only rolling-correlation module "
              "over bars.db (log-returns, Pearson, configurable window) -- there was no "
              "correlation-analysis code anywhere in this repo before this. Algo Lab tab: symbol "
              "picker, dry-run preview, grid submit (paper only, dedup-guarded same pattern as "
              "decider.py's 425-stale-order fix), and the P&L breakdown tables. Sup/Res Viz tab: "
              "candlestick + all critical_lines for a symbol/date overlaid and color-coded by "
              "detection method (existing SOURCE_COLORS, previously defined but never plotted "
              "anywhere), toggleable per source, hover shows the line's formula/inputs -- lets "
              "'theoretical, half-baked' S/R lines be judged against real price action. "
              "Correlation tab: 4x4 heatmap + rolling correlation-over-time chart for any pair; "
              "MNQ is flagged 'missing' since bars.db was never backfilled for it (MES/MYM/M2K "
              "only, 11,814 bars each). Fixed two pre-existing bugs found while building this: "
              "lib/algo_engine.py hardcoded tick_size=0.25 (silently wrong for MYM/M2K), and "
              "critical_lines.source/algo_type/note/confidence were only added by this dashboard's "
              "own _ensure_columns() rather than lib/db.py's canonical schema -- folded into "
              "lib/db.py so any module can rely on them. All new lib/ modules have --self-test "
              "per repo convention; new Flask routes verified via app.test_client() against the "
              "live galao.db before this deploy. Config: new algo_lab:/correlation: sections + "
              "paths.bars in trader/config.yaml, all parameters exposed there per 'everything "
              "configurable' -- decider/broker's existing single-symbol MES flow is untouched."),
    ("v4.26", "All tab: Symbols checkboxes on the overlay chart, mirroring Pairs on the diff chart",
              "The overlay+diff panel (both short-range and Long View) only had toggle checkboxes "
              "on the lower diff chart (MES-MYM/MES-M2K/MYM-M2K). Added a matching 'Symbols:' "
              "checkbox row above the upper overlay chart (MES/MYM/M2K). Refactored both loaders "
              "(_loadOverlayAll, _loadLongOverlay) to cache per-symbol series into _allOverlayCache "
              "instead of building+plotting traces inline, and extracted the actual render into a "
              "new _plotAllOverlay(), matching the existing _plotAllDiff() pattern. Unchecking a "
              "symbol removes its trace and redraws — since neither chart sets an explicit "
              "yaxis.range, Plotly's autorange recomputes from only the remaining checked traces' "
              "valid (non-gap) points within the current zoom window, so the visible line(s) get a "
              "taller/more sensitive Y span automatically, same mechanism the v4.24 de-mean fix "
              "relied on."),
    ("v4.25", "Long View diff panel now reads bars_30m_diffs_normalized directly at 30m res",
              "Follow-up to the new bars.db sanity-checked normalized/diff tables: /api/bars-long's "
              "pair mode, at native 30m resolution, now serves close_norm_a/close_norm_b/diff_norm "
              "straight from bars_30m_diffs_normalized instead of recomputing normalize-to-first-bar "
              "+ diff on every request. _loadLongOverlay() fetches this per-pair via the pair= query "
              "and feeds it straight into the existing _plotAllDiff() de-mean/±2σ renderer (unit "
              "label becomes 'norm' instead of '%'). 1h/4h/1d have no precomputed table at that "
              "granularity, so they're untouched — same on-the-fly %-change-from-first-bar calc as "
              "before. The top overlay price lines (all resolutions) are also untouched: normalizing "
              "them to the new fixed full-year 0-1 basis instead of %-change-in-visible-range would "
              "make short windows look flat, so that view stays range-relative by design."),
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
    ("v4.24", "All tab: diff lines de-meaned to zero; zoom-sync now covers Long View too",
              "Follow-up on the year-view feedback: MES−MYM/MES−M2K/MYM−M2K each now subtract "
              "their own mean over the loaded window, so all 3 pairs sit around zero regardless "
              "of absolute drift level (e.g. a pair that drifted -14% over a year no longer "
              "visually dwarfs one that stayed near 0). The Y-axis is not explicitly changed — "
              "removing the mean shrinks the actual data range, so Plotly's existing autorange "
              "is naturally far more sensitive without needing separate axis logic. The ±2σ band "
              "is the residual wobble around that zero line. Trade-off, stated plainly: this "
              "hides the absolute drift direction/magnitude a pair may have (that's what was "
              "asked for — the earlier alternative of per-pair Y-axes, which would have kept "
              "drift visible, was not what got picked). "
              "Also extracted the zoom-mirror between the overlay and diff charts into a shared "
              "_wireAllZoomMirror() and wired it into Long View (Month+) too — previously only "
              "Reset Zoom was unified across both view modes (v4.22); drag-to-zoom sync between "
              "upper/lower panels was still short-range-only until now."),
    ("v4.23", "broker.py: naked-position reconciliation on startup; fix commands stuck forever",
              "2026-07-20 incident follow-up. Broker/decider were externally terminated (not a "
              "code crash) while a large order-rejection storm was in flight; on restart, two "
              "existing positions (M2K, MNQ, filled days earlier) had zero resting protective "
              "orders — nothing in the codebase ever re-checks an already-FILLED command's "
              "position against IB reality, only fresh fills get TP/SL. Emergency-protected "
              "manually in the moment (mistakenly using Position.avgCost, which is multiplier-"
              "scaled for futures, as a raw price — turned two intended resting stops into "
              "instant-fill market orders; ~-$782 realized on paper, not real money). "
              "Two real fixes: (1) new reconcile_naked_positions(), called once at broker "
              "startup — any symbol with a non-zero IB position and no resting order gets an "
              "emergency stop, correctly priced via get_price() (fresh real quoted price) never "
              "avgCost. Verified against the live paper account: MES/MNQ prices come back sane "
              "and real-scale; MYM fails contract resolution (pre-existing CME-only hardcoding, "
              "MYM is CBOT — function catches this and skips rather than crashing). "
              "(2) poll_fills() was silently `continue`-ing forever on any SUBMITTED command "
              "whose ib_order_id had aged out of IB's trades() cache — 96 commands were stuck "
              "with zero visibility this way, some 18 days old. Now flags anything unmatched "
              "past 10 minutes as RECONCILE_REQUIRED instead of ignoring it indefinitely."),
    ("v4.22", "All tab: Reset Zoom button for the overlay + diff charts",
              "One button resets both charts to full-data autorange — explicit on both divs "
              "rather than relying on the zoom-sync mirror between them, since an autorange "
              "relayout doesn't carry the same range keys the mirror listens for, and Long View "
              "(Month+) charts aren't zoom-synced at all."),
    ("v4.21", "All tab: Month+ now uses the exact same overlay+diff chart as Week, just longer",
              "Feedback after v4.20: Week looked right (one overlay chart, 3 colored lines) but "
              "Month/2mo/6mo/Year still showed the original 3-separate-charts-plus-3-pair-charts "
              "Long View layout. Removed that 6-chart grid entirely — Month+ now feeds the *same* "
              "#chart-all-overlay / #chart-all-diff divs and the same pairwise-diff checkboxes/±2σ "
              "band code as Day/Week, just sourced from bars.db instead of tick-CSV. Normalization "
              "switches from ticks-from-open (fine for a day/week) to % change from the first bar "
              "(sane over months/years — a year's worth of tick-count would be unreadable). Weekend "
              "gaps are still hidden via rangebreaks; the intraday RTH-hours breaks are not, since "
              "Long View bars aren't RTH-restricted and hiding those hours would carve out real "
              "overnight data. Note: zoom-triggered auto-resolution-refine (built for the tick-CSV "
              "path) doesn't extend to this bars.db path yet — zooming Month+ charts just zooms."),
    ("v4.20", "All tab: unified Day/Week/Month/2mo/6mo/Year range presets",
              "Incorporates Long View (1-year 30-min bars) added by the Fetcher2026 session: "
              "scripts/backfill_bars.py, trader/data/bars.db (35k+ bars, MES/MYM/M2K), and "
              "GET /api/bars-long (symbol or pair, resampled via pandas). "
              "Replaced two disconnected range controls — the old 'Days: 1-10' input (tick-CSV, "
              "capped at 10 days since that's roughly all the tick data covers) and Long View's own "
              "separate day/resolution buttons — with one Day/Week/Month/2mo/6mo/Year preset row. "
              "Day/Week route to the existing tick-CSV overlay+diff-panel path; Month+ route to "
              "Long View/bars.db (tick data doesn't go back that far — Fetcher now only keeps a "
              "rolling ~23.5h window), each with a sensible default resolution (Month→1h, 2mo→4h, "
              "6mo/Year→1d) that the resolution buttons can still override. "
              "Fixed two integration bugs the merge would otherwise have hit: setAllInterval/"
              "_syncAllIntervalBtn were matching *any* .btn-group-sm in the tab, so clicking an "
              "interval button would have wrongly toggled 'active' on the new preset and Long View "
              "resolution buttons too — scoped to a dedicated #all-interval-group. And the All tab's "
              "click handler had two independent listeners (one loading the short-range charts, one "
              "loading Long View) that would have both fired on every click regardless of which "
              "section was visible — consolidated into one that re-invokes the active preset. "
              "Note: Long View's twin (/api/bars route) was also added to trader/visualizer/app.py, "
              "the legacy port-5001 file CLAUDE_STATE.md says never to run — left untouched, likely "
              "stray work from the same session before it targeted the right file."),
    ("v4.19", "Fix: 🔗 menu click did nothing — Bootstrap dropdown was rendering invisible",
              "v4.18 fixed the icon being clipped off-screen by giving #top-bar overflow-x:auto — "
              "but that same fix's overflow-y:hidden clips a Bootstrap dropdown-menu, which uses "
              "position:absolute and pops open *below* the 40px bar. The click was registering fine, "
              "the popup was just invisible. Replaced with a small custom toggle: position:fixed "
              "(escapes any ancestor's overflow clipping entirely, regardless of DOM nesting) with "
              "coordinates computed from the button's own getBoundingClientRect() at click time, "
              "plus a click-outside-to-close handler. Same approach already used successfully on "
              "Fetcher2026 and GevaExtract's menus."),
    ("v4.18", "Fix: top bar was overflowing and clipping controls (incl. the 🔗 menu) off-screen",
              "#top-bar had overflow:hidden with a growing number of flex-shrink:0 groups packed "
              "into one 40px row (7 tabs + 🔗 menu, date-range controls, session status/Start-Stop, "
              "4 price chips, version badge) — on anything but a wide window, later items were "
              "silently clipped past the edge rather than wrapping or scrolling, which is why the "
              "🔗 menu added in v4.11 became invisible/unreachable. Changed to overflow-x:auto so "
              "the whole bar scrolls horizontally instead of clipping — everything is reachable "
              "again, just may need a scroll on narrow windows."),
    ("v4.17", "Fixed: 425 stale MES orders from repeated session restarts; decider.py dedup guard",
              "Diagnostic test-trade run (5 MES MKT orders) exposed a real incident: decider.py's "
              "generate_commands() had zero deduplication — every run_session_start() call "
              "unconditionally inserted a fresh batch of commands for every armed line, with no "
              "check for whether an unresolved command already existed for that (line, direction, "
              "bracket) combo. Repeated session restarts while deploying today's earlier versions "
              "piled up 425 MES commands stuck at status=SUBMITTED, though IB itself showed 0 open "
              "orders (the DB was just never reconciled) — reconciled all 425 to CANCELLED. "
              "generate_commands() now skips any (critical_line_id, direction, bracket_size) combo "
              "that already has a PENDING/SUBMITTING/SUBMITTED command in flight; self-test extended "
              "to call it twice and assert the second call adds nothing. "
              "Also found and fixed a genuine race condition in lib/db.py: broker.py and decider.py "
              "both call init_db() on startup, and session.py launches them nearly simultaneously — "
              "their concurrent verified_trades view check-drop-create could collide "
              "('already exists' / 'no such view'), crashing broker on startup (caught live, "
              "auto-restarted by session.py's own crash recovery, then hardened and stress-tested "
              "with 120 concurrent init_db() calls after the real fix)."),
    ("v4.16", "Overlay mode: multi-day span (up to 10 trading days) with a Days control",
              "/api/history/<symbol> gained a days= param (1-10): walks backward from the anchor "
              "date collecting up to that many trading days with real RTH data (same missing-day "
              "fallback as before when days=1, so single-day callers are unaffected), concatenates "
              "their ticks, and returns a date_range [oldest, newest] alongside the existing date "
              "field. Overlay mode gets a new 'Days' number input (1-10, only visible in overlay "
              "mode) driving this. Both overlay charts (price + diff panel) add Plotly rangebreaks "
              "to hide weekends and non-RTH hours once days>1, so a multi-day view isn't mostly "
              "blank gaps between sessions. Left/right day navigation (D◀/D▶) already slides the "
              "anchor date by one trading day, so it doubles as sliding the whole multi-day window "
              "— no separate control needed. The day-info label shows the actual resolved range "
              "(e.g. missing days get silently skipped, so 5 requested days can span more than 5 "
              "calendar days) rather than an assumed one."),
    ("v4.15", "All tab: MNQ removed, new pairwise-diff opportunity panel below the overlay",
              "MNQ dropped from the All tab entirely (both overlay and per-symbol grid) — its "
              "tick-delta dwarfed MES/MYM/M2K on the shared y-axis, squashing the other 3 into a "
              "flat band. Removing it also freed the y-axis to auto-rescale to the remaining 3 "
              "(no fixed range was ever set, so this needed no extra code). "
              "New lower panel (50/50 split under the price overlay): pairwise diff lines for all "
              "3 pairs (MES−MYM, MES−M2K, MYM−M2K, in tick units), each with a checkbox to toggle "
              "(all on by default) plus a dotted ±2σ band computed once over the whole day's diff "
              "values per pair (a flat 'what's normal today' reference, not a rolling band). "
              "The two panels are separate Plotly figures (not subplots) with their zoom ranges "
              "kept in sync — dragging to zoom either one mirrors the same x-range to the other, "
              "and (with Auto on) triggers the existing bar-resolution auto-refine on both."),
    ("v4.14", "All/Overlay: 30s interval, hourglass while loading, auto bar-resolution on zoom",
              "All tab's overlay mode had no busy indicator at all — loading could take a while "
              "with no feedback, which is why it looked 'empty' rather than 'still loading'. "
              "Now loadAllSymbols()/_loadOverlayAll() wrap the existing _enterBusy()/_exitBusy() "
              "hourglass + input-disable around every load, in both overlay and per-symbol grid "
              "modes. Added a 30s interval button (the backend already supported sub-minute "
              "intervals via a float param — just no UI for it). New Auto mode (on by default, "
              "toggle via the ⚡ Auto button): zooming the overlay chart now auto-refines to a "
              "finer bar interval sized to the visible window (debounced 400ms) so zooming in "
              "reveals more bars instead of stretching the same few — ladder is 30m/15m/5m/1m/30s "
              "keyed to window width in seconds. Turn Auto off to pick the interval manually via "
              "the existing buttons instead."),
    ("v4.13", "Sandbox: thinner bars (small bargap instead of touching)",
              "bargap 0 -> 0.06 on the Sandbox price-level chart, shaving a small gap between "
              "bars that were previously touching edge-to-edge. Note: Plotly bar width is gap-"
              "fraction based, not literal pixels, so the exact px change varies with zoom/price "
              "range — this is an approximation, adjust further if it's not enough/too much."),
    ("v4.12", "Noticeable hourglass overlay during busy operations",
              "Busy state (build DB, analyze all, etc.) previously only changed the cursor to "
              "'wait' and dimmed buttons — easy to miss. Added a full-screen dark overlay with a "
              "large flipping hourglass (⏳) and a 'Working…' label, shown/hidden via the existing "
              "body.busy-wait class so no call-site changes were needed."),
    ("v4.11", "Session manager — Start/Stop broker+decider from the dashboard; cross-dashboard menu",
              "New trader/session.py: supervises broker.py + decider.py as managed subprocesses, "
              "streams their stdout to trader/logs/{broker,decider}_stdout.log, restarts on crash "
              "with backoff (max 5 attempts), and shuts both down cleanly via the SESSION=SHUTDOWN "
              "flag they already poll for. A PID-lock file stops a second supervisor (e.g. a "
              "standalone CLI run) from double-launching them against the same DB. "
              "Dashboard: Start/Stop Session button + live Broker/Decider status badges in the top "
              "bar, backed by GET/POST /api/session/{status,start,stop}. "
              "Fixed a real bug found while wiring this up: SessionManager was resolving the wrong "
              "config.yaml (back-trading/config.yaml instead of trader/config.yaml) when imported "
              "into the dashboard process, because lib.config_loader picks a config near the "
              "*launching* script and caches it globally on first use — session.py's own module-level "
              "logger setup was triggering that ambient lookup before SessionManager could ask for "
              "the right file explicitly. Now loads trader/config.yaml by its own file location, "
              "unaffected by whichever script imports it. "
              "Also added a small 🔗 menu (icon-only, not a new tab — trimmed tab padding too) "
              "linking to Fetcher2026 (:5050) and GevaExtract (:5005) alongside this dashboard, "
              "using the current page's hostname so it works from localhost, LAN, or VPN."),
    ("v4.10", "CL Algo: Monte Carlo N≥30 guard on learner convergence; scheduler script honest errors",
              "cl_algo_learner.py no longer declares CONVERGED off a fingerprint-stable top combo "
              "alone — the top combo must also have ≥30 fills on all 3 most recent scoring runs "
              "(cl_algo_score_history gained a top_n_fills column). Below that it holds at 'narrowing' "
              "with a reasoning note explaining why. Also fixed scripts/install_scheduler.ps1, which "
              "previously printed \"OK\" for Task Scheduler / firewall registration even when it silently "
              "failed in a non-elevated shell; it now verifies the task actually registered and reports "
              "FAILED honestly."),
    ("v4.09", "Price profile module, DB scheduler updates, Task Scheduler install script", None),
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
