"""
trader/correlation_signal.py
Correlation algorithm (Part 2 of the comparison-engine build cycle): Geva's
laggard/breakout-retest rule (Galgo2029/knowledge/Claims/correlation-trading.md,
AI-26-AI-34, DQ-005), implemented as price/bar-based polling -- not a live DOM
subscription (2026-09-11 decision: that would need a genuinely different,
reactive execution path than this system's poll loop). A new signal generator
feeding the EXISTING single-symbol command pipeline via critical_lines, same
shape as Algo 1-5 (trader/scripts/prep_research_lines.py) -- no commands-table
schema change needed for the core case.

Rule: watch this system's 4 symbols (MES/MNQ/MYM/M2K -- proxies for the taught
ES/NQ/YM/RTY) for a shared breakout. When >=MIN_LEADERS of them break a level
in the same direction, retest it, and FAIL to reclaim it (DQ-005, hard
disqualification: never enter on the break itself -- the retest is what drags
a too-early entry back into a loss), the remaining symbol(s) that haven't
broken yet ("laggards") get a critical_lines row armed in the break direction,
picked by $-distance-to-target when more than one qualifies (AI-33, via
lib/atr.py). decider.py's existing generate_commands_for_new_lines() (added
alongside this) turns that armed line into a real order within one poll cycle.

State persisted in `correlation_watch` (survives decider restarts) -- see
lib/db.py's schema comment for the WATCHING -> BROKEN -> RETESTED ->
FAILED_RECLAIM|RECLAIMED lifecycle.

Detection deliberately reads LIVE prices, not trader/data/bars.db: that table's
finest granularity is 30-min bars and it's stale in practice (backfill lag) --
useless for tracking a break/retest/reclaim sequence that plays out over
minutes. lib/atr.py's stale-tolerant daily-ATR estimate is fine for the laggard
tie-break (AI-33), which doesn't need fresh data, only a rough volatility read.

Usage (called from decider.py's replenishment loop, one call per poll cycle):
    from trader.correlation_signal import check_correlation_signal
    check_correlation_signal(prices, cfg, db_path)
        # prices: {"MES": 5500.25, "MNQ": ..., "MYM": ..., "M2K": ...}

Self-test:
    python trader/correlation_signal.py --self-test
"""

import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db, init_db
from lib.order_builder import get_tick_size
from lib.algo_pnl import SYMBOL_MULTIPLIERS
from lib.atr import atr20_points
from lib.logger import get_logger

log = get_logger("correlation_signal")

SYMBOLS = ["MES", "MNQ", "MYM", "M2K"]

# How close price must come to the line, after breaking it, to count as a
# "retest" -- and how far it must then move AWAY from the line (staying on the
# broken side) to confirm a fail-to-reclaim, vs. back through it to confirm a
# reclaim (false break). Both in ticks, symbol-scaled via get_tick_size().
RETEST_TICKS  = 3
CONFIRM_TICKS = 3

# A correlation event needs at least this many symbols confirmed FAILED_RECLAIM
# in the same direction, within this rolling window, before a laggard entry
# fires (AI-26/AI-30: "3 indices break," the remaining one(s) as laggard
# candidate(s), AI-33 if more than one qualifies).
MIN_LEADERS = 3
EVENT_WINDOW_MINUTES = 60


