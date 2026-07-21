"""
lib/algo_engine.py
Critical Lines Algorithm Engine.

Generates PENDING commands from armed critical lines using configurable
algorithm types and asymmetric TP/SL brackets.

Algorithm types:
  BOUNCE      – LMT only: BUY at SUPPORT, SELL at RESISTANCE (bounce off line)
  BREAKOUT    – STP only: SELL below SUPPORT, BUY above RESISTANCE (trade through)
  DIRECTIONAL – One order per line in canonical direction; entry type by toggle rule
  FADE        – Contrarian LMT: SELL at SUPPORT, BUY at RESISTANCE
  BOTH        – Full matrix: BUY+SELL × toggle-rule entry at every line

Usage:
    from lib.algo_engine import AlgoType, AlgoParams, generate_cl_commands, preview_cl_commands
    params = AlgoParams(algo_type=AlgoType.BOUNCE, tp_ticks=6, sl_ticks=4)
    n = generate_cl_commands("MES", "2026-07-04", current_price=5500.25, params=params,
                              db_path=db_path, quantity=2, tick_size=0.25)

Self-test:
    python -m lib.algo_engine --self-test
"""

import sys
import argparse

# Default tick size, used when a caller doesn't pass tick_size explicitly.
# MES/MNQ = 0.25. Callers trading MYM (1.0) or M2K (0.10) must pass tick_size
# explicitly -- this module has no way to know the symbol's real tick on its own.
TICK = 0.25

# Reference only -- per-symbol tick sizes for the 4 supported micro futures.
# Kept here so callers have one place to look this up; not used internally
# (every function takes tick_size explicitly so this module stays symbol-agnostic).
SYMBOL_TICKS = {"MES": 0.25, "MNQ": 0.25, "MYM": 1.0, "M2K": 0.10}


class AlgoType:
    BOUNCE      = "BOUNCE"
    BREAKOUT    = "BREAKOUT"
    DIRECTIONAL = "DIRECTIONAL"
    FADE        = "FADE"
    BOTH        = "BOTH"

    ALL = [BOUNCE, BREAKOUT, DIRECTIONAL, FADE, BOTH]


ALGO_DESCRIPTIONS = {
    AlgoType.BOUNCE:      (
        "BUY at SUPPORT, SELL at RESISTANCE. LMT entries — "
        "price touches the line and bounces back."
    ),
    AlgoType.BREAKOUT:    (
        "SELL below SUPPORT, BUY above RESISTANCE. STP entries — "
        "price breaks through the line, follow the momentum."
    ),
    AlgoType.DIRECTIONAL: (
        "One order per line in canonical direction. Toggle rule selects "
        "LMT (price approaching line) vs STP (price retreating from line)."
    ),
    AlgoType.FADE:        (
        "SELL at SUPPORT, BUY at RESISTANCE. Contrarian — "
        "bet the line will not hold."
    ),
    AlgoType.BOTH:        (
        "Full coverage: BUY+SELL × LMT+STP at every line. "
        "Maximum exposure, equivalent to the classic decider."
    ),
}

TP_TICK_OPTIONS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20]
SL_TICK_OPTIONS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20]


class AlgoParams:
    __slots__ = ("algo_type", "tp_ticks", "sl_ticks",
                 "direction_filter", "strength_max")

    def __init__(self, algo_type: str = AlgoType.BOTH,
                 tp_ticks: int = 4, sl_ticks: int = 4,
                 direction_filter: str = "ALL",
                 strength_max: int = 3):
        """
        algo_type       : one of AlgoType.*
        tp_ticks        : take-profit distance in ticks
        sl_ticks        : stop-loss distance in ticks
        direction_filter: ALL | BUY_ONLY | SELL_ONLY
        strength_max    : max strength NUMBER to trade (1=strong only, 2=+medium, 3=all)
        """
        self.algo_type        = algo_type
        self.tp_ticks         = tp_ticks
        self.sl_ticks         = sl_ticks
        self.direction_filter = direction_filter
        self.strength_max     = strength_max

    def to_dict(self):
        return {
            "algo_type":        self.algo_type,
            "tp_ticks":         self.tp_ticks,
            "sl_ticks":         self.sl_ticks,
            "direction_filter": self.direction_filter,
            "strength_max":     self.strength_max,
        }


