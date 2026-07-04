"""
lib/db.py
SQLite database setup and helpers for Galao.
Initializes schema, enables WAL mode, and provides CRUD helpers.

Tables: commands, positions, ib_events, system_state, critical_lines, release_notes,
        fetch_log, completed_trades

Usage:
    from lib.db import get_db, init_db
    with get_db() as con:
        con.execute("SELECT * FROM commands WHERE status='PENDING'")

Self-test:
    python -m lib.db --self-test
"""

import sys
import sqlite3
import argparse
from pathlib import Path
from contextlib import contextmanager

_db_path: Path = None


def set_db_path(path):
    global _db_path
    _db_path = Path(path)


def _resolve_path() -> Path:
    if _db_path:
        return _db_path
    from lib.config_loader import get_config
    try:
        cfg = get_config()
        return Path(cfg.paths.db)
    except Exception:
        return Path("data/galao.db")


@contextmanager
def get_db(path: Path = None):
    """Context manager — yields a sqlite3 connection with WAL mode and row_factory."""
    db_path = path or _resolve_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    line_price          REAL    NOT NULL,
    line_type           TEXT    NOT NULL,    -- SUPPORT | RESISTANCE
    line_strength       INTEGER NOT NULL,    -- 1=strong 2=medium 3=low
    direction           TEXT    NOT NULL,    -- BUY | SELL
    entry_type          TEXT    NOT NULL,    -- LMT | STP | MKT
    entry_price         REAL    NOT NULL,
    tp_price            REAL    NOT NULL,
    sl_price            REAL    NOT NULL,
    bracket_size        REAL    NOT NULL,
    source              TEXT,               -- critical_line | random_mkt | random_lmt | random_stp | test
    parent_command_id   INTEGER,            -- set when this command was auto-replenished from another
    critical_line_id    INTEGER REFERENCES critical_lines(id),  -- origin line when source=critical_line
    quantity            INTEGER NOT NULL DEFAULT 1,
    status              TEXT    NOT NULL DEFAULT 'PENDING',
    -- PENDING | SUBMITTING | SUBMITTED | FILLED | EXITING | CLOSED
    -- CANCELLED | ERROR | RECONCILE_REQUIRED
    ib_order_id         INTEGER,
    ib_tp_order_id      INTEGER,
    ib_sl_order_id      INTEGER,
    claimed_at          TEXT,               -- ISO UTC timestamp, set when status→SUBMITTING
    replenishment_issued INTEGER NOT NULL DEFAULT 0,  -- 1 after Decider spawned replacement
    fill_price          REAL,
    fill_time           TEXT,               -- ISO UTC
    exit_price          REAL,
    exit_time           TEXT,               -- ISO UTC
    exit_reason         TEXT,               -- TP | SL | STAGNATION | SHUTDOWN | MANUAL
    pnl_points          REAL,
    error_message       TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id      INTEGER NOT NULL REFERENCES commands(id),
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,       -- BUY | SELL
    quantity        INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_time      TEXT    NOT NULL,
    price_at_check  REAL,
    last_checked_at TEXT,
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS ib_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,      -- ERROR | WARNING | INFO | RECONNECT | DISCONNECT
    component   TEXT NOT NULL,
    code        INTEGER,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS system_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS critical_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    date        TEXT    NOT NULL,           -- YYYY-MM-DD
    line_type   TEXT    NOT NULL,           -- SUPPORT | RESISTANCE
    price       REAL    NOT NULL,
    strength    INTEGER NOT NULL,           -- 1=strong 2=medium 3=low
    armed       INTEGER NOT NULL DEFAULT 1, -- 0 after SL cool-down disarms
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS release_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program     TEXT NOT NULL,
    version     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    details     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    date         TEXT    NOT NULL,       -- YYYY-MM-DD
    file_type    TEXT    NOT NULL,       -- trades | bidask
    status       TEXT    NOT NULL,       -- ok | skipped | error
    rows_fetched INTEGER,
    error_msg    TEXT,
    fetched_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS completed_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id   INTEGER NOT NULL UNIQUE REFERENCES commands(id),
    symbol       TEXT    NOT NULL,
    source       TEXT,                   -- critical_line | random_mkt | random_lmt | random_stp | test
    direction    TEXT    NOT NULL,       -- BUY | SELL
    entry_type   TEXT    NOT NULL,       -- MKT | LMT | STP
    bracket_size REAL    NOT NULL,
    ib_order_id  INTEGER,
    fill_price   REAL    NOT NULL,
    fill_time    TEXT    NOT NULL,
    exit_price   REAL    NOT NULL,
    exit_time    TEXT    NOT NULL,
    exit_reason  TEXT    NOT NULL,       -- TP | SL | SHUTDOWN | MANUAL
    pnl_points   REAL    NOT NULL,
    recorded_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_commands_status      ON commands(status);