def _side(price: float, line_price: float) -> str:
    return "ABOVE" if price >= line_price else "BELOW"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _update_one_watch(con, symbol: str, date_str: str, line: dict, price: float, now: str) -> dict:
    """
    Advance (or create) the watch state for one (symbol, critical_line) pair
    given the latest price. Returns the updated row as a dict.
    """
    tick = get_tick_size(symbol)
    line_id, line_price, line_type = line["id"], line["price"], line["line_type"]

    row = con.execute(
        "SELECT * FROM correlation_watch WHERE symbol=? AND critical_line_id=?",
        (symbol, line_id)
    ).fetchone()

    side = _side(price, line_price)

    if row is None:
        con.execute("""
            INSERT INTO correlation_watch
                (symbol, date, critical_line_id, line_price, line_type,
                 last_price, last_side, status, updated_at)
            VALUES (?,?,?,?,?,?,?, 'WATCHING', ?)
        """, (symbol, date_str, line_id, line_price, line_type, price, side, now))
        return dict(con.execute(
            "SELECT * FROM correlation_watch WHERE symbol=? AND critical_line_id=?",
            (symbol, line_id)
        ).fetchone())

    row = dict(row)
    status = row["status"]

    if status == "WATCHING":
        if side != row["last_side"]:
            # Crossed the line for the first time -> BROKEN. Crossing UP through
            # a RESISTANCE or DOWN through a SUPPORT both read as a breakout in
            # the direction just crossed.
            direction = "UP" if side == "ABOVE" else "DOWN"
            con.execute("""
                UPDATE correlation_watch SET status='BROKEN', break_direction=?,
                    broken_at=?, last_price=?, last_side=?, updated_at=?
                WHERE id=?
            """, (direction, now, price, side, now, row["id"]))
        else:
            con.execute(
                "UPDATE correlation_watch SET last_price=?, updated_at=? WHERE id=?",
                (price, now, row["id"])
            )

    elif status == "BROKEN":
        if abs(price - line_price) <= RETEST_TICKS * tick:
            con.execute("""
                UPDATE correlation_watch SET status='RETESTED', retested_at=?,
                    last_price=?, last_side=?, updated_at=?
                WHERE id=?
            """, (now, price, side, now, row["id"]))
        else:
            con.execute(
                "UPDATE correlation_watch SET last_price=?, last_side=?, updated_at=? WHERE id=?",
                (price, side, now, row["id"])
            )

    elif status == "RETESTED":
        broke_up = row["break_direction"] == "UP"
        confirm_dist = CONFIRM_TICKS * tick
        if broke_up and price >= line_price + confirm_dist:
            con.execute("""
                UPDATE correlation_watch SET status='FAILED_RECLAIM', resolved_at=?,
                    last_price=?, last_side=?, updated_at=?
                WHERE id=?
            """, (now, price, side, now, row["id"]))
        elif (not broke_up) and price <= line_price - confirm_dist:
            con.execute("""
                UPDATE correlation_watch SET status='FAILED_RECLAIM', resolved_at=?,
                    last_price=?, last_side=?, updated_at=?
                WHERE id=?
            """, (now, price, side, now, row["id"]))
        elif (broke_up and price < line_price) or ((not broke_up) and price > line_price):
            # Crossed back through the line to the ORIGINAL (pre-break) side --
            # DQ-005's exact failure mode: the retest dragged it back. False break.
            con.execute("""
                UPDATE correlation_watch SET status='RECLAIMED', resolved_at=?,
                    last_price=?, last_side=?, updated_at=?
                WHERE id=?
            """, (now, price, side, now, row["id"]))
        else:
            con.execute(
                "UPDATE correlation_watch SET last_price=?, last_side=?, updated_at=? WHERE id=?",
                (price, side, now, row["id"])
            )

    else:
        # FAILED_RECLAIM / RECLAIMED are terminal for this line -- just track price.
        con.execute(
            "UPDATE correlation_watch SET last_price=?, last_side=?, updated_at=? WHERE id=?",
            (price, side, now, row["id"])
        )

    return dict(con.execute(
        "SELECT * FROM correlation_watch WHERE id=?", (row["id"],)
    ).fetchone())


def _pick_laggard(candidates: list, bars_db_path) -> str | None:
    """
    Among candidate laggard symbols, pick the one with the largest $-distance
    to its own ATR-based target (AI-33). Falls back to the first candidate if
    ATR data isn't available for any of them -- still correct for the common
    1-laggard case, where there's nothing to pick between.
    """
    if len(candidates) == 1:
        return candidates[0]

    best_sym, best_dollars = None, -1.0
    for sym in candidates:
        atr = atr20_points(bars_db_path, sym)
        if atr is None:
            continue
        dollars = atr * SYMBOL_MULTIPLIERS.get(sym, 1.0)
        if dollars > best_dollars:
            best_sym, best_dollars = sym, dollars
    return best_sym or candidates[0]