# ── Price calculation ──────────────────────────────────────────────────────────

def _rt(price: float, tick_size: float = TICK) -> float:
    return round(round(price / tick_size) * tick_size, 10)


def _calc_prices(direction: str, entry_type: str, line_price: float,
                 tp_ticks: int, sl_ticks: int, tick_size: float = TICK) -> dict:
    """
    Asymmetric bracket: TP and SL are independent tick counts.
    tick_size must match the traded symbol (MES/MNQ=0.25, MYM=1.0, M2K=0.10) --
    passing the wrong tick_size silently mis-prices every order for that symbol.
    Returns {entry_price, tp_price, sl_price}.
    """
    tp_dist = tp_ticks * tick_size
    sl_dist = sl_ticks * tick_size

    if direction == "BUY" and entry_type == "LMT":
        entry = _rt(line_price, tick_size)
        tp    = _rt(entry + tp_dist, tick_size)
        sl    = _rt(entry - sl_dist, tick_size)
    elif direction == "BUY" and entry_type == "STP":
        entry = _rt(line_price + tick_size, tick_size)
        tp    = _rt(entry + tp_dist, tick_size)
        sl    = _rt(entry - sl_dist, tick_size)
    elif direction == "SELL" and entry_type == "LMT":
        entry = _rt(line_price, tick_size)
        tp    = _rt(entry - tp_dist, tick_size)
        sl    = _rt(entry + sl_dist, tick_size)
    elif direction == "SELL" and entry_type == "STP":
        entry = _rt(line_price - tick_size, tick_size)
        tp    = _rt(entry - tp_dist, tick_size)
        sl    = _rt(entry + sl_dist, tick_size)
    else:
        raise ValueError(f"Unknown direction/entry_type: {direction}/{entry_type}")

    return {"entry_price": entry, "tp_price": tp, "sl_price": sl}


# ── Per-line command generation ────────────────────────────────────────────────

def _pairs_for_line(line_type: str, algo_type: str,
                    current_price: float, line_price: float) -> list:
    """
    Return list of (direction, entry_type) pairs to generate for this line.
    """
    pairs = []

    if algo_type == AlgoType.BOUNCE:
        if line_type == "SUPPORT":
            pairs = [("BUY", "LMT")]
        else:
            pairs = [("SELL", "LMT")]

    elif algo_type == AlgoType.BREAKOUT:
        if line_type == "SUPPORT":
            pairs = [("SELL", "STP")]   # break below support
        else:
            pairs = [("BUY", "STP")]    # break above resistance

    elif algo_type == AlgoType.DIRECTIONAL:
        price_above = current_price >= line_price
        if line_type == "SUPPORT":
            direction  = "BUY"
            entry_type = "LMT" if price_above else "STP"
        else:
            direction  = "SELL"
            entry_type = "LMT" if not price_above else "STP"
        pairs = [(direction, entry_type)]

    elif algo_type == AlgoType.FADE:
        if line_type == "SUPPORT":
            pairs = [("SELL", "LMT")]
        else:
            pairs = [("BUY", "LMT")]

    elif algo_type == AlgoType.BOTH:
        price_above = current_price >= line_price
        buy_type  = "LMT" if price_above else "STP"
        sell_type = "STP" if price_above else "LMT"
        pairs = [("BUY", buy_type), ("SELL", sell_type)]

    return pairs


