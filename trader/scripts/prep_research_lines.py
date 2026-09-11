"""
trader/scripts/prep_research_lines.py
Overnight research experiment (2026-09-06/07): insert critical_lines rows for the two
candidate-reason categories that showed the most consistent backtested edge across every
bracket size in CriticalExtraction's trades-based sweep (see run_backtest.py/
summarize_sweep.py) -- "PREVIOUS_DAY_LOW" and "PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE",
exact reason-key match only, not a fuzzy prefix. Only these two get inserted; everything
else CriticalExtraction's generator produces for the day is deliberately skipped.

For each qualifying line, this inserts TWO critical_lines rows:
  - the real line (source='research_ce'), at CriticalExtraction's own computed price.
  - a matched RANDOM-price control (source='research_random'), at the same distance
    from a reference price, in a random direction -- mirrors run_backtest.py's
    algo-vs-random pairing. NOTE: the backtest's "day_open" is the session's first real
    trade, unknowable before the session starts. This script uses the PRIOR trading
    day's closing daily bar as a documented stand-in reference instead -- a deliberate,
    named simplification (ponytail: proxy reference price, not the real open; revisit
    if a pre-market snapshot becomes available).
The "opposite direction" role needs no separate row at all: decider.py's
generate_commands() already creates BOTH a BUY and a SELL command for every armed line,
so trading a line in both directions is the existing, unmodified, battle-tested
behavior -- exactly what the backtest's "algo_opposite" control measures.

Bracket sizes and which symbols are actively traded are config.yaml settings
(orders.active_brackets, symbols:) -- this script only touches critical_lines, never
config.yaml, never commands, never places any order.

Usage:
    python trader/scripts/prep_research_lines.py                 # today, all 4 symbols
    python trader/scripts/prep_research_lines.py --date 2026-09-07
    python trader/scripts/prep_research_lines.py --dry-run        # print only, no DB writes

Self-test:
    python trader/scripts/prep_research_lines.py --self-test
"""

import sys
import json
import random
import argparse
import sqlite3
from pathlib import Path
from datetime import date as ddate

_CE_ROOT = Path(r"C:\Projects\CriticalExtraction")
sys.path.insert(0, str(_CE_ROOT))
from config import get_config as get_ce_config          # noqa: E402
from generator.candidates import generate_candidates    # noqa: E402
from generator.lines import build_lines                 # noqa: E402
from data.market_data import get_daily_bars             # noqa: E402

SYMBOLS = ["M2K", "MES", "MNQ", "MYM"]

# 2026-09-11: reinstated 3 reasons that were computed by knowledge/rules.py all along but
# excluded here since 2026-09-07 -- they didn't hold up as consistently as the original
# two in that sweep, but the sample was small at the time (before the 7-year Databento
# backfill). User decision: re-enable live rather than re-backtest first; watch results.
WINNING_REASONS = {
    "PREVIOUS_DAY_LOW", "PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE",
    "FIVE_DAY_HIGH+PIVOT_CONFLUENCE", "FIVE_DAY_LOW", "PREVIOUS_DAY_LOW+PIVOT_CONFLUENCE",
}
DB_PATH = Path(r"C:\Projects\CriticalCorallations2026\trader\data\galao.db")