def check_correlation_signal(prices: dict, cfg, db_path, bars_db_path=None) -> int:
    """
    One poll cycle: advance every symbol's line-watch state given `prices`,
    then check whether >=MIN_LEADERS symbols have FAILED_RECLAIM in the same
    direction (within EVENT_WINDOW_MINUTES) to arm a correlation line on the
    remaining laggard(s). Returns 1 if a line was armed, 0 otherwise.

    `prices`: {symbol: current_price}. Only symbols present (and in SYMBOLS)
    are processed -- a caller that can't get a live price for one symbol this
    cycle should omit it rather than pass a stale value.
    """
    init_db(db_path)
    bars_db_path = bars_db_path or (Path(db_path).parent / "bars.db")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = _now()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=EVENT_WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db(db_path) as con:
        for symbol, price in prices.items():
            if symbol not in SYMBOLS or price is None:
                continue
            lines = con.execute(
                "SELECT id, price, line_type FROM critical_lines "
                "WHERE symbol=? AND date=? AND armed=1", (symbol, date_str)
            ).fetchall()
            for line in lines:
                _update_one_watch(con, symbol, date_str, dict(line), price, now)

        for direction in ("UP", "DOWN"):
            leaders = con.execute("""
                SELECT DISTINCT symbol FROM correlation_watch
                WHERE status='FAILED_RECLAIM' AND break_direction=?
                  AND triggered_line_id IS NULL AND resolved_at >= ?
            """, (direction, cutoff)).fetchall()
            leader_symbols = {r["symbol"] for r in leaders}
            if len(leader_symbols) < MIN_LEADERS:
                continue

            candidates = [s for s in prices if s in SYMBOLS and s not in leader_symbols]
            # A candidate must be a genuine non-mover today: no BROKEN/RETESTED/
            # FAILED_RECLAIM state of its own in this same direction.
            candidates = [
                s for s in candidates
                if not con.execute(
                    "SELECT 1 FROM correlation_watch WHERE symbol=? AND date=? "
                    "AND status IN ('BROKEN','RETESTED','FAILED_RECLAIM') AND break_direction=?",
                    (s, date_str, direction)
                ).fetchone()
            ]
            if not candidates:
                continue

            laggard = _pick_laggard(candidates, bars_db_path)
            if not laggard or laggard not in prices:
                continue

            laggard_price = prices[laggard]
            line_type = "RESISTANCE" if direction == "UP" else "SUPPORT"
            note = json.dumps({"reason": "CORRELATION_LAGGARD", "kind": "real",
                               "break_direction": direction,
                               "leaders": sorted(leader_symbols)})

            cur = con.execute("""
                INSERT INTO critical_lines
                    (symbol, date, line_type, price, strength, armed, source, note)
                VALUES (?,?,?,?,1,1,'correlation',?)
            """, (laggard, date_str, line_type, laggard_price, note))
            new_line_id = cur.lastrowid

            # Mark every leader's triggering watch row as consumed so this same
            # event can't re-fire every poll cycle.
            for sym in leader_symbols:
                con.execute("""
                    UPDATE correlation_watch SET triggered_line_id=?
                    WHERE symbol=? AND status='FAILED_RECLAIM' AND break_direction=?
                      AND triggered_line_id IS NULL
                """, (new_line_id, sym, direction))

            log.info(f"Correlation signal: {direction} break confirmed by "
                     f"{sorted(leader_symbols)}, armed {laggard} {line_type} "
                     f"@ {laggard_price}")
            return 1

    return 0


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.config_loader import get_config
        cfg = get_config()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Arm a RESISTANCE line on all 4 symbols, all at a round "break" price.
            line_ids = {}
            with get_db(db_path) as con:
                for sym, price in [("MES", 5600.0), ("MNQ", 20000.0),
                                    ("MYM", 42000.0), ("M2K", 2200.0)]:
                    cur = con.execute(
                        "INSERT INTO critical_lines (symbol, date, line_type, price,"
                        " strength, armed, source) VALUES (?,?,'RESISTANCE',?,1,1,'manual')",
                        (sym, today, price)
                    )
                    line_ids[sym] = (cur.lastrowid, price)

            # Establish each watch row's initial state BELOW its line -- a break
            # is only detectable as a side CHANGE relative to a prior observation,
            # so the first poll must see "not broken yet" before a later one can
            # see the cross (this also matches the realistic case: a line usually
            # gets watched while price is still approaching it, not already past).
            n0 = check_correlation_signal(
                {"MES": 5599.0, "MNQ": 19999.0, "MYM": 41999.0, "M2K": 2199.0},
                cfg, db_path
            )
            assert n0 == 0

            # DQ-005 check: MES/MNQ/MYM break the level (price now above) but have
            # NOT yet retested -- must NOT arm anything on M2K yet, no matter how
            # "confirmed" the break looks, because entering on the break itself is
            # exactly what DQ-005 forbids.
            n = check_correlation_signal(
                {"MES": 5601.0, "MNQ": 20001.0, "MYM": 42001.0, "M2K": 2199.0},
                cfg, db_path
            )
            assert n == 0, "Must NOT arm on a bare break with no retest yet (DQ-005)"
            with get_db(db_path) as con:
                armed = con.execute(
                    "SELECT COUNT(*) FROM critical_lines WHERE source='correlation'"
                ).fetchone()[0]
            assert armed == 0

            # Retest: price comes back near the line (within RETEST_TICKS).
            n = check_correlation_signal(
                {"MES": 5600.05, "MNQ": 20000.1, "MYM": 42000.2, "M2K": 2199.0},
                cfg, db_path
            )
            assert n == 0, "Retest alone (no fail-to-reclaim confirmation yet) must not arm"

            # Fail to reclaim: price resumes UP, away from the line, on all 3 --
            # this is the actual DQ-005-compliant trigger. M2K hasn't moved.
            # MYM's move is larger than MES/MNQ's because CONFIRM_TICKS is scaled
            # by each symbol's own tick size (MYM=1.0 vs MES/MNQ=0.25) -- 2pts on
            # MYM wouldn't clear its own 3-tick=3pt confirmation distance.
            n = check_correlation_signal(
                {"MES": 5602.0, "MNQ": 20002.0, "MYM": 42005.0, "M2K": 2199.0},
                cfg, db_path
            )
            assert n == 1, "3 leaders FAILED_RECLAIM in the same direction must arm the laggard"

            with get_db(db_path) as con:
                armed_rows = con.execute(
                    "SELECT * FROM critical_lines WHERE source='correlation'"
                ).fetchall()
            assert len(armed_rows) == 1
            armed_row = dict(armed_rows[0])
            assert armed_row["symbol"] == "M2K", \
                f"Expected M2K (the only non-broken symbol) as laggard, got {armed_row['symbol']}"
            assert armed_row["line_type"] == "RESISTANCE"
            assert armed_row["armed"] == 1
            note = json.loads(armed_row["note"])
            assert note["break_direction"] == "UP"
            assert set(note["leaders"]) == {"MES", "MNQ", "MYM"}

            # Idempotency: the SAME event must not re-arm a second line on a later
            # poll cycle just because the leaders are still FAILED_RECLAIM.
            n_again = check_correlation_signal(
                {"MES": 5603.0, "MNQ": 20003.0, "MYM": 42003.0, "M2K": 2199.5},
                cfg, db_path
            )
            assert n_again == 0, "Same event must not re-arm a second laggard line"
            with get_db(db_path) as con:
                armed_count2 = con.execute(
                    "SELECT COUNT(*) FROM critical_lines WHERE source='correlation'"
                ).fetchone()[0]
            assert armed_count2 == 1

        # False-break (reclaim) case, separate DB: leaders break, retest, then
        # cross back through the line to the ORIGINAL side -- must resolve
        # RECLAIMED, never FAILED_RECLAIM, and never arm anything.
        with tempfile.TemporaryDirectory() as tmp2:
            db_path2 = Path(tmp2) / "test2.db"
            init_db(db_path2)
            today2 = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with get_db(db_path2) as con:
                for sym, price in [("MES", 5600.0), ("MNQ", 20000.0),
                                    ("MYM", 42000.0), ("M2K", 2200.0)]:
                    con.execute(
                        "INSERT INTO critical_lines (symbol, date, line_type, price,"
                        " strength, armed, source) VALUES (?,?,'RESISTANCE',?,1,1,'manual')",
                        (sym, today2, price)
                    )
            check_correlation_signal(
                {"MES": 5599.0, "MNQ": 19999.0, "MYM": 41999.0, "M2K": 2199.0}, cfg, db_path2)
            check_correlation_signal(
                {"MES": 5601.0, "MNQ": 20001.0, "MYM": 42001.0, "M2K": 2199.0}, cfg, db_path2)
            check_correlation_signal(
                {"MES": 5600.05, "MNQ": 20000.1, "MYM": 42000.2, "M2K": 2199.0}, cfg, db_path2)
            # Reclaim: back below the line on all 3, instead of resuming up.
            n_reclaim = check_correlation_signal(
                {"MES": 5598.0, "MNQ": 19998.0, "MYM": 41998.0, "M2K": 2199.0}, cfg, db_path2)
            assert n_reclaim == 0, "A reclaimed (false) break must never arm a laggard"
            with get_db(db_path2) as con:
                statuses = {r["symbol"]: r["status"] for r in con.execute(
                    "SELECT symbol, status FROM correlation_watch WHERE break_direction='UP'"
                ).fetchall()}
            for sym in ("MES", "MNQ", "MYM"):
                assert statuses[sym] == "RECLAIMED", f"{sym} should be RECLAIMED, got {statuses[sym]}"
            with get_db(db_path2) as con:
                armed3 = con.execute(
                    "SELECT COUNT(*) FROM critical_lines WHERE source='correlation'"
                ).fetchone()[0]
            assert armed3 == 0

        print("[self-test] correlation_signal: PASS")
        return True

    except Exception as e:
        print(f"[self-test] correlation_signal: FAIL — {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("correlation_signal — run --self-test to verify logic")
