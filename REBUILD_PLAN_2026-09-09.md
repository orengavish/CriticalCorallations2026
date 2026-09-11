# Trading System Rebuild Plan
> v1.3 — 2026-09-09 17:xx (month-scale monitoring view; staggered forced-exit; confirmed close-window logic already exists)

Ground truth for this plan comes from three places: (1) direct DB/`ib_events` forensics on
2026-09-08 done in today's session, (2) `CLAUDE_STATE.md` / `ORCHESTRATOR.md` /
`GevaExtract\OPERATIONS.md` (written 2026-08-18/23 — **~3 weeks stale, re-verify anything
load-bearing before relying on it**), and (3) the user's own requirements given verbally
across this session. Not a rewrite from zero — the goal is a plan for the *next generation*
of the system, reusing what already works.

---

## 1. What actually happened 2026-09-08 (root cause, not the symptom)

Previously suspected: two processes duplicate-launching (real, separately fixed today via
`lib/singleton_lock.py`, commit `3eb9822`, not yet pushed). **But that is not what produced
yesterday's storm.** Tracing the 18 MES commands that actually filled yesterday: their
`critical_line_id` is `NULL` and `source='geva_extract'` — they did not come from
`decider.py` at all.

Per `ORCHESTRATOR.md` / `GevaExtract\OPERATIONS.md`: **GevaExtract** (`C:\Projects\GevaExtract`,
Node.js, port 5005, its own separate project/Claude session) scrapes Geva's Facebook posts
for S/R lines and, on a Windows Scheduled Task (`GevaAutoTrade`, `auto-geva-scheduled.ps1`,
firing **10:00 / 12:30 / 15:00 CT = 15:00 / 17:30 / 20:00 UTC**), builds MES+MNQ brackets and
submits them by writing straight into the shared `commands` table
(`insert-commands.py`, `source='geva_extract'`).

The timestamps line up almost exactly: the order storm began **17:32 UTC**, two minutes after
the **17:30 UTC / 12:30 CT** scheduled run. `broker.py`'s `process_pending_commands()` drains
**every** `PENDING` row every 5s with **no cap and no check of how many orders are already
resting** on that symbol/side. GevaExtract's scheduled run evidently inserted a bracket-grid
batch (comment in `lib/db.py`: "geva_extract's bracket-grid fan-out" is an already-known
shape) large enough that broker fired dozens of orders into IB within a couple of seconds,
tripping **IB's own per-contract-per-side resting-order cap (15)** — error code 201
("Your account has a minimum of 15 orders working on either the buy or sell side for this
particular contract"), each followed by an auto-cancel (202). 849 code-201s / 537 code-202s
that day, the majority in this one ~4-minute window. Of the ~18 that got through before the
cap fully kicked in, 16 filled almost simultaneously; **14 of those 16 then lost their TP/SL
bracket legs to the same storm** and are still sitting `needs_review` (previously silently
hidden by the now-retired `RECONCILE_REQUIRED` status overwrite — also fixed today). Only 2
ever closed. A second wave (17:49-17:50, 127 "Not connected" errors) and a third
(18:25, new order-id sequence — a reconnect) compounded it.

**Root cause, precisely:** `broker.py` has no admission control between "row exists in
`commands` as PENDING" and "order sent to IB." It trusts every writer — `decider.py`
(has a dedup guard), Algo Lab (has `max_commands_per_submit`), and **GevaExtract (has
neither, and is a separate codebase/team)** — to never hand it more resting orders than IB
will accept. `ORCHESTRATOR.md`'s own golden rule #2 says broker is deliberately
**source-agnostic** by design ("If you add a fourth order-creation surface... give it its own
distinct `source` tag" — GevaExtract already complies with *that* rule; the gap is that
nothing enforces a volume/rate limit regardless of source). This is exactly why the fix
belongs in one place — `broker.py` — not in every writer.

**Open risk right now, unresolved from yesterday:** up to 14 FILLED-but-orphaned MES
positions and 71 stuck `SUBMITTED` rows from the 18:25 batch may still be live in IB paper.
Recommend checking IB paper positions/open orders directly before anything else below.

### 1.1 GevaExtract is not a problem to route around — it is a permanent co-existing input, and this system owns the safety contract for it

To be explicit, because it matters for how every phase below is framed: GevaExtract is not
a rogue or temporary integration to be tolerated, patched-around, or eventually removed.
It's a second, permanent source of trade ideas (Geva's Facebook-sourced S/R levels) that is
meant to keep running alongside this system's own `critical_line`/Algo Lab sources,
indefinitely. `GevaExtract\OPERATIONS.md`'s own system map already states the intended
division of labor precisely: *"Nothing here talks to IB directly except `broker.py` /
`decider.py`. GevaExtract only ever writes rows into `galao.db`; **CC2026 owns
execution**."* GevaExtract's job is to detect and propose. Everything about whether a
proposal is safe to actually send to IB — volume, pacing, resting-order limits, entry
buffers — is this system's job, unconditionally, regardless of which source produced the
row. That responsibility was never actually being met (broker.py has zero admission
control today, for any source), which is the real bug — not GevaExtract's existence, and
not GevaExtract's behavior. Phase 1 below is written on this basis: the fix is entirely on
this system's side, GevaExtract needs to change nothing, and it should be free to keep
writing exactly as it does today, at whatever volume, without ever being able to cause a
repeat of 2026-09-08 — because this system, not GevaExtract, is responsible for what
actually reaches IB.

