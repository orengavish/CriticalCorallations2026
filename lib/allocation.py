"""
lib/allocation.py

Shared source of truth for the capacity-allocation plan (approved 2026-09-12): which
algorithm family (GevaExtract / Critical Line / Spread / Correlation) owns how many
resting-order slots, per futures pair (MES+ES, MNQ+NQ, MYM+YM, M2K+RTY) and per
dedicated stock group. Used by BOTH trading_dashboard.py (Allocation tab display,
theoretical vs. actual) and broker.py (the real admission-control gate) -- one table,
not two copies that can silently drift apart the way the per-symbol cap once did (see
lib/db.py's compute_side_resting() docstring for that exact history).

Design decided this session:
- Futures: each pair's slots are a POOLED cap per family, not sub-split between the
  micro and full-size contract -- e.g. Critical Line's 5 slots on MES+ES can be any mix
  of MES/ES commands, enforced against the combined count, not 2.5-and-2.5. The one
  exception already specified precisely is GevaExtract's MES+ES split (8 ES + 7 MES),
  which GevaExtract's own pipeline (a separate repo) is responsible for honoring on the
  submission side -- this module still enforces GevaExtract's total (15) as a pooled cap
  from the admission side, it doesn't second-guess the 8/7 split.
- Stocks: the top-30 (of the eventual ~100-symbol universe) are dedicated 10 per family
  (Critical Line / Spread / Correlation only -- GevaExtract doesn't trade stocks). Only
  that family may rest orders on its own dedicated symbols. The remaining ~70-symbol
  "shared" tier (not yet in the live symbols list -- see trader/config.yaml) has no
  distinct per-family sub-allocation defined yet; a stock not in any dedicated list is
  treated as ungoverned by this module (falls back to whatever flat per-symbol cap the
  caller already applies) until that expansion actually happens.

Self-test: python -m lib.allocation --self-test
"""

from __future__ import annotations

ALLOC_FAMILY_SOURCES = {
    "GevaExtract":    {"geva_extract", "geva_manual"},
    "Critical Line":  {"research_ce", "research_ce_stock", "research_random", "research_random_stock"},
    "Spread":         {"spread", "spread_control"},
    "Correlation":    {"correlation", "correlation_control"},
}


def family_for_source(source: str) -> str:
    for fam, srcs in ALLOC_FAMILY_SOURCES.items():
        if source in srcs:
            return fam
    return "Other"


