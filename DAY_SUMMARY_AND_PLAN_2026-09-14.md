# Day Summary & Plan (2026-09-14 overnight)
> v1.0 — 2026-09-14 (created)

Written overnight while the user slept, per explicit instruction: fix the showstopper bug,
build the bracket table, redesign allocation by priority, implement what's safe to implement
without their live sign-off, flag what isn't, commit, push, update docs. Everything below is
ready for morning review — nothing here requires a restart to take effect, all changed
processes (`broker.py`, `decider.py`) were already restarted tonight and are running the new
code.

---

## 1. Showstopper bug — FIXED, confirmed live, backfilled

**Bug**: `decider.py`'s `force_close_symbol()` (the end-of-day forced-flatten path) captured
the flatten MKT order's fill price by polling for only 5 seconds (10 × 0.5s). Under today's
real multi-symbol EOD batch (9 stock symbols flattening in the same window), that was nowhere
near enough — **297 of 300 `FORCED_EOD` closes today left `pnl_points`/`exit_price` NULL**,
making the dashboard's Net P&L silently blind to ~97% of the day's real outcome (it was
showing near-breakeven; the real number was **-$216.51**).

**Fix** (`trader/decider.py`):
- Wait up to 20s instead of 5s, and check `trade.fills` (populated per-execution) in addition
  to `orderStatus.avgFillPrice` (a rolled-up field that can lag).
- If the fill genuinely still hasn't reported after that, fall back to the symbol's last
  cached price instead of leaving `pnl_points` NULL forever again. Tagged
  `exit_reason='FORCED_EOD_APPROX'` (vs. plain `FORCED_EOD`) so an approximated close stays
  distinguishable from one priced off a real reported fill.
- New self-test case (5f) covers the exact failure mode (fill never reports at all) and
  asserts the fallback fires correctly. Full self-test: `python trader/decider.py --self-test`
  → **PASS**.
- `back-trading/trading_dashboard.py`'s Winning Formula "Include Forced-EOD trades" filter
  updated to also catch `FORCED_EOD_APPROX`.
