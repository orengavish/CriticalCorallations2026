"""
trader/scripts/prep_research_lines_stocks.py
Same job as prep_research_lines.py (the two backtested-winning candidate reasons +
a matched random-price control, inserted into Galao's critical_lines, armed) but for
the ~100-stock research universe instead of the 4 futures -- a separate script because
the DATA SOURCE differs: stocks route through MultiSymbolTrader's own already-computed
critical_lines (mst_db, written by scheduler/morning_lines.py from cached IB daily
bars), not a live CriticalExtraction generator call against Fetcher2026 data.

Prerequisite: mst_data/daily_bars.py --fetch-all and scheduler/morning_lines.py must
already have been run for the target date (both against the paper IB connection).

Reads mst_db's critical_lines.note JSON ({"reasons": [...], "has_pivot_confluence": bool})
to reconstruct the same reason-key format CriticalExtraction's own backtest used
("+".join(sorted(reasons)) [+"+PIVOT_CONFLUENCE"]).

Usage:
    python trader/scripts/prep_research_lines_stocks.py --date 2026-09-08
    python trader/scripts/prep_research_lines_stocks.py --date 2026-09-08 --dry-run

Self-test:
    python trader/scripts/prep_research_lines_stocks.py --self-test
"""

import sys
import json
import random
import argparse
import sqlite3
from pathlib import Path

_MST_ROOT = Path(r"C:\Projects\MultiSymbolTrader")
sys.path.insert(0, str(_MST_ROOT))
from mst_data.daily_bars import get_daily_bars  # noqa: E402

WINNING_REASONS = {"PREVIOUS_DAY_LOW", "PREVIOUS_DAY_HIGH+PIVOT_CONFLUENCE"}
MST_DB_PATH = _MST_ROOT / "data_cache" / "multisymbol_trader.db"  # matches mst_db.DB_PATH
GALAO_DB_PATH = Path(r"C:\Projects\CriticalCorallations2026\trader\data\galao.db")

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


def _reason_key(note_json: str) -> str:
    try:
        note = json.loads(note_json) if note_json else {}
    except (json.JSONDecodeError, TypeError):
        return ""
    reasons = note.get("reasons", [])
    key = "+".join(sorted(set(r for r in reasons if not r.startswith("PIVOT_"))))
    if note.get("has_pivot_confluence"):
        key += "+PIVOT_CONFLUENCE"
    return key


def find_qualifying_lines(as_of_date: str, mst_db_path: Path = MST_DB_PATH) -> list[dict]:
    con = sqlite3.connect(str(mst_db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol, line_type, price, note FROM critical_lines WHERE date=?",
        (as_of_date,),
    ).fetchall()
    con.close()

    rng = random.Random(f"research_lines_stocks_{as_of_date}")
    pairs = []
    for r in rows:
        reason = _reason_key(r["note"])
        if reason not in WINNING_REASONS:
            continue
        strength = 1 if "PIVOT_CONFLUENCE" in reason else 2
        bars = get_daily_bars(r["symbol"], as_of_date, lookback_days=1)
        ref_price = bars[-1]["close"] if bars else None

        pair = {"symbol": r["symbol"], "date": as_of_date, "reason": reason,
                "real": {"line_type": r["line_type"], "price": r["price"], "strength": strength},
                "random": None}
        if ref_price is not None:
            distance = abs(r["price"] - ref_price)
            random_price = ref_price + rng.choice([-1, 1]) * distance
            random_type = "SUPPORT" if random_price < ref_price else "RESISTANCE"
            pair["random"] = {"line_type": random_type, "price": round(random_price, 2),
                               "strength": strength, "reference_price": ref_price}
        pairs.append(pair)
    return pairs


def insert_pairs(pairs: list[dict], db_path: Path = GALAO_DB_PATH, dry_run: bool = False) -> int:
    inserted = 0
    con = None
    if not dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA_CRITICAL_LINES)
        con.execute(
            "DELETE FROM critical_lines WHERE date=? AND source IN"
            " ('research_ce_stock','research_random_stock')",
            (pairs[0]["date"] if pairs else "1970-01-01",),
        )

    for pair in pairs:
        note = json.dumps({"reason": pair["reason"], "kind": "real",
                            "experiment": "2026-09-07_two_winning_reasons_stocks"})
        print(f"  [real]   {pair['symbol']:6} {pair['real']['line_type']:10} "
              f"@ {pair['real']['price']:<10} reason={pair['reason']}")
        if not dry_run:
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength, "
                "armed, source, algo_type, note) VALUES (?,?,?,?,?,1,'research_ce_stock','RESEARCH',?)",
                (pair["symbol"], pair["date"], pair["real"]["line_type"],
                 pair["real"]["price"], pair["real"]["strength"], note),
            )
            inserted += 1
        if pair["random"] is not None:
            r = pair["random"]
            rnote = json.dumps({"reason": pair["reason"], "kind": "random_control",
                                 "reference_price": r["reference_price"],
                                 "matched_real_price": pair["real"]["price"],
                                 "experiment": "2026-09-07_two_winning_reasons_stocks"})
            print(f"  [random] {pair['symbol']:6} {r['line_type']:10} @ {r['price']:<10} "
                  f"(matched to reason={pair['reason']})")
            if not dry_run:
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, "
                    "armed, source, algo_type, note) VALUES (?,?,?,?,?,1,'research_random_stock','RESEARCH',?)",
                    (pair["symbol"], pair["date"], r["line_type"], r["price"], r["strength"], rnote),
                )
                inserted += 1
    if con is not None:
        con.commit()
        con.close()
    return inserted