---

## 2. Requirements from this session (consolidated)

1. Scale to **34 symbols** (30 stocks + 4 futures), using **control groups** — reuse the
   existing `research_ce` (treatment) vs `research_random` (control) source-tagging already
   in `critical_lines` (158 armed lines, 79 symbols, dated 2026-09-08 already exist under
   this exact scheme — this isn't new, it's already-generated data waiting to be used).
2. Target volume: **~120+ commands resting** pre-bracket-multiplication, **~500 after ×4
   bracket sizes** — deliberately *higher* than yesterday's real numbers (2 fills / 16
   "submitted" as visible on the broken dashboard), on the reasoning that only a few lines
   ever get touched and only a fraction of those fill, so the resting pool needs headroom.
   **This is in direct tension with the IB per-side cap (15) uncovered above** — §3.1 is a
   hard prerequisite before this number is safe to run live, paper or not.
3. Decision loop, "as fast as possible": check price vs. line, submit **only if direction is
   correct**, plus a **new safety buffer** (~2-4 points) between current price and entry
   before submitting, so nothing already-underwater goes out.
4. Monitoring: add submission date/time to the trade display; a proper
   submitted/pending/etc. sub-paneled dashboard, similar to a prior project's concept.
5. Confirm/build the duplicate-process guard — **done today** (`lib/singleton_lock.py`),
   needs push + a documentation update on the GevaExtract side (it's a separate launcher
   path, see §4).

---

## 3. Construction plan

### Phase 0 — Stop the bleeding (before anything else)
- [ ] Check IB paper positions/open orders directly for the 14 orphaned MES fills + 71 stuck
      `SUBMITTED` rows from yesterday. Close/cancel what's genuinely dangling.
- [ ] Push commit `3eb9822` (singleton lock + `needs_review` fix) — blocked on my end by the
      permission classifier, needs you to run `git push` or approve it.
- [ ] Kill the stray `trader/visualizer/app.py` (port 5001) process I started earlier for
      read-only inspection (PID 4400) — it's the documented "do not run" legacy dashboard.
      (My own attempt to stop it was blocked by the same classifier — one `Stop-Process` on
      your end clears it.)
- [ ] Be aware of `GevaAutoTrade`'s fixed fire times (15:00 / 17:30 / 20:00 UTC) — don't do
      live rollout testing inside those windows until Phase 1 is in place.

