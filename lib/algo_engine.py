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
                              db_path=db_path, quantity=2)
"""

TICK = 0.25


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

def _rt(price: float) -> float:
    return round(round(price / TICK) * TICK, 10)


def _calc_prices(direction: str, entry_type: str, line_price: float,
                 tp_ticks: int, sl_ticks: int) -> dict:
    """
    Asymmetric bracket: TP and SL are independent tick counts.
    Returns {entry_price, tp_price, sl_price}.
    """
    tp_dist = tp_ticks * TICK
    sl_dist = sl_ticks * TICK

    if direction == "BUY" and entry_type == "LMT":
        entry = _rt(line_price)
        tp    = _rt(entry + tp_dist)
        sl    = _rt(entry - sl_dist)
    elif direction == "BUY" and entry_type == "STP":
        entry = _rt(line_price + TICK)
        tp    = _rt(entry + tp_dist)
        sl    = _rt(entry - sl_dist)
    elif direction == "SELL" and entry_type == "LMT":
        entry = _rt(line_price)
        tp    = _rt(entry - tp_dist)
        sl    = _rt(entry + sl_dist)
    elif direction == "SELL" and entry_type == "STP":
        entry = _rt(line_price - TICK)
        tp    = _rt(entry - tp_dist)
        sl    = _rt(entry + sl_dist)
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


def _build_cmds(line: dict, params: AlgoParams, current_price: float) -> list:
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
                              params.tp_ticks, params.sl_ticks)
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
            "bracket_size":     params.tp_ticks * TICK,
            "source":           "cl_algo",
            "critical_line_id": line.get("id"),
        })
    return cmds


# ── Public API ─────────────────────────────────────────────────────────────────

def preview_cl_commands(symbol: str, date_str: str, current_price: float,
                         params: AlgoParams, db_path) -> int:
    """Count commands that would be generated without inserting."""
    from lib.db import get_db
    with get_db(db_path) as con:
        lines = [dict(r) for r in con.execute(
            "SELECT * FROM critical_lines WHERE symbol=? AND date=? AND armed=1"
            " ORDER BY price",
            (symbol, date_str)
        ).fetchall()]
    return sum(len(_build_cmds(ln, params, current_price)) for ln in lines)


def generate_cl_commands(symbol: str, date_str: str, current_price: float,
                          params: AlgoParams, db_path,
                          quantity: int = 1,
                          algo_run_id: int = None) -> int:
    """
    Generate and INSERT PENDING commands for all armed lines.
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
        cmds = _build_cmds(line, params, current_price)
        for cmd in cmds:
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, source, critical_line_id, quantity, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """, (
                    symbol,
                    cmd["line_price"], cmd["line_type"], cmd["line_strength"],
                    cmd["direction"], cmd["entry_type"],
                    cmd["entry_price"], cmd["tp_price"], cmd["sl_price"],
                    cmd["bracket_size"], cmd["source"], cmd["critical_line_id"],
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
