"""
trader/scripts/import_geva_manual_lines.py
Automates real Geva-line ingestion (2026-09-10): reads today's already-parsed
support/resistance lines from GevaExtract's own geva.db (populated by its Facebook
scraper, extract.js) and inserts them into this system's own critical_lines table as
source='geva_manual', with a matched random-distance control per line
(source='geva_manual_control') -- same shape as the hand-parsed process used on
2026-09-09 (verified against that day's actual rows: strength '!'->1, ''->2, '?'->3;
control_price = reference_price + choice([-1,+1]) * |real_price - reference_price|).

GevaExtract's own EXECUTION pipeline stays fully blocked (2026-09-09 incident) -- this
only ever reads geva.db, a plain SQLite file, never touches GevaExtract's server/API.

Idempotent per day: re-running for a date that's already been imported is a no-op
unless --force.

Usage:
    python trader/scripts/import_geva_manual_lines.py                        # today, fetches live MES price as reference
    python trader/scripts/import_geva_manual_lines.py --date 2026-09-10
    python trader/scripts/import_geva_manual_lines.py --reference-price 7650.0  # skip the live IB fetch
    python trader/scripts/import_geva_manual_lines.py --dry-run
    python trader/scripts/import_geva_manual_lines.py --force               # redo an already-imported date

Self-test:
    python trader/scripts/import_geva_manual_lines.py --self-test
"""

import sys
import json
import random
import sqlite3
import argparse
from pathlib import Path
from datetime import date as ddate

GEVA_DB_PATH = Path(r"C:\Projects\GevaExtract\geva.db")
GALAO_DB_PATH = Path(r"C:\Projects\CriticalCorallations2026\trader\data\galao.db")

_STRENGTH_MAP = {"!": 1, "": 2, "?": 3}
_TYPE_MAP = {"sup": "SUPPORT", "res": "RESISTANCE"}


def read_geva_lines(as_of_date: str, geva_db_path: Path = GEVA_DB_PATH) -> list[dict]:
    """Real Geva lines already parsed into geva.db for as_of_date. sym is always 'ES'
    in geva.db (full-size S&P) -- same index price as this system's own MES."""
    con = sqlite3.connect(str(geva_db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT price, line_type, strength FROM lines WHERE date=? ORDER BY price",
        (as_of_date,)
    ).fetchall()
    con.close()
    return [
        {"price": r["price"], "line_type": _TYPE_MAP[r["line_type"]],
         "strength": _STRENGTH_MAP.get(r["strength"], 2)}
        for r in rows
    ]


def build_controls(real_lines: list[dict], reference_price: float,
                    rng: random.Random | None = None) -> list[dict]:
    rng = rng or random.Random()
    controls = []
    for line in real_lines:
        distance = abs(line["price"] - reference_price)
        control_price = reference_price + rng.choice([-1, 1]) * distance
        control_type = "RESISTANCE" if control_price >= reference_price else "SUPPORT"
        controls.append({
            "price": round(control_price, 2), "line_type": control_type,
            "strength": line["strength"],
            "matched_real_price": line["price"], "reference_price": reference_price,
        })
    return controls


def _already_imported(con, as_of_date: str) -> bool:
    row = con.execute(
        "SELECT COUNT(*) FROM critical_lines WHERE date=? AND source='geva_manual'",
        (as_of_date,)
    ).fetchone()
    return row[0] > 0


def import_lines(as_of_date: str, reference_price: float, db_path: Path = GALAO_DB_PATH,
                  dry_run: bool = False, force: bool = False,
                  geva_db_path: Path = GEVA_DB_PATH, rng: random.Random | None = None) -> dict:
    real_lines = read_geva_lines(as_of_date, geva_db_path)
    if not real_lines:
        return {"date": as_of_date, "real_inserted": 0, "control_inserted": 0,
                "skipped_reason": "no Geva lines found in geva.db for this date"}

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        if _already_imported(con, as_of_date) and not force:
            return {"date": as_of_date, "real_inserted": 0, "control_inserted": 0,
                    "skipped_reason": "already imported for this date (use --force to redo)"}

        controls = build_controls(real_lines, reference_price, rng=rng)

        if dry_run:
            return {"date": as_of_date, "real_inserted": len(real_lines),
                    "control_inserted": len(controls), "dry_run": True}

        if force:
            con.execute(
                "DELETE FROM critical_lines WHERE date=? AND source IN"
                " ('geva_manual','geva_manual_control')", (as_of_date,)
            )

        real_ids = []
        for line in real_lines:
            cur = con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength,"
                " armed, source, algo_type) VALUES ('MES', ?, ?, ?, ?, 1, 'geva_manual', 'MANUAL')",
                (as_of_date, line["line_type"], line["price"], line["strength"])
            )
            real_ids.append(cur.lastrowid)

        for control, real_id in zip(controls, real_ids):
            note = json.dumps({
                "matched_real_line_id": real_id,
                "matched_real_price": control["matched_real_price"],
                "reference_price": control["reference_price"],
            })
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength,"
                " armed, source, algo_type, note)"
                " VALUES ('MES', ?, ?, ?, ?, 1, 'geva_manual_control', 'MANUAL', ?)",
                (as_of_date, control["line_type"], control["price"], control["strength"], note)
            )
        con.commit()
        return {"date": as_of_date, "real_inserted": len(real_lines), "control_inserted": len(controls)}
    finally:
        con.close()


