# Galao System — Dynamic Capacity Allocation
Version: 1.0.0 | Date: 2026-09-14

---

## What changed

`lib/allocation.py`'s per-family futures-pool caps (`ALLOC_PAIR_PLAN`) were,
until 2026-09-14, static reservations: GevaExtract/Critical Line/Spread/
Correlation each got a fixed slot count per futures pool (e.g. MNQ+NQ: 10
each), enforced by `check_admission()` (broker.py's "Gate 1b"), regardless
of whether a family was actually using its share.

Per explicit user instruction:

> "we need to fix the allocation mechanism because we consume the most
> resource, the most expensive resource for algorithm that might run and
> might not. This should be dynamic, the allocation."

Confirmed live before the fix: Critical Line repeatedly hit "at its
allocated cap of 10" on NQ and MYM while Spread and Correlation — both
brand-new, not yet firing — sat on unused shares of the same pool's real
combined capacity. Static reservation was blocking real signal volume in
favor of capacity nobody was using.

## The fix

`lib/allocation.py` gained `dynamic_cap_for(con, family, symbol, direction)`,
used by `check_admission()` in place of the old static `allocated_cap_for()`
lookup (which still exists, unchanged, for the dashboard's "theoretical"
display — see below):

```
effective_cap(family) = nominal_share(family)
                       + sum(max(0, nominal_share(other) - resting(other))
                             for other in same_pool_families)
```

A family's ceiling grows to absorb whatever every other family on the same
futures pool isn't currently using, and shrinks back the moment those
families start resting real orders — recomputed fresh on every admission
check from live `commands` table state, not a separate reservation that
needs releasing or can leak.

This only applies to the 4 futures pools (`ALLOC_PAIR_PLAN`). Each pool's
per-family shares already sum to exactly that pool's true combined capacity
(15-per-symbol x 2 symbols = 30; e.g. MNQ+NQ's 10+10+10), so this is
reclaiming genuinely idle capacity, not oversubscribing. Dedicated stocks
(`ALLOC_STOCK_DEDICATED`) are unaffected — each of the 30 dedicated symbols
is already exclusive to one family, so there is nothing to reclaim there.

## Why this is safe

Two independent gates already existed and still both apply — nothing about
this change removes either:

- **Gate 1** (`lib.db.compute_side_resting`, broker.py's flat per-symbol cap,
  `orders.max_resting_per_side`): a hard ceiling per INDIVIDUAL symbol
  (e.g. MES alone can never exceed ~15 resting on one side), independent of
  family. This runs BEFORE Gate 1b and is the real backstop — even if
  `dynamic_cap_for()` is ever momentarily too generous to two families
  simultaneously reclaiming the same idle slot (a live query race, not a
  reserved allocation, so there's no lock between the read and a
  competitor's own read), Gate 1's flat per-symbol count physically cannot
  be exceeded, because it's checked independently against the same live
  `commands` table right before Gate 1b runs.
- **Gate 1b** (this change): now dynamic, but a family's floor (its own
  nominal share) is still always honored — it can never be pushed below what
  the static table already guaranteed it, only grow beyond it when room
  exists.

## What did NOT change

- `ALLOC_PAIR_PLAN`, `ALLOC_STOCK_DEDICATED`, `allocated_cap_for()`: all
  unchanged. The dashboard's `/api/allocation` "theoretical" column still
  reads the static nominal table directly — showing the guaranteed floor,
  not the live reclaimed ceiling, is the right "theoretical" reading.
- GevaExtract's internal 8 ES + 7 MES sub-split, and its own pipeline's
  responsibility for honoring it: unaffected, this module still only
  enforces GevaExtract's pooled total.

## Open / not done

- The dashboard doesn't yet show the LIVE dynamic ceiling alongside the
  static "allocated" figure — only "allocated" (nominal) and "actual"
  (currently resting) are displayed. Someone watching the dashboard during
  a capacity-constrained moment won't see WHY a family got through when its
  own nominal share was full (the reclaim isn't visible, only its effect).
  Small, additive follow-up if it turns out to matter in practice: expose
  `dynamic_cap_for()`'s result per family/pool in `/api/allocation`'s
  response alongside `allocated`/`actual`.
- The 70-symbol shared stock tier (beyond the 30 currently dedicated) still
  has no allocation plan at all — `allocated_cap_for()`/`dynamic_cap_for()`
  both correctly return `None` (ungoverned) for those symbols today, same
  as before this change. Out of scope here; tracked separately.
