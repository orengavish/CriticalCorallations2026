# RESTART_PROJECT — Bootstrapping the Whole System on a Fresh PC
> Supersedes `NEW_PC_SETUP.md` (dated 2026-06-30, references the pre-split `galgo2026.git`
> monolith and a since-abandoned raw-`ibapi` install path — kept only for archaeology, do
> not follow it). This doc covers all three live sibling projects plus the shared IB
> Gateway they depend on. Written 2026-08-18, verified against the actual running system.

---

## 0. What you're standing up

Three independent git repos plus one shared IB Gateway instance:

| Project | Repo | Language | Port | Role |
|---|---|---|---|---|
| CriticalCorallations2026 (this repo, "CC2026") | `github.com/orengavish/CriticalCorallations2026` | Python | 5003 | Trading brain — dashboard, broker, decider, algo research |
| Fetcher2026 | `github.com/orengavish/Fetcher2026` | Python | 5050 (+5004) | Tick-CSV + OHLCV-bars data pipeline |
| GevaExtract | `github.com/orengavish/GevaExtract` | Node.js | 5005 | Facebook S/R-line scraper + auto-trade submitter |

All three connect to **one IB Gateway** (paper account, port 4002) via **IBC**
(`C:\IBC`), which is not itself a git repo and lives outside all three projects.

**Everything is paper trading only.** IB port 4002 is the paper account. Port 4001 (live)
is never used by any of these projects — don't connect anything to it.

---

## 1. Prerequisites