def self_test() -> bool:
    try:
        import tempfile
        fixture_db = None
        with tempfile.TemporaryDirectory() as tmp:
            fixture_db = Path(tmp) / "mst_fixture.db"
            con = sqlite3.connect(str(fixture_db))
            con.execute(
                "CREATE TABLE critical_lines (symbol TEXT, date TEXT, line_type TEXT,"
                " price REAL, note TEXT)"
            )
            con.executemany(
                "INSERT INTO critical_lines VALUES (?,?,?,?,?)",
                [
                    ("FAKE1", "2026-09-08", "SUPPORT", 100.0,
                     json.dumps({"reasons": ["PREVIOUS_DAY_LOW"], "has_pivot_confluence": False})),
                    ("FAKE2", "2026-09-08", "RESISTANCE", 200.0,
                     json.dumps({"reasons": ["PREVIOUS_DAY_HIGH"], "has_pivot_confluence": True})),
                    ("FAKE3", "2026-09-08", "SUPPORT", 50.0,
                     json.dumps({"reasons": ["FIVE_DAY_LOW"], "has_pivot_confluence": False})),
                ],
            )
            con.commit()
            con.close()

            import mst_data.daily_bars as db_mod
            original_gdb = get_daily_bars
            fake_bars = {"FAKE1": [{"date": "2026-09-05", "close": 99.0}],
                         "FAKE2": [{"date": "2026-09-05", "close": 201.0}]}

            def fake_get_daily_bars(symbol, as_of_date, lookback_days):
                return fake_bars.get(symbol, [])

            mod = sys.modules[__name__]
            mod.get_daily_bars = fake_get_daily_bars
            try:
                pairs = find_qualifying_lines("2026-09-08", mst_db_path=fixture_db)
            finally:
                mod.get_daily_bars = original_gdb

            assert len(pairs) == 2, f"expected 2 qualifying (FAKE3 must be excluded), got {len(pairs)}"
            symbols_found = {p["symbol"] for p in pairs}
            assert symbols_found == {"FAKE1", "FAKE2"}

            test_galao_db = Path(tmp) / "galao_fixture.db"
            n = insert_pairs(pairs, db_path=test_galao_db, dry_run=False)
            assert n == 4  # 2 real + 2 random

            con2 = sqlite3.connect(str(test_galao_db))
            con2.row_factory = sqlite3.Row
            rows = con2.execute("SELECT * FROM critical_lines WHERE source='research_ce_stock'").fetchall()
            assert len(rows) == 2
            for row in rows:
                assert row["armed"] == 1
            con2.close()

        print(f"[self-test] prep_research_lines_stocks: PASS")
        return True
    except Exception as e:
        print(f"[self-test] prep_research_lines_stocks: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, required unless --self-test")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    if not args.date:
        print("--date is required (the date scheduler/morning_lines.py was run for)")
        sys.exit(1)

    pairs = find_qualifying_lines(args.date)
    print(f"Date: {args.date} -- {len(pairs)} qualifying stock line(s) "
          f"(reasons: {', '.join(sorted(WINNING_REASONS))})")
    if not pairs:
        print("Nothing to insert.")
        sys.exit(0)
    n = insert_pairs(pairs, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\n[dry-run] would insert {sum(2 if p['random'] else 1 for p in pairs)} row(s) -- no DB writes made.")
    else:
        print(f"\nInserted {n} row(s) into {GALAO_DB_PATH}")
