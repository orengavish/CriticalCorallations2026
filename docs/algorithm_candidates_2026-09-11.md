# Trading algorithm candidates — full list, graded
> v1.0 — 2026-09-11 (created)

Context: today's system trades on 3 signal sources — GevaExtract's manual Facebook lines,
and two backtest-selected line rules (Algo 1 = `PREVIOUS_DAY_LOW`, Algo 2 =
`PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE`, see `RESEARCH_RUNBOOK_2026-09-07.md`). Separately,
`lib/algo_engine.py` already implements 5 *entry styles* (BOUNCE/BREAKOUT/DIRECTIONAL/
FADE/BOTH) appliable to any line, already running as paper-trade parameter sweeps via
`algo_lab.py`. This doc is about new **signal sources** (what decides a price level
matters), not new entry styles (already covered).

## How to read the grades
- **Edge (importance)**: how likely this is to add real, non-overfit trading value —
  based on how well-documented/robust the underlying effect is in real markets, not a
  promise.
- **Difficulty**: engineering effort *in this specific codebase*, not in the abstract —
  accounts for what's already built.
- **Reuses**: what existing code/data this can build on, so difficulty isn't guessed in
  a vacuum.
- **Architecture fit**: this system polls (broker every few seconds, decider similarly)
  rather than reacting to streaming ticks — this matters a lot for anything DOM-based,
  see the note at the bottom.

---

## Tier 1 — same shape as what's already live (lowest difficulty, proven pipeline)

These reuse `prep_research_lines.py`'s exact pattern end to end: a new `CandidateLevel`
reason (or a new WINNING_REASONS entry), same `critical_lines` table, same
decider/broker/dashboard plumbing, zero new infrastructure.

| # | Algorithm | Edge | Difficulty | Reuses |
|---|---|---|---|---|
| 1 | **`FIVE_DAY_HIGH`/`FIVE_DAY_LOW`** (bare, no pivot) | Medium | **Trivial** | Already computed by `knowledge/rules.py`, already in every day's candidate list, already backtested in the same sweep as Algo 1/2 (see `backtest/summarize_sweep.py`) — just never added to `WINNING_REASONS`. This is the single fastest thing on this list to ship. |
| 2 | **Opening Range Breakout (ORB)** — first N minutes' high/low as the level | Medium-High | Low | Well-documented futures-specific edge (unlike most of this list, ORB is intraday-native, matches your actual instrument). Needs one new candidate function in `knowledge/rules.py`, nothing else changes. |
| 3 | **Session VWAP** as a dynamic S/R / mean-reversion anchor | Medium | Low-Medium | `bars.db` has volume already (used by `correlation_lab`); needs a running VWAP calc, otherwise same pipeline. |
| 4 | **Volume Profile POC/VAH/VAL** (point of control, value area) | Medium | Medium | Needs a volume-at-price histogram per session — more math than #1-3 but no new data source; `bars.db`'s volume column already exists. |
| 5 | Round-number / psychological levels | Low | Trivial | Weak edge alone (well-known, likely priced in / low differentiation) — only worth it as a *confluence modifier* on top of #1-4, same way pivot confluence works today. |
| 6 | Fibonacci retracement off recent swing | Low-Medium | Low-Medium | Needs swing-high/low detection logic; edge is contested in the literature (works partly because enough people watch it, not because of an underlying mechanism) — I'd rank this low priority. |

## Tier 2 — correlation-based (half-built already: `lib/correlation_lab.py`)

This is the one your project's own name points at. `correlation_lab.py` already computes
rolling pairwise log-return correlation across MES/MNQ/MYM/M2K from 7 years of 30-min
bars — read-only today, built explicitly "to surface correlation ideas that could later
become a third algo type" (its own docstring). The hard data-plumbing is done; what's
missing is a trading rule on top of it.

| # | Algorithm | Edge | Difficulty | Reuses |
|---|---|---|---|---|
| 7 | **Correlation-break reversion** — trade the pair back together when two normally-correlated symbols (e.g. MES vs MNQ) diverge further than historical norm | Medium-High | Medium | `correlation_lab.py`'s `rolling_correlation_series`/`correlation_matrix` directly; needs a new signal-generation layer (something like `prep_research_lines.py` but symbol-pair-aware) and decider.py support for a 2-symbol-linked command (today's schema is single-symbol per line). |
| 8 | Lead-lag signal (one instrument's move predicts another's with a short lag) | Medium | Medium-High | Same data source as #7; needs lag-correlation analysis (not currently in `correlation_lab.py`, a real addition) and is more sensitive to exact timing than a straightforward spread trade. |
| 9 | Beta-adjusted relative strength (symbol outperforming what its usual correlation implies) | Medium | Medium | Similar to #7/#8, one more derived metric on the same base data. |

## Tier 3 — technical indicators (well-known, moderate build, moderate edge)