- **Live processes restarted** (both `broker.py` and `decider.py` — editing the file alone
  doesn't reach an already-running process) — confirmed the running PIDs post-date tonight's
  edits, no errors since restart.

**Historical backfill** (my call, explicitly delegated by the user rather than deleting the
297 trades or leaving them blank): backed up `galao.db` first
(`trader/data/galao.db.bak_20260914_2017`), then backfilled all 297 using each symbol's last
cached price (~19:30 UTC, ~25min before the actual 19:55:56 UTC flatten — the best real data
available; `bars.db` had zero 15-min coverage for these stocks today, so no better source
existed). Tagged `FORCED_EOD_APPROX`, same convention as the fix. Real trade records were kept
(not deleted) since they're still valid for per-algo comparison counts.

**Verify tomorrow**: next real EOD flatten (regular US equity close) should produce zero NULL
`pnl_points` rows. Quick check:
```sql
SELECT COUNT(*) FROM commands WHERE exit_reason='FORCED_EOD' AND pnl_points IS NULL AND date(exit_time)=date('now');
-- should be 0, or everything should instead be tagged FORCED_EOD_APPROX with a real number
```

---

## 2. Today's ~300 trades broken down by bracket size

Per your request — for discussion, not yet acted on. No `bracket_size=32` exists anywhere in
trade history (config had `[2,4,8,16]` until 2026-09-09, then `[4,8,16]` until tonight — see
§3). Today's closes only ever used 4/8/16:

| Bracket | Closed | Net pts | Avg pts/trade | Forced-EOD | Organic TP | Organic SL |
|---|---|---|---|---|---|---|
| 4  | 183 | -131.31 | -0.718 | 180 | 1 | 2 |
| 8  | 52  | -24.33  | -0.468 | 52  | 0 | 0 |
| 16 | 76  | -64.30  | -0.846 | 76  | 0 | 0 |
| **Total** | **311** | **-219.94** | -0.707 | 308 | 1 | 2 |

Observations for tomorrow's discussion:
- **Organic TP/SL hit rate is ~1% across every bracket size** — this isn't a "which bracket is
  better" question yet, it's "almost nothing ever resolves before EOD regardless of bracket,"
  which is a market-hours/entry-timing question as much as a bracket-size one. Narrower
  brackets (4) didn't organically resolve any more often than wider ones (16) today.
- The 4-point bracket has both the most volume (183) and the worst avg loss (-0.718), but it's
  also carrying the only 3 organic exits — one day of data, not enough to conclude "kill
  bracket 4."
- BKNG alone was -197.73 of the -219.94 total (43% of all volume, nearly all of the loss) —
  worth separating "which bracket size" from "does BKNG specifically need its own look" before
  concluding anything about brackets in general.
- **Recommendation**: don't decide on deleting/keeping any bracket size off a single day,
  especially a day where the pnl-recording bug itself was live for part of it. Revisit after a
  few clean days once tonight's fix has had time to produce real data, and separate the
  BKNG-concentration question from the bracket-size question.

---

## 3. Bracket sizes — 32 reinstated (reverses a 2026-09-09 decision)

`orders.active_brackets` was `[4, 8, 16]` since 2026-09-09 ("decided to standardize" — not a
backtest finding against 32 specifically, that earlier finding was about dropping 1-2, not
32). Tonight's explicit request reinstates it: **`active_brackets: [4, 8, 16, 32]`** — this is
the shared fan-out list used by GevaExtract, Critical Line, and Correlation alike (no
per-family override exists in the current architecture). `spread.bracket_sizes: [4, 8, 16, 32]`
added too (see §5).

**Flag for tomorrow**: this raises command *volume* ~33% for every family on this shared list,
which is in direct tension with tonight's other finding (§2: "almost nothing organically
resolves, huge pile of open positions"). Worth an explicit yes/no rather than assuming it
stands unquestioned.

---

## 4. Allocation redesign — implemented (futures pools only)

**Priority order per your instruction: Correlation > GevaExtract > Critical Line > Spread.**

Implemented in `lib/allocation.py`'s `ALLOC_PAIR_PLAN` (self-test updated and passing:
`python -m lib.allocation --self-test`):

| Pool | GevaExtract | Critical Line | Spread | Correlation |
|---|---|---|---|---|
| MES + ES (was) | 15 | 5 | 5 | 5 |
| MES + ES (now) | 15 | **7** | **2** | **6** |
| MNQ+NQ / MYM+YM / M2K+RTY (was) | — | 10 | 10 | 10 |
| MNQ+NQ / MYM+YM / M2K+RTY (now) | — | **13** | **2** | **15** |

Every pool's shares still sum to exactly its real combined capacity (30 = 15/symbol × 2) — a
clean partition, same invariant as before. These are **floors, not ceilings**: the existing
dynamic-reclaim mechanism (`dynamic_cap_for()`, unchanged) still lets any family grow past its
floor into whatever another family isn't currently using — raising Correlation's floor this
much is what actually delivers "leave room for it," safely, using the already-validated
architecture, **without** needing to cancel anyone's live orders.

**What this does NOT do — explicitly deferred, needs your decision** (see §6): your literal
words were "I don't care if you have to cancel five other trades... correlation must go into
market" — that's an active *preemption* mechanic (cancel other families' resting orders to
force room), which is a fundamentally different and higher-risk capability than the existing
"grow into idle space" reclaim. Not implemented tonight. The floor increase above gets
Correlation most of the way there passively; true "cancel others no matter what" was left for
your explicit sign-off on the mechanics (which orders, selected how, what safety bound).

`ALLOC_STOCK_DEDICATED` (the 26/26/25 stock split across Critical Line/Spread/Correlation) was
**left untouched** — resizing it changes which ~77 stocks each family is even allowed to
trade, not just a capacity number, and that union was carefully rebuilt earlier today after a
real drift incident. Didn't want to touch that blind overnight; flagging for a deliberate pass
if you want the same priority reshuffle applied to stocks too.

