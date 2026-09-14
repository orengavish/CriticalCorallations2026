# Galao System — Spread's Per-Leg Stop-Loss (deviation from AI-35)
Version: 1.0.0 | Date: 2026-09-14

---

## Status: deliberate deviation from Geva's design, by explicit user instruction

`Galgo2029/knowledge/Claims/spread-diff-trading.md` (AI-35) is explicit: a
spread position is 1 long + 1 short leg, hedged, with **no per-leg
stop-loss** — the hedge itself bounds risk, and the position is closed only
by the strategy's own DIFF "1-2-3" pattern (AI-35e), never a resting order.
`trader/spread_manager.py` and `lib/order_builder_spread.py` were built
exactly that way originally, and both modules' own docstrings say so.

As of 2026-09-14, this system deviates from that design: **each leg now gets
a real resting STP order**, per an explicit user instruction given while
enabling Spread for the first time in live paper trading:

> "that's simple to fix. For now, again, we are talking about control
> groups. It will be bracket of four as other algorithms that we have,
> meaning not bracketed four, but a few brackets as we do with all other
> types. In the documents, we will mention with a big note that this is not
> given design, and this is still an open issue."

This is **not** a correction of a bug — AI-35's "no stop" design was
implemented correctly. It is a deliberate, acknowledged departure, made
because Spread is being run today as a control-group experiment (the
literal AI-35a reading vs. its reversed/mean-reversion control, `source=
'spread'` vs `'spread_control'`, already an existing pattern) rather than a
faithful reproduction of Geva's exact method, and because the stated
near-term goal for this whole system (see
`Galgo2029/knowledge/...` session notes, 2026-09-14) is maximizing the
volume of normally-exited paper trades collected over the next 1-4 weeks,
not preserving AI-35 exactly as taught.

## What changed, mechanically

- `trader/spread_manager.py`'s `open_spread_position()` now calls
  `_place_leg_stop()` for each leg right after both legs' MKT entries fill:
  a real `StopOrder`, `bracket_size` points off the current market price
  (`ibc.get_price()`), opposite direction from the leg (protects it).
  Best-effort — a leg with no live price available is left unprotected
  exactly as before this change (logged loudly, not a crash).
- `spread_positions` (see `lib/db.py`) gained 5 nullable columns:
  `bracket_size`, `sl_price_a`, `sl_price_b`, `sl_order_id_a`,
  `sl_order_id_b`. Rows opened before this change have NULLs here.
- `trader/spread_manager.py`'s `check_spread_exit()` now checks, every poll
  cycle, whether either leg's stop has already filled at IB
  (`_leg_stops_hit()`, same `ibc.paper.trades()` scan pattern as
  `broker.py`'s `poll_tp_sl_fills()`) **before** evaluating the DIFF-pattern
  exit. If a leg's stop fired: `_close_spread_position_after_leg_stop()`
  cancels the *other* leg's still-resting stop and flattens it at market
  (the hedge is broken the instant one leg is stopped out), and marks the
  position CLOSED with `close_reason='SL_HIT'`.
- The existing DIFF-pattern close path (`_close_spread_position()`, used by
  both the normal GAP_CLOSED/ADVERSE_BREAK exit and the portfolio
  kill-switch) now cancels both legs' resting stops first, so a stop never
  survives past its position being flattened by the primary exit logic
  (would otherwise become an orphaned order resting against whatever
  happens to occupy that symbol next).
- `cfg.spread.bracket_size` (new config key, default **8** — the mid-point
  of `generator.bracket_sizes`' `[2, 4, 8, 16]`) controls the stop distance.

## What did NOT change

- The DIFF-pattern exit (AI-35e) is still the **primary** close logic,
  unchanged. The per-leg stop is a safety net, not a replacement — most
  positions are still expected to close via GAP_CLOSED/ADVERSE_BREAK, same
  as before this change.
- `broker.py`'s `reconcile_naked_positions()` still exempts
  `source IN ('spread', 'spread_control')` from its own naked-position
  protection. That exemption is still correct: this system's own
  `check_spread_exit()` is now responsible for making sure a stopped-out
  leg's partner never sits unhedged (see above) — `reconcile_naked_positions()`
  doesn't need to duplicate that.

## Open issue — flagged, not resolved

1. **"A few brackets, as other algo types" was scoped down to one value.**
   Critical Line / GevaExtract fan out each armed line into multiple
   commands, one per `generator.bracket_sizes` entry (`[2, 4, 8, 16]`),
   letting the comparison engine judge which bracket size performs best.
   Spread does **not** do that fan-out today — `cfg.spread.bracket_size` is
   a single value (8), not a 4-way spawn. Reason: `check_spread_signals()`
   has no admission-gate/capacity check at all before calling
   `open_spread_position()` (confirmed 2026-09-14 — it only checks
   "is there already an OPEN position on this pair", nothing else), unlike
   every other algorithm family's commands, which go through
   `lib.allocation.check_admission()` before ever resting an order. Fanning
   out 4x on top of that gap, on the same day Spread is enabled live for
   the first time, was judged too much simultaneous change. Revisit once
   Spread is wired into the admission-gate (see the separate, still-pending
   dynamic-allocation work) — at that point a real multi-bracket-size
   fan-out is naturally capacity-safe, the same way it already is for
   Critical Line/GevaExtract.
2. **Stop price is based on the price at signal time, not the actual leg
   fill price.** Every other bracket-using algorithm family gets a
   post-fill rebase (`broker.py`'s `_rebase_queue`/`_drain_rebase_queue()`)
   correcting TP/SL to the real fill price once it's known. Spread's new
   stop does not participate in that mechanism — wiring it in would mean
   routing spread commands through the same SUBMITTED→FILLED lifecycle
   everyone else uses, a bigger change than today's scope. On liquid micro
   index futures the basis-point gap between "price at signal time" and
   "actual MKT fill price" a few hundred milliseconds later is expected to
   be small (often zero, at most 1 tick), but this is a known,
   undocumented-elsewhere approximation, not a guarantee.