def _fetch_live_mes_price() -> float:
    """Only imported when actually needed (no --reference-price given) -- keeps
    --self-test and --dry-run runnable without a live IB connection."""
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from lib.config_loader import get_config
    from lib.ib_client import IBClient
    cfg = get_config(Path(__file__).parent.parent / "config.yaml")
    ibc = IBClient(cfg)
    ibc.connect(live=True, paper=False)
    try:
        price = ibc.get_price("MES")
        if price is None:
            raise RuntimeError("IB returned no price for MES")
        return price
    finally:
        ibc.disconnect()


def self_test() -> bool:
    try:
        import tempfile

        # 1. read_geva_lines + strength/type mapping, against a fixture geva.db.
        with tempfile.TemporaryDirectory() as tmp:
            geva_db = Path(tmp) / "geva.db"
            con = sqlite3.connect(str(geva_db))
            con.execute("CREATE TABLE lines (id INTEGER PRIMARY KEY, sym TEXT, date TEXT,"
                        " line_type TEXT, price REAL, strength TEXT)")
            con.executemany(
                "INSERT INTO lines (sym, date, line_type, price, strength) VALUES (?,?,?,?,?)",
                [("ES", "2026-09-09", "sup", 7607.75, "!"),
                 ("ES", "2026-09-09", "sup", 7621.25, ""),
                 ("ES", "2026-09-09", "sup", 7669.75, "?"),
                 ("ES", "2026-09-09", "res", 7838.25, "")]
            )
            con.commit(); con.close()

            lines = read_geva_lines("2026-09-09", geva_db_path=geva_db)
            assert len(lines) == 4
            by_price = {l["price"]: l for l in lines}
            assert by_price[7607.75]["strength"] == 1 and by_price[7607.75]["line_type"] == "SUPPORT"
            assert by_price[7621.25]["strength"] == 2
            assert by_price[7669.75]["strength"] == 3
            assert by_price[7838.25]["line_type"] == "RESISTANCE"

            # 2. build_controls -- verified formula against 2026-09-09's real hand-entered
            # control rows: control = ref + choice([-1,1]) * |real-ref|, type from side.
            controls = build_controls(lines, reference_price=7643.0, rng=random.Random(0))
            for c in controls:
                dist = abs(c["matched_real_price"] - 7643.0)
                assert abs(abs(c["price"] - 7643.0) - dist) < 1e-9, \
                    "control must be exactly `distance` away from the reference price"
                expected_type = "RESISTANCE" if c["price"] >= 7643.0 else "SUPPORT"
                assert c["line_type"] == expected_type

            # 3. import_lines end to end, with idempotency + force.
            galao_db = Path(tmp) / "galao.db"
            con = sqlite3.connect(str(galao_db))
            con.execute("""CREATE TABLE critical_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, date TEXT, line_type TEXT,
                price REAL, strength INTEGER, armed INTEGER DEFAULT 1, source TEXT,
                algo_type TEXT, note TEXT)""")
            con.commit(); con.close()

            r1 = import_lines("2026-09-09", reference_price=7643.0, db_path=galao_db,
                               geva_db_path=geva_db, rng=random.Random(1))
            assert r1["real_inserted"] == 4 and r1["control_inserted"] == 4

            con = sqlite3.connect(str(galao_db))
            con.row_factory = sqlite3.Row
            real_rows = con.execute(
                "SELECT * FROM critical_lines WHERE source='geva_manual'").fetchall()
            assert len(real_rows) == 4
            assert all(r["symbol"] == "MES" and r["armed"] == 1 for r in real_rows)
            control_rows = con.execute(
                "SELECT * FROM critical_lines WHERE source='geva_manual_control'").fetchall()
            assert len(control_rows) == 4
            for cr in control_rows:
                note = json.loads(cr["note"])
                assert note["reference_price"] == 7643.0
                assert note["matched_real_line_id"] in {r["id"] for r in real_rows}
            con.close()

            # Re-running without --force: no-op.
            r2 = import_lines("2026-09-09", reference_price=7643.0, db_path=galao_db,
                               geva_db_path=geva_db)
            assert r2["real_inserted"] == 0 and r2.get("skipped_reason")

            # --force: replaces, doesn't duplicate.
            r3 = import_lines("2026-09-09", reference_price=7650.0, db_path=galao_db,
                               geva_db_path=geva_db, force=True, rng=random.Random(2))
            assert r3["real_inserted"] == 4
            con = sqlite3.connect(str(galao_db))
            total = con.execute(
                "SELECT COUNT(*) FROM critical_lines WHERE source IN"
                " ('geva_manual','geva_manual_control')").fetchone()[0]
            assert total == 8, f"force must replace, not duplicate -- got {total}"
            con.close()

            # No lines for this date in geva.db -- clean no-op, not an error.
            r4 = import_lines("1999-01-01", reference_price=7000.0, db_path=galao_db,
                               geva_db_path=geva_db)
            assert r4["real_inserted"] == 0 and "no Geva lines" in r4["skipped_reason"]

        print("[self-test] import_geva_manual_lines: PASS")
        return True
    except Exception as e:
        print(f"[self-test] import_geva_manual_lines: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default: today")
    parser.add_argument("--reference-price", type=float, default=None,
                         help="Skip the live IB fetch and use this as the control reference price")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Redo an already-imported date")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)

    as_of = args.date or ddate.today().isoformat()
    ref_price = args.reference_price
    if ref_price is None:
        print("No --reference-price given -- fetching live MES price from IB...")
        ref_price = _fetch_live_mes_price()
        print(f"Live MES reference price: {ref_price}")

    result = import_lines(as_of, ref_price, dry_run=args.dry_run, force=args.force)
    print(result)
    sys.exit(0 if "error" not in result else 1)
