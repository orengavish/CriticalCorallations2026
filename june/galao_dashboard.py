#!/usr/bin/env python3
"""
galao_dashboard.py — Combined Galao Fetcher + Trader browser dashboard.
  python galao_dashboard.py           # mock fetcher, real trader DB
  python galao_dashboard.py --real    # real fetcher + real trader DB
  python galao_dashboard.py --port 5050

Network: http://192.168.1.132:5050 (from any home-network device)
"""

import argparse, random, socket, sqlite3, subprocess, sys, threading, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
try:
    from statistics import median
except ImportError:
    def median(lst): s=sorted(lst); n=len(s); return (s[n//2-1]+s[n//2])/2 if n%2==0 else s[n//2]

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from flask import Flask, jsonify, request
app = Flask(__name__)

SYMBOLS     = ["MES","MNQ","MYM","M2K"]
DTYPES      = ["TRADES","BID_ASK"]
FETCH_START = date(2026, 6, 16)
HOLIDAYS    = {date(2026,1,1),date(2026,1,19),date(2026,2,16),date(2026,4,3),
               date(2026,5,25),date(2026,7,3),date(2026,9,7),date(2026,11,26),date(2026,12,25)}

_mock_mode   = True
_mock_state  = {}
_mock_gw     = {"status":"down"}
_lock        = threading.Lock()
_gw_up_since = None

# ── Process handles (broker + decider) ───────────────────────────────────────
_procs = {"broker": None, "decider": None}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _working_days(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in HOLIDAYS: out.append(d)
        d += timedelta(days=1)
    return out

def _gw_port():
    try:
        from lib.config_loader import get_config
        return get_config().ib.live_port
    except Exception: return 4002

def _gw_is_up(host="127.0.0.1", port=None, timeout=1.5):
    port = port or _gw_port()
    try:
        with socket.create_connection((host, port), timeout=timeout): return True
    except OSError: return False

def _cfg():
    from lib.config_loader import get_config
    return get_config()

def _history_dir():
    try: return Path(_cfg().paths.history)
    except Exception: return _ROOT/"trader"/"data"/"history"

def _galao_db_path():
    try: return Path(_cfg().paths.db)
    except Exception: return _ROOT/"trader"/"data"/"galao.db"

def _fetch_db_path():
    try: return _galao_db_path().parent/"fetch_progress.db"
    except Exception: return _ROOT/"trader"/"data"/"fetch_progress.db"

# ── Mock ──────────────────────────────────────────────────────────────────────

_MOCK_TARGETS = {"MES":62000,"MNQ":30000,"MYM":8500,"M2K":7000}

def _init_mock():
    days = _working_days(FETCH_START, date.today())
    rng  = random.Random(42)
    with _lock:
        for i,d in enumerate(days):
            ds = d.isoformat()
            for sym in SYMBOLS:
                tgt = _MOCK_TARGETS[sym]
                for dtype in DTYPES:
                    key = (sym,ds,dtype)
                    done_p = max(0.0, 0.88 - i*0.07)
                    if rng.random() < done_p:
                        cnt = int(tgt*rng.uniform(0.85,1.15))
                        ver = rng.choice(["pass","pass","pass","warn",None])
                        _mock_state[key] = {"status":"done","count":cnt,"target":tgt,"verify":ver,"notes":[]}
                    else:
                        _mock_state[key] = {"status":"missing","count":0,"target":tgt,"verify":None,"notes":[]}

def _mock_run_fetch(sym, ds, dtype):
    key = (sym,ds,dtype)
    target = int(_MOCK_TARGETS.get(sym,40000)*random.uniform(0.85,1.15))
    with _lock:
        _mock_state[key] = {"status":"in_progress","count":0,"target":target,"verify":None,"notes":[]}
    def _sim():
        steps, done = 20, 0
        for _ in range(steps):
            time.sleep(0.35)
            done = min(done + target//steps + random.randint(-300,300), target)
            with _lock:
                if _mock_state.get(key,{}).get("status")=="in_progress":
                    _mock_state[key]["count"] = done
        time.sleep(0.15)
        with _lock:
            _mock_state[key] = {"status":"done","count":target,"target":target,"verify":"pass","notes":[]}
    threading.Thread(target=_sim, daemon=True).start()

def _mock_gw_start():
    with _lock: _mock_gw["status"] = "starting"
    def _s():
        global _gw_up_since
        time.sleep(5)
        with _lock: _mock_gw["status"] = "up"
        _gw_up_since = datetime.now(timezone.utc)
    threading.Thread(target=_s, daemon=True).start()

def _mock_gw_stop():
    global _gw_up_since
    with _lock: _mock_gw["status"] = "down"
    _gw_up_since = None

# ── Fetch DB ──────────────────────────────────────────────────────────────────

def _load_fetch_rows():
    path = _fetch_db_path()
    if not path.exists(): return {}
    rows = {}
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT symbol,date,data_type,records_fetched,finished,"
                               "verify_status,verify_notes FROM fetch_progress")
        except sqlite3.OperationalError:
            cur = conn.execute("SELECT symbol,date,data_type,records_fetched,finished FROM fetch_progress")
        for r in cur.fetchall():
            rows[(r["symbol"],r["date"],r["data_type"])] = r
        conn.close()
    except Exception: pass
    return rows

def _csv_exists(sym, ds, dtype):
    ftype = "trades" if dtype=="TRADES" else "bidask"
    p = _history_dir()/f"{sym}_{ftype}_{ds.replace('-','')}.csv"
    return p.exists() and p.stat().st_size > 500

def _estimate_targets(db_rows):
    buckets = {}
    for (sym,ds,dtype),r in db_rows.items():
        cnt = int(r["records_fetched"] or 0)
        if r["finished"] and cnt>1000:
            buckets.setdefault((sym,dtype),[]).append(cnt)
    return {f"{sym}_{dtype}": int(median(v)) for (sym,dtype),v in buckets.items() if v}

def _get_real_fetch_state():
    db  = _load_fetch_rows()
    est = _estimate_targets(db)
    state = {}
    for d in _working_days(FETCH_START, date.today()):
        ds = d.isoformat()
        for sym in SYMBOLS:
            for dtype in DTYPES:
                r    = db.get((sym,ds,dtype))
                on_d = _csv_exists(sym,ds,dtype)
                cnt  = int(r["records_fetched"] or 0) if r else 0
                vs   = (r["verify_status"] if r and "verify_status" in r.keys() else None)
                vn   = (r["verify_notes"]  if r and "verify_notes"  in r.keys() else "") or ""
                notes= [n for n in vn.split(" | ") if n]
                tgt  = est.get(f"{sym}_{dtype}")
                if on_d or (r and r["finished"]):
                    state[(sym,ds,dtype)] = {"status":"done","count":cnt,"target":tgt,"verify":vs,"notes":notes}
                elif r and not r["finished"]:
                    state[(sym,ds,dtype)] = {"status":"in_progress","count":cnt,"target":tgt,"verify":None,"notes":[]}
                else:
                    state[(sym,ds,dtype)] = {"status":"missing","count":0,"target":tgt,"verify":None,"notes":[]}
    return state, est

# ── Trader DB ─────────────────────────────────────────────────────────────────

def _galao_conn():
    path = _galao_db_path()
    if not path.exists(): return None
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "commands" not in tables:
            conn.close()
            return None
        return conn
    except Exception: return None

def _get_trader_status():
    conn = _galao_conn()
    if conn is None:
        return {"error": "galao.db not ready — start broker once to initialize"}

    try:
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status='OPEN' ORDER BY entry_time DESC").fetchall()]

        recent = [dict(r) for r in conn.execute(
            "SELECT id,symbol,direction,entry_type,fill_price,exit_price,"
            "pnl_points,exit_reason,status,source,fill_time,exit_time "
            "FROM commands WHERE status IN ('CLOSED','FILLED','CANCELLED','ERROR') "
            "ORDER BY updated_at DESC LIMIT 8").fetchall()]

        pending_n  = conn.execute("SELECT COUNT(*) FROM commands WHERE status='PENDING'").fetchone()[0]
        submitted_n= conn.execute("SELECT COUNT(*) FROM commands WHERE status IN ('SUBMITTING','SUBMITTED')").fetchone()[0]
        total_n    = conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]

        lines = [dict(r) for r in conn.execute(
            "SELECT id,symbol,date,line_type,price,strength,armed,created_at "
            "FROM critical_lines ORDER BY armed DESC, strength ASC, date DESC LIMIT 30").fetchall()]

        price_cache = {}
        try:
            for r in conn.execute("SELECT * FROM price_cache").fetchall():
                price_cache[r["symbol"]] = dict(r)
        except Exception: pass

        tp_n = conn.execute("SELECT COUNT(*) FROM completed_trades WHERE exit_reason='TP'").fetchone()[0]
        sl_n = conn.execute("SELECT COUNT(*) FROM completed_trades WHERE exit_reason='SL'").fetchone()[0]
        pnl  = conn.execute("SELECT SUM(pnl_points) FROM completed_trades").fetchone()[0] or 0

        conn.close()
        return {
            "positions": positions, "recent": recent,
            "pending": pending_n, "submitted": submitted_n, "total_commands": total_n,
            "critical_lines": lines, "price_cache": price_cache,
            "stats": {"tp": tp_n, "sl": sl_n, "total_pnl": round(pnl,2)},
        }
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return {"error": str(e)}

def _get_combined_prices():
    """Price per symbol: price_cache (live fill) → CSV (delayed). Returns rich dict."""
    result = {}
    pc = {}
    conn = _galao_conn()
    if conn:
        try:
            for r in conn.execute("SELECT * FROM price_cache").fetchall():
                pc[r["symbol"]] = dict(r)
            conn.close()
        except Exception: pass

    try:
        from trader.verify_data import last_price
        hd = _history_dir()
    except Exception:
        last_price = None; hd = None

    now_utc = datetime.now(timezone.utc)
    for sym in SYMBOLS:
        entry = None
        if sym in pc:
            upd = pc[sym].get("updated_at","")
            try:
                upd_dt = datetime.fromisoformat(upd.replace("Z","+00:00"))
                age_h  = (now_utc - upd_dt).total_seconds() / 3600
                src    = "live fill" if age_h < 4 else "stale fill"
                cls    = "live" if age_h < 4 else "stale"
                entry  = {"price": pc[sym]["last_price"], "high": None, "low": None,
                          "time_ct": upd_dt.strftime("%H:%M UTC"), "date": upd_dt.date().isoformat(),
                          "source": src, "src_class": cls}
            except Exception: pass
        if entry is None and last_price and hd:
            csv_p = last_price(sym, hd)
            if csv_p:
                entry = {**csv_p, "source": "delayed CSV", "src_class": "delayed"}
        result[sym] = entry
    return result

# ── Process management ────────────────────────────────────────────────────────

def _proc_status(name):
    p = _procs.get(name)
    if p is None: return "stopped"
    if p.poll() is None: return "running"
    _procs[name] = None
    return "stopped"

def _start_proc(name):
    if _proc_status(name) == "running": return False, "already running"
    scripts = {"broker": "trader/broker.py", "decider": "trader/decider.py"}
    if name not in scripts: return False, "unknown proc"
    try:
        _procs[name] = subprocess.Popen(
            [sys.executable, scripts[name]], cwd=str(_ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True, "started"
    except Exception as e: return False, str(e)

def _stop_proc(name):
    p = _procs.get(name)
    if p and p.poll() is None:
        p.terminate()
        _procs[name] = None
        return True
    return False

# ── Auto-verify ───────────────────────────────────────────────────────────────

def _auto_verify_loop():
    time.sleep(15)
    while True:
        try:
            db = _load_fetch_rows(); hd = _history_dir()
            from trader.verify_data import check_and_mark
            pending = [(r["symbol"],r["date"],r["data_type"]) for r in db.values()
                       if r["finished"] and not (r["verify_status"] if "verify_status" in r.keys() else None)]
            for sym,ds,dtype in pending[:4]:
                try: check_and_mark(sym, date.fromisoformat(ds), dtype, hd)
                except Exception: pass
        except Exception: pass
        time.sleep(25)

# ── Gateway routes ────────────────────────────────────────────────────────────

@app.route("/api/gateway")
def api_gateway():
    global _gw_up_since
    if _mock_mode:
        with _lock: st = _mock_gw["status"]
        ups = int((datetime.now(timezone.utc)-_gw_up_since).total_seconds()) if _gw_up_since and st=="up" else None
        return jsonify({"status":st,"mock":True,"up_seconds":ups})
    up = _gw_is_up()
    if up and not _gw_up_since: _gw_up_since = datetime.now(timezone.utc)
    if not up: _gw_up_since = None
    ups = int((datetime.now(timezone.utc)-_gw_up_since).total_seconds()) if _gw_up_since and up else None
    return jsonify({"status":"up" if up else "down","mock":False,"port":_gw_port(),"up_seconds":ups})

@app.route("/api/gateway/start",methods=["POST"])
def api_gw_start():
    if _mock_mode: _mock_gw_start(); return jsonify({"ok":True})
    try:
        from lib.ibc_launcher import try_start_gateway
        ok = try_start_gateway(_cfg(), label="dashboard")
        return jsonify({"ok":ok})
    except Exception as e: return jsonify({"ok":False,"msg":str(e)}),500

@app.route("/api/gateway/stop",methods=["POST"])
def api_gw_stop():
    global _gw_up_since
    if _mock_mode: _mock_gw_stop(); return jsonify({"ok":True})
    try: subprocess.run(["taskkill","/F","/FI","WINDOWTITLE eq IB Gateway*"],capture_output=True,timeout=10)
    except Exception: pass
    _gw_up_since = None
    return jsonify({"ok":True})

# ── Fetch routes ──────────────────────────────────────────────────────────────

@app.route("/api/fetch/status")
def api_fetch_status():
    if _mock_mode:
        with _lock: state = {k:dict(v) for k,v in _mock_state.items()}
        est = {f"{sym}_{dt}": _MOCK_TARGETS[sym] for sym in SYMBOLS for dt in DTYPES}
    else:
        state, est = _get_real_fetch_state()

    days = list(reversed(_working_days(FETCH_START, date.today())))
    totals = {"done":0,"pass":0,"warn":0,"fail":0,"active":0,"missing":0}
    procs  = {}

    for sym in SYMBOLS:
        active = None
        done=pass_=warn=fail=miss=0
        for d in days:
            ds = d.isoformat()
            for dtype in DTYPES:
                c = state.get((sym,ds,dtype),{"status":"missing","count":0,"target":None,"verify":None})
                s=c["status"]; v=c.get("verify")
                if s=="done":
                    done+=1
                    if v=="pass": pass_+=1
                    elif v=="warn": warn+=1
                    elif v=="fail": fail+=1
                elif s=="in_progress":
                    if active is None:
                        active={"date":ds,"dtype":dtype,"count":c["count"],"target":c.get("target")}
                    done_sofar=0  # keep active from first in_progress found
                else: miss+=1
        totals["done"]+=done; totals["pass"]+=pass_; totals["warn"]+=warn
        totals["fail"]+=fail; totals["missing"]+=miss
        if active: totals["active"]+=1
        procs[sym] = {
            "active": active, "done":done, "total":len(days)*2,
            "pass":pass_, "warn":warn, "fail":fail, "missing":miss,
        }

    return jsonify({
        "mock":_mock_mode, "estimates":est, "processes":procs,
        "totals":totals, "any_active": totals["active"]>0,
    })

@app.route("/api/fetch/all",methods=["POST"])
def api_fetch_all():
    if _mock_mode:
        today=date.today()
        with _lock:
            keys=[(sym,d.isoformat(),dt) for d in _working_days(FETCH_START,today)
                  for sym in SYMBOLS for dt in DTYPES
                  if _mock_state.get((sym,d.isoformat(),dt),{}).get("status")=="missing"]
        def _stagger():
            for sym,ds,dt in keys: _mock_run_fetch(sym,ds,dt); time.sleep(0.06)
        threading.Thread(target=_stagger,daemon=True).start()
        return jsonify({"ok":True,"mock":True,"queued":len(keys)})
    for sym in SYMBOLS:
        subprocess.Popen([sys.executable,"trader/fetcher.py",
            "--symbol",sym,"--from-date",FETCH_START.isoformat(),"--bid-ask"],cwd=str(_ROOT))
    return jsonify({"ok":True,"mock":False,"processes":len(SYMBOLS)})

@app.route("/api/fetch/one",methods=["POST"])
def api_fetch_one():
    d=request.json or {}
    sym=d.get("symbol","").upper(); ds=d.get("date",""); dtype=d.get("dtype","TRADES").upper()
    if sym not in SYMBOLS: return jsonify({"error":"bad symbol"}),400
    if _mock_mode: _mock_run_fetch(sym,ds,dtype); return jsonify({"ok":True})
    cmd=[sys.executable,"trader/fetcher.py","--symbol",sym,"--date",ds]
    if dtype=="BID_ASK": cmd.append("--bid-ask")
    subprocess.Popen(cmd,cwd=str(_ROOT))
    return jsonify({"ok":True})

@app.route("/api/fetch/verify_all",methods=["POST"])
def api_verify_all():
    if _mock_mode:
        with _lock:
            for c in _mock_state.values():
                if c.get("status")=="done" and c.get("verify") is None:
                    c["verify"]=random.choice(["pass","pass","warn"])
        return jsonify({"ok":True})
    def _run():
        try:
            from trader.verify_data import check_and_mark
            db=_load_fetch_rows(); hd=_history_dir()
            for r in db.values():
                if r["finished"] and not (r["verify_status"] if "verify_status" in r.keys() else None):
                    try: check_and_mark(r["symbol"],date.fromisoformat(r["date"]),r["data_type"].lower(),hd)
                    except Exception: pass
        except Exception: pass
    threading.Thread(target=_run,daemon=True).start()
    return jsonify({"ok":True})

# ── Trader routes ─────────────────────────────────────────────────────────────

@app.route("/api/trader/status")
def api_trader_status():
    td = _get_trader_status()
    td["broker"]  = _proc_status("broker")
    td["decider"] = _proc_status("decider")
    return jsonify(td)

@app.route("/api/trader/prices")
def api_trader_prices():
    if _mock_mode:
        bases={"MES":5230,"MNQ":19800,"MYM":42100,"M2K":2105}
        return jsonify({sym:{"price":round(bases[sym]+random.uniform(-15,15),2),
            "high":round(bases[sym]+random.uniform(10,40),2),
            "low":round(bases[sym]-random.uniform(10,40),2),
            "source":"mock","src_class":"mock","time_ct":"16:45 CT","date":date.today().isoformat()}
            for sym in SYMBOLS})
    return jsonify(_get_combined_prices())

@app.route("/api/trader/start",methods=["POST"])
def api_trader_start():
    name = (request.json or {}).get("proc","broker")
    ok, msg = _start_proc(name)
    return jsonify({"ok":ok,"msg":msg,"status":_proc_status(name)})

@app.route("/api/trader/stop",methods=["POST"])
def api_trader_stop():
    name = (request.json or {}).get("proc","broker")
    ok = _stop_proc(name)
    return jsonify({"ok":ok,"status":_proc_status(name)})

@app.route("/api/trader/fire",methods=["POST"])
def api_trader_fire():
    d = request.json or {}
    sym  = d.get("symbol","MES").upper()
    dire = d.get("direction","BUY").upper()
    src  = d.get("source","manual")
    line_id = d.get("line_id")
    price   = d.get("price")

    conn = _galao_conn()
    if conn is None: return jsonify({"error":"galao.db not ready"}),500

    try:
        cfg = _cfg()
        tick    = cfg.orders.tick_size
        bracket = cfg.orders.tp_ticks * tick
        qty     = cfg.orders.quantity_per_symbol.get(sym, 1)
    except Exception:
        tick=0.25; bracket=1.0; qty=1

    def rt(p): return round(round(p/tick)*tick, 10)

    if price is None:
        # fallback: use cached price or recent CSV
        p = _get_combined_prices().get(sym)
        price = p["price"] if p else None
    if price is None: return jsonify({"error":"no price available"}),400

    ep = rt(float(price))
    if dire=="BUY":
        tp = rt(ep+bracket); sl = rt(ep-bracket)
    else:
        tp = rt(ep-bracket); sl = rt(ep+bracket)

    try:
        conn.execute("""
            INSERT INTO commands
              (symbol,line_price,line_type,line_strength,direction,entry_type,
               entry_price,tp_price,sl_price,bracket_size,source,critical_line_id,quantity,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'PENDING')""",
            (sym, ep, "SUPPORT" if dire=="BUY" else "RESISTANCE", 1,
             dire, "MKT", ep, tp, sl, bracket, src, line_id, qty))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({"ok":True,"command_id":new_id,"entry":ep,"tp":tp,"sl":sl})
    except Exception as e:
        conn.close()
        return jsonify({"error":str(e)}),500

# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Galao</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
a{color:inherit;text-decoration:none}

/* header */
header{display:flex;align-items:center;gap:10px;padding:8px 16px;border-bottom:1px solid #21262d;flex-wrap:wrap}
h1{font-size:.82rem;font-weight:700;color:#6e7681;letter-spacing:.1em}
.badge{padding:2px 8px;border-radius:999px;font-size:.64rem;font-weight:700}
.bm{background:#3d1f8c22;color:#a78bfa;border:1px solid #6d28d944}
.bl{background:#14532d22;color:#3fb950;border:1px solid #238636}
.gw{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:6px;font-size:.72rem;font-weight:600;border:1px solid transparent;transition:all .3s}
.gw-down    {background:#1c1010;border-color:#4a1c1c;color:#f85149}
.gw-starting{background:#1c1a10;border-color:#4a3b1c;color:#d29922;animation:gp 1.2s infinite}
.gw-up      {background:#0e1f14;border-color:#1a4226;color:#3fb950}
@keyframes gp{0%,100%{opacity:1}50%{opacity:.4}}
.gd{width:6px;height:6px;border-radius:50%;background:currentColor}
.gwb{padding:2px 8px;border-radius:4px;font-size:.67rem;font-weight:600;cursor:pointer;border:none;transition:all .12s}
.gwb:disabled{opacity:.3;cursor:default}
.gwb-on{background:#1a4226;color:#3fb950;border:1px solid #238636}.gwb-on:hover{background:#238636;color:#fff}
.gwb-off{background:#2d1010;color:#f85149;border:1px solid #4a1c1c}.gwb-off:hover{background:#4a1c1c;color:#fff}
.lupd{margin-left:auto;font-size:.63rem;color:#4d5566}
.rdot{width:6px;height:6px;border-radius:50%;background:#30363d;display:inline-block}
.rdot.live{background:#3fb950;animation:bl 1.4s infinite}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.25}}

/* section headers */
.sec{padding:6px 16px 2px;font-size:.68rem;font-weight:700;color:#4d5566;letter-spacing:.1em;
     border-top:1px solid #21262d;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sec-title{color:#6e7681}

/* toolbar */
.tb{display:flex;align-items:center;gap:6px;padding:4px 16px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:5px;
     font-size:.72rem;font-weight:500;cursor:pointer;border:none;transition:all .12s}
.btn:disabled{opacity:.35;cursor:default}
.btn-g{background:#238636;color:#fff}.btn-g:hover:not(:disabled){background:#2ea043}
.btn-v{background:#1f3a5f;color:#60a5fa;border:1px solid #1e40af44}.btn-v:hover:not(:disabled){background:#1e40af;color:#fff}
.btn-r{background:#5a1010;color:#f85149;border:1px solid #7d1c1c44}.btn-r:hover:not(:disabled){background:#7d1c1c;color:#fff}
.btn-x{background:transparent;color:#8b949e;border:1px solid #30363d}.btn-x:hover{background:#161b22;color:#c9d1d9}
.btn-buy{background:#14532d44;color:#3fb950;border:1px solid #238636}.btn-buy:hover{background:#238636;color:#fff}
.btn-sell{background:#3d100022;color:#f85149;border:1px solid #7d1c1c44}.btn-sell:hover{background:#7d1c1c;color:#fff}

/* cards grid */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:6px 16px}

/* fetch card */
.fcard{background:#161b22;border:1px solid #21262d;border-radius:7px;padding:9px 11px;display:flex;flex-direction:column;gap:3px}
.fcard-h{display:flex;align-items:center;justify-content:space-between}
.fsym{font-size:.77rem;font-weight:700;color:#8b949e;letter-spacing:.06em}
.fst{padding:1px 5px;border-radius:3px;font-size:.59rem;font-weight:700}
.fs-idle {background:#21262d;color:#4d5566}
.fs-fetch{background:#c2410c22;color:#f0883e;border:1px solid #c2410c44;animation:sp 1.1s infinite}
.fs-done {background:#14532d22;color:#3fb950}
@keyframes sp{0%,100%{opacity:1}50%{opacity:.45}}
.pbar{background:#0d1117;border-radius:3px;height:5px;margin:3px 0;overflow:hidden;border:1px solid #21262d}
.pbar-fill{height:100%;border-radius:3px;transition:width .6s ease;background:linear-gradient(90deg,#c2410c,#f0883e)}
.fmeta{font-size:.61rem;color:#6e7681;display:flex;justify-content:space-between}
.fspd{color:#f0883e;font-weight:600;font-family:monospace}
.fstats{margin-top:2px;font-size:.61rem;display:flex;gap:6px;flex-wrap:wrap}
.sv{color:#3fb950}.sw{color:#d29922}.sf{color:#f85149}.sm{color:#6e7681}

/* price card */
.pcard{background:#161b22;border:1px solid #21262d;border-radius:7px;padding:9px 11px}
.psym{font-size:.72rem;font-weight:700;color:#8b949e;letter-spacing:.06em;margin-bottom:3px}
.pprice{font-size:1.22rem;font-weight:700;color:#e6edf3;line-height:1}
.phl{font-size:.61rem;color:#4d5566;margin-top:2px}
.phl b{color:#6e7681}
.psrc{font-size:.58rem;margin-top:3px;padding:1px 5px;border-radius:3px;display:inline-block}
.src-live   {background:#14532d22;color:#3fb950;border:1px solid #23863633}
.src-stale  {background:#3d2f0022;color:#d29922;border:1px solid #7d5c1033}
.src-delayed{background:#21262d;color:#6e7681;border:1px solid #30363d}
.src-mock   {background:#3d1f8c22;color:#a78bfa;border:1px solid #6d28d944}

/* summary bar */
.summ{padding:2px 16px 4px;font-size:.7rem;display:flex;gap:10px;flex-wrap:wrap;color:#6e7681}

/* tables */
.tbl-wrap{padding:4px 16px}
.tbl-label{font-size:.63rem;font-weight:600;color:#4d5566;letter-spacing:.06em;margin-bottom:4px}
table{border-collapse:collapse;font-size:.69rem;width:100%}
th{padding:4px 6px;text-align:left;font-weight:600;color:#6e7681;border-bottom:1px solid #21262d;white-space:nowrap}
td{padding:3px 6px;border-bottom:1px solid #0d1117;white-space:nowrap;vertical-align:middle}
tr:hover td{background:#ffffff04}
.tag{display:inline-block;padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:700}
.tag-buy {background:#14532d22;color:#3fb950;border:1px solid #23863633}
.tag-sell{background:#3d100022;color:#f85149;border:1px solid #7d1c1c44}
.tag-tp  {background:#14532d22;color:#3fb950}
.tag-sl  {background:#3d100022;color:#f85149}
.tag-arm {background:#14532d44;color:#3fb950;border:1px solid #238636}
.tag-dis {background:#21262d;color:#4d5566}
.str1{color:#f0883e}.str2{color:#d29922}.str3{color:#6e7681}

/* process row */
.proc-row{padding:6px 16px 10px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.proc-chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:6px;
           font-size:.71rem;font-weight:600;border:1px solid transparent}
.pc-run{background:#0e1f14;border-color:#1a4226;color:#3fb950}
.pc-stp{background:#1c1010;border-color:#4a1c1c;color:#f85149}
.pc-dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.empty{padding:20px 16px;font-size:.7rem;color:#4d5566;font-style:italic}
</style>
</head>
<body>

<header>
  <h1>GALAO</h1>
  <span id="mbadge" class="badge bm">MOCK</span>
  <div id="gw-pill" class="gw gw-down"><span class="gd"></span><span id="gw-lbl">Gateway Down</span><span id="gw-up" style="font-size:.62rem;color:#4d5566;margin-left:3px"></span></div>
  <button id="gw-on"  class="gwb gwb-on"  onclick="gwStart()">Start</button>
  <button id="gw-off" class="gwb gwb-off" onclick="gwStop()" style="display:none">Stop</button>
  <span class="lupd"><span class="rdot" id="rdot"></span>&nbsp;<span id="lupd">—</span></span>
</header>

<!-- ── FETCH ──────────────────────────────────────────────────────────── -->
<div class="sec"><span class="sec-title">FETCH</span></div>

<div class="cards" id="fcards"></div>

<div class="summ" id="fsumm">Loading…</div>
<div class="tb">
  <button id="btn-fa" class="btn btn-g" onclick="fetchAll()">&#9654; Fetch All Missing</button>
  <button id="btn-va" class="btn btn-v" onclick="verifyAll()">&#10003; Verify All</button>
  <button            class="btn btn-x" onclick="refresh()">&#8635; Refresh</button>
</div>

<!-- ── TRADE ──────────────────────────────────────────────────────────── -->
<div class="sec" style="margin-top:6px"><span class="sec-title">TRADE</span><span id="trade-err" style="color:#f85149;font-size:.63rem"></span></div>

<div class="cards" id="pcards"></div>

<div class="tbl-wrap" style="margin-top:4px">
  <div class="tbl-label">CRITICAL LINES</div>
  <table><thead><tr><th>Sym</th><th>Type</th><th>Price</th><th>Str</th><th>Status</th><th>Date</th><th></th></tr></thead>
  <tbody id="lines-body"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody></table>
</div>

<div class="tbl-wrap" style="margin-top:8px">
  <div class="tbl-label">OPEN POSITIONS</div>
  <table><thead><tr><th>#</th><th>Sym</th><th>Dir</th><th>Qty</th><th>Entry</th><th>Status</th><th>Since</th></tr></thead>
  <tbody id="pos-body"><tr><td colspan="7" class="empty">—</td></tr></tbody></table>
</div>

<div class="tbl-wrap" style="margin-top:8px">
  <div class="tbl-label">RECENT TRADES</div>
  <table><thead><tr><th>#</th><th>Sym</th><th>Dir</th><th>Fill</th><th>Exit</th><th>Reason</th><th>PnL pts</th></tr></thead>
  <tbody id="trades-body"><tr><td colspan="7" class="empty">—</td></tr></tbody></table>
</div>

<div class="proc-row" id="proc-row">
  <div id="bk-chip" class="proc-chip pc-stp"><span class="pc-dot"></span>Broker: stopped</div>
  <button id="bk-start" class="btn btn-g"  onclick="startProc('broker')">Start Broker</button>
  <button id="bk-stop"  class="btn btn-r"  onclick="stopProc('broker')" style="display:none">Stop Broker</button>
  <div id="dc-chip" class="proc-chip pc-stp" style="margin-left:12px"><span class="pc-dot"></span>Decider: stopped</div>
  <button id="dc-start" class="btn btn-g"  onclick="startProc('decider')">Start Decider</button>
  <button id="dc-stop"  class="btn btn-r"  onclick="stopProc('decider')" style="display:none">Stop Decider</button>
</div>

<!-- fire dialog -->
<div id="fire-dlg" style="display:none;position:fixed;inset:0;background:#000000bb;z-index:99;display:flex;align-items:center;justify-content:center">
  <div style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;min-width:280px">
    <div style="font-size:.82rem;font-weight:700;margin-bottom:12px;color:#e6edf3">Fire Order</div>
    <div id="fire-info" style="font-size:.72rem;color:#6e7681;margin-bottom:14px"></div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-buy"  onclick="fireLine('BUY')">BUY</button>
      <button class="btn btn-sell" onclick="fireLine('SELL')">SELL</button>
      <button class="btn btn-x"    onclick="closeDlg()">Cancel</button>
    </div>
  </div>
</div>

<div style="height:40px"></div>

<script>
const SYM=['MES','MNQ','MYM','M2K'];
let _fTimer=null, _tTimer=null, _gwTimer=null, _pTimer=null;
let _fActive=false, _gwSt='down';
let _est={}, _ring={};   // per-sym rolling ring for 10s throughput
let _prev={};            // per-sym {key,count,time} for speed
let _fireLine=null;      // pending fire dialog context

SYM.forEach(s=>{ _ring[s]=[]; });

// ── utils ──

function fmtP(v){return v==null?'—':Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function fmtK(n){return n>=1000?(n/1000).toFixed(1)+'k':Math.round(n)+''}
function fmtUp(s){if(!s)return'';if(s<60)return s+'s';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h?h+'h '+m+'m':m+'m'}
function fmtTime(iso){if(!iso)return'—';try{return new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch{return iso}}

// ── gateway ──

async function pollGw(){
  try{
    const d=await(await fetch('/api/gateway')).json();
    _gwSt=d.status;
    document.getElementById('gw-pill').className='gw gw-'+_gwSt;
    document.getElementById('gw-lbl').textContent={down:'Gateway Down',starting:'Starting…',up:'Gateway Ready'}[_gwSt]||_gwSt;
    document.getElementById('gw-up').textContent=_gwSt==='up'&&d.up_seconds?'· '+fmtUp(d.up_seconds):'';
    const on=document.getElementById('gw-on'), off=document.getElementById('gw-off');
    on.style.display=_gwSt==='up'?'none':'inline-block'; on.disabled=_gwSt==='starting';
    off.style.display=_gwSt==='up'?'inline-block':'none';
    document.getElementById('btn-fa').disabled=!_isMock()&&_gwSt!=='up';
  }catch(e){}
  clearTimeout(_gwTimer);_gwTimer=setTimeout(pollGw,_gwSt==='starting'?2000:12000);
}
async function gwStart(){document.getElementById('gw-on').disabled=true;await fetch('/api/gateway/start',{method:'POST'});pollGw()}
async function gwStop(){await fetch('/api/gateway/stop',{method:'POST'});pollGw()}
function _isMock(){return document.getElementById('mbadge').textContent==='MOCK'}

// ── fetch cards ──

function buildFCards(){
  document.getElementById('fcards').innerHTML=SYM.map(s=>`
<div class="fcard">
  <div class="fcard-h"><span class="fsym">${s}</span><span class="fst fs-idle" id="fst-${s}">IDLE</span></div>
  <div id="finfo-${s}" style="font-size:.61rem;color:#6e7681;min-height:.8rem"></div>
  <div id="fpb-${s}" style="display:none">
    <div class="pbar"><div class="pbar-fill" id="ff-${s}" style="width:0%"></div></div>
    <div class="fmeta"><span id="fcnt-${s}"></span><span class="fspd" id="fspd-${s}"></span></div>
    <div style="font-size:.59rem;color:#6e7681" id="f10-${s}"></div>
  </div>
  <div class="fstats" id="fstat-${s}"></div>
</div>`).join('')
}

function updateFCard(sym, proc, est){
  const act=proc&&proc.active;
  const stEl=document.getElementById(`fst-${sym}`);
  const pbEl=document.getElementById(`fpb-${sym}`);
  const inEl=document.getElementById(`finfo-${sym}`);

  if(act){
    stEl.className='fst fs-fetch'; stEl.textContent='FETCHING';
    inEl.textContent=(act.dtype==='TRADES'?'TRADES':'BID/ASK')+' · '+act.date;
    pbEl.style.display='block';

    // speed
    const key=sym+'_'+act.date+'_'+act.dtype;
    const now=Date.now(), pr=_prev[sym];
    let spd=0;
    if(pr&&pr.key===key){const dt=(now-pr.time)/1000;if(dt>0)spd=Math.max(0,(act.count-pr.count)/dt);}
    _prev[sym]={key,count:act.count,time:now};

    // 10s ring
    _ring[sym].push({t:now,c:act.count});
    _ring[sym]=_ring[sym].filter(e=>now-e.t<10500);
    let rate10='';
    if(_ring[sym].length>=2){
      const ol=_ring[sym][0],nw=_ring[sym].at(-1),dt=(nw.t-ol.t)/1000;
      const d10=nw.c-ol.c;
      if(dt>0&&d10>0) rate10=fmtK(Math.round(d10/dt*10))+' recs/10s';
    }

    const tgt=act.target||est[sym+'_'+act.dtype];
    const pct=tgt&&tgt>0?Math.min(100,Math.round(act.count/tgt*100)):null;
    document.getElementById(`ff-${sym}`).style.width=(pct??0)+'%';
    document.getElementById(`fcnt-${sym}`).textContent=fmtK(act.count)+(tgt?' / '+fmtK(tgt):'')+(pct!=null?' · '+pct+'%':'');
    document.getElementById(`fspd-${sym}`).textContent=spd>200?fmtK(spd)+'/s':'';
    document.getElementById(`f10-${sym}`).textContent=rate10;
  } else {
    const allDone=proc&&proc.done===proc.total;
    stEl.className='fst '+(allDone?'fs-done':'fs-idle');
    stEl.textContent=allDone?'DONE':'IDLE';
    inEl.textContent=proc?proc.done+'/'+proc.total+' files':'';
    pbEl.style.display='none';
    _ring[sym]=[];
  }

  if(proc){
    const un=proc.done-proc.pass-proc.warn-proc.fail;
    const pts=[];
    if(proc.pass) pts.push(`<span class="sv">✓${proc.pass}</span>`);
    if(proc.warn) pts.push(`<span class="sw">⚠${proc.warn}</span>`);
    if(proc.fail) pts.push(`<span class="sf">✗${proc.fail}</span>`);
    if(un>0)      pts.push(`<span class="sm">·${un} unver</span>`);
    if(proc.missing>0) pts.push(`<span class="sm">—${proc.missing} miss</span>`);
    document.getElementById(`fstat-${sym}`).innerHTML=pts.join(' ');
  }
}

async function pollFetch(){
  try{
    const d=await(await fetch('/api/fetch/status')).json();
    _fActive=d.any_active;
    _est=d.estimates||{};
    document.getElementById('mbadge').textContent=d.mock?'MOCK':'LIVE';
    document.getElementById('mbadge').className='badge '+(d.mock?'bm':'bl');
    document.getElementById('rdot').className='rdot'+(d.any_active?' live':'');
    document.getElementById('lupd').textContent=new Date().toLocaleTimeString();
    document.getElementById('btn-fa').disabled=!d.mock&&_gwSt!=='up';

    if(d.processes) SYM.forEach(s=>updateFCard(s,d.processes[s],_est));

    const t=d.totals||{};
    const pct=t.done>0?Math.round((t.pass+t.warn)/t.done*100):0;
    const pts=[];
    if(t.pass)    pts.push(`<span class="sv">✓${t.pass} pass</span>`);
    if(t.warn)    pts.push(`<span class="sw">⚠${t.warn} warn</span>`);
    if(t.fail)    pts.push(`<span class="sf">✗${t.fail} fail</span>`);
    if(t.done)    pts.push(`<span style="color:#4d5566">verified ${pct}% of ${t.done}</span>`);
    if(t.active)  pts.push(`<span class="sw">… ${t.active} sym fetching</span>`);
    if(t.missing) pts.push(`<span class="sm">—${t.missing} missing</span>`);
    document.getElementById('fsumm').innerHTML=pts.join('&nbsp;&nbsp;');
  }catch(e){console.error(e)}
  clearTimeout(_fTimer);_fTimer=setTimeout(pollFetch,_fActive?1500:5000);
}

async function fetchAll(){await fetch('/api/fetch/all',{method:'POST'});pollFetch()}
async function verifyAll(){
  document.getElementById('btn-va').disabled=true;
  await fetch('/api/fetch/verify_all',{method:'POST'});
  setTimeout(()=>{document.getElementById('btn-va').disabled=false;pollFetch();},5000)
}
function refresh(){pollFetch();pollTrader();pollGw();}

// ── price cards ──

function buildPCards(){
  document.getElementById('pcards').innerHTML=SYM.map(s=>`
<div class="pcard">
  <div class="psym">${s}</div>
  <div class="pprice" id="pp-${s}">—</div>
  <div class="phl"    id="ph-${s}"></div>
  <div id="psrc-${s}"></div>
</div>`).join('')
}

function updatePrice(sym,info){
  document.getElementById(`pp-${sym}`).textContent=info?fmtP(info.price):'—';
  const hl=document.getElementById(`ph-${sym}`);
  hl.innerHTML=info&&info.high?`<b>H</b> ${fmtP(info.high)}&nbsp;&nbsp;<b>L</b> ${fmtP(info.low)}`:'';
  const sc=document.getElementById(`psrc-${sym}`);
  if(info&&info.source){
    sc.innerHTML=`<span class="psrc src-${info.src_class}">${info.source}${info.time_ct?' · '+info.time_ct:''}</span>`;
  } else { sc.innerHTML=''; }
}

async function pollPrices(){
  try{
    const d=await(await fetch('/api/trader/prices')).json();
    SYM.forEach(s=>updatePrice(s,d[s]||null));
  }catch(e){}
  clearTimeout(_pTimer);_pTimer=setTimeout(pollPrices,60000);
}

// ── trader ──

function strClass(n){return n==1?'str1':n==2?'str2':'str3'}
function strLabel(n){return n==1?'strong':n==2?'medium':'low'}

async function pollTrader(){
  try{
    const d=await(await fetch('/api/trader/status')).json();
    if(d.error){
      document.getElementById('trade-err').textContent='  '+d.error;
    } else {
      document.getElementById('trade-err').textContent='';

      // critical lines
      const lb=document.getElementById('lines-body');
      if(!d.critical_lines||!d.critical_lines.length){
        lb.innerHTML='<tr><td colspan="7" class="empty">No critical lines in DB</td></tr>';
      } else {
        lb.innerHTML=d.critical_lines.map(l=>`<tr>
          <td style="font-weight:600">${l.symbol}</td>
          <td style="color:${l.line_type==='SUPPORT'?'#3fb950':'#f85149'}">${l.line_type}</td>
          <td style="font-family:monospace">${fmtP(l.price)}</td>
          <td class="${strClass(l.strength)}">${strLabel(l.strength)}</td>
          <td>${l.armed?'<span class="tag tag-arm">ARMED</span>':'<span class="tag tag-dis">disarmed</span>'}</td>
          <td style="color:#4d5566;font-size:.62rem">${l.date||''}</td>
          <td>${l.armed?`<button class="btn btn-g" style="padding:1px 7px;font-size:.62rem" onclick="openFireDlg(${l.id},'${l.symbol}',${l.price})">Fire</button>`:''}
          </td></tr>`).join('');
      }

      // positions
      const pb=document.getElementById('pos-body');
      if(!d.positions||!d.positions.length){
        pb.innerHTML='<tr><td colspan="7" class="empty">No open positions</td></tr>';
      } else {
        pb.innerHTML=d.positions.map(p=>`<tr>
          <td style="color:#4d5566">${p.id}</td>
          <td style="font-weight:600">${p.symbol}</td>
          <td><span class="tag tag-${p.direction.toLowerCase()}">${p.direction}</span></td>
          <td>${p.quantity}</td>
          <td style="font-family:monospace">${fmtP(p.entry_price)}</td>
          <td>${p.status}</td>
          <td style="color:#4d5566;font-size:.62rem">${fmtTime(p.entry_time)}</td></tr>`).join('');
      }

      // recent trades
      const tb=document.getElementById('trades-body');
      if(!d.recent||!d.recent.length){
        tb.innerHTML='<tr><td colspan="7" class="empty">No completed trades yet</td></tr>';
      } else {
        tb.innerHTML=d.recent.map(r=>{
          const pnl=r.pnl_points;
          const pnlStr=pnl!=null?(pnl>0?'+':'')+Number(pnl).toFixed(2):'—';
          const pnlCol=pnl==null?'#6e7681':pnl>0?'#3fb950':'#f85149';
          return `<tr>
            <td style="color:#4d5566">${r.id}</td>
            <td style="font-weight:600">${r.symbol}</td>
            <td><span class="tag tag-${(r.direction||'').toLowerCase()}">${r.direction||'—'}</span></td>
            <td style="font-family:monospace">${fmtP(r.fill_price)}</td>
            <td style="font-family:monospace">${fmtP(r.exit_price)}</td>
            <td>${r.exit_reason?`<span class="tag tag-${(r.exit_reason||'').toLowerCase()}">${r.exit_reason}</span>`:'—'}</td>
            <td style="color:${pnlCol};font-family:monospace;font-weight:600">${pnlStr}</td></tr>`;
        }).join('');
      }

      // stats in section header
      if(d.stats){
        const s=d.stats;
        const pnlCol=s.total_pnl>0?'#3fb950':s.total_pnl<0?'#f85149':'#6e7681';
        document.getElementById('trade-err').innerHTML=
          `<span style="color:#4d5566">TP:${s.tp||0} · SL:${s.sl||0} · PnL: <b style="color:${pnlCol}">${s.total_pnl>0?'+':''}${(s.total_pnl||0).toFixed(2)} pts</b></span>`;
      }
    }

    // process chips
    updProc('broker',  d.broker||'stopped',  'bk');
    updProc('decider', d.decider||'stopped', 'dc');

  }catch(e){console.error(e)}
  clearTimeout(_tTimer);_tTimer=setTimeout(pollTrader,6000);
}

function updProc(name,status,pre){
  const chip=document.getElementById(pre+'-chip');
  const running=status==='running';
  chip.className='proc-chip '+(running?'pc-run':'pc-stp');
  chip.innerHTML=`<span class="pc-dot"></span>${name.charAt(0).toUpperCase()+name.slice(1)}: ${status}`;
  document.getElementById(pre+'-start').style.display=running?'none':'inline-flex';
  document.getElementById(pre+'-stop').style.display=running?'inline-flex':'none';
}

async function startProc(name){
  await fetch('/api/trader/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proc:name})});
  setTimeout(pollTrader,1000);
}
async function stopProc(name){
  await fetch('/api/trader/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proc:name})});
  setTimeout(pollTrader,1000);
}

// ── fire dialog ──

let _fd={};
function openFireDlg(lineId,sym,price){
  _fd={lineId,sym,price};
  document.getElementById('fire-info').textContent=
    `${sym} critical line at ${fmtP(price)} — choose direction for MKT bracket`;
  document.getElementById('fire-dlg').style.display='flex';
}
function closeDlg(){document.getElementById('fire-dlg').style.display='none';}
async function fireLine(dir){
  closeDlg();
  const r=await(await fetch('/api/trader/fire',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:_fd.sym,direction:dir,source:'critical_line',line_id:_fd.lineId,price:_fd.price})
  })).json();
  if(r.error) alert('Fire error: '+r.error);
  else { alert(`Order queued #${r.command_id}: ${dir} ${_fd.sym} @ ${fmtP(r.entry)} TP ${fmtP(r.tp)} SL ${fmtP(r.sl)}`); pollTrader(); }
}

// ── init ──
buildFCards(); buildPCards();
pollGw(); pollPrices().then(()=>pollFetch()); pollTrader();
</script>
</body>
</html>"""

@app.route("/")
def index(): return _HTML

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    _mock_mode = not args.real
    if _mock_mode:
        _init_mock()
        print("[galao] MOCK fetch + real trader DB — http://localhost:%d" % args.port)
    else:
        print("[galao] REAL mode — http://localhost:%d" % args.port)
        threading.Thread(target=_auto_verify_loop, daemon=True).start()
    print("[galao] Network: http://192.168.1.132:%d" % args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