def _build_cmds(line: dict, params: AlgoParams, current_price: float,
               tick_size: float = TICK) -> list:
    """Build command dicts for one critical line (not inserted yet)."""
    if line["strength"] > params.strength_max:
        return []

    pairs = _pairs_for_line(
        line["line_type"], params.algo_type, current_price, line["price"]
    )

    if params.direction_filter == "BUY_ONLY":
        pairs = [(d, e) for d, e in pairs if d == "BUY"]
    elif params.direction_filter == "SELL_ONLY":
        pairs = [(d, e) for d, e in pairs if d == "SELL"]

    cmds = []
    for direction, entry_type in pairs:
        prices = _calc_prices(direction, entry_type, line["price"],
                              params.tp_ticks, params.sl_ticks, tick_size)
        cmds.append({
            "symbol":           line.get("symbol", ""),
            "line_price":       line["price"],
            "line_type":        line["line_type"],
            "line_strength":    line["strength"],
            "direction":        direction,
            "entry_type":       entry_type,
            "entry_price":      prices["entry_price"],
            "tp_price":         prices["tp_price"],
            "sl_price":         prices["sl_price"],
            "bracket_size":     params.tp_ticks * tick_size,
            "source":           "cl_algo",
            "critical_line_id": line.get("id"),
        })
    return cmds


# ── Public API ─────────────────────────────────────────────────────────────────

def preview_cl_commands(symbol: str, date_str: str, current_price: float,
                         params: AlgoParams, db_path,
                         tick_size: float = TICK) -> int:
    """Count commands that would be generated without inserting."""
    from lib.db import get_db
    with get_db(db_path) as con:
        lines = [dict(r) for r in con.execute(
            "SELECT * FROM critical_lines WHERE symbol=? AND date=? AND armed=1"
            " ORDER BY price",
            (symbol, date_str)
        ).fetchall()]
    return sum(len(_build_cmds(ln, params, current_price, tick_size)) for ln in lines)


