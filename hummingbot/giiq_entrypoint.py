"""C48-2 wrapper around the official Hummingbot headless quickstart.

Refuses to start unless the Cove gate hard locks hold, sizes order_amount against NAV,
writes the locked strategy + kill switch + HL testnet API-wallet connector into conf/
(not the volume), strips the raw HL key from the env, then execs upstream quickstart.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import assert_locks  # noqa: E402

HB_HOME = Path(os.environ.get("HB_HOME", "/home/hummingbot"))
HB_PYTHON = "/opt/conda/envs/hummingbot/bin/python"
QUICKSTART = "bin/hummingbot_quickstart.py"
BAKED_CONFIG = HERE / "conf_perpetual_market_making_c48_2.yml"
STRATEGY_FILE_NAME = "conf_perpetual_market_making_c48_2.yml"
CONNECTOR = "hyperliquid_perpetual_testnet"
HL_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
KILL_SWITCH_RATE = -4.0

REQUIRED_ENV = ("CONFIG_PASSWORD", "HL_TESTNET_ADDRESS", "HL_TESTNET_API_WALLET_KEY", "GIIQ_NAV_USD")
SECRET_ENV = ("HL_TESTNET_API_WALLET_KEY",)
# SCRIPT_CONFIG would start a V2 script (e.g. v2_funding_rate_arb) instead of the locked strategy.
FORBIDDEN_ENV = ("SCRIPT_CONFIG",)


class LockViolation(Exception):
    pass


def _truthy(value) -> bool:
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def fetch_testnet_mid(coin: str = "BTC") -> float:
    req = urllib.request.Request(
        HL_TESTNET_INFO_URL, data=json.dumps({"type": "allMids"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return float(json.load(resp)[coin])


def prepare(env: dict, argv: list[str], fetch_mid=fetch_testnet_mid) -> tuple[dict, dict]:
    """Validate env + locks. Returns (strategy_config, env_for_upstream)."""
    if argv:
        raise LockViolation(f"extra CLI args are not allowed (could override locks): {argv}")
    for name in FORBIDDEN_ENV:
        if env.get(name, "").strip():
            raise LockViolation(f"{name} is not allowed (only perpetual_market_making path 1)")
    cfn = env.get("CONFIG_FILE_NAME", "").strip()
    if cfn and cfn != STRATEGY_FILE_NAME:
        raise LockViolation(f"CONFIG_FILE_NAME must be {STRATEGY_FILE_NAME!r}, got {cfn!r}")
    if _truthy(env.get("HL_USE_VAULT", "")):
        raise LockViolation("HL_USE_VAULT is not allowed (no vault mode)")
    missing = [n for n in REQUIRED_ENV if not env.get(n, "").strip()]
    if missing:
        raise LockViolation(f"missing required env: {', '.join(missing)}")

    config = assert_locks.load(BAKED_CONFIG)
    override = env.get("GIIQ_ORDER_AMOUNT", "").strip()
    if override:
        config["order_amount"] = float(override)
    errors = assert_locks.check_config(config)

    nav = float(env["GIIQ_NAV_USD"])
    mid_env = env.get("GIIQ_MID_PRICE", "").strip()
    mid = float(mid_env) if mid_env else fetch_mid()
    if not errors:
        errors += assert_locks.check_notional(float(config["order_amount"]), nav, mid)
    if errors:
        raise LockViolation("; ".join(errors))

    out = {k: v for k, v in env.items() if k not in SECRET_ENV}
    out.update(HEADLESS_MODE="true", CONFIG_FILE_NAME=STRATEGY_FILE_NAME)
    pct = float(config["order_amount"]) * mid / nav * 100
    print(f"C48-2 sizing: order_amount={config['order_amount']} BTC x mid {mid:.1f} = "
          f"${float(config['order_amount']) * mid:.2f} ({pct:.2f}% of NAV ${nav:.0f}; target 2%, cap 8%)",
          flush=True)
    return config, out


def write_strategy(config: dict, conf_dir: Path) -> Path:
    path = conf_dir / "strategies" / STRATEGY_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def write_kill_switch(conf_dir: Path) -> Path:
    """Upstream kill switch stops the bot when profitability <= -4% (Cove DD stop)."""
    path = conf_dir / "conf_client.yml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["kill_switch_mode"] = {"kill_switch_rate": KILL_SWITCH_RATE}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_connector(env: dict, conf_dir: Path) -> Path:
    """Encrypt the HL testnet API-wallet credentials the same way `connect` does."""
    from hummingbot.client.config.config_crypt import (
        ETHKeyFileSecretManger,
        store_password_verification,
        validate_password,
    )
    from hummingbot.client.config.config_helpers import ClientConfigAdapter, save_to_yml
    from hummingbot.client.config.security import Security
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_utils import (
        HyperliquidPerpetualTestnetConfigMap,
    )

    connectors = conf_dir / "connectors"
    connectors.mkdir(parents=True, exist_ok=True)
    for stale in connectors.glob("*.yml"):
        stale.unlink()
    sm = ETHKeyFileSecretManger(env["CONFIG_PASSWORD"])
    if (conf_dir / ".password_verification").exists():
        if not validate_password(sm):
            raise LockViolation("CONFIG_PASSWORD does not match conf/.password_verification")
    else:
        store_password_verification(sm)
    Security.secrets_manager = sm
    cm = HyperliquidPerpetualTestnetConfigMap(
        hyperliquid_perpetual_testnet_mode="api_wallet",
        use_vault=False,
        hyperliquid_perpetual_testnet_address=env["HL_TESTNET_ADDRESS"].strip(),
        hyperliquid_perpetual_testnet_secret_key=env["HL_TESTNET_API_WALLET_KEY"].strip(),
    )
    path = connectors / f"{CONNECTOR}.yml"
    save_to_yml(path, ClientConfigAdapter(cm))
    os.chmod(path, 0o600)
    return path


def main() -> int:
    if sys.argv[1:2] == ["--check-config"]:
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else BAKED_CONFIG
        errors = assert_locks.check_config(assert_locks.load(path))
        if errors:
            print("C48-2 lock check FAILED: " + "; ".join(errors), file=sys.stderr)
            return 2
        print(f"C48-2 lock check OK: {path}")
        return 0
    try:
        config, env = prepare(dict(os.environ), sys.argv[1:])
        conf_dir = HB_HOME / "conf"
        write_strategy(config, conf_dir)
        write_kill_switch(conf_dir)
        write_connector(dict(os.environ), conf_dir)
        (HB_HOME / "data" / "logs").mkdir(parents=True, exist_ok=True)
    except (LockViolation, OSError, ValueError, KeyError) as e:
        print(f"C48-2 refusing to start: {e}", file=sys.stderr)
        return 2
    print(f"C48-2 locks OK; starting hummingbot headless: {STRATEGY_FILE_NAME} on {CONNECTOR}", flush=True)
    os.chdir(HB_HOME)
    os.execve(HB_PYTHON, [HB_PYTHON, QUICKSTART], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