### Phase 1 — Admission control in `broker.py` (the actual fix, prerequisite for scaling up)
This is what makes "~500 resting trades" survivable instead of a repeat of yesterday at
larger scale.
- [ ] In `process_pending_commands()`, before submitting each command, check current
      resting-order count for that `(symbol, side)` — either query `ibc`'s own open orders,
      or count local `SUBMITTED`/unresolved-`FILLED` rows — and skip (leave `PENDING`) once
      near IB's cap (use a config value, default comfortably under 15, e.g. 10, to leave
      headroom for TP/SL child legs which count too).
- [ ] Add a per-poll-cycle submission cap (e.g. `max_submits_per_cycle` in `config.yaml`) so
      a sudden batch of 100+ new `PENDING` rows (from GevaExtract, Algo Lab, or the new
      control-group generator) drains gradually over several 5s cycles instead of firing all
      at once.
- [ ] This lives in `broker.py` only — **zero change needed in GevaExtract**, `decider.py`,
      or Algo Lab. Per §1.1, safety is this system's responsibility, not each writer's; the
      fix belongs at the one chokepoint every writer already routes through, so GevaExtract
      (and any future source) is protected automatically, permanently, without ever needing
      to coordinate with this repo again.
- [ ] Validate with `broker.py`'s existing `--dry-run` mode (`_run_broker_dry`, already
      built) against a synthetic 500-row PENDING batch before ever pointing it at real IB.
- [ ] **Re-check live price immediately before `place_bracket()`, not just once at row
      creation.** Confirmed bug, discussed this session: `process_pending_commands()` submits
      using whatever `entry_price`/`tp_price`/`sl_price` were computed whenever the row was
      *created* (by `decider.py`, GevaExtract, or Algo Lab) — it never calls
      `get_current_price()` again at the moment it actually calls IB. A row can sit `PENDING`
      for an unknown span (longer under a burst like 2026-09-08, or under the new
      per-cycle submission cap from this same phase) before broker reaches it, so the order
      can go out stale relative to where the market actually is by then. Fix: call
      `get_current_price()` right before submit, and apply the same entry-buffer check from
      Phase 2 against *that* fresh price, not the price at creation time — reject/re-queue
      (leave `PENDING`) if it's no longer valid rather than sending it anyway.

### Phase 2 — Entry safety buffer (new requirement)
- [ ] Add `orders.min_entry_buffer_points` (or per-asset-class, since futures "points" ≠
      stock ticks — MES tick=0.25, stocks tick=0.01, this needs to scale, not be one flat
      number) to `config.yaml`.
