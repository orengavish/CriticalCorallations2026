# Galao System — Replenishment
Version: 1.0.0 | Date: 2026-09-13

---

## Status: disabled by policy decision (not a bug, not a bypass)

As of 2026-09-13, `system_state.REPLENISH_ENABLED` is set to `0` and there is
no reachable UI control anywhere in this system that can flip it back to `1`:

- The live dashboard (`back-trading/trading_dashboard.py`) never had a
  replenishment control at all.
- The legacy dashboard (`trader/visualizer/templates/dashboard.html`, not
  currently served by anything) had a toggle button (`#btn-replenish`); it is
  now `disabled`, its `onclick` removed, and the page no longer fetches the
  live flag value to display — the button just reads "REPLENISH: DISABLED"
  unconditionally. The underlying `/api/replenish` GET/POST endpoints in
  `trader/visualizer/app.py` are untouched, just no longer wired to anything
  clickable.
- `trader/daily_paper_session.py` (a legacy, currently-unscheduled 2-hour
  session orchestrator) used to set `REPLENISH_ENABLED=1` unconditionally on
  every run. That line now sets `0` instead, so even if that script is ever
  run again by accident, it won't silently turn this back on.

None of the underlying mechanism was deleted. This doc exists so a future
decision to re-enable it is a deliberate, informed one — see "How to
re-enable" below.

## What replenishment actually is — two separate mechanisms

**1. `decider.py`'s own `replenish()`** (`trader/decider.py`, function
`replenish()`, called from `run_replenishment_loop()`). Fires the instant a
command reaches `status='FILLED'` — **not** when it closes. For each such
command whose line is still armed, it immediately inserts a fresh `PENDING`
command at the same line/price, marking the original
`replenishment_issued=1` so it only fires once per fill. This is the
"keep re-arming a still-valid signal" mechanism, and it ran unconditionally
whenever `SESSION` wasn't `SHUTDOWN` — no flag gated this one specifically
(the `REPLENISH_ENABLED` flag only ever gated the second mechanism below).

**2. `broker.py`'s `replenish_if_enabled()`** (`trader/broker.py`, gated by
`system_state.REPLENISH_ENABLED`). Fires on `status='CLOSED'` commands with no
child yet, and (as of the 2026-09-13 fix below) only for sources
`lib.allocation.family_for_source()` doesn't recognize as a governed signal
family — i.e. only the deliberately-random baseline sources
(`random_lmt`/`random_mkt`/`random_stp`, see `trader/random_gen.py`), where a
fresh, direction-randomized market order is the actual intended behavior (a
running null-hypothesis control), not a bug.

## Why it's disabled

Discussed directly with the user (2026-09-13). The system's actual model is:
decider.py generates a command for every armed line; broker.py's admission
gates (`compute_side_resting`/`check_admission`, capped at IB's real
~15-resting-orders-per-side limit, further split per algorithm family) decide
*which* of all currently-PENDING candidates get to consume the next free
slot, by priority. Capacity is the scarce resource, not signal ideas — the
system's own priority queue already exists specifically to make sure the
best currently-available candidate wins whatever room opens up.

Mechanism 1 above (`decider.py`'s `replenish()`) doesn't respect that model:
it manufactures a *guaranteed* new competitor for a line that already got a
turn, the instant it fills — while the original position is often still
open, meaning one line can occupy two of the ~15 precious slots
simultaneously. It doesn't literally "jump the queue" (the new command still
goes through the same admission gates and the same priority sort as
everything else), but it does add demand to a capacity-capped resource for a
signal that's already been serviced, competing equally against lines that
have never been tried at all. That is a real cost against the very "handle
our 15-slot bottleneck carefully" discipline the rest of this system (Gate
1/1b, per-family capacity allocation, the 2026-09-13 admission-cap
double-count fix, entry-cutoff-at-submission) was built around.

This was also the same code path implicated in a real, separate incident the
same day: `broker.py`'s `replenish_if_enabled()` had a stale source filter
(`source != 'critical_line'`, a literal string no longer used by anything)
that let it fire for **real signal sources**, not just the intended random
baseline ones — spawning coin-flip-direction market orders disguised as the
same strategy. That specific bug is fixed (see git log,
"Fix replenish_if_enabled placing random-direction orders for real signals"),
independently of this policy decision to disable replenishment outright.

## Current state, precisely

- `system_state.REPLENISH_ENABLED = '0'`.
- This flag only ever gated mechanism 2 (`broker.py`'s CLOSED-based path).
  Mechanism 1 (`decider.py`'s FILLED-based `replenish()`) has **no flag of
  its own** — it is still live in the sense that its code path runs on every
  replenishment-loop cycle. Disabling it today was done by policy discussion,
  not a code flag; if it needs to be forcibly stopped in code too (rather
  than relying on nobody re-enabling `REPLENISH_ENABLED` and this doc's
  guidance), that would need its own explicit change — not made as part of
  this pass, per instruction to disable, not delete or rewrite, the
  mechanism.

## How to re-enable in the future

1. Read the reasoning above again first — has the actual concern (queue
   pollution under capacity scarcity) been addressed, or just forgotten?
2. Set `system_state.REPLENISH_ENABLED = '1'` (e.g. via
   `lib.db.set_system_state`) to restore mechanism 2 (random-baseline
   replenishment only, post-fix).
3. For mechanism 1 (`decider.py`'s `replenish()`), no flag exists — it will
   already be running unless someone has separately gated or removed it since
   this doc was written. Check `trader/decider.py`'s `replenish()` and
   `run_replenishment_loop()` directly before assuming its current state.
4. Consider, before restoring the old unconditional-on-fill behavior: fold
   "this line is still armed and has no in-flight command" into the *same*
   generation pass that already handles brand-new lines (`decider.py`'s
   `generate_commands_for_new_lines`-style dedup-guard check), so a re-armed
   candidate has to win its slot through the same priority sort as every
   other candidate — instead of being force-inserted as a guaranteed new
   `PENDING` row the instant a fill event fires. This was the middle-ground
   design discussed with the user but not implemented (out of scope for a
   "disable, don't rewrite" pass) — a real option if replenishment is wanted
   back without reintroducing the queue-pollution concern.
5. Once there's enough live trade volume, the Winning Formula table
   (`back-trading/trading_dashboard.py`'s "Winning Formula" tab) and the CL
   Algo backtest pipeline can directly compare replenished vs. non-replenished
   trades' real expectancy — turning this from a judgment call into an
   evidence-based one.
