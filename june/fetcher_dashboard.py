#!/usr/bin/env python3
"""
fetcher_dashboard.py  —  Galao fetch-progress browser UI.
  python fetcher_dashboard.py           # mock mode (default)
  python fetcher_dashboard.py --real    # live DB + history/
  python fetcher_dashboard.py --port 5050
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
_mock_state  = {}   # (sym, date_str, dtype) -> {status, count, target, verify, notes}
_mock_gw     = {"status": "down"}
_lock        = threading.Lock()
_gw_up_since = None   # datetime utc

# ── helpers ───────────────────────────────────────────────────────────────────

def _working_days(start: date, end: date):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
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

# ── mock ──────────────────────────────────────────────────────────────────────

def _init_mock():
    today = date.today()
    days  = _working_days(FETCH_START, today)
    rng   = random.Random(42)
    with _lock:
        for i, d in enumerate(days):
            ds = d.isoformat()
            for sym in SYMBOLS:
                tgt = {"MES":62000,"MNQ":30000,"MYM":8500,"M2K":7000}[sym]
                for dtype in DTYPES:
                    key  = (sym, ds, dtype)
                    done_p = max(0.0, 0.88 - i * 0.07)
                    if rng.random() < done_p:
                        cnt = int(tgt * rng.uniform(0.85, 1.15))
                        ver = rng.choice(["pass","pass","pass","warn",None])
                        _mock_state[key] = {"status":"done","count":cnt,"target":tgt,"verify":ver,"notes":[]}
                    else:
                        _mock_state[key] = {"status":"missing","count":0,"target":tgt,"verify":None,"notes":[]}

def _mock_run_fetch(sym, ds, dtype):
    key    = (sym, ds, dtype)
    target = {"MES":62000,"MNQ":30000,"MYM":8500,"M2K":7000}.get(sym, 40000)
    target = int(target * random.uniform(0.85, 1.15))
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

# ── real data ─────────────────────────────────────────────────────────────────

def _history_dir():
    try:
        from lib.config_loader import get_config
        return Path(get_config().paths.history)
    except Exception: return _ROOT/"data"/"history"

def _prog_db_path():
    try:
        from lib.config_loader import get_config
        return Path(get_config().paths.db).parent/"fetch_progress.db"
    except Exception: return _ROOT/"data"/"fetch_progress.db"

def _load_db_rows():
    path = _prog_db_path()
    if not path.exists(): return {}
    rows = {}
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT symbol,date,data_type,records_fetched,finished,"
                "verify_status,verify_notes FROM fetch_progress")
        except sqlite3.OperationalError:
            cur = conn.execute(
                "SELECT symbol,date,data_type,records_fetched,finished FROM fetch_progress")
        for r in cur.fetchall():
            rows[(r["symbol"], r["date"], r["data_type"])] = r
        conn.close()
    except Exception: pass
    return rows

def _csv_exists(sym, ds, dtype):
    ftype = "trades" if dtype=="TRADES" else "bidask"
    dc = ds.replace("-","")
    p = _history_dir()/f"{sym}_{ftype}_{dc}.csv"
    return p.exists() and p.stat().st_size > 500

def _estimate_targets(db_rows):
    buckets = {}
    for (sym, ds, dtype), r in db_rows.items():
        cnt = int(r["records_fetched"] or 0)
        if r["finished"] and cnt > 1000:
            buckets.setdefault((sym, dtype), []).append(cnt)
    return {f"{sym}_{dtype}": int(median(v)) for (sym,dtype),v in buckets.items() if v}

def _get_real_state():
    db  = _load_db_rows()
    est = _estimate_targets(db)
    state = {}
    for d in _working_days(FETCH_START, date.today()):
        ds = d.isoformat()
        for sym in SYMBOLS:
            for dtype in DTYPES:
                r     = db.get((sym, ds, dtype))
                on_d  = _csv_exists(sym, ds, dtype)
                cnt   = int(r["records_fetched"] or 0) if r else 0
                vs    = (r["verify_status"] if r and "verify_status" in r.keys() else None)
                vn    = (r["verify_notes"]  if r and "verify_notes"  in r.keys() else "") or ""
                notes = [n for n in vn.split(" | ") if n]
                tgt   = est.get(f"{sym}_{dtype}")
                if on_d or (r and r["finished"]):
                    state[(sym,ds,dtype)] = {"status":"done","count":cnt,"target":tgt,"verify":vs,"notes":notes}
                elif r and not r["finished"]:
                    state[(sym,ds,dtype)] = {"status":"in_progress","count":cnt,"target":tgt,"verify":None,"notes":[]}
                else:
                    state[(sym,ds,dtype)] = {"status":"missing","count":0,"target":tgt,"verify":None,"notes":[]}
    return state, est

# ── auto-verify background thread ─────────────────────────────────────────────

def _auto_verify_loop():
    time.sleep(15)
    while True:
        try:
            db = _load_db_rows()
            hd = _history_dir()
            from trader.verify_data import check_and_mark
            pending = [(r["symbol"], r["date"], r["data_type"])
                       for r in db.values()
                       if r["finished"] and not (r["verify_status"] if "verify_status" in r.keys() else None)]
            for sym, ds, dtype in pending[:4]:
                try: check_and_mark(sym, date.fromisoformat(ds), dtype, hd)
                except Exception: pass
        except Exception: pass
        time.sleep(25)

# ── gateway routes ────────────────────────────────────────────────────────────

@app.route("/api/gateway")
def api_gateway():
    global _gw_up_since
    if _mock_mode:
        with _lock: st = _mock_gw["status"]
        ups = int((datetime.now(timezone.utc)-_gw_up_since).total_seconds()) if _gw_up_since and st=="up" else None
        return jsonify({"status":st,"mock":True,"up_seconds":ups})
    up = _gw_is_up()
    if up and not _gw_up_since:  _gw_up_since = datetime.now(timezone.utc)
    if not up: _gw_up_since = None
    ups = int((datetime.now(timezone.utc)-_gw_up_since).total_seconds()) if _gw_up_since and up else None
    return jsonify({"status":"up" if up else "down","mock":False,"port":_gw_port(),"up_seconds":ups})

@app.route("/api/gateway/start",methods=["POST"])
def api_gw_start():
    if _mock_mode: _mock_gw_start(); return jsonify({"ok":True,"mock":True})
    try:
        from lib.config_loader import get_config
        from lib.ibc_launcher import try_start_gateway
        ok = try_start_gateway(get_config(), label="dashboard")
        return jsonify({"ok":ok,"mock":False})
    except Exception as e: return jsonify({"ok":False,"msg":str(e)}),500

@app.route("/api/gateway/stop",methods=["POST"])
def api_gw_stop():
    global _gw_up_since
    if _mock_mode: _mock_gw_stop(); return jsonify({"ok":True,"mock":True})
    try:
        subprocess.run(["taskkill","/F","/FI","WINDOWTITLE eq IB Gateway*"],capture_output=True,timeout=10)
    except Exception: pass
    _gw_up_since = None
    return jsonify({"ok":True,"mock":False})

# ── status routes ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    if _mock_mode:
        with _lock: state = {k: dict(v) for k, v in _mock_state.items()}
        estimates = {f"{sym}_{dt}": {"MES":62000,"MNQ":30000,"MYM":8500,"M2K":7000}[sym]
                     for sym in SYMBOLS for dt in DTYPES}
    else:
        state, estimates = _get_real_state()

    days  = list(reversed(_working_days(FETCH_START, date.today())))
    rows  = []
    total_done=0; total_pass=0; total_warn=0; total_fail=0; total_act=0; total_miss=0

    for d in days:
        ds    = d.isoformat()
        cells = {}
        for sym in SYMBOLS:
            for dtype in DTYPES:
                c = state.get((sym,ds,dtype), {"status":"missing","count":0,"target":None,"verify":None,"notes":[]})
                cells[f"{sym}_{dtype}"] = c
                s = c["status"]; v = c.get("verify")
                if s=="done":
                    total_done+=1
                    if v=="pass": total_pass+=1
                    elif v=="warn": total_warn+=1
                    elif v=="fail": total_fail+=1
                elif s=="in_progress": total_act+=1
                else: total_miss+=1
        rows.append({"date":ds,"cells":cells})

    processes = {}
    for sym in SYMBOLS:
        active_cell = None
        for d in days:
            for dtype in DTYPES:
                c = state.get((sym, d.isoformat(), dtype), {})
                if c.get("status")=="in_progress":
                    active_cell = {"date":d.isoformat(),"dtype":dtype,**c}
                    break
            if active_cell: break
        done_s  = sum(1 for d in days for dt in DTYPES if state.get((sym,d.isoformat(),dt),{}).get("status")=="done")
        pass_s  = sum(1 for d in days for dt in DTYPES if state.get((sym,d.isoformat(),dt),{}).get("verify")=="pass")
        warn_s  = sum(1 for d in days for dt in DTYPES if state.get((sym,d.isoformat(),dt),{}).get("verify")=="warn")
        fail_s  = sum(1 for d in days for dt in DTYPES if state.get((sym,d.isoformat(),dt),{}).get("verify")=="fail")
        total_s = len(days)*2
        processes[sym] = {
            "active": active_cell,
            "done": done_s, "total": total_s,
            "pass": pass_s, "warn": warn_s, "fail": fail_s,
        }

    return jsonify({
        "mock": _mock_mode, "rows": rows, "estimates": estimates,
        "processes": processes, "any_active": total_act>0,
        "summary": {"done":total_done,"pass":total_pass,"warn":total_warn,
                    "fail":total_fail,"active":total_act,"missing":total_miss},
    })

@app.route("/api/prices")
def api_prices():
    if _mock_mode:
        bases = {"MES":5230,"MNQ":19800,"MYM":42100,"M2K":2105}
        r = {}
        for sym,base in bases.items():
            p = base+random.uniform(-15,15)
            r[sym]={"price":round(p,2),"high":round(p+random.uniform(10,40),2),
                    "low":round(p-random.uniform(10,40),2),"time_ct":"16:45 CT","date":date.today().isoformat()}
        return jsonify(r)
    try:
        from trader.verify_data import last_price
        hd = _history_dir()
        return jsonify({sym: last_price(sym, hd) for sym in SYMBOLS})
    except Exception as e:
        return jsonify({"error":str(e)}),500

# ── fetch/verify routes ───────────────────────────────────────────────────────

@app.route("/api/fetch",methods=["POST"])
def api_fetch():
    d=request.json or {}
    sym=d.get("symbol","").upper(); ds=d.get("date",""); dtype=d.get("dtype","TRADES").upper()
    if sym not in SYMBOLS: return jsonify({"error":"bad symbol"}),400
    if _mock_mode:
        _mock_run_fetch(sym,ds,dtype); return jsonify({"ok":True,"mock":True})
    cmd=[sys.executable,str(_ROOT/"trader"/"fetcher.py"),"--symbol",sym,"--date",ds]
    if dtype=="BID_ASK": cmd.append("--bid-ask")
    subprocess.Popen(cmd, cwd=str(_ROOT))
    return jsonify({"ok":True,"mock":False})

@app.route("/api/fetch_all_missing",methods=["POST"])
def api_fetch_all_missing():
    if _mock_mode:
        today=date.today()
        with _lock:
            keys=[(sym,d.isoformat(),dt) for d in _working_days(FETCH_START,today)
                  for sym in SYMBOLS for dt in DTYPES
                  if _mock_state.get((sym,d.isoformat(),dt),{}).get("status")=="missing"]
        def _stagger():
            for sym,ds,dt in keys:
                _mock_run_fetch(sym,ds,dt); time.sleep(0.06)
        threading.Thread(target=_stagger,daemon=True).start()
        return jsonify({"ok":True,"mock":True,"queued":len(keys)})
    for sym in SYMBOLS:
        cmd=[sys.executable,str(_ROOT/"trader"/"fetcher.py"),
             "--symbol",sym,"--from-date",FETCH_START.isoformat(),"--bid-ask"]
        subprocess.Popen(cmd,cwd=str(_ROOT))
    return jsonify({"ok":True,"mock":False,"processes":len(SYMBOLS)})

@app.route("/api/verify_cell",methods=["POST"])
def api_verify_cell():
    d=request.json or {}
    sym=d.get("symbol","").upper(); ds=d.get("date",""); dtype=d.get("dtype","trades").lower()
    if sym not in SYMBOLS: return jsonify({"error":"bad symbol"}),400
    if _mock_mode:
        key=(sym,ds,dtype.upper())
        with _lock:
            c=_mock_state.get(key,{})
            if c.get("status")=="done":
                c["verify"]=random.choice(["pass","pass","warn"])
        return jsonify({"ok":True,"mock":True})
    def _run():
        try:
            from trader.verify_data import check_and_mark
            check_and_mark(sym,date.fromisoformat(ds),dtype,_history_dir())
        except Exception: pass
    threading.Thread(target=_run,daemon=True).start()
    return jsonify({"ok":True,"mock":False})

@app.route("/api/verify_all",methods=["POST"])
def api_verify_all():
    if _mock_mode:
        with _lock:
            for c in _mock_state.values():
                if c.get("status")=="done" and c.get("verify") is None:
                    c["verify"]=random.choice(["pass","pass","warn"])
        return jsonify({"ok":True,"mock":True})
    def _run():
        try:
            from trader.verify_data import check_and_mark
            db=_load_db_rows(); hd=_history_dir()
            for r in db.values():
                if r["finished"] and not (r["verify_status"] if "verify_status" in r.keys() else None):
                    try: check_and_mark(r["symbol"],date.fromisoformat(r["date"]),r["data_type"].lower(),hd)
                    except Exception: pass
        except Exception: pass
    threading.Thread(target=_run,daemon=True).start()
    return jsonify({"ok":True,"mock":False})

# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Galao Fetcher</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}

/* header */
header{display:flex;align-items:center;gap:10px;padding:8px 16px;border-bottom:1px solid #21262d;flex-wrap:wrap}
header h1{font-size:.8rem;font-weight:700;color:#6e7681;letter-spacing:.1em}
.badge{padding:2px 8px;border-radius:999px;font-size:.65rem;font-weight:700;letter-spacing:.05em}
.badge-mock{background:#3d1f8c22;color:#a78bfa;border:1px solid #6d28d944}
.badge-real{background:#14532d22;color:#3fb950;border:1px solid #238636}
.rdot{width:6px;height:6px;border-radius:50%;background:#30363d;display:inline-block}
.rdot.live{background:#3fb950;animation:bl 1.4s infinite}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.25}}
.gw{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:6px;
    font-size:.72rem;font-weight:600;border:1px solid transparent;transition:all .3s}
.gw-down    {background:#1c1010;border-color:#4a1c1c;color:#f85149}
.gw-starting{background:#1c1a10;border-color:#4a3b1c;color:#d29922;animation:gp 1.2s infinite}
.gw-up      {background:#0e1f14;border-color:#1a4226;color:#3fb950}
@keyframes gp{0%,100%{opacity:1}50%{opacity:.4}}
.gw-dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.gwb{padding:2px 8px;border-radius:4px;font-size:.68rem;font-weight:600;cursor:pointer;
     border:none;transition:all .15s;margin-left:2px}
.gwb:disabled{opacity:.3;cursor:default}
.gwb-on {background:#1a4226;color:#3fb950;border:1px solid #238636}.gwb-on:hover{background:#238636;color:#fff}
.gwb-off{background:#2d1010;color:#f85149;border:1px solid #4a1c1c}.gwb-off:hover{background:#4a1c1c;color:#fff}
.uptime{font-size:.62rem;color:#4d5566}
.lupd{margin-left:auto;font-size:.65rem;color:#4d5566}

/* toolbar */
.tb{display:flex;align-items:center;gap:7px;padding:7px 16px 4px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:4px;padding:4px 11px;border-radius:5px;
     font-size:.74rem;font-weight:500;cursor:pointer;border:none;transition:all .12s}
.btn:disabled{opacity:.35;cursor:default}
.btn-g{background:#238636;color:#fff}.btn-g:hover:not(:disabled){background:#2ea043}
.btn-v{background:#1f3a5f;color:#60a5fa;border:1px solid #1e40af44}.btn-v:hover:not(:disabled){background:#1e40af;color:#fff}
.btn-x{background:transparent;color:#8b949e;border:1px solid #30363d}.btn-x:hover{background:#161b22;color:#c9d1d9}

/* summary bar */
.summ{padding:2px 16px 6px;font-size:.71rem;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.sv{color:#3fb950}.sw{color:#d29922}.sf{color:#f85149}.sa{color:#f0883e}.sm{color:#6e7681}
.pct-bar{font-size:.62rem;color:#4d5566;margin-left:2px}

/* process cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:8px 16px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px 12px;
      display:flex;flex-direction:column;gap:3px;min-height:128px}
.card-head{display:flex;align-items:center;justify-content:space-between}
.card-sym{font-size:.78rem;font-weight:700;color:#8b949e;letter-spacing:.06em}
.card-st{padding:1px 6px;border-radius:3px;font-size:.6rem;font-weight:700;letter-spacing:.04em}
.st-idle {background:#21262d;color:#4d5566}
.st-fetch{background:#c2410c22;color:#f0883e;border:1px solid #c2410c44;animation:sp 1.1s infinite}
.st-done {background:#14532d22;color:#3fb950}
@keyframes sp{0%,100%{opacity:1}50%{opacity:.45}}
.card-price{font-size:1.28rem;font-weight:700;color:#e6edf3;line-height:1;margin-top:2px}
.card-hl{font-size:.62rem;color:#4d5566}
.card-hl b{color:#6e7681}
.card-info{font-size:.63rem;color:#6e7681;min-height:.9rem}
.pbar{background:#0d1117;border-radius:3px;height:5px;margin:3px 0;overflow:hidden;border:1px solid #21262d}
.pbar-fill{height:100%;border-radius:3px;transition:width .6s ease;
           background:linear-gradient(90deg,#c2410c,#f0883e)}
.card-cnt{display:flex;justify-content:space-between;font-size:.62rem;color:#6e7681}
.card-spd{color:#f0883e;font-weight:600;font-family:monospace}
.card-stats{margin-top:auto;display:flex;gap:7px;font-size:.62rem;flex-wrap:wrap}
.card-stats .sv{color:#3fb950}.card-stats .sw{color:#d29922}.card-stats .sf{color:#f85149}

/* grid */
.wrap{overflow-x:auto;padding:0 16px 40px}
table{border-collapse:collapse;font-size:.7rem;width:100%}
thead th{position:sticky;top:0;background:#0d1117;padding:5px 4px;text-align:center;
  font-weight:600;color:#6e7681;border-bottom:2px solid #21262d;white-space:nowrap;z-index:2}
thead th.sym-h{color:#c9d1d9;font-size:.75rem;border-left:1px solid #21262d}
thead th.dt-h{font-size:.59rem;color:#4d5566;font-weight:500}
thead th.date-h{text-align:left;min-width:82px;border-left:none}
td{padding:2px 3px;border-bottom:1px solid #0d1117;border-left:1px solid #0d1117;vertical-align:middle}
td.date{color:#6e7681;font-size:.67rem;font-family:monospace;border-left:none;
  position:sticky;left:0;background:#0d1117;z-index:1;white-space:nowrap;padding-right:6px}
tr:hover td{background:#ffffff04}
tr:hover td.date{background:#0d1117}

/* cell pills */
.cell{display:flex;align-items:center;gap:2px;min-width:66px}
.pill{display:inline-flex;align-items:center;padding:2px 5px;border-radius:999px;
  font-size:.59rem;font-weight:700;white-space:nowrap;gap:2px}
.p-miss {background:#21262d;color:#4d5566;border:1px solid #30363d}
.p-prog {background:#4a1c0022;color:#f0883e;border:1px solid #c2410c44;animation:sp 1.1s infinite}
.p-unver{background:#14532d22;color:#6e9e6e;border:1px solid #23863622}
.p-pass {background:#14532d44;color:#3fb950;border:1px solid #238636}
.p-warn {background:#3d2f0022;color:#d29922;border:1px solid #7d5c1033}
.p-fail {background:#3d100022;color:#f85149;border:1px solid #7d1c1c44}
.pm{font-size:.54rem;color:#6e7681;margin-left:1px}
.abn{background:transparent;border:1px solid #30363d33;border-radius:3px;
  padding:1px 4px;font-size:.57rem;cursor:pointer;opacity:0;transition:opacity .1s;margin-left:1px}
tr:hover .abn{opacity:1}
.abn.f{color:#388bfd}.abn.f:hover{background:#388bfd22}
.abn.v{color:#a78bfa}.abn.v:hover{background:#7c3aed22}
.abn.r{color:#f0883e}.abn.r:hover{background:#c2410c22}
</style>
</head>
<body>

<header>
  <h1>GALAO FETCHER</h1>
  <span id="mbadge" class="badge badge-mock">MOCK</span>
  <div id="gw-pill" class="gw gw-down" style="margin-left:4px">
    <span class="gw-dot"></span>
    <span id="gw-label">Gateway Down</span>
    <span id="gw-uptime" class="uptime"></span>
  </div>
  <button id="gw-on"  class="gwb gwb-on"  onclick="gwStart()">Start</button>
  <button id="gw-off" class="gwb gwb-off" onclick="gwStop()" style="display:none">Stop</button>
  <span class="lupd"><span class="rdot" id="rdot"></span>&nbsp;<span id="lupd">—</span></span>
</header>

<div class="cards" id="proc-cards"></div>

<div class="tb">
  <button id="btn-fa" class="btn btn-g" onclick="fetchAll()">&#9654; Fetch All Missing</button>
  <button id="btn-va" class="btn btn-v" onclick="verifyAll()">&#10003; Verify All</button>
  <button            class="btn btn-x" onclick="poll()">&#8635; Refresh</button>
</div>
<div class="summ" id="summ">Loading…</div>

<div class="wrap">
  <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
</div>

<script>
const SYM=['MES','MNQ','MYM','M2K'], DT=['TRADES','BID_ASK'];
let _active=false, _gwSt='down', _timer=null, _gwTimer=null, _priceTimer=null;
let _prev={};    // sym -> {key, count, time}
let _est={};     // "SYM_DTYPE" -> number
let _prices={};  // sym -> price info

// ── gateway ──

function fmtUp(s){if(!s)return'';if(s<60)return s+'s';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h?h+'h '+m+'m':m+'m'}

async function pollGw(){
  try{
    const d=await(await fetch('/api/gateway')).json();
    _gwSt=d.status;
    document.getElementById('gw-pill').className='gw gw-'+_gwSt;
    document.getElementById('gw-label').textContent={down:'Gateway Down',starting:'Starting…',up:'Gateway Ready'}[_gwSt]||_gwSt;
    document.getElementById('gw-uptime').textContent=_gwSt==='up'&&d.up_seconds?'· '+fmtUp(d.up_seconds):'';
    document.getElementById('gw-on').style.display=_gwSt==='up'?'none':'inline-block';
    document.getElementById('gw-on').disabled=_gwSt==='starting';
    document.getElementById('gw-off').style.display=_gwSt==='up'?'inline-block':'none';
  }catch(e){}
  clearTimeout(_gwTimer);_gwTimer=setTimeout(pollGw,_gwSt==='starting'?2000:12000);
}
async function gwStart(){document.getElementById('gw-on').disabled=true;await fetch('/api/gateway/start',{method:'POST'});pollGw()}
async function gwStop(){await fetch('/api/gateway/stop',{method:'POST'});pollGw()}

// ── prices ──

function fmtP(v){return v==null?'—':v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}

async function pollPrices(){
  try{_prices=await(await fetch('/api/prices')).json();}catch(e){}
  clearTimeout(_priceTimer);_priceTimer=setTimeout(pollPrices,60000);
}

// ── process cards ──

function buildCards(){
  document.getElementById('proc-cards').innerHTML=SYM.map(s=>`
<div class="card">
  <div class="card-head"><span class="card-sym">${s}</span><span class="card-st st-idle" id="cst-${s}">IDLE</span></div>
  <div class="card-price" id="cprice-${s}">—</div>
  <div class="card-hl"    id="chl-${s}"></div>
  <div class="card-info"  id="cinfo-${s}"></div>
  <div id="cpbar-${s}" style="display:none">
    <div class="pbar"><div class="pbar-fill" id="cfill-${s}" style="width:0%"></div></div>
    <div class="card-cnt"><span id="ccnt-${s}"></span><span class="card-spd" id="cspd-${s}"></span></div>
  </div>
  <div class="card-stats" id="cstat-${s}"></div>
</div>`).join('')
}

function updateCard(sym, proc){
  const p=_prices&&_prices[sym];
  document.getElementById(`cprice-${sym}`).textContent=p?fmtP(p.price):'—';
  document.getElementById(`chl-${sym}`).innerHTML=p?`<b>H</b> ${fmtP(p.high)}&nbsp;&nbsp;<b>L</b> ${fmtP(p.low)}<span style="margin-left:7px;font-size:.58rem;color:#4d5566">${p.time_ct||''}</span>`:'';

  const act=proc&&proc.active;
  const stEl=document.getElementById(`cst-${sym}`);
  const pbEl=document.getElementById(`cpbar-${sym}`);
  const inEl=document.getElementById(`cinfo-${sym}`);

  if(act){
    stEl.className='card-st st-fetch'; stEl.textContent='FETCHING';
    inEl.textContent=(act.dtype==='TRADES'?'TRADES':'BID/ASK')+' · '+act.date;
    pbEl.style.display='block';

    const key=sym+'_'+act.date+'_'+act.dtype;
    const now=Date.now(), pr=_prev[sym];
    let spd=0;
    if(pr&&pr.key===key){const dt=(now-pr.time)/1000;if(dt>0)spd=Math.max(0,(act.count-pr.count)/dt);}
    _prev[sym]={key,count:act.count,time:now};

    const tgt=act.target||_est[sym+'_'+act.dtype];
    const pct=tgt&&tgt>0?Math.min(100,Math.round(act.count/tgt*100)):null;
    document.getElementById(`cfill-${sym}`).style.width=(pct!=null?pct:0)+'%';
    document.getElementById(`ccnt-${sym}`).textContent=
      fmtK(act.count)+(tgt?' / '+fmtK(tgt):'')+(pct!=null?' · '+pct+'%':'');
    document.getElementById(`cspd-${sym}`).textContent=spd>200?fmtK(spd,1)+'/s':'';
  } else {
    const allDone=proc&&proc.done===proc.total;
    stEl.className='card-st '+(allDone?'st-done':'st-idle');
    stEl.textContent=allDone?'DONE':'IDLE';
    inEl.textContent=proc?(proc.done+'/'+proc.total+' files done'):'';
    pbEl.style.display='none';
  }

  if(proc){
    const unver=proc.done-proc.pass-proc.warn-proc.fail;
    const parts=[];
    if(proc.pass) parts.push(`<span class="sv">✓ ${proc.pass}</span>`);
    if(proc.warn) parts.push(`<span class="sw">⚠ ${proc.warn}</span>`);
    if(proc.fail) parts.push(`<span class="sf">✗ ${proc.fail}</span>`);
    if(unver>0)   parts.push(`<span style="color:#4d5566">· ${unver} unver</span>`);
    document.getElementById(`cstat-${sym}`).innerHTML=parts.join(' ');
  }
}

function fmtK(n,dec){return n>=1000?(n/1000).toFixed(dec!=null?dec:1)+'k':Math.round(n)+''}

// ── grid ──

function buildHead(){
  let h='<tr><th class="date-h">DATE</th>';
  SYM.forEach(s=>h+=`<th class="sym-h" colspan="2">${s}</th>`);
  h+='</tr><tr><th></th>';
  SYM.forEach(()=>DT.forEach(d=>h+=`<th class="dt-h">${d==='TRADES'?'TRADES':'BID/ASK'}</th>`));
  document.getElementById('thead').innerHTML=h+'</tr>';
}

function cellHtml(sym,ds,dtype,cell){
  const{status,count,target,verify,notes}=cell;
  const tip=notes&&notes.length?' title="'+notes.join('\n')+'"':'';
  const pct=target&&count>0?Math.min(100,Math.round(count/target*100)):null;
  const pm=pct!=null&&pct<100?`<span class="pm">${pct}%</span>`:'';

  if(status==='in_progress')
    return`<div class="cell"><span class="pill p-prog">… ${fmtK(count)}</span>${pm}</div>`;
  if(status==='done'){
    let pc='p-unver',ic='·';
    if(verify==='pass'){pc='p-pass';ic='✓';}
    else if(verify==='warn'){pc='p-warn';ic='⚠';}
    else if(verify==='fail'){pc='p-fail';ic='✗';}
    const vb=!verify?`<button class="abn v" onclick="verifyCell('${sym}','${ds}','${dtype}')">V</button>`:'';
    const rb=verify==='fail'?`<button class="abn r" onclick="fetchOne('${sym}','${ds}','${dtype}')">&#8635;</button>`:'';
    return`<div class="cell"><span class="pill ${pc}"${tip}>${ic} ${fmtK(count)}</span>${vb}${rb}</div>`;
  }
  return`<div class="cell"><span class="pill p-miss">—</span><button class="abn f" onclick="fetchOne('${sym}','${ds}','${dtype}')">&#9654;</button></div>`;
}

// ── main poll ──

async function poll(){
  try{
    const data=await(await fetch('/api/status')).json();
    _active=data.any_active;
    if(data.estimates) _est=data.estimates;
    const s=data.summary||{};

    document.getElementById('mbadge').textContent=data.mock?'MOCK':'LIVE';
    document.getElementById('mbadge').className='badge '+(data.mock?'badge-mock':'badge-real');
    document.getElementById('rdot').className='rdot'+(data.any_active?' live':'');
    document.getElementById('lupd').textContent=new Date().toLocaleTimeString();
    document.getElementById('btn-fa').disabled=!data.mock&&_gwSt!=='up';

    if(data.processes) SYM.forEach(sym=>updateCard(sym,data.processes[sym]));

    const pct=s.done>0?Math.round((s.pass+s.warn)/s.done*100):0;
    const pts=[];
    if(s.pass)  pts.push(`<span class="sv">✓ ${s.pass} pass</span>`);
    if(s.warn)  pts.push(`<span class="sw">⚠ ${s.warn} warn</span>`);
    if(s.fail)  pts.push(`<span class="sf">✗ ${s.fail} fail</span>`);
    if(s.done)  pts.push(`<span class="pct-bar">verified ${pct}% of ${s.done} done</span>`);
    if(s.active)pts.push(`<span class="sa">… ${s.active} fetching</span>`);
    if(s.missing)pts.push(`<span class="sm">— ${s.missing} missing</span>`);
    document.getElementById('summ').innerHTML=pts.join('&nbsp;&nbsp;');

    let html='';
    for(const row of data.rows){
      html+=`<tr><td class="date">${row.date}</td>`;
      SYM.forEach(sym=>DT.forEach(dt=>{
        const c=row.cells[sym+'_'+dt]||{status:'missing',count:0,target:null,verify:null,notes:[]};
        if(!c.target) c.target=_est[sym+'_'+dt]||null;
        html+=`<td>${cellHtml(sym,row.date,dt,c)}</td>`;
      }));
      html+='</tr>';
    }
    document.getElementById('tbody').innerHTML=html;
  }catch(e){console.error(e)}
  clearTimeout(_timer);_timer=setTimeout(poll,_active?1500:5000);
}

// ── actions ──

async function fetchOne(sym,ds,dtype){
  await fetch('/api/fetch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym,date:ds,dtype})});poll();}
async function fetchAll(){await fetch('/api/fetch_all_missing',{method:'POST'});poll();}
async function verifyCell(sym,ds,dtype){
  await fetch('/api/verify_cell',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym,date:ds,dtype:dtype.toLowerCase()})});
  setTimeout(poll,3000);}
async function verifyAll(){
  document.getElementById('btn-va').disabled=true;
  await fetch('/api/verify_all',{method:'POST'});
  setTimeout(()=>{document.getElementById('btn-va').disabled=false;poll();},5000);}

buildCards();buildHead();pollGw();pollPrices().then(()=>poll());
</script>
</body>
</html>"""


@app.route("/")
def index():
    return _HTML

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    _mock_mode = not args.real
    if _mock_mode:
        _init_mock()
        print("[dashboard] MOCK mode — http://localhost:%d" % args.port)
    else:
        print("[dashboard] REAL mode — http://localhost:%d" % args.port)
        threading.Thread(target=_auto_verify_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
