#!/usr/bin/env python3
"""Assert C48-3 Chainstack HL grid hard locks (Cove gate). Exit 0 = pass.

Usage: python3 assert_locks.py [locked.yaml]
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).with_name("bots") / "c48_3_btc_grid_locked.yaml"

MAX_ALLOCATION_PCT = 2.0
STOP_LOSS_MIN_PCT = 4.0
STOP_LOSS_MAX_PCT = 8.0
MAX_DRAWDOWN_PCT = 4.0
MAX_POSITION_SIZE_PCT = 8.0
MAX_LEVERAGE = 3
TARGET_LEVERAGE = 2
SYMBOL = "BTC"
# Upstream KeyManager / adapter would take a key, key file or address from these YAML fields,
# and `dex` would route to a HIP-3 builder dex instead of the main BTC perp.
FORBIDDEN_TOP_KEYS = (
    "private_key", "testnet_private_key", "mainnet_private_key",
    "private_key_file", "testnet_key_file", "mainnet_key_file",
)
FORBIDDEN_EXCHANGE_KEYS = ("dex", "account_address")


def _num(value) -> float:
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    return float(value)


def check_config(data: dict) -> list[str]:
    errors: list[str] = []
    ex = data.get("exchange") or {}
    acct = data.get("account") or {}
    grid = data.get("grid") or {}
    rm = data.get("risk_management") or {}

    if ex.get("testnet") is not True:
        errors.append(f"exchange.testnet must be true, got {ex.get('testnet')!r}")
    if ex.get("type") not in ("hyperliquid", "hl"):
        errors.append(f"exchange.type must be hyperliquid, got {ex.get('type')!r}")
    for key in FORBIDDEN_EXCHANGE_KEYS:
        if ex.get(key) is not None:
            errors.append(f"exchange.{key} is not allowed")
    for key in FORBIDDEN_TOP_KEYS:
        if data.get(key) is not None:
            errors.append(f"{key} is not allowed in the yaml (use env secrets)")

    checks = [
        ("account.max_allocation_pct", acct.get("max_allocation_pct"),
         lambda v: 0 < v <= MAX_ALLOCATION_PCT, f"0 < x <= {MAX_ALLOCATION_PCT}"),
        ("risk_management.stop_loss_pct", rm.get("stop_loss_pct"),
         lambda v: STOP_LOSS_MIN_PCT <= v <= STOP_LOSS_MAX_PCT,
         f"{STOP_LOSS_MIN_PCT} <= x <= {STOP_LOSS_MAX_PCT}"),
        ("risk_management.max_drawdown_pct", rm.get("max_drawdown_pct"),
         lambda v: 0 < v <= MAX_DRAWDOWN_PCT, f"0 < x <= {MAX_DRAWDOWN_PCT}"),
        ("risk_management.max_position_size_pct", rm.get("max_position_size_pct"),
         lambda v: 0 < v <= MAX_POSITION_SIZE_PCT, f"0 < x <= {MAX_POSITION_SIZE_PCT}"),
    ]
    for name, value, ok, want in checks:
        try:
            if not ok(_num(value)):
                errors.append(f"{name} want {want}, got {value!r}")
        except (TypeError, ValueError):
            errors.append(f"{name} missing/invalid: {value!r}")

    if rm.get("stop_loss_enabled") is not True:
        errors.append(f"risk_management.stop_loss_enabled must be true, got {rm.get('stop_loss_enabled')!r}")
    # Upstream only runs StopLossRule in polling mode; "grouped" would disable it.
    if rm.get("tpsl_mode", "polling") != "polling":
        errors.append(f"risk_management.tpsl_mode must be 'polling', got {rm.get('tpsl_mode')!r}")
    if grid.get("symbol") != SYMBOL:
        errors.append(f"grid.symbol must be {SYMBOL!r}, got {grid.get('symbol')!r}")
    if data.get("active") is not True:
        errors.append("active must be true")
    return errors


def load(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT_CONFIG
    errors = check_config(load(path))
    if errors:
        print("FAIL:")
        for e in errors:
            print(" -", e)
        return 1
    print("PASS: C48-3 hard locks OK on", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