_SCHEMA_CRITICAL_LINES = """
CREATE TABLE IF NOT EXISTS critical_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    line_type   TEXT    NOT NULL,
    price       REAL    NOT NULL,
    strength    INTEGER NOT NULL,
    armed       INTEGER NOT NULL DEFAULT 1,
    source      TEXT    DEFAULT 'manual',
    algo_type   TEXT    DEFAULT 'MANUAL',
    note        TEXT,
    confidence  TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def _reason_key(line) -> str:
    key = "+".join(sorted(set(r for r in line.reasons if not r.startswith("PIVOT_"))))
    if line.has_pivot_confluence:
        key += "+PIVOT_CONFLUENCE"
    return key


def _reference_price(symbol: str, as_of_date: str, ce_cfg) -> float | None:
    """Prior trading day's close -- documented stand-in for the real (unknowable-ahead-
    of-time) session open. See module docstring."""
    bars = get_daily_bars(symbol, as_of_date, lookback_days=1)
    return bars[-1]["close"] if bars else None


def find_qualifying_lines(as_of_date: str, symbols: list[str] | None = None) -> list[dict]:
    """One dict per qualifying (real line, matched random control) pair."""
    symbols = symbols or SYMBOLS
    ce_cfg = get_ce_config()
    rng = random.Random(f"research_lines_{as_of_date}")  # deterministic per date, reproducible
    pairs = []

    for symbol in symbols:
        candidates = generate_candidates(symbol, as_of_date, ce_cfg)
        lines = build_lines(candidates, symbol, as_of_date, ce_cfg)
        ref_price = _reference_price(symbol, as_of_date, ce_cfg)

        for line in lines:
            reason = _reason_key(line)
            if reason not in WINNING_REASONS:
                continue
            line_type = "SUPPORT" if line.side == "sup" else "RESISTANCE"
            strength = 1 if line.has_pivot_confluence else 2

            pair = {
                "symbol": symbol, "date": as_of_date, "reason": reason,
                "real": {"line_type": line_type, "price": line.price, "strength": strength},
                "random": None,
            }
            if ref_price is not None:
                distance = abs(line.price - ref_price)
                random_price = ref_price + rng.choice([-1, 1]) * distance
                random_type = "SUPPORT" if random_price < ref_price else "RESISTANCE"
                pair["random"] = {"line_type": random_type, "price": round(random_price, 2),
                                   "strength": strength, "reference_price": ref_price}
            pairs.append(pair)

    return pairs


def insert_pairs(pairs: list[dict], db_path: Path = DB_PATH, dry_run: bool = False) -> int:
    inserted = 0
    con = None
    if not dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA_CRITICAL_LINES)
        # Idempotency: this experiment's own rows for this date only -- never touches
        # manual/other-source lines already in the table.
        con.execute(
            "DELETE FROM critical_lines WHERE date=? AND source IN ('research_ce','research_random')",
            (pairs[0]["date"],) if pairs else (ddate.today().isoformat(),),
        )

    for pair in pairs:
        real_note = json.dumps({"reason": pair["reason"], "kind": "real",
                                 "experiment": "2026-09-07_two_winning_reasons"})
        print(f"  [real]   {pair['symbol']} {pair['real']['line_type']:10} "
              f"@ {pair['real']['price']:<10} reason={pair['reason']}")
        if not dry_run:
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength, "
                "armed, source, algo_type, note) VALUES (?,?,?,?,?,1,'research_ce','RESEARCH',?)",
                (pair["symbol"], pair["date"], pair["real"]["line_type"],
                 pair["real"]["price"], pair["real"]["strength"], real_note),
            )
            inserted += 1

        if pair["random"] is not None:
            r = pair["random"]
            rand_note = json.dumps({"reason": pair["reason"], "kind": "random_control",
                                     "reference_price": r["reference_price"],
                                     "matched_real_price": pair["real"]["price"],
                                     "experiment": "2026-09-07_two_winning_reasons"})
            print(f"  [random] {pair['symbol']} {r['line_type']:10} @ {r['price']:<10} "
                  f"(matched to reason={pair['reason']})")
            if not dry_run:
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, "
                    "armed, source, algo_type, note) VALUES (?,?,?,?,?,1,'research_random','RESEARCH',?)",
                    (pair["symbol"], pair["date"], r["line_type"], r["price"], r["strength"], rand_note),
                )
                inserted += 1

    if con is not None:
        con.commit()
        con.close()
    return inserted


def self_test() -> bool:
    try:
        import tempfile
        pairs = find_qualifying_lines("2026-09-07", symbols=["M2K", "MES", "MNQ", "MYM"])
        assert isinstance(pairs, list)
        for p in pairs:
            assert p["reason"] in WINNING_REASONS
            assert p["real"]["line_type"] in ("SUPPORT", "RESISTANCE")
            if p["random"] is not None:
                assert p["random"]["line_type"] in ("SUPPORT", "RESISTANCE")

        # Reproducibility: same date -> same random-control prices (seeded by date).
        pairs2 = find_qualifying_lines("2026-09-07", symbols=["M2K", "MES", "MNQ", "MYM"])
        assert [p["random"]["price"] if p["random"] else None for p in pairs] == \
               [p["random"]["price"] if p["random"] else None for p in pairs2], \
            "random control price must be reproducible for the same date"

        with tempfile.TemporaryDirectory() as tmp:
            test_db = Path(tmp) / "test_galao.db"
            n = insert_pairs(pairs, db_path=test_db, dry_run=False)
            assert n == sum(2 if p["random"] else 1 for p in pairs)

            con = sqlite3.connect(str(test_db))
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM critical_lines WHERE source='research_ce'").fetchall()
            assert len(rows) == sum(1 for p in pairs)
            for r in rows:
                assert r["armed"] == 1

            # Re-running for the same date must not duplicate (idempotent replace).
            n2 = insert_pairs(pairs, db_path=test_db, dry_run=False)
            con2 = sqlite3.connect(str(test_db))
            total = con2.execute(
                "SELECT COUNT(*) FROM critical_lines WHERE source IN ('research_ce','research_random')"
            ).fetchone()[0]
            assert total == n, f"re-running must replace, not duplicate -- got {total}, expected {n}"
            con.close(); con2.close()

        print(f"[self-test] prep_research_lines: PASS ({len(pairs)} qualifying lines on 2026-09-07 fixture)")
        return True
    except Exception as e:
        print(f"[self-test] prep_research_lines: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default: today")
    parser.add_argument("--dry-run", action="store_true", help="print only, no DB writes")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)

    as_of = args.date or ddate.today().isoformat()
    pairs = find_qualifying_lines(as_of)
    print(f"Date: {as_of} -- {len(pairs)} qualifying line(s) "
          f"(reasons: {', '.join(sorted(WINNING_REASONS))})")
    if not pairs:
        print("Nothing to insert -- no line today matches either winning reason exactly.")
        sys.exit(0)
    n = insert_pairs(pairs, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\n[dry-run] would insert {sum(2 if p['random'] else 1 for p in pairs)} row(s) -- no DB writes made.")
    else:
        print(f"\nInserted {n} row(s) into {DB_PATH}")
