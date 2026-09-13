# Galao System — Dynamic Exit Management (DOM/Level-2, trailing stops)
Version: 1.0.0 | Date: 2026-09-13

---

## Status: no code change, decision record only

This is a discussion/analysis record, not a change log. Nothing in the
codebase was touched as a result of this conversation (2026-09-13). Captured
here so a future session starts from the same place instead of re-deriving
this from scratch.

## What exists today

Every algorithm family except one uses a **fixed, one-shot bracket**: TP/SL
prices are set once at order construction (`lib/order_builder.py`) and left
alone until they fill, aside from the *one-time* rebase right after entry
fills to the real fill price (`trader/broker.py`'s `_drain_rebase_queue()`).

**Correlation is the one exception.** `trader/correlation_trail.py`
implements a real trailing stop — per Geva's AI-31 rule ("immediate SL beyond
entry once moving; very tight trailing stop... hold while momentum runs your
way, exit when it flips"). It runs every broker poll cycle, ratchets the SL
toward the market by a fixed tick distance, and never loosens it. This is
cheap: no special data feed (same last-price the rest of the system already
has), simple ratchet logic (`_compute_new_sl()`, fully unit-tested with no IB
dependency), same `ibc.paper.modifyOrder()` IB-interaction pattern as the
already-proven rebase queue.

## The question raised

Should Critical Line (or another family) get a DOM/Level-2-order-book-driven
dynamic exit — continuously adjusting the exit point using market depth data,
not just price?

## Conclusion reached

**No — not with this system's current architecture, and not for Critical
Line's current strategy design.**

Reasoning:

1. **Cost/complexity is a different order of magnitude than a trailing
   stop.** A trailing stop (like Correlation's) is cheap and already proven.
   A DOM-driven exit needs: a paid Level-2/market-depth data subscription
   from IB (real order-book depth for futures is a separate, billed feed,
   not included in basic last-price/bid-ask access); a streaming ingestion
   architecture (this system is poll-based everywhere, 1-30s cadences even
   after the 2026-09-13 latency pass — order-book signals matter at a much
   faster timescale than that); an actual order-flow/microstructure signal
   model (a real research problem, not just an engineering one); and
   backtesting infrastructure for order-book data, which doesn't exist at all
   today (the CL Algo pipeline works on trade prints/OHLC, not book
   snapshots).

2. **No algorithm's own documented rules currently call for it.** Checked
   against Geva's rules as implemented in this system: Correlation is the
   only family whose logic explicitly wants a moving exit, and it already has
   one (trailing stop, price-based, not order-book-based). Critical Line is
   a fixed-level-touch strategy — the bracket already encodes the entire
   thesis (this specific line, this specific distance). A continuously-
   adjusted exit would be a different strategy riding on top of Critical
   Line, not a fix to it.

3. **Doing it poorly would likely be worse than today's static bracket.**
   Reacting to order-book snapshots that are already stale by the time a
   poll-based system observes them means whipsawing a stop on noise, not
   signal. This is closer to an HFT problem (colocated infrastructure,
   sub-millisecond reaction) than something a retail/API-based (ib_insync,
   not colocated) system is positioned to compete in.

## If this comes up again

- A **price-based trailing stop for Critical Line** (not order-book-based) is
  cheap to test — reuse `trader/correlation_trail.py`'s exact template.
  Backtest it through the CL Algo pipeline first, rather than assuming it
  helps and shipping it live.
- A **DOM/Level-2-driven** exit mechanism is a from-scratch project, not an
  extension: new data subscription (real cost), new streaming architecture,
  new signal research, new backtesting capability. Don't scope it as "add a
  feature to Critical Line" — it's closer to "stand up a new algorithm
  family."
