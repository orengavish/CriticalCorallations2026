"""
lib/config_loader.py
Loads and validates config.yaml. Returns a namespace object.
All other modules import get_config() from here.

Usage:
    from lib.config_loader import get_config
    cfg = get_config()
    cfg.ib.live_port

Self-test:
    python lib/config_loader.py --self-test
"""

import sys
import argparse
from pathlib import Path
from types import SimpleNamespace

_cached = None


def _find_config() -> Path:
    """
    Locate config.yaml by walking up from the calling script's directory.
    Search order:
      1. $GALGO_CONFIG env var (explicit override)
      2. Directory of sys.argv[0] (the script being run)
      3. Parent of sys.argv[0] directory
      4. Hard fallback: lib/../trader/config.yaml
    This lets every sub-project find its own config.yaml without any
    per-script setup — just run `python trader/runner.py` and it works.
    """
    import os
    if "GALGO_CONFIG" in os.environ:
        return Path(os.environ["GALGO_CONFIG"])
    if sys.argv:
        script_dir = Path(sys.argv[0]).resolve().parent
        for candidate_dir in (script_dir, script_dir.parent):
            cfg = candidate_dir / "config.yaml"
            if cfg.exists():
                return cfg
    return Path(__file__).parent.parent / "trader" / "config.yaml"

REQUIRED_KEYS = [
    "ib", "symbols", "session", "orders",
    "position", "shutdown", "decider", "broker", "paths"
]

IB_REQUIRED = [
    "live_host", "live_port", "live_client_ids",
    "paper_host", "paper_port", "paper_client_ids"
]


def _dict_to_ns(d):
    """Recursively convert dict to SimpleNamespace."""
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _dict_to_ns(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_dict_to_ns(i) for i in d]
    return d


def _validate(raw: dict):
    for key in REQUIRED_KEYS:
        if key not in raw:
            raise ValueError(f"config.yaml missing required section: '{key}'")
    for key in IB_REQUIRED:
        if key not in raw["ib"]:
            raise ValueError(f"config.yaml ib section missing: '{key}'")
    if not raw["symbols"]:
        raise ValueError("config.yaml: symbols list is empty")
    # 2026-09-13 fix: these two nested keys used to be read directly, raising an
    # unguarded KeyError (instead of this function's own intended ValueError
    # message) if either was missing from an otherwise-present `orders:` section
    # -- a plausible mid-edit config.yaml state, not just a hypothetical.
    for subkey in ("active_brackets", "tick_size"):
        if subkey not in raw["orders"]:
            raise ValueError(f"config.yaml: orders.{subkey} is missing")
    if not raw["orders"]["active_brackets"]:
        raise ValueError("config.yaml: orders.active_brackets is empty")
    tick = raw["orders"]["tick_size"]
    if tick <= 0:
        raise ValueError(f"config.yaml: orders.tick_size must be > 0, got {tick}")


def get_config(path: Path = None) -> SimpleNamespace:
    """Load config.yaml (cached after first call)."""
    global _cached
    if _cached is not None:
        return _cached

    import yaml
    cfg_path = path or _find_config()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")

    with open(cfg_path) as f:
        raw = yaml.safe_load(f)

    _validate(raw)

    # Resolve all paths relative to the config file so they're absolute
    # regardless of which directory the calling script runs from.
    cfg_dir = cfg_path.resolve().parent
    if "paths" in raw:
        raw["paths"] = {k: str(cfg_dir / v) for k, v in raw["paths"].items()}

    _cached = _dict_to_ns(raw)
    return _cached


def reset_cache():
    """Clear cached config — used in tests."""
    global _cached
    _cached = None


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    try:
        # Test 1: real config loads
        reset_cache()
        cfg = get_config()
        assert cfg.ib.live_port == 4001,      f"live_port wrong: {cfg.ib.live_port}"
        assert cfg.ib.paper_port == 4002,     f"paper_port wrong: {cfg.ib.paper_port}"
        assert "MES" in cfg.symbols,          "MES not in symbols"
        assert cfg.orders.tick_size == 0.25,  f"tick_size wrong: {cfg.orders.tick_size}"

        # Test 2: missing required key raises
        reset_cache()
        import yaml
        bad = {"ib": {"live_host":"x","live_port":1,"live_client_ids":[1],
                      "paper_host":"x","paper_port":2,"paper_client_ids":[2]},
               "symbols": ["MES"]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(bad, f)
            tmp = f.name
        try:
            raised = False
            try:
                get_config(Path(tmp))
            except ValueError:
                raised = True
            assert raised, "Missing key should raise ValueError"
        finally:
            os.unlink(tmp)

        # Test 3: file not found raises
        reset_cache()
        raised = False
        try:
            get_config(Path("nonexistent_xyz.yaml"))
        except FileNotFoundError:
            raised = True
        assert raised, "Missing file should raise FileNotFoundError"

        # Test 4: `orders:` section present but missing a required subkey raises
        # ValueError, not a raw KeyError (2026-09-13 fix -- these two were read
        # directly with no existence check, unlike every other validated key).
        reset_cache()
        real = yaml.safe_load(Path(_find_config()).read_text())
        broken = dict(real)
        broken["orders"] = {k: v for k, v in real["orders"].items() if k != "tick_size"}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(broken, f)
            tmp2 = f.name
        try:
            raised2 = False
            try:
                get_config(Path(tmp2))
            except ValueError:
                raised2 = True
            except KeyError:
                raised2 = False  # explicitly the bug this test guards against
            assert raised2, "orders.tick_size missing should raise ValueError, not KeyError"
        finally:
            os.unlink(tmp2)

        reset_cache()
        print("[self-test] config_loader: PASS")
        return True

    except Exception as e:
        print(f"[self-test] config_loader: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    cfg = get_config()
    print(f"Config loaded OK — symbols={cfg.symbols}  live={cfg.ib.live_port}  paper={cfg.ib.paper_port}")