- [ ] Enforce it wherever a command is created: `decider.py generate_commands()`/`replenish()`
      already computes `entry_price` vs `current_price` via `determine_entry_type()` /
      `calc_bracket_prices()` in `lib/order_builder.py` — add the distance check right there,
      reject (skip, don't insert) if the line is closer than the buffer to current price in
      the direction that would matter.
- [ ] Per §1.1, enforce this as a second gate in `broker.py`'s admission control from
      Phase 1, not inside GevaExtract's `insert-commands.py` or any other writer — reject/
      leave-PENDING any command whose `entry_price` is already inside the buffer of the
      current cached price at submit time. Covers every source, including GevaExtract,
      automatically and permanently, with no cross-repo coordination required. (Trade-off:
      an already-too-close line still shows as a resting PENDING row rather than never being
      created — acceptable, since it's this system's job to refuse it at submit time either
      way, not to prevent it from being proposed.)

### Phase 2.5 — Staggered forced-exit near close (new requirement, confirmed real gap)
The entry-cutoff (30 min before close) and forced-exit (5 min before close, market orders)
windows **already exist and already match this requirement** —
`lib/session_clock.py`'s `is_entry_cutoff`/`is_forced_exit_time`, wired into
`decider.py`'s `run_replenishment_loop()`. Nothing to build there. The actual gap:
`decider.py:force_close_symbol()` ([decider.py:269-286](trader/decider.py#L269-L286)) loops
over every open position for a symbol and fires a market exit for each **back-to-back with
no delay** — the "dump everything at once" pattern flagged this session (most of these will
likely be losers at that point; hitting the market with all of them simultaneously is its
own avoidable cost).
- [ ] Add a minimum spacing (config: `shutdown.exit_stagger_seconds`, default per this
      session's ask ≈5s) between each `placeOrder(contract, mkt)` call inside that loop's
      `for cmd in filled:` — a plain `time.sleep()` between iterations is enough, this
      function already runs once per symbol per poll cycle, not on a tight hot path.
      Acceptable to briefly block `decider.py`'s loop while flattening — this only fires in
      the last 5 minutes of a symbol's session, once.

### Phase 3 — Scale to 34 symbols with control groups
- [ ] Add symbols to `config.yaml`'s `symbols:` list. 90 symbols already have armed
      `critical_lines` rows dated 2026-09-08 sourced `research_ce`/`research_random` sitting
      unused (candidates: ABBV, ABT, ACN, ADI, ADP, AMD, AMGN, AXP, BA, BAC, BKNG, BLK, BMY,
      BSX, CB, CI, CMCSA, CME, COP, CSCO, CVS, CVX, DE, DHR, DUK, ELV, ETN, GE, GILD, GOOG,
      GS, HD, IBM, ISRG, JNJ, KO, LIN, MA, MDLZ, ... — **need your pick of the specific 30**,
      this list is just alphabetical, not ranked by anything).
- [ ] Reuse `lib/algo_lab.py`'s grid machinery (`build_param_grid`, `submit_grid`,
      `max_param_combos`, `max_commands_per_submit`) as the generator — it already does
      exactly "many parameter combos, deterministically capped, tagged by source" which is
      the same shape as "control group vs treatment, ~120 resting, capped." Don't build a new
      generator; parametrize this one against the 34-symbol list and the `research_ce`/
      `research_random` source split.
- [ ] Verify per-symbol line freshness before trusting any of these 90: spot-checked MES
      itself has technical (`pivot`/`vwap`/`volume`/`ohlc`) lines last computed **2026-07-16**
      — 7+ weeks stale. If the new 30-symbol set relies on similarly stale technical lines
      (as opposed to the fresher `research_ce`/`research_random` rows, which do exist for
      09-08), that's a silent quality problem worth checking symbol-by-symbol, not assumed.
- [ ] Check IB Gateway's historical-data pacing budget (60 requests/10-min, **account-wide**,
      per `ORCHESTRATOR.md` golden rule #1) — going from 14 to 34 symbols multiplies
      `decider.py`'s per-pass price-fetch time roughly 2.4x (config comment already shows the
      14-symbol math: ~21s/pass at ~1.5s/fetch). `replenishment_poll_seconds` (currently 30s)
      will likely need raising, or price fetches need batching, or this budget gets blown
      account-wide (affecting Fetcher2026 and GevaExtract's own price calls too — this is a
      shared resource, not private to this repo).

### Phase 4 — Monitoring dashboard (replanned, was too vague — now grounded in the actual code)

**Correction to earlier in this session**: I was reading/serving `trader/visualizer/app.py`
(port 5001) — per `CLAUDE_STATE.md` this is explicitly "legacy/wrong." The real,
currently-used dashboard is **`back-trading/trading_dashboard.py` (port 5003)**,
**Trading → Submitted** tab (`api_submitted()` / `loadSubmitted()`).

**What's actually there today, checked directly against the code:**
- `/api/submitted` ([trading_dashboard.py:1184-1192](back-trading/trading_dashboard.py#L1184))
  hard-codes `WHERE source='trading_dashboard'` — per the source-count table already in
  `ORCHESTRATOR.md`, that source has **5 rows, total, ever**. The tab is blind to
  `critical_line`, `algo_lab`, and `geva_extract` — i.e. blind to essentially all real
  trading activity. **This, not a missing "concept," is why it doesn't look like a
  monitoring dashboard** — the one filter it silently applies excludes almost everything
  you'd actually want to watch.
- Columns shown: ID, Sym, Dir, entry Type (LMT/STP — not source), Entry, TP, SL, Bracket,
  Status, Fill price, "Updated" (`updated_at` truncated to `HH:MM`, **no date, no separate
  submitted/created timestamp at all**).
- Filter bar: a Refresh button and a 5s auto-refresh checkbox. Nothing else — no status
  filter, no date filter, no source/type filter, no summary tiles.

**Fixes, mapped directly to what you asked for:**
- [ ] **Source filter bug**: change `/api/submitted` to query all sources by default (drop
      the hard-coded `WHERE source='trading_dashboard'`), with source as a *selectable*
      filter instead of a silent hard-code.
- [ ] **Date/time submitted**: add `created_at` as its own column (full timestamp, not
      truncated) — currently only `updated_at` is even sent to the frontend, and only its
      time-of-day. Show both created_at (submitted) and updated_at (last change).
- [ ] **Today / past filter**: add a date-range control. The codebase already has this
      exact pattern elsewhere in the same file — `api_available_dates()`
      ([trading_dashboard.py:1195-1204](back-trading/trading_dashboard.py#L1195-L1204)) reads
      `date_from`/`date_to` query params with sensible defaults. Port that pattern into
      `/api/submitted`: a "Today" quick-filter (`date(created_at) = date('now')`) plus an
      explicit from/to range for "past."
- [ ] **Filter by status**: multi-select checkboxes (PENDING / SUBMITTING / SUBMITTED /
      FILLED / CLOSED / CANCELLED / ERROR / needs_review=1) — reuse the `status_in` query-param
      pattern already implemented in `trader/visualizer/app.py:262` (`api_commands`) even
      though that file is otherwise legacy; the filter logic itself is fine to port.
- [ ] **Filter by type/source**: dropdown or chip filter over `source` (`critical_line`,
      `algo_lab`, `geva_extract`, `research_ce`, `research_random`, `trading_dashboard`) —
      this is the fix that actually surfaces GevaExtract's and decider's real activity,
      which today's hard-coded filter hides entirely.
- [ ] **Small summary dashboard**: a stat-tile strip above the table — counts per status
      (today, and all-time), split by source, refreshed on the same poll. This is the same
      shape as `trader/visualizer/app.py`'s `/api/stats` (status counts, error/needs_review
      counts) — reuse that query logic, wire it into the real dashboard instead, extended
      with a `GROUP BY source` breakdown since that's the dimension currently invisible.
- [ ] Phase-panel layout (PENDING / SUBMITTED / FILLED / needs_review / CLOSED as visually
      distinct sub-panels, not just table rows) — reuse
      `C:\Projects\All\app\critical-line-live.html`'s SEARCHING/PENDING/MONITORING/EXITING
      color-coded panel concept as the layout reference, applied to the corrected/filterable
      data above rather than rebuilding the panel idea from scratch.
- [ ] **Month-scale history view (separate from the live table).** Confirmed: nothing has
      been purged — `commands` goes back to 2026-05-05, 68,722 rows total
      (`geva_extract` 47,680, `critical_line` 7,171, `random_mkt` 8,101, etc.) — so "what did
      we submit last month" is answerable in principle, but `/api/submitted`'s
      `ORDER BY id DESC LIMIT 200` (even once the source bug above is fixed) can only ever
      show a few hours of `geva_extract`-level volume, never a month. A raw table is the
      wrong shape at that scale. Add a second, aggregated view: counts per day × status
      (× source, once that filter exists) over an arbitrary date range — a small
      calendar/rollup, not 68,722 individual rows — with the existing filtered raw-row table
      as the drill-down once a specific day/range is picked. This is the same underlying
      counting query as the summary stat-tiles above, just grouped by day instead of
      collapsed to "today."

### Phase 5 — Rollout
- [ ] Full sequence dry-run first (`broker.py --dry-run`) with the real 34-symbol/500-row
      volume, to prove Phase 1's admission control actually holds the line before any real
      IB order goes out.
- [ ] Then a reduced live-paper test (e.g. current 14 symbols + Phase 1/2 fixes only) before
      flipping on the full 34.

---

## 4. Risks

| Risk | Detail | Mitigation |
|---|---|---|
| IB per-side resting-order cap (15) | Confirmed root cause of 09-08. Scaling to ~500 resting trades multiplies exposure, not just count. | Phase 1 admission control — hard prerequisite, not optional. |
| GevaExtract writes at its own volume/timing | Separate Node.js app/repo/Claude session, own scheduled task, writes straight into `commands`. This is by design and permanent (§1.1) — not something to constrain on GevaExtract's side. | This system (broker.py) owns admission safety for every source unconditionally. Phase 1's gate is source-agnostic — GevaExtract keeps writing exactly as it does today, unchanged. Tell that project's session the gate now exists, so nobody assumes CC2026 is still unprotected. |
| IB Gateway itself | "The fragile link" per `GevaExtract\OPERATIONS.md` — a prior outage (2026-08-15 to 08-17) came from a Java-version-detection bug in `StartIBC.bat` (fixed, but **not version-controlled**, lives only on disk at `C:\IBC`, will need reapplying if IBC is ever reinstalled). Historical-data pacing (60 req/10min) is account-wide. | Know the fragility exists; re-verify Gateway health before/during rollout (`ORCHESTRATOR.md` §3 health-check commands); don't add fetch load carelessly. |
| Duplicate processes | Two layers: (a) two OS processes both running broker.py/decider.py — **fixed today**, `lib/singleton_lock.py`. (b) `trading_dashboard.py`'s own session-start button and GevaExtract's scheduled task both call the same `/api/session/start` REST endpoint — worth confirming that endpoint itself doesn't double-spawn before relying on the lock as the only safety net. | Push today's fix; verify the REST endpoint is idempotent as a second check. |
| Stale critical lines | MES's own technical S/R lines are 7+ weeks stale (last: 2026-07-16). Unclear if this generalizes to other symbols in the 90-candidate pool. | Check freshness per symbol before trusting a line for the new 30-symbol set; prefer the `research_ce`/`research_random` rows (confirmed fresh, dated 09-08) over older `pivot`/`vwap`/`ohlc` sourced ones where both exist. |
| Docs used to build this plan are ~3 weeks stale | `CLAUDE_STATE.md` (08-18), `ORCHESTRATOR.md` (08-18), `GEVAEXTRACT` docs (08-23) — all explicitly warn not to be trusted at face value without a fresh check. | Re-run the `ORCHESTRATOR.md` §3 health-check block before Phase 0 actions; don't assume any specific port/task/version claim above is still exactly true. |
| Config drift on symbol expansion | `algo_lab.symbols`, `correlation.symbols`, and the top-level `symbols:` are three independent lists already (per `config.yaml`) — easy to update one and not the others. | When adding the 30 symbols, check all three lists, decide deliberately which should include the new set (top-level + algo_lab certainly; correlation probably not, it's futures-only by design). |

---

## 5. Still needed from you before Phase 3 can start (batched)

1. The specific **30 stock symbols** (candidate pool of ~90 already-armed exists — see
   Phase 3 — or a different criterion, e.g. largest-cap, sector spread, etc.).
2. Confirm **"control group"** = the existing `research_random` (control) vs `research_ce`
   (treatment) source split already in the DB — that's my working assumption; correct me if
   you meant something else by "votrolg."
3. Exact **safety buffer value** and unit — 2/3/4 *points* reads as futures-native; needs a
   decision on how that scales to stock ticks (0.01) vs MES/MNQ/MYM/M2K (0.25 tick, but very
   different point-value/volatility per contract).
4. Confirm the **~500 target** is total across all 34 symbols combined, not per-symbol.
