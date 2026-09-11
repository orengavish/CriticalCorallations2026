# Start-of-Day Checklist
> v2.0 — 2026-09-11 (rewritten — Day Start panel now does steps 3/4/6; futures schedule
> claim in step 7 corrected; DeciderDailyRestart fixed)

Run this every trading morning, in order. Most of it is now a single dashboard panel
(**Broker tab → Day Start**, http://localhost:5003) instead of manual commands — the
panel's own buttons gray out automatically when there's nothing for them to do, so a
grayed button IS the answer to "do I need this."

## 1. Git up to date
```
git status
git fetch && git status
```
Confirm: no uncommitted changes you don't recognize, branch not diverged from the remote
(or you know why it is).

## 2. Preflight (automated)
```
python trader/preflight.py
```
Checks IB LIVE port 4001, IB PAPER port 4002, a live price fetch, and a DB read/write —
hard-fails (non-zero exit) if any of the four don't pass. Fix any FAIL before continuing.

## 3. Day Start panel — Verify
Open the dashboard's Broker tab. Click **Verify** (or just look — it auto-refreshes every
15s). Read the summary line: stale CANCELLED noise, stale PENDING/SUBMITTED, needs_review
count, and IB's real resting-orders/open-positions count.

**Note from 2026-09-10**: at 34 symbols, dozens to ~2,000 resting orders during an active
session is normal churn, not a leak — it settles to a small number (~27-90) at rest. Zero
open positions overnight is the actual "clean" signal; a nonzero resting-order count alone
isn't.

- **Clean DB noise** grays out on its own once there's nothing stale to archive — click it
  when it's NOT grayed out, don't run this blind.
- **Cancel & Flatten IB** grays out once IB shows zero resting orders/positions. If it's
  clickable and you weren't expecting resting exposure, look before clicking — it cancels
  ALL resting orders and flattens ALL positions (LIVE+PAPER), immediately.
- There is no `/api/reset` button on this panel deliberately — that endpoint wipes
  `commands`/`positions`/`ib_events`/`system_state` entirely (trade history, not just
  noise). Only run it manually, only when you've decided you actually want a full wipe.

## 4. DB check
Covered by Verify's `needs_review` count above. If non-zero, look at those commands
directly:
```
sqlite3 trader/data/galao.db "SELECT id, symbol, status, review_note FROM commands WHERE needs_review=1;"
```

## 5. No open issues waiting
```
cat C:/Projects/All/tasks-i-gave-user.md
```
Confirm `Counter: 0`. If not, those are blocking items only you can act on.

## 6. Generate today's critical lines — Day Start panel buttons
- **Extract Futures Lines** — runs Geva's real-line import (auto-read from GevaExtract's
  `geva.db`) + the futures Algo 1/2 research-line prep, for all 4 futures symbols in one
  click. Stays clickable all day as long as Geva hasn't posted real lines yet (safe to
  re-click — the algo half is idempotent, and a re-check might finally catch a late post);
  grays out once Geva's real lines land for today.
- **Extract Stock Lines** — runs the stock Algo 1/2 research-line prep for all 30 stocks.
  Deterministic per date, so it grays out permanently once run today — no benefit to a
  second click.
- The panel's summary line shows exactly how many of the 4 futures (with a per-symbol
  count) and how many of the 30 stocks actually got a line today — a gap like
  2026-09-11's (only 2/4 futures symbols got a line, traced to a dead upstream
  Fetcher2026 bar-fetch pipeline stuck at 2026-09-04) is now visible at a glance instead
  of requiring a manual DB query.
- Manual fallback, if the dashboard is down:
  ```
  python trader/scripts/import_geva_manual_lines.py
  python trader/scripts/prep_research_lines.py --date $(date +%F)
  python trader/scripts/prep_research_lines_stocks.py --date $(date +%F)
  ```

## 7. Trading schedule — corrected 2026-09-11, was wrong in v1.0
**Futures do NOT gate on market-open at all.** `decider.py`'s `run_session_start()`
generates futures commands unconditionally the moment decider starts (no wait) —
previously this doc claimed "futures start at their own CME open," which was never
actually true in code and left a real gap (decider restarts at 8:00 AM IL, then sat idle
for ~9h doing nothing for futures until the shared 17:00 IL gate cleared). Stocks still
wait for the regular equity open + 30min delay (→ ~17:00 IL).

Two manual overrides exist on the Day Start panel for when you need it now rather than
waiting for the next natural restart:
- **Force Futures Now** — restarts decider so it re-scans for new futures lines
  immediately. Grays out whenever decider is already running (there's nothing to force —
  futures already generate on every start).
- **Force All Symbols Now** — same, but also skips the stock-open wait for today only.
  Grays out once already forced today, or once the stock gate has naturally passed.

`DeciderDailyRestart` (08:00 IL, the daily trigger that makes decider pick up a new day's
date and lines) was found broken — registered out-of-band, failing every day with
`ERROR_FILE_NOT_FOUND` — and is now fixed and properly registered via
`scripts/install_scheduler.ps1` (re-run as Administrator if it's ever suspect again).