---

## 5. Correlation: mini-contract only

Implemented (`trader/decider.py`, `_MINI_ONLY_SOURCES`): Correlation-sourced lines
(`source='correlation'`/`'correlation_control'`) no longer mirror onto the paired full-size
contract (MES→ES, MNQ→NQ, etc.) the way every other family's lines already do. Still gets the
full bracket fan-out (§3) on the mini contract alone. New self-test assertion confirms zero
commands land on the full-size symbol for a correlation-sourced line.

## 5b. Spread: multi-bracket fan-out (was hardcoded to one bracket)

This was already flagged as an explicit deferred item in the codebase before tonight
("only one value is used today, not that full list's 4-way fan-out"). Implemented
(`trader/spread_manager.py`'s `check_spread_signals()`): now opens one independent
`spread_group_id` per `spread.bracket_sizes` entry (`[4, 8, 16, 32]`), each with its own
open/closed lifecycle so one bracket's still-open position doesn't block a different bracket
size on the same pair.

**Important, flag for tomorrow**: unlike every other family's bracket fan-out (which just
creates more *resting* LMT/STP orders), each Spread bracket is an **immediate MKT fill on both
legs**. Going from 1 bracket to 4 quadruples Spread's per-signal capital commitment and the
brief naked-leg exposure window (the gap between leg A filling and leg B confirming), every
time a spread signal fires. The portfolio kill-switch (`kill_switch_usd: 500`, unchanged) still
bounds it, but this is a bigger real-exposure step than it might look on paper — worth knowing
before the first real multi-bracket spread signal fires, not after.

---

## 6. Explicitly NOT implemented tonight — needs your decision

Two things from tonight's instructions were deliberately left as proposals rather than blind
overnight implementation, because they're higher-risk/higher-ambiguity than anything above:

**A. Correlation preemption ("cancel 5 other trades if you have to").** Sketch of how it could
work, for discussion: when Correlation's signal fires and even the raised floor (§4) plus
dynamic reclaim would still refuse it, actively cancel N resting orders from the
lowest-priority family present (Spread first, then Critical Line) on that pool, oldest-first,
up to some safety bound, before submitting Correlation's order. Needs your sign-off on: which
orders get cancelled (age? family? worst-priced?), how many is "enough," and whether this
should also cancel *filled* positions (not just resting orders) if that's what's actually
blocking capacity.

**B. Spread on "the full 100 symbol DB."** Currently 6 hardcoded pairs across the 4 futures.
Expanding to the full stock universe is combinatorially large (100 symbols → 4,950 possible
pairs) — needs a pair-selection strategy (correlated pairs only? same-sector? a fixed curated
list?) before this is buildable, not just a size increase. Flagging as a real feature to design
next, not something to guess-implement.

---

## 7. Verified live tonight

- IB Gateway: both paper (4002, orders) and live (4001, data) confirmed up after the earlier
  PC-outage restart.
- Full stack restarted after today's outage: CC2026 dashboard/broker/decider, Fetcher2026,
  GevaExtract — all 6 ports confirmed listening.
- `trading_dashboard.py` bumped v5.27 → v5.32 across several fixes today (held-back tile
  layout bug, poisoned-config-cache 500 on `/api/session/status`, per-algo hover breakdowns on
  every broker stat, new Spread/Correlation live-diagnostics panel).
- All touched modules' self-tests pass: `lib/allocation.py`, `trader/decider.py`,
  `trader/spread_manager.py`.

## 8. First thing tomorrow morning

1. You mentioned changing IB account market-data permissions so the **paper account may now
   get live market data** — test this first, before anything else, since several parts of this
   system assume paper=orders/live=data-only as a hard rule (see the paper/live split).
2. Review §2's bracket table and §6's two open design questions.
3. Watch for the next real EOD flatten to confirm §1's fix produced zero NULL `pnl_points`.
