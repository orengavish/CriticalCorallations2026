# Research runbook — two-winning-reasons experiment (prepared overnight 2026-09-06)

## ⚠️ Read this first: 2026-09-07 is a US market holiday (Labor Day)

`may_scheduler.py` has 2026-09-07 hardcoded in its own `_HOLIDAYS` set — this is real:
Labor Day 2026 falls on Monday, September 7. CME equity-index futures (MES/MNQ/MYM/M2K)
run at most a thin, closed-pit overnight session that day, not a normal trading day.
**If you want a normal-volume test, run this on Tuesday 2026-09-08 instead** — no changes
needed, just re-run `prep_research_lines.py` with no `--date` (it defaults to whatever day
you actually run it on) and everything below still applies verbatim.

Everything below is already prepared and tested for 2026-09-07 specifically, in case you
want to watch a live (thin) holiday session anyway.

## What's already done (tested, not yet live)

1. **Critical lines inserted** into `trader/data/galao.db` (`critical_lines` table),
   armed, for the two backtested reasons that showed a consistent edge across every
   bracket size (`PREVIOUS_DAY_LOW`, `PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE` — see
   CriticalExtraction's `summarize_sweep.py`). For 2026-09-07, only 2 lines qualified:
   - M2K RESISTANCE @ 2979.4
   - MYM RESISTANCE @ 53773.0
   Each also got a matched random-price control line (`source='research_random'`) at the
   same distance from the prior day's close, for the algo-vs-random comparison. The
   "opposite direction" role needs no separate line — `decider.py` already generates
   both a BUY and a SELL command per line, unmodified, which *is* the opposite-direction
   control.
   Re-run anytime with: `python trader/scripts/prep_research_lines.py [--date YYYY-MM-DD] [--dry-run]`

2. **`config.yaml` changed** (backup at `trader/config.yaml.bak_20260906`):
   - `symbols:` expanded from `[MES]` to `[MES, MNQ, MYM, M2K]`
   - `orders.active_brackets` changed from `[2, 4]` to `[4, 8, 16, 32]` (backtesting found
     no reason to trade 1-2)

3. **A real bug found and fixed**: `decider.py` was rounding every symbol's entry/TP/SL
   at the flat `cfg.orders.tick_size` (0.25, MES's tick) — harmless while `symbols:[MES]`
   only, but MYM (real tick 1.0) and M2K (real tick 0.10) would have gotten silently
   mis-rounded, non-tradeable prices the moment they went live. Fixed by adding a shared
   `get_tick_size()`/`TICK_BY_SYMBOL` to `lib/order_builder.py` (broker.py already had its
   own private copy of this table — decider.py had none). `lib/order_builder.py`,
   `trader/decider.py`, and `trader/session.py` self-tests all pass with the fix in.

## What's NOT done — needs you

1. **IB Gateway is not running** (checked tonight — connection refused on port 4001).
   Starting it needs your login/2FA, which I can't do. Start it in paper mode before
   anything else.
2. **Nobody has started the actual session.** `broker.py --self-test` couldn't complete
   past this either, for the same reason (needs a live IB connection).
3. **This exact config (4 symbols, this bracket set, these specific research lines) has
   never been run live before tonight.** Watch the first session yourself rather than
   walking away — this system's own git history has real incidents (naked position,
   425 stale orders) from scope changes that weren't supervised on their first run.

## How to start it

```
cd C:\Projects\CriticalCorallations2026\trader
# 1. Start IB Gateway in paper mode (your login/2FA) if not already running.
# 2. Regenerate today's lines fresh (safe to re-run — idempotent replace):
python scripts\prep_research_lines.py
# 3. Boot the session (broker + decider, supervised, auto-restart on crash):
python -c "from session import SessionManager; from lib.config_loader import get_config; s = SessionManager(get_config()); s.start()"
```
(`session.py`'s own `__main__` only exposes `--self-test` — it's driven programmatically
or via `may_scheduler.py`'s daily automation, which won't fire today because of the
holiday check above. The one-liner above calls the same `start()` the scheduler would.)

## How to monitor

- **Visualizer dashboard**: `http://localhost:5001` (per `config.yaml`'s `visualizer:`
  section) if the dashboard process is running (`python dashboard.py --real` from
  Fetcher2026, or this project's own equivalent — check which one's already up: one
  was running tonight under PID 24080).
- **Logs**: `trader/logs/broker.log`, `trader/logs/decider.log`, `trader/logs/session.log`.
- **Direct DB check** (from `trader/`): `python -c "import sqlite3; con=sqlite3.connect('data/galao.db'); con.row_factory=sqlite3.Row; [print(dict(r)) for r in con.execute(\"SELECT * FROM commands WHERE source='critical_line' ORDER BY id DESC LIMIT 20\")]"`
- **Stop cleanly**: write `SESSION=SHUTDOWN` to `system_state` (same DB), or however the
  dashboard's stop control does it — never kill -9 the broker process directly.

## Known limitation in tonight's prep

The random-control price uses the **prior day's close** as a stand-in for the real
session open (which isn't knowable before the session starts) — a documented
simplification, not a bug. For MYM specifically, tonight's random draw happened to land
exactly on the real line's own price (53773.0) — a coincidence of the RNG, not an error;
it means today's MYM random-control isn't actually distinct from the real line.
