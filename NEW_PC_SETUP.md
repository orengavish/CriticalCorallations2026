# New PC Setup — Galgo June 2026

Follow these steps in order. Everything after "git clone" is self-contained.

---

## 1. Install Python 3.11

Download: https://www.python.org/downloads/release/python-3119/
- Choose **Windows installer (64-bit)**
- Check **"Add Python to PATH"** during install
- Verify: `python --version` → should show `3.11.x`

---

## 2. Install Git

Download: https://git-scm.com/download/win  
Accept all defaults.

---

## 3. Clone the repo

```
git clone https://github.com/orengavish/galgo2026.git C:\Projects\galgo2026
cd C:\Projects\galgo2026
```

---

## 4. Install Python dependencies

```
pip install -r june/requirements.txt
```

---

## 5. Install IB TWS API (ibapi)

`ibapi` is not on PyPI — must be installed manually from IB.

1. Download **TWS API** from: https://interactivebrokers.github.io/
   - Get the Windows installer (e.g. `twsapi_macunix.1019.05.zip` or similar)
2. Run the installer → installs to `C:\TWS API\`
3. Install the Python package:
   ```
   cd "C:\TWS API\source\pythonclient"
   python setup.py install
   ```
4. Verify: `python -c "import ibapi; print('ok')`

---

## 6. Install IBC (IB Gateway controller)

IBC starts/stops the IB Gateway on demand. Download from:
https://github.com/IbcAlpha/IBC/releases

Install to `C:\IBC\` — the config.yaml assumes `C:\IBC\StartGateway.bat`.

Configure IBC for **paper trading** mode (port 4002).

---

## 7. Install IB Gateway

Download from: https://www.interactivebrokers.com/en/trading/tws.php  
Use **Gateway** (not full TWS). Log in with paper account credentials.

---

## 8. Verify config.yaml paths

Open `june/trader/config.yaml` and confirm:
- `ib.ibc_startgateway_bat: "C:\\IBC\\StartGateway.bat"` — matches your IBC install
- `paths.db: data/galao.db` — no changes needed
- `google_drive.enabled: false` — leave false until credentials are ready

---

## 9. Backfill all tick data from IB

The CSV history is NOT in git (too large — re-fetch from IB).
The `galao.db` IS in git (commands/trades history preserved).

Start IBC gateway first:
```
C:\IBC\StartGateway.bat
```
Wait ~30 seconds for it to be ready, then:

```
cd C:\Projects\galgo2026\june
python trader/fetcher.py --from-date 2026-06-16 --bid-ask
```

This fetches TRADES + BID_ASK for all 4 symbols (MES, MNQ, MYM, M2K) from June 16 to today.
Will take a while — IB paces requests at ~1 request per 10 seconds.

Check progress / what's missing:
```
python trader/fetch_priority.py
```

---

## 10. Set up daily Task Scheduler entry

So the fetcher runs automatically at 17:30 CT (23:30 UTC) every day:

1. Open **Task Scheduler** → Create Basic Task
2. Name: `GalaoFetcherJune`
3. Trigger: Daily at **23:30** (UTC)
4. Action: Start a program
   - Program: `python`
   - Arguments: `trader/fetch_scheduler.py --run-now`
   - Start in: `C:\Projects\galgo2026\june`
5. Save

Or run manually any time:
```
python june/trader/fetch_scheduler.py --run-now
```

---

## 11. Smoke-test

```
cd C:\Projects\galgo2026\june
python -c "from lib import db, ib_client, config_loader; print('lib ok')"
python -c "from trader import fetcher, broker, decider; print('trader ok')"
python -c "from back_trading import bt_engine, bt_db; print('bt ok')" 2>/dev/null || python -c "import importlib.util; print('check back-trading imports manually')"
```

---

## Summary checklist

- [ ] Python 3.11 installed, in PATH
- [ ] Git installed
- [ ] `git clone` done to `C:\Projects\galgo2026`
- [ ] `pip install -r june/requirements.txt`
- [ ] `ibapi` installed from TWS API installer
- [ ] IBC installed to `C:\IBC\`
- [ ] IB Gateway installed, paper account login verified
- [ ] `config.yaml` paths confirmed
- [ ] Gateway started, `fetcher.py --from-date 2026-06-16 --bid-ask` run
- [ ] Task Scheduler entry `GalaoFetcherJune` created
- [ ] Smoke-test passed