| # | Algorithm | Edge | Difficulty | Reuses |
|---|---|---|---|---|
| 10 | Moving-average crossover (trend-following) | Low-Medium | Low-Medium | New indicator calc on `bars.db`; well-known enough that most of its edge is likely arbitraged away on liquid micro futures, but cheap to test properly. |
| 11 | RSI / Stochastic divergence | Low-Medium | Medium | Same category as #10 — cheap to build, edge is contested for pure mean-reversion signals in trending futures. |
| 12 | Bollinger Band breakout/reversion | Low-Medium | Low-Medium | Same as #10/#11. |
| 13 | ATR-based volatility breakout / adaptive stop sizing | Medium | Low-Medium | This one's more interesting as a **risk-management layer** (sizing TP/SL by current volatility) than a standalone signal — could improve Algo 1/2's existing brackets rather than being its own algorithm. |

## Tier 4 — DOM / order-book based (you specifically asked about this)

**Read the architecture-fit note below before investing in this tier — it's not really
about how easy the IB API is to call.**

| # | Algorithm | Edge | Difficulty | Architecture fit |
|---|---|---|---|---|
| 14 | Order-book imbalance (bid size vs ask size skew) | Medium (if reacted to fast enough) | High | **Poor fit today** — see note below |
| 15 | Large resting-order / "wall" detection as dynamic S/R | Medium | High | Poor fit today |
| 16 | Tape reading / aggressive order-flow rate | Medium-High (if fast) | Very High | Poor fit today |

**Architecture note**: fetching DOM data via IB's API is indeed straightforward
(`reqMktDepth`) — that's not the hard part. The hard part is that order-book signals
decay in milliseconds to low seconds, and this system's entire execution model is
**polling**: broker checks for work every `command_poll_seconds` (a handful of seconds),
decider's replenishment loop every `replenishment_poll_seconds` (30-90s). Today's
price-fetch fix (persistent ticker subscriptions) made *snapshot reads* cheap — it did
not turn this into a reactive, tick-driven system. Trading DOM signals for real would
need a genuinely different execution path (a subscribed depth handler making
submission decisions directly, bypassing the poll loop) — a materially bigger
architecture change than anything else on this list, not just "add a new signal type."
I'd treat this tier as a distinct future project, not a slot in the step-by-step queue
with the others, unless you want to fund that architecture change specifically.

## Tier 5 — statistical / quant (higher difficulty, real overfitting risk)

| # | Algorithm | Edge | Difficulty | Notes |
|---|---|---|---|---|
| 17 | Mean-reversion on rolling z-score of price | Low-Medium | Medium | Needs real out-of-sample validation discipline — easy to backtest into something that looks great and isn't. |
| 18 | Multi-day momentum/trend continuation | Low-Medium | Medium | Same overfitting caution as #17. |
| 19 | Volatility-regime-conditional switching (trade differently in high/low vol) | Medium | High | Interesting as a *meta-layer* over existing algos rather than a new algo itself — e.g. "only trade Algo 2 when 5-day realized vol is above X." |

## Tier 6 — news/sentiment (extends what GevaExtract already is)

| # | Algorithm | Edge | Difficulty | Notes |
|---|---|---|---|---|
| 20 | Automated economic-calendar awareness (pause/widen around high-impact releases) | Medium (risk-reduction, not alpha) | Low-Medium | This is defensive, not a new signal — genuinely useful given GevaExtract already showed manual news-driven signal has value. |
| 21 | Automated sentiment analysis (broader news/social, not just Geva's posts) | Low-Medium, highly uncertain | Very High | GevaExtract IS a manual, curated version of this already, with real signal (per your own trading results). Automating general sentiment is a much harder, noisier problem — I'd deprioritize this. |

## Tier 7 — calendar/seasonality (cheap, weak)

| # | Algorithm | Edge | Difficulty |
|---|---|---|---|
| 22 | Day-of-week effects | Low | Trivial |
| 23 | Month-end/quarter-end flow effects | Low | Trivial |

---

## My recommendation, in order, and why

Given you said it took a week to get to today's "2 basic algorithms" state, and you want
to move faster from here — the fastest wins come from reusing the pipeline you already
paid for, not from novelty:

1. **`FIVE_DAY_HIGH`/`FIVE_DAY_LOW` (#1)** — this is nearly free. It's already computed,
   already in the same backtest sweep as Algo 1/2. Confirm its bracket-by-bracket
   performance in that same sweep output, and if it holds up, it's a one-line config
   change (`WINNING_REASONS`), not a build.
2. **Opening Range Breakout (#2)** — genuinely futures-native (unlike most of Tier 3-7),
   small build, and a distinctly *different* kind of signal from your current two
   (intraday-formed, not previous-day-derived) — good diversification, not just more of
   the same.
3. **Correlation-break reversion (#7)** — this is the one I'd actually get excited about:
   the data layer already exists and was explicitly built with this in mind, it's
   conceptually different from everything else you're trading (a genuine third
   signal *family*, not a variation on S/R levels), and it's the one most aligned with
   what this project is actually named for. Medium difficulty because `decider.py`'s
   schema is single-symbol per line — that's the one real schema change needed, not
   the signal logic itself.
4. **ATR-based adaptive bracket sizing (#13)** — not a new algorithm, but likely a real
   improvement to Algo 1/2 as they stand today (fixed tick brackets don't account for a
   quiet vs volatile day) — worth a look alongside whichever new signal you pick.
5. Everything in **Tier 4 (DOM)** — hold off until/unless you're ready to fund the
   execution-architecture change specifically; bolting it onto the current poll loop
   would waste the DOM data's actual edge.

Your call on order, per what you said — this is my ranking, not a decision.