- **Windows** (this whole stack assumes Windows paths, Task Scheduler, IBC's `.bat` launchers).
- **Python 3.11** — install from https://www.python.org/downloads/release/python-3119/,
  check "Add Python to PATH". Verify: `python --version`.
  - **Important**: all three Python-based process groups (CC2026's dashboard/broker/decider,
    Fetcher2026's fetchers/watchdogs) run against **one shared global Python install**, not
    per-project virtualenvs. On the current machine that's
    `C:\Users\galsh\AppData\Local\Programs\Python\Python311\python.exe` — always launch
    scripts with this fully-qualified path (or whatever the equivalent is on the new
    machine), not a bare `python`, which resolves per-shell and can silently pick up an
    unrelated interpreter missing required packages (this exact mistake crash-looped the
    bars-fetch watchdog on 2026-08-17 — see `Fetcher2026\OPERATIONS.md` §2a).
- **Node.js** (for GevaExtract) — https://nodejs.org, LTS version. Verify: `node --version`.
- **Git** — https://git-scm.com/download/win, accept defaults.

---

## 2. Clone the three repos

```powershell
git clone https://github.com/orengavish/CriticalCorallations2026.git C:\Projects\CriticalCorallations2026
git clone https://github.com/orengavish/Fetcher2026.git C:\Projects\Fetcher2026
git clone https://github.com/orengavish/GevaExtract.git C:\Projects\GevaExtract
```

Paths matter — several scripts and cross-repo references (e.g. GevaExtract's
`galao-db.js`/`insert-commands.py`, the cross-dashboard 🔗 menu) hardcode
`C:\Projects\CriticalCorallations2026\...` as an absolute path. Clone to exactly these
locations under `C:\Projects\` unless you're prepared to grep-and-fix every hardcoded path.

---

## 3. Install the shared Python environment

One `pip install` covers CC2026 and Fetcher2026 both (there is no working
`requirements.txt` at either repo's root as of this writing — this list was reconstructed
from `pip freeze` against the actual running production interpreter):

```powershell
pip install flask ib-insync pandas numpy pyarrow PyYAML python-dateutil pytz tzdata `
  requests psutil yfinance matplotlib scipy statsmodels peewee `
  beautifulsoup4 curl_cffi Werkzeug Jinja2
```

`ib-insync` (not raw `ibapi`/TWS API — the old `NEW_PC_SETUP.md` install path via
`C:\TWS API\source\pythonclient\setup.py install` is obsolete, don't do that) handles the
IB Gateway socket protocol directly; no separate TWS API installer is needed.

Verify:
```powershell
python -c "import flask, ib_insync, pandas, psutil; print('ok')"
```

---

## 4. Install GevaExtract's Node dependencies + Playwright

```powershell
cd C:\Projects\GevaExtract
npm install
npx playwright install chromium
```

GevaExtract uses `sql.js` (pure-JS SQLite) for its own `geva.db`, and Playwright
(non-headless Chromium) for the Facebook scraper.

---

## 5. Install IBC + IB Gateway

1. Download **IB Gateway** from https://www.interactivebrokers.com/en/trading/tws.php —
   use *Gateway*, not full TWS.
2. Download **IBC** (the automation wrapper that logs Gateway in unattended) from
   https://github.com/IbcAlpha/IBC/releases. Install to `C:\IBC\` — every config in this
   system assumes that exact path (`ib.ibc_startgateway_bat: "C:\\IBC\\StartGateway.bat"`
   in `trader/config.yaml`).
3. Create `C:\Users\<you>\Documents\IBC\config.ini` with your **paper account** login —
   this file has real credentials, is not in git, and must be created by hand on every new
   machine:
   ```ini
   TradingMode=paper
   IbLoginId=<your IB username>
   IbPassword=<your IB password>
   ```
   (Consult IBC's own README for the full option set — the two above are the minimum.)

4. **Known bug, fix immediately after install** (hit 2026-08-17, see
   `GevaExtract\OPERATIONS.md` §4 incident log for full detail): on current Windows
   builds, `cmd.exe` no longer resolves a bare executable name via the current directory,
   which breaks `C:\IBC\scripts\StartIBC.bat`'s Java-version detection (a `for /f` +
   backtick-pipe reading `java.exe -XshowSettings:properties` fails silently, then the next
   line throws `set was unexpected at this time`, aborting before Gateway ever launches).
   **Fix**: open `C:\IBC\scripts\StartIBC.bat`, find the `java_version` detection block,
   and replace the backtick-pipe pattern with a plain redirect to a temp file:
   ```bat
   "%JAVA_PATH%\java.exe" -XshowSettings:properties > "%TEMP%\ibc_java_version.txt" 2>&1
   for /f "tokens=1,2 delims== usebackq" %%A in (`findstr /C:"java.version =" "%TEMP%\ibc_java_version.txt"`) do set java_version=%%B
   del "%TEMP%\ibc_java_version.txt" >nul 2>&1
   ```
   This is not version-controlled (`C:\IBC` isn't a git repo) — reapply after any IBC
   reinstall/upgrade.

5. **Launch Gateway correctly**: use `C:\IBC\StartGateway.bat /INLINE /COLOR` (as the
   interactive user, not via a SYSTEM-context scheduled task — see §7 below for why that
   matters). A plain `Start-Process ... -ArgumentList "paper"` takes a different code path
   inside `StartGateway.bat` that spawns a nested `start` and is *also* broken, independent
   of the Java bug above.

6. Verify: after ~30-60s, `Get-NetTCPConnection -LocalPort 4002 -State Listen` should show
   the `java.exe` process listening, and `C:\IBC\Logs\IBC-*.txt` should show `IBC: Login has
   completed`.

---

## 6. Config files that need manual attention (not fully covered by git)

| File | Repo-relative path | Contains | In git? |
|---|---|---|---|
| `trader/config.yaml` | CC2026 | IB ports/client IDs, symbols, DB paths | Yes, but verify paths match your clone location |
| `back-trading/config.yaml` | CC2026 | Separate backtest-engine config | Yes |
| `trader/config.yaml` | Fetcher2026 | Same shape, own `paths.db`/`paths.history` — **known bug on the current machine: points at a dead, empty `galao.db` copy instead of CC2026's live one, fix before relying on Fetcher2026's priority queue** | Yes |
| `C:\Users\<you>\Documents\IBC\config.ini` | outside all repos | IB login credentials | **No — create manually, see §5** |
| `secrets.ini` | CC2026 root, Fetcher2026 root | Gmail SMTP creds for `send_email.py` alerting | **No — not present on the current machine either; email alerting is a known no-op everywhere until this is created.** Format: `[gmail]\nuser = ...\napp_password = ...` |
| GevaExtract's Facebook session | `GevaExtract\fb-profile\` | Logged-in Chromium profile (cookies etc.) | **No, gitignored — must re-authenticate on every new machine, see §8** |

---

## 7. Windows Task Scheduler — what should exist, what actually does (as of 2026-08-18)

| Task | Project | Status on current machine | Notes |
|---|---|---|---|
| `GalgoFetcher2026` | Fetcher2026 (old TRADES/BID_ASK pipeline watchdog) | Installed, but **misconfigured** — registered with `-UserId "SYSTEM" -LogonType ServiceAccount`, which resolves `%USERPROFILE%` to the SYSTEM profile, so IBC can't find `config.ini` (§5) and Gateway auto-restart never succeeds. Root cause of a real 19-day outage (2026-07-29 to 2026-08-17). **Fix before relying on it**: either change the task principal to the interactive user in `Fetcher2026\scripts\install_scheduler.ps1` before installing, or make IBC's config path explicit in `StartGateway.bat` instead of `%USERPROFILE%`-relative. Full detail: `Fetcher2026\OPERATIONS.md` §4. |
| `GalgoDashboard2026` | Fetcher2026 | Same installer, same fix needed before trusting it | |
| CC2026 auto-start | CC2026 | **Not installed** as of this writing | `scripts\install_scheduler.ps1` exists; run elevated: `Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\CriticalCorallations2026\scripts\install_scheduler.ps1'` |
| `GevaExtract\DailyExtract` | GevaExtract | Installed and working, daily Facebook scrape | `schtasks /Create /TN "GevaExtract\DailyExtract" /TR "C:\Projects\GevaExtract\run-daily.bat" /SC DAILY /ST 09:00 /RU "%USERNAME%" /RL HIGHEST /F` |
| `GevaAutoTrade` | GevaExtract | Installed and working, runs `auto-geva-scheduled.ps1` 3×/day (10:00/12:30/15:00 CT) | Ensures CC2026 + GevaExtract + session are all up, fetches lines if stale, builds/submits trades |

**Recommendation for a fresh machine**: install all of these fresh rather than assuming
the SYSTEM-context bug is fixed — apply the principal fix from the table above *before*
running `install_scheduler.ps1` for Fetcher2026, or you'll reproduce the exact 19-day
outage on the new machine too.

---

## 8. Data that must be re-created (not restorable from git)

- **`geva.db`** (GevaExtract) — never committed to git, exists only on disk. If migrating
  from an existing machine, copy the file directly (`GevaExtract\geva.db`); if starting
  fresh, run `node backfill.js` to pull historical Facebook posts.
- **GevaExtract's Facebook session** (`fb-profile/`) — run `node save-auth.js`, a real
  (non-headless) browser window opens, log into Facebook manually, session persists to
  `fb-profile/`. Must be redone whenever the session expires or Facebook logs it out.
- **Tick-CSV history** (Fetcher2026's output) — not in git (too large). Currently lands at
  `C:\Projects\Galgo2026\june\trader\data\history\` (a legacy path outside all three
  current repos — see `Fetcher2026\OPERATIONS.md`/`FETCHER2026.md` for why). Re-backfill
  via `python trader/fetch_scheduler.py --backfill` after Gateway is up; expect this to
  take days given IB's pacing limits.
- **`bars.db`** (CC2026) — `trader/data/bars.db`, 1-year-plus of 30-min OHLCV bars,
  currently ~85k rows/symbol after the 7-year Databento CSV merge (v5.01). Check whether
  it's tracked in git (large binary — verify with `git ls-files trader/data/bars.db`); if
  not, re-backfill via `python scripts/backfill_bars.py` then
  `python scripts/build_bars_normalized.py` / `build_bars_diffs.py`, and re-run
  `python scripts/import_7year_bars.py` if you have the Databento CSVs.
- **`galao.db`** (CC2026, shared with GevaExtract) — the live trading DB. Check whether
  it's tracked in git; if it is, treat the tracked copy as a point-in-time snapshot, not
  live state. `bars_watchdog_schedule.json` / progress DBs (Fetcher2026) are runtime state,
  not meant to be migrated — let them rebuild from a fresh `time_spent: 0` state on a new
  machine rather than copying stale values over.

---

## 9. Start-up order

```powershell
# 1. Gateway first — everything else needs this
C:\IBC\StartGateway.bat /INLINE /COLOR
# wait ~30-60s, confirm: Get-NetTCPConnection -LocalPort 4002 -State Listen

# 2. CC2026 dashboard (spawns broker/decider once session is started)
cd C:\Projects\CriticalCorallations2026
Start-Process python -ArgumentList "back-trading/trading_dashboard.py" -WindowStyle Hidden
Invoke-WebRequest -Method POST http://localhost:5003/api/session/start -UseBasicParsing

# 3. Fetcher2026 — both pipelines
cd C:\Projects\Fetcher2026
$py = "C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe"   # use the fully-qualified path, see §1
Start-Process $py -ArgumentList "trader/fetch_scheduler.py --backfill" -WindowStyle Hidden
Start-Process $py -ArgumentList "dashboard.py --real" -WindowStyle Hidden
Start-Process $py -ArgumentList "trader/bars_watchdog_supervisor.py" -WindowStyle Hidden
Start-Process $py -ArgumentList "trader/bars_status_server.py" -WindowStyle Hidden

# 4. GevaExtract
cd C:\Projects\GevaExtract
Start-Process "C:\Program Files\nodejs\node.exe" -ArgumentList "server.js" -WindowStyle Hidden
```

---

## 10. Verify everything is up

```powershell
foreach ($p in 4002,5003,5004,5005,5050) {
  $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conn) { "$p LISTENING" } else { "$p NOT LISTENING" }
}
Invoke-WebRequest http://localhost:5003/api/session/status -UseBasicParsing   # {"broker":"running","decider":"running",...}
Invoke-WebRequest http://localhost:5005/api/prices -UseBasicParsing
Invoke-WebRequest http://localhost:5004/api/status -UseBasicParsing          # bars-pipeline health per stage
Invoke-WebRequest http://localhost:5050/api/status -UseBasicParsing
```

Full per-project health-check detail: `Fetcher2026\OPERATIONS.md` and
`GevaExtract\OPERATIONS.md` (the latter covers CC2026 + Gateway too).

---

## 11. Permissions

All three repos run Claude Code with `"defaultMode": "bypassPermissions"` in
`.claude/settings.local.json` — no confirmation prompts. Set this the same way on a fresh
checkout if you want the same no-friction working style. CC2026's own settings also carry
a `PostToolUse` self-test hook pointing at a nonexistent path
(`C:/Projects/galgo2026/.claude/hooks/selftest_on_edit.py`) — harmless (fires a swallowed
error on every edit) but worth either fixing or removing on a fresh setup rather than
carrying the broken reference forward.
