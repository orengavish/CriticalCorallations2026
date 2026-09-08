# Research runbook — two-winning-reasons experiment

Filename kept from when this was written (2026-09-06 night); target trading day is now
**2026-09-08** (2026-09-07 was Labor Day — see below). Current as of 2026-09-08 morning.

## Current live status

- **IB Gateway: both ports confirmed working** (checked 2026-09-07 evening with a real
  functional test, not just a port check — LIVE fetched a genuine MES price, PAPER
  connected and accepted/cancelled real test orders for both a future (MES) and a stock
  (AAPL)). Starting/stopping Gateway (port 4001, needs 2FA) stays the user's own step —
  never assume it's up, always verify.
- **Scope: 14 symbols** — 4 futures (MES, MNQ, MYM, M2K) + 10 stocks (AAPL, MSFT, NVDA,
  GOOGL, AMZN, META, AVGO, TSLA, LLY, V). This was deliberately scaled back from a
  briefly-tried full 104-symbol universe (4 futures + all 100 stocks) at the user's own
  request the night before first live run — smaller, well-understood blast radius.
  JPM skipped (no armed line today); BRK.B skipped (known IB dotted-class-share ticker
  quirk, avoided for a first run). Re-expanding later is just editing `config.yaml`'s
  `symbols:` list — the other ~90 stocks' lines are still armed in the DB, nothing to
  regenerate.
- **Critical lines**: armed in `trader/data/galao.db`'s `critical_lines` table for
  2026-09-08, filtered to the two backtested reasons that held up across every bracket
  size in CriticalExtraction's sweep (`PREVIOUS_DAY_LOW`, `PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE`).
  Every real line has a **matched random-price control** armed alongside it (same
  distance from a reference price, opposite RNG draw) — confirmed 1:1 real:random for
  every one of the 14 active symbols that has a line today. The "opposite direction"
  role needs no separate line: `decider.py` already fires both a BUY and a SELL per
  line automatically (see below).
  Regenerate anytime (idempotent replace, safe to re-run):
  - Futures: `python trader/scripts/prep_research_lines.py --date 2026-09-08`
  - Stocks: `python trader/scripts/prep_research_lines_stocks.py --date 2026-09-08`
    (needs `mst_data/daily_bars.py --fetch-all` + `scheduler/morning_lines.py` to have
    been run first, from `MultiSymbolTrader`, against fresh daily bars)
- **`config.yaml` changed** (backup at `trader/config.yaml.bak_20260906`):
  `orders.active_brackets`: `[2,4]` → `[4,8,16,32]` (backtesting found no reason to
  trade 1-2). `decider.replenishment_poll_seconds`: `10` → `30` (14 symbols x
  ~1.5s/price-fetch ≈ 21s/pass; 30s comfortably fits — see inline comment in
  config.yaml for the full sizing history, since this number moved twice tonight as
  scope changed from 4 → 104 → 14 symbols).

## Stop orders — already live, not something to add

`decider.py` automatically toggles every command between LMT and STP based on current
price vs. the line, using the real IB `StopOrder` type (`lib/order_builder.py`'s
`determine_entry_type()`/`build_bracket()`) — this is unrelated to and unaffected by the
backtest's decision to drop a "stop-entry-style" *research role* (which was about
simulation complexity in a historical tick replay, not about whether the live system can
place real stop orders). Nothing to build here; it's been part of every test tonight.

## Bugs found and fixed along the way

1. **Per-symbol tick size**: `decider.py` (and later two more call sites in
   `broker.py`) were rounding every symbol's entry/TP/SL at a flat tick size (0.25,
   MES's own) — harmless with `symbols:[MES]` only, would have silently mis-rounded
   MYM (real tick 1.0), M2K (0.10), and every stock (0.01) once added. Fixed at the
   root: `lib/order_builder.py`'s `get_tick_size()`/`TICK_BY_SYMBOL`, now the single
   source every caller uses.
2. **Stock contract support**: `lib/ib_client.py`'s `get_contract()` only ever built
   `Future()` contracts. Now branches by symbol — the 4 known futures unchanged,
   everything else resolves as `Stock(symbol, "SMART", "USD")`.
3. **Stale import**: `MultiSymbolTrader/mst_data/daily_bars.py --fetch-all` referenced
   `data.symbols` (pre-rename module name); fixed to `mst_data.symbols`.

## New: 30-minute entry cutoff + 5-minute forced-flatten

Mirrors the backtest's own session-window rule (`CriticalExtraction/backtest/simulate_trades.py`).
`lib/session_clock.py` (new) tracks each symbol's own market-close time/timezone
(futures: America/Chicago 16:00; stocks: America/New_York 16:00 — tracked
independently, not one shared clock). Wired into `decider.py`:
- `generate_commands()`/`replenish()` stop generating new entries for a symbol once
  within 30 minutes of *that symbol's own* close.
- A new `force_close_symbol()` market-flattens any still-open position for a symbol
  once within 5 minutes of its close — deliberately symbol-scoped (not an account-wide
  `reqGlobalCancel`, unlike `daily_paper_session.py`'s own end-of-session
  `force_close_all`), so symbols on different close clocks don't interfere with each
  other.

## How to start it

```
cd C:\Projects\CriticalCorallations2026\trader
# 1. Confirm IB Gateway is up (both live 4001 and paper 4002) -- your own step.
# 2. Regenerate today's lines fresh if it's a new day (safe to re-run):
python scripts\prep_research_lines.py --date 2026-09-08
python scripts\prep_research_lines_stocks.py --date 2026-09-08   # after re-fetching stock data for the new date
# 3. Boot the session (broker + decider, supervised, auto-restart on crash):
python -c "from session import SessionManager; from lib.config_loader import get_config; s = SessionManager(get_config()); s.start()"
```
(`session.py`'s own `__main__` only exposes `--self-test` — it's driven programmatically
or via `may_scheduler.py`'s daily automation. The one-liner above calls the same
`start()` the scheduler would.)

## How to monitor

- **Visualizer dashboard**: `http://localhost:5001` (per `config.yaml`'s `visualizer:`
  section) if the dashboard process is running.
- **Logs**: `trader/logs/broker.log`, `trader/logs/decider.log`, `trader/logs/session.log`.
- **Direct DB check** (from `trader/`): `python -c "import sqlite3; con=sqlite3.connect('data/galao.db'); con.row_factory=sqlite3.Row; [print(dict(r)) for r in con.execute(\"SELECT * FROM commands WHERE source='critical_line' ORDER BY id DESC LIMIT 20\")]"`
- **Stop cleanly**: write `SESSION=SHUTDOWN` to `system_state` (same DB), or however the
  dashboard's stop control does it — never kill -9 the broker process directly.

## Known limitations, still true

- The random-control price uses the **prior day's close** as a stand-in for the real
  session open (unknowable before the session starts) — a documented simplification,
  not a bug. Occasionally the random draw lands exactly on the real line's own price
  (RNG coincidence, e.g. MYM on 2026-09-07's fixture) — that pair just isn't a distinct
  comparison that day.
- `galao.db` is committed to git and is now ~80MB — GitHub has started warning it's
  approaching their recommended/hard size limits. Worth watching if it keeps growing.
- `position_manager.py`, `preflight.py`, and the P&L multiplier-scaling logic in
  `broker.py` have not been specifically audited for stock-readiness (only the paths
  actually exercised tonight — contract resolution, tick sizing, order submission —
  were verified).