ALLOC_PAIRS = [
    ("MES + ES",  ["MES", "ES"]),
    ("MNQ + NQ",  ["MNQ", "NQ"]),
    ("MYM + YM",  ["MYM", "YM"]),
    ("M2K + RTY", ["M2K", "RTY"]),
]
# 2026-09-14 (overnight allocation/bracket plan, explicit user priority order:
# Correlation > GevaExtract > Critical Line > Spread): rebalanced from the original
# 2026-09-12 even/near-even split. Rationale, per family:
#   - Correlation: "the most important resource in the system... if correlation is
#     found, this is a must." Its own trigger (3-of-4 futures FAILED_RECLAIM within
#     60min, see trader/correlation_signal.py) is genuinely rare -- these are FLOORS,
#     not ceilings, and dynamic_cap_for() already lets a family grow past its floor
#     into whatever others aren't using right now, so a big guaranteed floor here is
#     the safe way to get "leave room for it" without needing to cancel anyone else's
#     live resting orders (an explicit "cancel 5 other trades if you have to" ask was
#     NOT implemented -- see docs/allocation_priority_redesign_2026-09-14.md for why
#     that's flagged as a separate, unresolved design question instead).
#   - GevaExtract: unchanged at 15 on MES+ES (its only pool -- it never trades
#     MNQ/MYM/M2K, see family_for_source/docstring) -- "still the most valuable
#     resource we have... will have priority."
#   - Critical Line: raised on every pool ("we can allocate much more" -- it's the
#     one live-and-actually-firing family besides GevaExtract right now).
#   - Spread: cut to a minimal floor everywhere ("spread will always have at least
#     one empty slot... minimal number of spread") -- it's still enabled and can
#     still opportunistically reclaim idle capacity same as before, just with a much
#     smaller guaranteed minimum.
# Every pool's shares still sum to exactly that pool's real combined capacity
# (30 = max_resting_per_side 15 x 2 symbols) -- a clean partition, not oversubscribed,
# same invariant as the original plan.
ALLOC_PAIR_PLAN = {
    "MES + ES":  {"GevaExtract": 15, "Critical Line": 7, "Spread": 2, "Correlation": 6},
    "MNQ + NQ":  {"Critical Line": 13, "Spread": 2, "Correlation": 15},
    "MYM + YM":  {"Critical Line": 13, "Spread": 2, "Correlation": 15},
    "M2K + RTY": {"Critical Line": 13, "Spread": 2, "Correlation": 15},
}
# 2026-09-14: expanded from 30 to 77 of the ~100-stock universe
# (MultiSymbolTrader/mst_data/sp100_symbols.txt) and rebuilt from a clean union, closing
# a real drift found live: the original 30-symbol plan here and trader/config.yaml's
# actual `symbols:` list had silently diverged over time -- 11 names each way (e.g. GOOG/
# JPM/COST/CRM/WMT/PEP/NFLX were "planned" here but never in config.yaml's live list,
# while AMD/CSCO/GS/QCOM/etc. were live-traded but ungoverned by this plan at all,
# falling back to only the flat per-symbol cap with no family-level share). Rebuilt from
# the union of both sets plus every stock MultiSymbolTrader found a real armed line for
# on 2026-09-14 once its own data-fetch bug (paper-port historical data, see
# docs/data_granularity.md-adjacent notes in daily_bars.py) was fixed -- split into 3
# roughly-even groups (26/26/25) round-robin by sp100 rank, so each family gets a mix of
# higher- and lower-rank names rather than one family getting only the largest-cap ones.
# BRK.B excluded here (and from config.yaml) -- 0 bars from IB's Stock() contract as of
# 2026-09-14, a ticker-format issue (likely needs "BRK B" with a space, not "BRK.B"),
# not yet fixed.
ALLOC_STOCK_DEDICATED = {
    "Critical Line": ["AAPL", "GOOGL", "META", "LLY", "XOM", "COST", "JNJ", "BAC", "KO",
                       "ADBE", "TMO", "MCD", "IBM", "VZ", "INTU", "CMCSA", "AXP", "RTX",
                       "BKNG", "ADI", "CI", "ADP", "BMY", "SLB", "PGR", "FISV"],
    "Spread":        ["MSFT", "GOOG", "AVGO", "JPM", "UNH", "HD", "NFLX", "CRM", "CVX",
                       "PEP", "LIN", "WFC", "GE", "PM", "NOW", "SPGI", "HON", "UPS",
                       "BLK", "TJX", "VRTX", "CB", "DUK", "NOC", "COP", "USB"],
    "Correlation":   ["NVDA", "AMZN", "TSLA", "V", "MA", "PG", "ABBV", "WMT", "MRK",
                       "AMD", "CSCO", "DHR", "CAT", "TXN", "QCOM", "LOW", "BA", "GS",
                       "PLD", "CVS", "SCHW", "MO", "BSX", "ETN", "MU"],
}


def pair_for_symbol(symbol: str):
    """(pair_name, [both symbols in the pair]) if symbol is a futures contract covered
    by the plan, else (None, None)."""
    for pair_name, syms in ALLOC_PAIRS:
        if symbol in syms:
            return pair_name, syms
    return None, None


def dedicated_family_for_stock(symbol: str):
    """Which family exclusively owns this stock symbol, or None if it's not one of
    the 30 dedicated symbols (shared-pool or ungoverned)."""
    for fam, syms in ALLOC_STOCK_DEDICATED.items():
        if symbol in syms:
            return fam
    return None


def pool_symbols_for(symbol: str) -> list:
    """Every symbol that shares ONE resting-order budget with `symbol` for allocation
    purposes: both contracts of a futures pair, or just itself for a stock."""
    _, pair_syms = pair_for_symbol(symbol)
    return pair_syms if pair_syms else [symbol]


