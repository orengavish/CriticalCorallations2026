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
ALLOC_PAIR_PLAN = {
    "MES + ES":  {"GevaExtract": 15, "Critical Line": 5, "Spread": 5, "Correlation": 5},
    "MNQ + NQ":  {"Critical Line": 10, "Spread": 10, "Correlation": 10},
    "MYM + YM":  {"Critical Line": 10, "Spread": 10, "Correlation": 10},
    "M2K + RTY": {"Critical Line": 10, "Spread": 10, "Correlation": 10},
}
# Top-30 of the real ~100-stock universe (MultiSymbolTrader/mst_data/sp100_symbols.txt),
# in order -- first 10 each dedicated to one algorithm.
ALLOC_STOCK_DEDICATED = {
    "Critical Line": ["AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "BRK.B", "AVGO", "TSLA"],
    "Spread":        ["LLY", "JPM", "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "JNJ"],
    "Correlation":   ["NFLX", "ABBV", "BAC", "CRM", "WMT", "KO", "CVX", "MRK", "ADBE", "PEP"],
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


def check_admission(con, symbol: str, direction: str, source: str):
    """
    Family-level admission check for one new command, on top of (not instead of) the
    existing flat per-symbol-per-direction cap in lib.db.compute_side_resting(). Uses
    the exact same "same-direction entries + 2x opposite-direction entries" IB-realistic
    counting formula, scoped to this family's own sources and this symbol's pool.

    Returns (allowed: bool, resting: int, cap: int | None). cap=None means this
    symbol isn't governed by the plan -- always allowed here (the flat per-symbol cap
    elsewhere is still the real gate for it).
    """
    family = family_for_source(source)
    cap = allocated_cap_for(family, symbol)
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

        # 4. Allocated cap: futures pool (pooled, not sub-split)
        assert allocated_cap_for("GevaExtract", "MES") == 15
        assert allocated_cap_for("GevaExtract", "ES")  == 15
        assert allocated_cap_for("Critical Line", "MES") == 5
        assert allocated_cap_for("Critical Line", "NQ")  == 10
        assert allocated_cap_for("Correlation", "MNQ") == 10
        # GevaExtract never trades MNQ+NQ etc -- 0, not None (it IS a governed pool,
        # just with a zero share for this family).
        assert allocated_cap_for("GevaExtract", "MNQ") == 0

        # 5. Allocated cap: dedicated stocks
        assert allocated_cap_for("Critical Line", "AAPL") == 10
        assert allocated_cap_for("Spread", "AAPL") == 0          # AAPL is Critical Line's, not Spread's
        assert allocated_cap_for("Correlation", "NFLX") == 10
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

                # Critical Line already has 5 BUY entries resting on MES (at its cap of 5
                # for MES+ES pooled) -- a 6th should be refused.
                for i in range(5):
                    _insert("MES", "BUY", "research_ce", "SUBMITTED")
                allowed, resting, cap = check_admission(con, "MES", "BUY", "research_ce")
                assert not allowed and resting == 5 and cap == 5, (allowed, resting, cap)

                # But the SAME family still has room on ES (0 used, cap 5 on the SAME
                # pooled MES+ES budget) -- wait, pooled means MES+ES SHARE the cap, so
                # this should ALSO be refused, since the pool is already at 5/5.
                allowed_es, resting_es, cap_es = check_admission(con, "ES", "BUY", "research_ce")
                assert not allowed_es and resting_es == 5 and cap_es == 5, \
                    "MES+ES is a POOLED cap -- ES should see the same 5 resting MES has"

                # A DIFFERENT family (Spread) on the same MES+ES pool has its own
                # separate 5-slot budget, untouched by Critical Line's usage.
                allowed_spread, resting_spread, cap_spread = check_admission(con, "MES", "BUY", "spread")
                assert allowed_spread and resting_spread == 0 and cap_spread == 5, \
                    (allowed_spread, resting_spread, cap_spread)

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
