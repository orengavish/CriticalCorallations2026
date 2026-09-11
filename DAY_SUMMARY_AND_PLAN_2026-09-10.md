# Day Summary (2026-09-09) + Forward Plan
> v1.0 — 2026-09-10 (created)

## Part 1 — What happened yesterday

### Bugs found and fixed
1. **Duplicate broker/decider launches** (root cause of 2026-09-08's order storm, alongside #6) — `lib/singleton_lock.py`, OS-level exclusive lock, wired into both processes' startup.
2. **`RECONCILE_REQUIRED` status overwrite was hiding real activity** from every dashboard view filtered on FILLED/SUBMITTED — replaced with a `needs_review`/`review_note` flag that never touches lifecycle status.
3. **Broker never re-checked live price before submitting** — a PENDING row could sit stale, then fire at whatever price IB gave it once finally sent (this is how the 2026-08-17 batch filled ~100–185 points from intended entry). Fixed: fresh price + entry-buffer check immediately before every submit.
4. **Admission cap undercounted real IB exposure by ~2x** — it only counted each command's own entry side, never its TP/SL legs, which rest on the *opposite* side the instant a bracket is submitted (confirmed live: three legs go resting together, not just on fill). Fixed to count both directions.
5. **No market-open gate** — `decider.py` could seed brackets off a pre-market price. Added `is_before_open`/`seconds_until_open` in `lib/session_clock.py`; `run_session_start` now blocks until the real open (8:30 CT / 9:30 ET).
6. **Root cause of the 2026-09-08 storm, precisely identified**: GevaExtract's own automated pipeline (a separate Node.js project) — not this system — generated a 32-command grid per real line (MES **and a synthetic, non-real MNQ scaling**; Geva has never posted an MNQ line, ever, in 4+ years of history) and broker's uncapped submit loop fired it all at once into IB's own 15-per-side cap. **GevaExtract's own execution is now blocked entirely** (`broker.py` Gate 0 cancels anything `source='geva_extract'`) — its *signal* (Geva's real Facebook lines) is still used, now through this system's own `decider.py` pipeline (`source='geva_manual'`), not GevaExtract's unsupervised insert path.
7. **`decider.py` hardcoded `source='critical_line'`** for every command regardless of the underlying line's real source — fixed to propagate the line's own source, which is what makes Real/Control/GevaExtract distinguishable downstream at all.
8. **IB client-ID visibility gap** — `IBClient` picks a random client ID from a pool on every connect; every polling function reads only `ib.trades()` (that session's own local cache). A restart with a new ID starts with an empty view of orders a previous session placed — a likely major contributor to false "bracket vanished" (`RECONCILED`) flags. Fixed: `reqAllOpenOrders()` called on every paper connect.
9. **Verified retroactively** (2026-09-09 evening): ~89% of that day's "closed" trades were `RECONCILED` (estimated exits from bug #8), not real fills. Once filtered to real fills only, results normalized (e.g. 0%/100% win splits became a much more ordinary 50%).
10. Several dashboard bugs from building the new screens: a `LIMIT 300` feeding a *displayed* count (froze at exactly 300 regardless of the real number), an ID-selector CSS rule (`#tab-stats{display:flex}`) that out-specified Bootstrap's tab-hide logic and broke every other tab, a flexbox `min-width:0` gap that turned a vertical list into horizontal overflow, and the old `/api/submitted` endpoint hardcoding a source with 5 rows total, ever.
11. **Not actually fixed, flagged as residual**: `session.py`'s `SessionManager` has no way to recognize an already-running session across a dashboard restart. It assumes "not in my memory" = dead and tries to respawn — the respawn correctly gets blocked by the singleton lock (#1), but the resulting rapid retry-and-fail loop looks exactly like a real crash storm in the logs. Happened once yesterday, cost real time to diagnose as a false alarm.

### Cleanup performed
- Deleted 1,595 GevaExtract-MNQ noise rows (synthetic, never real Geva signal).
- Earlier in the day: cut `commands` from 68,722 → 26 rows, keeping only execution-verified good Sep 2/3 trades as a clean baseline; `ib_events` cleared (406,852 → 0).
- IB-side: `reqGlobalCancel()` + flatten, more than once — confirmed the account can look alarming (~2,000 resting orders) purely from natural churn across 34 live symbols while the session is active; at rest it settles to a small number (27, then 87 again after a full trading session — both are plausible steady states, not leaks, at this symbol count).

### Config / algorithm changes
- `active_brackets` standardized to `[4, 8, 16]` (dropped 32).
- Symbol universe expanded 14 → 34 (4 futures + 30 stocks, picked for liquidity — a judgment call, not ranked against a live volume feed).
- `replenishment_poll_seconds` 30 → 90 (34 symbols no longer fit the old window).
- Added 12 real Geva MES lines (hand-parsed from the actual Facebook post text, verified against `GevaExtract/geva.db`'s own post history) + 12 matched random-distance control lines (`geva_manual` / `geva_manual_control`).
- `geva_manual` is now priority 1 for submission and **exempt from the admission cap** ("always all possible" real Geva trades, per explicit decision) — it still competes for IB's real per-side limit once actually submitted, it just isn't held back by our own cap.

### Screens built
- **Broker** (Pending/Submitted/Filled queue, live browser-tab-title counts, at-cap indicator, closed-today strip).
- **Results** (several redesigns): single-select bucket, bracket/entry-type/exit-reason filters (including "real fills only, exclude RECONCILED"), a bucket-vs-bucket comparison with Stop/Limit broken out inside each, a flat per-trade list (not aggregated — a summary table was hiding that "n=12" could mean 2 real events fanned across brackets), sortable columns, hover glossary on bucket names.

### Verified right now (2026-09-10), for accuracy rather than memory
- DB: 0 `needs_review`. `geva_extract` residue is fully resolved (176 `CANCELLED` + 26 `CLOSED`, **none stuck** `PENDING`/`SUBMITTED` — the block is holding).
- IB (paper): 87 resting orders, 1 open position (MES −3) — the session ran continuously overnight (futures trade near round-the-clock); this looks like live, managed exposure, not orphaned noise, but wasn't specifically re-verified leg-by-leg this morning.
- Session: `broker`/`decider`/dashboard all running, uptime ~9.3 hours.

---

## Part 2 — Your stated goals

1. **Maximize real GevaExtract lines** (the actual Facebook signal, via `geva_manual`).
2. **Maximize, and clearly classify, "algorithm 1 / algorithm 2" critical lines** — the two-winning-reasons research algorithms (`PREVIOUS_DAY_LOW` and `PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE`), today lumped into a `Real` bucket without the specific algorithm surfaced.
3. **Fewer control-group *trades*** (not fewer control lines necessarily — just less resulting volume from them).
4. **A dashboard view that clearly shows Real vs Control performance at a glance.**
5. Know exactly what's left to clean, in the DB and in IB, before each day starts — with buttons to actually do it.

---

## Part 3 — Plan to get there

### Goal 1 — more real Geva lines, reliably
Today's 12 lines were hand-parsed from pasted post text — a one-off, not repeatable without you re-pasting each morning. Proposal: **read `GevaExtract/geva.db` directly** (the scraper's own output — real ground truth, already fetched daily by GevaExtract's *existing* Facebook scraper) and feed it into our own `geva_manual` pipeline automatically. This uses GevaExtract for what it's actually good at (extraction) while keeping it fully out of execution (the part that was blocked, correctly). No new scraping code needed — `extract.js` already runs on its own schedule; we'd just be the first *safe* consumer of its output.

### Goal 2 — classify algorithm 1 vs 2, and get more of them
`prep_research_lines.py` (futures) and `prep_research_lines_stocks.py` (stocks) already tag each line with its exact winning reason in `note` JSON — the classification data already exists, it's just not surfaced. Proposal: add the specific reason (not just the `Real` bucket) as its own visible field on Results — e.g. "Real · Algorithm 1 (Previous Day Low)" vs "Real · Algorithm 2 (Prev Day High + Pivot)" — and run `prep_research_lines.py` for futures daily (only the stocks version was actually run yesterday; the futures one wasn't run at all this session).

### Goal 3 — fewer control trades without breaking the comparison
Today's control generator pairs 1 control line per real line (already fairly minimal at the line level), but each line still fans out across all 3 brackets. Proposal: **only generate 1 bracket size for control lines** (real lines keep all 3) — the comparison still holds at the line level, but control volume drops to a third with no loss of what's being tested.

### Goal 4 — a clear Real-vs-Control dashboard view
Largely built already (the comparison card + bucket grid from yesterday). Two refinements to actually deliver "at a glance": make the Real-vs-Control comparison the *default landing view* of Results (not something you scroll to), and split "Real" by algorithm 1/2 there too, so the comparison reads as *Algorithm 1 vs Algorithm 2 vs Control vs GevaExtract*, not one blended "Real" number.

### Goal 5 — start-of-day cleanup, as buttons
Propose a small "Day Start" panel (Broker or a new screen) with three actions, each already proven manually today:
1. **Verify** (read-only) — reports current DB residual counts (pending/needs_review/stale) and IB's real order/position counts side by side, so you know the true state before deciding to clean anything.
2. **Clean DB noise** — cancels/purges stale non-today noise while explicitly preserving verified-good historical trades (same logic as today's manual cleanup, as a button instead of me running a script).
3. **Cancel & flatten IB** — the existing `reqGlobalCancel()` + flatten action (currently only reachable on the legacy port-5001 dashboard) surfaced properly on the main dashboard, so it's one workflow instead of two dashboards.

---

## Open decisions before implementing
- Goal 1: comfortable reading `GevaExtract/geva.db` directly (read-only) as a cross-project dependency, or prefer to keep pasting manually for now?
- Goal 3: confirm "1 bracket for control, 3 for real" is the right lever, vs. some other reduction (e.g. control every 2nd line instead of every line)?
- Should `prep_research_lines.py` (futures) run automatically each morning, or manually for now like yesterday's stock run?