CREATE TABLE IF NOT EXISTS price_cache (
    symbol          TEXT PRIMARY KEY,
    last_price      REAL    NOT NULL,
    last_fill_ts    TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'fill',
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS algo_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    date                TEXT    NOT NULL,           -- YYYY-MM-DD
    algo_type           TEXT    NOT NULL,           -- BOUNCE | BREAKOUT | DIRECTIONAL | FADE | BOTH
    tp_ticks            INTEGER NOT NULL,
    sl_ticks            INTEGER NOT NULL,
    direction_filter    TEXT    NOT NULL DEFAULT 'ALL',   -- ALL | BUY_ONLY | SELL_ONLY
    strength_max        INTEGER NOT NULL DEFAULT 3,       -- 1=strong only 2=+medium 3=all
    commands_generated  INTEGER NOT NULL DEFAULT 0,
    current_price       REAL,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_commands_status      ON commands(status);
CREATE INDEX IF NOT EXISTS idx_commands_symbol      ON commands(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status     ON positions(status);
CREATE INDEX IF NOT EXISTS idx_critical_lines_sd    ON critical_lines(symbol, date);
CREATE INDEX IF NOT EXISTS idx_fetch_log_sd         ON fetch_log(symbol, date);
CREATE INDEX IF NOT EXISTS idx_completed_source     ON completed_trades(source);
CREATE INDEX IF NOT EXISTS idx_completed_exit_time  ON completed_trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_algo_runs_sym_date   ON algo_runs(symbol, date);
"""


def init_db(path: Path = None):
    """Create all tables and indexes if they don't exist."""
    with get_db(path) as con:
        con.executescript(_SCHEMA)
    # Migrations for existing DBs — safe to re-run
    _migrate(path)


_VERIFIED_TRADES_VIEW = """
CREATE VIEW verified_trades AS
WITH RECURSIVE ancestry(cmd_id, root_cmd_id, root_critical_line_id, chain_depth) AS (
    -- Root commands: no parent
    SELECT id, id, critical_line_id, 0
    FROM commands WHERE parent_command_id IS NULL
    UNION ALL
    -- Walk down: children inherit root info; child's own critical_line_id wins if set
    SELECT c.id,
           ancestry.root_cmd_id,
           COALESCE(c.critical_line_id, ancestry.root_critical_line_id),
           ancestry.chain_depth + 1
    FROM commands c
    INNER JOIN ancestry ON ancestry.cmd_id = c.parent_command_id
)
SELECT
    ct.id,
    ct.command_id,
    ct.symbol,
    ct.source,
    c.direction,
    c.entry_type,
    c.bracket_size,
    c.entry_price,
    c.tp_price,
    c.sl_price,
    ct.fill_price,
    ct.fill_time,
    ct.exit_price,
    ct.exit_time,
    -- exit_reason derived from actual prices — immune to order-ID label errors
    CASE
        WHEN c.direction='BUY'  AND ct.exit_price >= c.tp_price THEN 'TP'
        WHEN c.direction='BUY'  AND ct.exit_price <= c.sl_price THEN 'SL'
        WHEN c.direction='SELL' AND ct.exit_price <= c.tp_price THEN 'TP'
        WHEN c.direction='SELL' AND ct.exit_price >= c.sl_price THEN 'SL'
        ELSE 'STAGNATION'
    END AS exit_reason,
    ct.exit_reason AS raw_exit_reason,
    ct.pnl_points,
    -- Direct lineage
    c.parent_command_id,
    c.critical_line_id,
    -- Full chain ancestry (walk to root)
    anc.root_cmd_id,
    anc.root_critical_line_id,
    anc.chain_depth,
    ct.recorded_at
FROM completed_trades ct
JOIN commands c   ON ct.command_id = c.id
JOIN ancestry anc ON anc.cmd_id   = ct.command_id
WHERE
    -- real source only
    ct.source IS NOT NULL
    AND ct.source NOT IN ('test')
    -- all required fields present
    AND ct.fill_price  IS NOT NULL
    AND ct.exit_price  IS NOT NULL
    AND ct.pnl_points  IS NOT NULL
    AND ct.fill_time   IS NOT NULL
    AND ct.exit_time   IS NOT NULL
    -- exclude instant fill+exit (mass-reconnect / stale-data artifacts)
    AND ct.fill_time != ct.exit_time
    -- pnl_points must match price arithmetic (catches any write-path bugs)
    AND ABS(ct.pnl_points - CASE c.direction
            WHEN 'BUY'  THEN ct.exit_price - ct.fill_price
            ELSE             ct.fill_price  - ct.exit_price
        END) < 0.01
    -- fill must be inside the bracket (stale/gap fills already past TP or SL are invalid)
    AND NOT (c.direction='BUY'  AND ct.fill_price >= c.tp_price)
    AND NOT (c.direction='SELL' AND ct.fill_price <= c.tp_price)
    AND NOT (c.direction='BUY'  AND ct.fill_price <= c.sl_price)
    AND NOT (c.direction='SELL' AND ct.fill_price >= c.sl_price)
"""


def _migrate(path: Path = None):
    alter_stmts = [
        "ALTER TABLE commands ADD COLUMN source TEXT",
        "ALTER TABLE commands ADD COLUMN parent_command_id INTEGER",
        "ALTER TABLE commands ADD COLUMN critical_line_id INTEGER REFERENCES critical_lines(id)",
        # Idempotent CREATE IF NOT EXISTS for tables added after initial schema
        """CREATE TABLE IF NOT EXISTS price_cache (
            symbol       TEXT PRIMARY KEY,
            last_price   REAL NOT NULL,
            last_fill_ts TEXT NOT NULL,
            source       TEXT NOT NULL DEFAULT 'fill',
            updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",
        """CREATE TABLE IF NOT EXISTS algo_runs (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol             TEXT    NOT NULL,
            date               TEXT    NOT NULL,
            algo_type          TEXT    NOT NULL,
            tp_ticks           INTEGER NOT NULL,
            sl_ticks           INTEGER NOT NULL,
            direction_filter   TEXT    NOT NULL DEFAULT 'ALL',
            strength_max       INTEGER NOT NULL DEFAULT 3,
            commands_generated INTEGER NOT NULL DEFAULT 0,
            current_price      REAL,
            created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_algo_runs_sym_date ON algo_runs(symbol, date)",
    ]
    with get_db(path) as con:
        for stmt in alter_stmts:
            try:
                con.execute(stmt)
            except Exception:
                pass
        # Always recreate the view so schema changes are picked up on restart.
        # Guard: if verified_trades exists as a TABLE (older DB), drop it as a table first.
        row = con.execute(
            "SELECT type FROM sqlite_master WHERE name='verified_trades'"
        ).fetchone()
        if row and row[0] == "table":
            con.execute("DROP TABLE verified_trades")
        elif row and row[0] == "view":
            con.execute("DROP VIEW verified_trades")
        con.execute(_VERIFIED_TRADES_VIEW)


# ── CRUD helpers ──────────────────────────────────────────────────────────────

def get_system_state(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_system_state(con, key: str, value: str):
    con.execute(
        "INSERT INTO system_state(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (key, value)
    )


def update_command_status(con, command_id: int, status: str, **kwargs):
    """Update command status plus any optional fields (ib_order_id, fill_price, etc.)."""
    fields = {"status": status, **kwargs}
    sets = ", ".join(f"{k}=?" for k in fields)
    sets += ", updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    values = list(fields.values()) + [command_id]
    con.execute(f"UPDATE commands SET {sets} WHERE id=?", values)


def record_completed_trade(con, command_id: int) -> bool:
    """
    Write one row to completed_trades for a CLOSED command.
    INSERT OR IGNORE on UNIQUE(command_id) guarantees exactly-once semantics —
    safe to call multiple times for the same command_id.
    Returns True if a new row was inserted, False if already recorded.
    """
    row = con.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    if not row:
        return False
    if row["fill_price"] is None or row["exit_price"] is None or row["pnl_points"] is None:
        return False
    cur = con.execute("""
        INSERT OR IGNORE INTO completed_trades
            (command_id, symbol, source, direction, entry_type, bracket_size,
             ib_order_id, fill_price, fill_time, exit_price, exit_time,
             exit_reason, pnl_points)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        command_id, row["symbol"], row["source"],
        row["direction"], row["entry_type"], row["bracket_size"],
        row["ib_order_id"],
        row["fill_price"], row["fill_time"],
        row["exit_price"], row["exit_time"],
        row["exit_reason"], row["pnl_points"],
    ))
    return cur.rowcount == 1


def _root_critical_line_id(con, cmd) -> int | None:
    """Walk up parent_command_id chain to find the root's critical_line_id."""
    cid = cmd["critical_line_id"]
    if cid is not None:
        return cid
    cur_id = cmd["parent_command_id"]
    for _ in range(50):  # max chain depth guard
        if cur_id is None:
            return None
        row = con.execute(
            "SELECT critical_line_id, parent_command_id FROM commands WHERE id=?",
            (cur_id,)
        ).fetchone()
        if row is None:
            return None
        if row["critical_line_id"] is not None:
            return row["critical_line_id"]
        cur_id = row["parent_command_id"]
    return None


def spawn_replenishment(con, parent_cmd, price: float, tick: float) -> int:
    """
    Insert one PENDING command derived from parent_cmd at the current price.
    Sets parent_command_id and inherits root critical_line_id for full traceability.
    Returns the new command id.
    """
    import random
    source     = parent_cmd["source"] or "random_mkt"
    bracket    = parent_cmd["bracket_size"]
    direction  = random.choice(["BUY", "SELL"])
    qty        = parent_cmd["quantity"]

    entry_type_map = {
        "random_lmt": "LMT",
        "random_stp": "STP",
        "critical_line": "LMT",
    }
    entry_type = entry_type_map.get(source, "MKT")

    def rt(p):
        return round(round(p / tick) * tick, 10)

    # Entry offset: 1 tick for LMT/STP so fills quickly; 0 for MKT
    offset = tick if entry_type in ("LMT", "STP") else 0.0

    if entry_type == "MKT":
        entry_price = rt(price)
    elif entry_type == "LMT":
        entry_price = rt(price - offset) if direction == "BUY" else rt(price + offset)
    else:  # STP
        entry_price = rt(price + offset) if direction == "BUY" else rt(price - offset)

    tp_price = rt(entry_price + bracket) if direction == "BUY" else rt(entry_price - bracket)
    sl_price = rt(entry_price - bracket) if direction == "BUY" else rt(entry_price + bracket)

    # Inherit critical_line_id from chain root so origin is always traceable
    inherited_line_id = _root_critical_line_id(con, parent_cmd)

    con.execute("""
        INSERT INTO commands
            (symbol, line_price, line_type, line_strength,
             direction, entry_type, entry_price, tp_price, sl_price,
             bracket_size, source, parent_command_id, critical_line_id, quantity, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (
        parent_cmd["symbol"], entry_price,
        "SUPPORT" if direction == "BUY" else "RESISTANCE", 1,
        direction, entry_type, entry_price, tp_price, sl_price,
        bracket, source, parent_cmd["id"], inherited_line_id, qty,
    ))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_pending_commands(con, symbol: str = None) -> list:
    if symbol:
        return con.execute(
            "SELECT * FROM commands WHERE status='PENDING' AND symbol=?", (symbol,)
        ).fetchall()
    return con.execute("SELECT * FROM commands WHERE status='PENDING'").fetchall()


def get_filled_commands(con, symbol: str = None) -> list:
    """Return FILLED commands where replenishment has not yet been issued."""
    if symbol:
        return con.execute(
            "SELECT * FROM commands WHERE status='FILLED'"
            " AND replenishment_issued=0 AND symbol=?", (symbol,)
        ).fetchall()
    return con.execute(
        "SELECT * FROM commands WHERE status='FILLED' AND replenishment_issued=0"
    ).fetchall()


def insert_fetch_log(con, symbol: str, date: str, file_type: str,
                     status: str, rows_fetched: int = None, error_msg: str = None):
    con.execute(
        "INSERT INTO fetch_log(symbol, date, file_type, status, rows_fetched, error_msg)"
        " VALUES(?,?,?,?,?,?)",
        (symbol, date, file_type, status, rows_fetched, error_msg)
    )


def get_fetch_log(con, days: int = 30) -> list:
    """Return fetch_log rows for the last N calendar days, newest first."""
    return con.execute(
        "SELECT * FROM fetch_log"
        " WHERE date >= date('now', ?)"
        " ORDER BY fetched_at DESC",
        (f"-{days} days",)
    ).fetchall()


def get_fetch_log_latest(con, symbol: str, date: str) -> list:
    """Return all fetch_log rows for a specific symbol + date."""
    return con.execute(
        "SELECT * FROM fetch_log WHERE symbol=? AND date=? ORDER BY fetched_at DESC",
        (symbol, date)
    ).fetchall()



# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            # 1. Init creates all tables
            init_db(db_path)
            with get_db(db_path) as con:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            expected = {"commands", "positions", "ib_events", "system_state",
                        "critical_lines", "release_notes", "fetch_log", "completed_trades"}
            assert expected <= tables, f"Missing tables: {expected - tables}"

            # 2. WAL mode is active
            with get_db(db_path) as con:
                mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"Expected WAL, got {mode}"

            # 3. system_state round-trip
            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
                val = get_system_state(con, "SESSION")
            assert val == "RUNNING", f"system_state read: {val}"

            # 4. Upsert updates, not duplicates
            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "SHUTDOWN")
                count = con.execute(
                    "SELECT COUNT(*) FROM system_state WHERE key='SESSION'"
                ).fetchone()[0]
                val2 = get_system_state(con, "SESSION")
            assert count == 1,          "Upsert created duplicate row"
            assert val2 == "SHUTDOWN",  f"Upsert didn't update value: {val2}"

            # 5. Insert a command and update its status
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price, bracket_size)
                    VALUES ('MES', 6500.0, 'SUPPORT', 2,
                            'BUY', 'STP', 6500.25, 6502.25, 6498.25, 2.0)
                """)
                cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

            with get_db(db_path) as con:
                update_command_status(con, cmd_id, "SUBMITTING", claimed_at="2026-04-07T10:00:00Z")
                row = con.execute("SELECT status, claimed_at FROM commands WHERE id=?",
                                  (cmd_id,)).fetchone()
            assert row["status"] == "SUBMITTING",             f"Status: {row['status']}"
            assert row["claimed_at"] == "2026-04-07T10:00:00Z", f"claimed_at: {row['claimed_at']}"

            # 6. get_pending_commands (should be empty now)
            with get_db(db_path) as con:
                pending = get_pending_commands(con)
            assert len(pending) == 0, f"Expected 0 pending, got {len(pending)}"

            # 7. fetch_log round-trip
            with get_db(db_path) as con:
                insert_fetch_log(con, "MES", "2026-04-07", "trades", "ok", 45000)
                insert_fetch_log(con, "MNQ", "2026-04-07", "trades", "error",
                                 error_msg="IB timeout")
            with get_db(db_path) as con:
                rows = get_fetch_log(con, days=365)
            assert len(rows) == 2, f"Expected 2 fetch_log rows, got {len(rows)}"
            assert rows[0]["symbol"] in ("MES", "MNQ")

            # 8. completed_trades round-trip via record_completed_trade
            with get_db(db_path) as con:
                update_command_status(
                    con, cmd_id, "CLOSED",
                    fill_price=6500.25, fill_time="2026-04-07T10:00:05Z",
                    exit_price=6502.25, exit_time="2026-04-07T10:05:00Z",
                    exit_reason="TP", pnl_points=2.0,
                )
                inserted = record_completed_trade(con, cmd_id)
            assert inserted, "record_completed_trade should return True on first insert"
            with get_db(db_path) as con:
                ct = con.execute(
                    "SELECT * FROM completed_trades WHERE command_id=?", (cmd_id,)
                ).fetchone()
            assert ct is not None and ct["exit_reason"] == "TP"
            with get_db(db_path) as con:
                again = record_completed_trade(con, cmd_id)
            assert not again, "record_completed_trade must be idempotent"

            # 9. Rollback on error — no partial writes
            try:
                with get_db(db_path) as con:
                    con.execute("INSERT INTO system_state(key,value) VALUES('ROLLBACK_TEST','yes')")
                    raise RuntimeError("forced rollback")
            except RuntimeError:
                pass
            with get_db(db_path) as con:
                v = get_system_state(con, "ROLLBACK_TEST")
            assert v is None, "Rollback failed — value persisted"

        print("[self-test] db: PASS")
        return True

    except Exception as e:
        print(f"[self-test] db: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    # Default: init production DB
    init_db()
    print("DB initialized.")
