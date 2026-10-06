#!/usr/bin/env python3
"""Assert C48-2 Hummingbot hard locks (Cove gate 2026-10-05). Exit 0 = pass.

Usage: python3 assert_locks.py [conf.yml] [--nav USD --mid PRICE]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).with_name("conf_perpetual_market_making_c48_2.yml")

REQUIRED = {
    "strategy": "perpetual_market_making",
    "derivative": "hyperliquid_perpetual_testnet",
    "leverage": 2,
    "order_levels": 1,
    "order_level_amount": 0,
    "position_mode": "One-way",
    "price_source": "current_market",
}
FORBIDDEN_STRATEGIES = {"v2_funding_rate_arb", "funding_rate_arb", "spot_perpetual_arbitrage"}
MAX_LEVERAGE = 2
TARGET_NAV_PCT = 0.02
MAX_NAV_PCT = 0.08


def _num(value):
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    return float(value)


def check_config(data: dict) -> list[str]:
    errors: list[str] = []
    strat = str(data.get("strategy", ""))
    if strat in FORBIDDEN_STRATEGIES or "funding" in strat or "arb" in strat:
        errors.append(f"forbidden strategy {strat!r} (funding-arb / path 2 not approved)")

    for key, want in REQUIRED.items():
        got = data.get(key)
        if isinstance(want, int):
            try:
                if _num(got) != want:
                    errors.append(f"{key} want {want} got {got!r}")
            except (TypeError, ValueError):
                errors.append(f"{key} missing/invalid: {got!r}")
        elif got != want:
            errors.append(f"{key} want {want!r} got {got!r}")

    try:
        if _num(data.get("leverage")) > MAX_LEVERAGE:
            errors.append(f"leverage >{MAX_LEVERAGE} forbidden: {data.get('leverage')!r}")
    except (TypeError, ValueError):
        pass

    deriv = str(data.get("derivative", "")).lower()
    if "bitunix" in deriv or not deriv.startswith("hyperliquid"):
        errors.append(f"non-HL derivative forbidden: {deriv!r}")

    if data.get("market") != "BTC-USD":
        errors.append(f"market want 'BTC-USD' got {data.get('market')!r}")

    for key in ("stop_loss_spread", "long_profit_taking_spread", "short_profit_taking_spread", "order_amount"):
        try:
            if not _num(data.get(key)) > 0:
                errors.append(f"{key} must be >0, got {data.get(key)!r}")
        except (TypeError, ValueError):
            errors.append(f"{key} missing/invalid: {data.get(key)!r}")

    # order_override places arbitrary extra orders and would bypass order_levels=1.
    if data.get("order_override"):
        errors.append("order_override must be empty")
    return errors


def check_notional(order_amount: float, nav_usd: float, mid: float) -> list[str]:
    """order_amount is BASE size; notional = order_amount * mid must be <= NAV * 8%."""
    if not nav_usd > 0 or not mid > 0:
        return [f"NAV ({nav_usd!r}) and mid ({mid!r}) must be >0 to size orders"]
    notional = order_amount * mid
    cap = nav_usd * MAX_NAV_PCT
    if notional > cap:
        return [f"order notional ${notional:.2f} exceeds NAV 8% cap ${cap:.2f} "
                f"(target order_amount = NAV*0.02/mid = {target_order_amount(nav_usd, mid):.6f})"]
    return []


def target_order_amount(nav_usd: float, mid: float) -> float:
    return nav_usd * TARGET_NAV_PCT / mid


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))
    ap.add_argument("--nav", type=float)
    ap.add_argument("--mid", type=float)
    args = ap.parse_args(argv)
    data = load(Path(args.config))
    errors = check_config(data)
    if args.nav is not None or args.mid is not None:
        try:
            errors += check_notional(_num(data.get("order_amount")), args.nav or 0, args.mid or 0)
        except (TypeError, ValueError):
            pass
    if errors:
        print("FAIL:")
        for e in errors:
            print(" -", e)
        return 1
    print("PASS: C48-2 hard locks OK on", args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
