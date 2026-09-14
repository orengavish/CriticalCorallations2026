# Galao System — Bar Granularity (15-min OHLCV)
Version: 1.0.0 | Date: 2026-09-14

---

## What changed and why

Before 2026-09-14, `trader/data/bars.db`'s `bars_30m` table (30-min OHLCV) was
the only bar granularity this repo had, and it fed `lib/atr.py` (ATR sizing /
laggard tie-break) and `lib/spread_diff.py` (Spread's actual entry signal).

This did not match Geva's own documented rule:

- **AI-9** (`Galgo2029/knowledge/Claims/entries-exits-and-risk.md`): "15-min
  bars to execute; 3-month daily chart to determine overall trend direction."
- **AI-35b** (`Galgo2029/knowledge/Claims/spread-diff-trading.md`): the DIFF
  indicator is explicitly "set up on both the daily chart and the **15-min
  intraday chart**."

Per the standing rule that a code/Geva mismatch is an implementation bug to
fix, not an open question (see this repo's own conventions), this was fixed:
a new `bars_15m` table was added, `lib/atr.py` and `lib/spread_diff.py` now
read it instead of `bars_30m`, and `scripts/backfill_bars.py` defaults to
fetching 15-min bars.

## What did NOT change

- **`bars_30m` itself is untouched** — still has its 7-year (2019-05-05
  onward) Databento-sourced history, still fed by
  `scripts/import_7year_bars.py`. `lib/correlation_lab.py` (a read-only
  dashboard exploration tool, not on the trading hot path — nothing in
  `trader/` calls it) still reads `bars_30m` deliberately, since rebuilding
  that 7-year history at 15-min granularity is out of scope and not needed
  for anything live.
- `scripts/backfill_bars.py --bar-size 30` still works and still writes
  `bars_30m`, for anyone who needs the legacy table.

## Why only 60 days, not a full year

`lib/atr.py`'s `atr20_points()` and `lib/spread_diff.py`'s
`SWING_WINDOW_DAYS`/`EXTREME_LOOKBACK_BARS` only ever look back ~20 trading
days. A full year was never actually needed by either live caller — the
original `backfill_bars.py` fetching "1 Y" was more than the system uses, and
at 15-min granularity a 1-year request is both slower and more likely to hit
IB's historical-data pacing/timeout limits than a 60-day one. `--duration` is
still an explicit CLI flag if a larger window is ever wanted.

## Re-running the fetch

```
cd trader
python ../scripts/backfill_bars.py            # defaults: 15-min, 60 D, live port 4001
```

`bars.db` has no scheduled/automatic updater — this script is run manually.
It is idempotent (`INSERT OR IGNORE`, keyed on `(symbol, ts)`), safe to
re-run at any time to refresh.