def allocated_cap_for(family: str, symbol: str):
    """
    The number of resting slots `family` is allotted for `symbol`'s pool, per the
    approved plan. Returns None if this symbol isn't governed by the plan at all (an
    un-dedicated stock, or any symbol outside the 4 futures pairs + 30 dedicated
    stocks) -- callers should fall back to the flat per-symbol-per-direction cap only
    in that case, not treat None as zero.

    2026-09-12 bug found live in broker.py's self-test: `family` NOT being one of the
    plan's 4 named families (e.g. "Other" -- an untagged/legacy/test source with no
    recognized commands.source) must ALSO return None here, unconditionally, even on
    an otherwise-governed symbol. The plan only allocates shares to GevaExtract/
    Critical Line/Spread/Correlation; anything outside those 4 was falling through to
    `.get(family, 0)`'s 0 default, which check_admission() then read as "capped at
    zero, forever" -- silently blocking EVERY untagged command on any governed symbol,
    regardless of how much real capacity existed. "Not one of the 4 families" must mean
    "ungoverned", exactly like an un-dedicated stock, not "zero slots".
    """
    if family not in ALLOC_FAMILY_SOURCES:
        return None

    pair_name, _ = pair_for_symbol(symbol)
    if pair_name:
        return ALLOC_PAIR_PLAN.get(pair_name, {}).get(family, 0)

    owner = dedicated_family_for_stock(symbol)
    if owner is not None:
        return len(ALLOC_STOCK_DEDICATED[family]) if family == owner else 0

    return None


def dynamic_cap_for(con, family: str, symbol: str, direction: str) -> int | None:
    """
    2026-09-14: the effective cap for `family` on `symbol`'s pool RIGHT NOW, not
    just its static nominal share. Only meaningful for the futures pools (each
    ALLOC_PAIR_PLAN pool's per-family shares already sum to exactly that pool's
    real combined capacity -- 15-per-symbol x 2 symbols = 30, e.g. MNQ+NQ's
    10+10+10 -- so it's a clean partition, not oversubscribed). Dedicated stocks
    are exclusive to one family already (no sharing possible there, nothing to
    reclaim), so this returns the same as allocated_cap_for() for those.

    Motivation (explicit user instruction, 2026-09-14): "we consume the most
    resource... for algorithm that might run and might not. This should be
    dynamic." Before this, GevaExtract/Critical Line's nominal share sat
    reserved and unusable by Spread/Correlation even while those families had
    zero resting orders (a brand-new signal source that may not fire for
    hours) -- confirmed live: Critical Line pinned at "cap of 10" on NQ/MYM
    while Spread/Correlation's shares on those same pools sat idle.

    Returns nominal_share + (sum of every OTHER family's own unused nominal
    share on this pool right now) -- i.e. a family may grow past its own
    guaranteed floor into whatever the others aren't currently using, and
    shrinks back the instant they start using it again (recomputed fresh on
    every call from live commands-table state, no separate reservation to
    release/leak). Returns None if ungoverned, same contract as
    allocated_cap_for().
    """
    nominal = allocated_cap_for(family, symbol)
    if nominal is None:
        return None

    pair_name, pool_syms = pair_for_symbol(symbol)
    if not pair_name:
        return nominal  # dedicated stock: exclusive already, nothing to borrow

    from lib.db import compute_family_pool_resting
    plan = ALLOC_PAIR_PLAN.get(pair_name, {})
    unused_from_others = 0
    for fam, fam_nominal in plan.items():
        if fam == family:
            continue
        fam_resting = compute_family_pool_resting(
            con, ALLOC_FAMILY_SOURCES.get(fam, set()), pool_syms, direction)
        unused_from_others += max(0, fam_nominal - fam_resting)

    return nominal + unused_from_others