def generate_cl_commands(symbol: str, date_str: str, current_price: float,
                          params: AlgoParams, db_path,
                          quantity: int = 1,
                          tick_size: float = TICK,
                          source: str = "cl_algo",
                          params_json: str = None) -> int:
    """
    Generate and INSERT PENDING commands for all armed lines.
    tick_size must match the traded symbol (see SYMBOL_TICKS) -- defaults to
    0.25 (MES/MNQ) for backward compatibility with existing callers.
    source/params_json let callers (e.g. lib.algo_lab) tag rows for later
    P&L attribution without this module needing to know about that scheme.
    Returns count of commands inserted.
    """
    from lib.db import get_db
    with get_db(db_path) as con:
        lines = [dict(r) for r in con.execute(
            "SELECT * FROM critical_lines WHERE symbol=? AND date=? AND armed=1"
            " ORDER BY price",
            (symbol, date_str)
        ).fetchall()]

    count = 0
    for line in lines:
        cmds = _build_cmds(line, params, current_price, tick_size)
        for cmd in cmds:
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, source, critical_line_id, algo_type,
                         params_json, quantity, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """, (
                    symbol,
                    cmd["line_price"], cmd["line_type"], cmd["line_strength"],
                    cmd["direction"], cmd["entry_type"],
                    cmd["entry_price"], cmd["tp_price"], cmd["sl_price"],
                    cmd["bracket_size"], source, cmd["critical_line_id"],
                    params.algo_type, params_json,
                    quantity,
                ))
            count += 1

    return count


def record_algo_run(db_path, symbol: str, date_str: str,
                    algo_type: str, tp_ticks: int, sl_ticks: int,
                    direction_filter: str, strength_max: int,
                    commands_generated: int, current_price: float = None) -> int:
    """Insert a row into algo_runs; return the new run id."""
    from lib.db import get_db
    with get_db(db_path) as con:
        con.execute("""
            INSERT INTO algo_runs
                (symbol, date, algo_type, tp_ticks, sl_ticks,
                 direction_filter, strength_max, commands_generated, current_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, date_str, algo_type, tp_ticks, sl_ticks,
              direction_filter, strength_max, commands_generated, current_price))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_algo_runs(db_path, limit: int = 30) -> list:
    """Return recent algo_runs rows, newest first."""
    from lib.db import get_db
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT * FROM algo_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    from pathlib import Path
    try:
        from lib.db import init_db, get_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            with get_db(db_path) as con:
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                    " VALUES ('MES', '2026-04-07', 'SUPPORT', 6490.0, 2, 1)"
                )
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                    " VALUES ('MES', '2026-04-07', 'RESISTANCE', 6510.0, 1, 1)"
                )

            # 1. BOUNCE: BUY LMT at SUPPORT, SELL LMT at RESISTANCE — one order/line
            params = AlgoParams(algo_type=AlgoType.BOUNCE, tp_ticks=8, sl_ticks=4)
            n = preview_cl_commands("MES", "2026-04-07", 6500.0, params, db_path)
            assert n == 2, f"BOUNCE preview: expected 2, got {n}"
            n = generate_cl_commands("MES", "2026-04-07", 6500.0, params, db_path,
                                      params_json='{"tp_ticks":8,"sl_ticks":4}')
            assert n == 2, f"BOUNCE generate: expected 2, got {n}"
            with get_db(db_path) as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM commands WHERE source='cl_algo'"
                ).fetchall()]
            assert len(rows) == 2
            support_row = next(r for r in rows if r["line_price"] == 6490.0)
            assert support_row["direction"] == "BUY" and support_row["entry_type"] == "LMT"
            assert support_row["algo_type"] == "BOUNCE"
            assert support_row["params_json"] == '{"tp_ticks":8,"sl_ticks":4}'
            # Asymmetric bracket: tp=8 ticks, sl=4 ticks, tick=0.25 → TP dist=2.0, SL dist=1.0
            assert abs(support_row["tp_price"] - 6492.0) < 1e-9, support_row["tp_price"]
            assert abs(support_row["sl_price"] - 6489.0) < 1e-9, support_row["sl_price"]

            # 2. Per-symbol tick_size actually changes math (MYM tick=1.0, not 0.25)
            p_mym = _calc_prices("BUY", "LMT", 40000.0, tp_ticks=10, sl_ticks=5, tick_size=1.0)
            assert p_mym["tp_price"] == 40010.0, f"MYM TP wrong: {p_mym['tp_price']}"
            assert p_mym["sl_price"] == 39995.0, f"MYM SL wrong: {p_mym['sl_price']}"
            # M2K tick=0.10
            p_m2k = _calc_prices("SELL", "LMT", 2100.0, tp_ticks=10, sl_ticks=5, tick_size=0.10)
            assert abs(p_m2k["tp_price"] - 2099.0) < 1e-9, f"M2K TP wrong: {p_m2k['tp_price']}"
            assert abs(p_m2k["sl_price"] - 2100.5) < 1e-9, f"M2K SL wrong: {p_m2k['sl_price']}"

            # 3. BREAKOUT: STP only, opposite direction of BOUNCE
            params2 = AlgoParams(algo_type=AlgoType.BREAKOUT, tp_ticks=4, sl_ticks=4)
            n2 = preview_cl_commands("MES", "2026-04-07", 6500.0, params2, db_path)
            assert n2 == 2, f"BREAKOUT preview: expected 2, got {n2}"

            # 4. strength_max filters out weaker lines
            params3 = AlgoParams(algo_type=AlgoType.BOTH, tp_ticks=4, sl_ticks=4, strength_max=1)
            n3 = preview_cl_commands("MES", "2026-04-07", 6500.0, params3, db_path)
            # Only the RESISTANCE line (strength=1) qualifies; BOTH = 2 orders/line
            assert n3 == 2, f"strength_max filter: expected 2, got {n3}"

            # 5. direction_filter narrows BOTH down to one side
            params4 = AlgoParams(algo_type=AlgoType.BOTH, tp_ticks=4, sl_ticks=4,
                                  direction_filter="BUY_ONLY")
            n4 = preview_cl_commands("MES", "2026-04-07", 6500.0, params4, db_path)
            assert n4 == 2, f"BUY_ONLY filter: expected 2 (one per line), got {n4}"

            # 6. record_algo_run / get_algo_runs round-trip
            run_id = record_algo_run(db_path, "MES", "2026-04-07", AlgoType.BOUNCE,
                                      8, 4, "ALL", 3, commands_generated=2,
                                      current_price=6500.0)
            assert run_id > 0
            runs = get_algo_runs(db_path, limit=5)
            assert any(r["id"] == run_id for r in runs)

        print("[self-test] algo_engine: PASS")
        return True

    except Exception as e:
        print(f"[self-test] algo_engine: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("algo_engine — run --self-test to verify logic")