def check_admission(con, symbol: str, direction: str, source: str):
    """
    Family-level admission check for one new command, on top of (not instead of) the
    existing flat per-symbol-per-direction cap in lib.db.compute_side_resting(). Uses
    the exact same "same-direction entries + 2x opposite-direction entries" IB-realistic
    counting formula, scoped to this family's own sources and this symbol's pool.

    Cap is dynamic (dynamic_cap_for(), 2026-09-14) -- a family may use more than its
    nominal share of a shared futures pool when another family in that pool isn't
    currently using its own share.

    Returns (allowed: bool, resting: int, cap: int | None). cap=None means this
    symbol isn't governed by the plan -- always allowed here (the flat per-symbol cap
    elsewhere is still the real gate for it).
    """
    family = family_for_source(source)
    cap = dynamic_cap_for(con, family, symbol, direction)
    if cap is None:
        return True, 0, None

    from lib.db import compute_family_pool_resting
    pool = pool_symbols_for(symbol)
    resting = compute_family_pool_resting(con, ALLOC_FAMILY_SOURCES.get(family, {source}), pool, direction)
    return resting < cap, resting, cap


# ── Self-test ───────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        # 1. Family classification
        assert family_for_source("geva_extract") == "GevaExtract"
        assert family_for_source("research_ce_stock") == "Critical Line"
        assert family_for_source("spread_control") == "Spread"
        assert family_for_source("correlation") == "Correlation"
        assert family_for_source("something_else") == "Other"

        # 2. Pair lookup
        assert pair_for_symbol("MES") == ("MES + ES", ["MES", "ES"])
        assert pair_for_symbol("ES")  == ("MES + ES", ["MES", "ES"])
        assert pair_for_symbol("RTY") == ("M2K + RTY", ["M2K", "RTY"])
        assert pair_for_symbol("AAPL") == (None, None)

        # 3. Pool symbols
        assert set(pool_symbols_for("MNQ")) == {"MNQ", "NQ"}
        assert pool_symbols_for("AAPL") == ["AAPL"]

        # 4. Allocated cap: futures pool (pooled, not sub-split). 2026-09-14 rebalance:
        # Correlation > GevaExtract > Critical Line > Spread (see ALLOC_PAIR_PLAN's own
        # comment for rationale).
        assert allocated_cap_for("GevaExtract", "MES") == 15
        assert allocated_cap_for("GevaExtract", "ES")  == 15
        assert allocated_cap_for("Critical Line", "MES") == 7
        assert allocated_cap_for("Critical Line", "NQ")  == 13
        assert allocated_cap_for("Correlation", "MNQ") == 15
        assert allocated_cap_for("Spread", "MNQ") == 2
        # GevaExtract never trades MNQ+NQ etc -- 0, not None (it IS a governed pool,
        # just with a zero share for this family).
        assert allocated_cap_for("GevaExtract", "MNQ") == 0

        # 5. Allocated cap: dedicated stocks (2026-09-14: 26/26/25 split, up from 10/10/10)
        assert allocated_cap_for("Critical Line", "AAPL") == 26
        assert allocated_cap_for("Spread", "AAPL") == 0           # AAPL is Critical Line's, not Spread's
        assert allocated_cap_for("Spread", "NFLX") == 26
        assert allocated_cap_for("Critical Line", "UNKNOWNSTOCK") is None  # not yet governed

        # 5b. "Other" (an untagged/legacy/test source) must be UNGOVERNED even on an
        # otherwise-governed symbol -- a real bug caught live 2026-09-12: this used to
        # fall through to a 0 default, silently blocking every untagged command on
        # MES/MNQ/etc. forever regardless of real capacity. Not one of the plan's 4
        # named families must mean "no opinion here", same as an un-dedicated stock.
        assert allocated_cap_for("Other", "MES") is None
        assert allocated_cap_for("Other", "AAPL") is None

        # 6. check_admission against a real (temp) DB
        import tempfile
        from pathlib import Path
        from lib.db import get_db, init_db
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alloc_test.db"
            init_db(db_path)
            with get_db(db_path) as con:
                def _insert(symbol, direction, source, status):
                    con.execute("""
                        INSERT INTO commands
                            (symbol, line_price, line_type, line_strength, direction,
                             entry_type, entry_price, tp_price, sl_price, bracket_size,
                             source, quantity, logical_trade_id, status)
                        VALUES (?, 100, 'SUPPORT', 1, ?, 'MKT', 100, 100, 100, 4,
                                ?, 1, ?, ?)
                    """, (symbol, direction, source, f"lt-{symbol}-{direction}-{source}-{status}", status))

                # Critical Line has 7 BUY entries resting on MES -- its own nominal
                # share on MES+ES (7, 2026-09-14 rebalance) -- but with GevaExtract/
                # Spread/Correlation all completely idle on this pool right now,
                # Critical Line's EFFECTIVE cap grows to reclaim their unused share
                # (15+2+6=23 unused, +7 own nominal = 30, the pool's full real
                # combined size) -- an 8th is now ALLOWED, not refused. This is the
                # actual fix for the user-reported problem: a family "that might run
                # and might not" (GevaExtract/Spread/Correlation, all idle here) must
                # not keep capacity reserved and unusable by a family that wants it
                # right now.
                for i in range(7):
                    _insert("MES", "BUY", "research_ce", "SUBMITTED")
                allowed, resting, cap = check_admission(con, "MES", "BUY", "research_ce")
                assert allowed and resting == 7 and cap == 30, (allowed, resting, cap)

                # Pooling still holds: ES sees the same resting/cap as MES (shared budget).
                allowed_es, resting_es, cap_es = check_admission(con, "ES", "BUY", "research_ce")
                assert allowed_es and resting_es == 7 and cap_es == 30, \
                    "MES+ES is a POOLED cap -- ES should see the same numbers MES has"

                # A DIFFERENT family (Spread) on the same MES+ES pool: its own nominal
                # floor is 2 (2026-09-14: cut to a minimal floor), but it ALSO reclaims
                # GevaExtract's (15) and Correlation's (6) fully-unused shares --
                # Critical Line's own share is fully used (7 resting == 7 nominal,
                # nothing left there to reclaim from it) -- so Spread's cap = 2 (own) +
                # 15 (GevaExtract idle) + 0 (CL fully used) + 6 (Correlation idle) = 23.
                allowed_spread, resting_spread, cap_spread = check_admission(con, "MES", "BUY", "spread")
                assert allowed_spread and resting_spread == 0 and cap_spread == 23, \
                    (allowed_spread, resting_spread, cap_spread)

                # Fill Critical Line to the pool's true combined ceiling (30 = 15/symbol
                # x 2) -- now EVERY other family's reclaim-from-CL contribution is 0
                # (nothing left to borrow from a fully-consumed family), proving the
                # reclaim shrinks back down rather than staying permanently generous.
                for i in range(7, 30):
                    _insert("MES" if i % 2 == 0 else "ES", "BUY", "research_ce", "SUBMITTED")
                allowed_full, resting_full, cap_full = check_admission(con, "MES", "BUY", "research_ce")
                assert not allowed_full and resting_full == 30 and cap_full == 30, \
                    (allowed_full, resting_full, cap_full)
                # Spread's cap stays 2(own)+15(GevaExtract idle)+0(CL exhausted)
                # +6(Correlation idle) = 23 (CL's own nominal floor was already fully
                # used before, contributing 0 either way) -- but a real drop is
                # visible once GevaExtract itself starts using its share too:
                for i in range(10):
                    _insert("MES", "SELL", "geva_manual", "SUBMITTED")
                # opposite-direction FILLED-style resting counts 2x in the same-family
                # pool formula (compute_family_pool_resting) -- use BUY here instead to
                # keep this simple and additive against Spread's own BUY-side cap check.
                allowed_spread2, resting_spread2, cap_spread2 = check_admission(con, "MES", "BUY", "spread")
                assert cap_spread2 < cap_spread, \
                    f"GevaExtract now active -- Spread's reclaimed cap must shrink, got {cap_spread2} vs {cap_spread}"

                # An un-dedicated stock is ungoverned -- always "allowed" here (cap=None).
                allowed_stock, resting_stock, cap_stock = check_admission(con, "ZZZZ", "BUY", "research_ce")
                assert allowed_stock and cap_stock is None

        print("PASS -- lib.allocation: family classification, pair/stock pooling, "
              "and DB-backed admission check")
        return True
    except Exception as e:
        import traceback
        print(f"FAIL -- lib.allocation self_test: {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
