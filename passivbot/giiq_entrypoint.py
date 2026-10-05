"""C48-1 wrapper around upstream Passivbot's container/entrypoint.sh.

Refuses to start unless the Cove gate hard locks hold, renders Hyperliquid
credentials from env into a tmpfs path (never the volume), then execs upstream.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

UPSTREAM_ENTRYPOINT = "/app/container/entrypoint.sh"
DEFAULT_CONFIG_PATH = "/app/giiq/configs/giiq_c48_1_paper.json"
RENDERED_KEYS_PATH = "/run/passivbot/giiq-api-keys.json"

MAX_LEVERAGE = 3
MAX_TWEL = 0.08
MAX_HSL_RED_THRESHOLD = 0.08
ALLOWED_EXCHANGES = {"hyperliquid"}

# Upstream entrypoint options that would bypass the baked, reviewed config.
FORBIDDEN_ENV = ("PB_CONFIG_INLINE", "PB_EXCHANGE", "PB_API_KEY", "PB_API_SECRET")


class LockViolation(Exception):
    pass


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def check_config(config: dict, pb_user: str | None = None) -> list[str]:
    errors = []
    live = config.get("live", {})
    long_ = config.get("bot", {}).get("long", {})
    short = config.get("bot", {}).get("short", {})

    leverage = live.get("leverage")
    if not isinstance(leverage, (int, float)) or not 0 < leverage <= MAX_LEVERAGE:
        errors.append(f"live.leverage={leverage!r} must be in (0, {MAX_LEVERAGE}]")

    if live.get("market_orders_allowed") is not False:
        errors.append("live.market_orders_allowed must be false")

    twel = long_.get("risk", {}).get("total_wallet_exposure_limit")
    if not isinstance(twel, (int, float)) or not 0 <= twel <= MAX_TWEL:
        errors.append(f"bot.long.risk.total_wallet_exposure_limit={twel!r} must be in [0, {MAX_TWEL}]")

    short_twel = short.get("risk", {}).get("total_wallet_exposure_limit")
    if short_twel != 0:
        errors.append(f"bot.short.risk.total_wallet_exposure_limit={short_twel!r} must be 0 (long only)")

    if live.get("hsl_signal_mode", "coin") not in ("coin", "pside"):
        errors.append("live.hsl_signal_mode must be coin or pside so bot.long.hsl applies")

    hsl = long_.get("hsl", {})
    if hsl.get("enabled") is not True:
        errors.append("bot.long.hsl.enabled must be true")
    red = hsl.get("red_threshold")
    if not isinstance(red, (int, float)) or not 0 < red <= MAX_HSL_RED_THRESHOLD:
        errors.append(f"bot.long.hsl.red_threshold={red!r} must be in (0, {MAX_HSL_RED_THRESHOLD}]")

    if config.get("coin_overrides"):
        errors.append("coin_overrides must be empty (per-coin overrides could bypass locks)")

    user = live.get("user")
    if pb_user is not None and user != pb_user:
        errors.append(f"live.user={user!r} must equal PB_USER={pb_user!r}")
    return errors


def check_api_keys(keys: dict, pb_user: str) -> list[str]:
    entry = keys.get(pb_user)
    if not isinstance(entry, dict):
        return [f"api-keys has no entry for PB_USER={pb_user!r}"]
    errors = []
    if entry.get("exchange") not in ALLOWED_EXCHANGES:
        errors.append(f"api-keys[{pb_user}].exchange={entry.get('exchange')!r} not in {sorted(ALLOWED_EXCHANGES)}")
    if _truthy(entry.get("is_vault", False)):
        errors.append(f"api-keys[{pb_user}].is_vault must be false (vault mode is not allowed)")
    return errors


def render_hl_keys(env: dict, pb_user: str, path: str = RENDERED_KEYS_PATH) -> str:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        pb_user: {
            "exchange": "hyperliquid",
            "wallet_address": env["HL_WALLET_ADDRESS"].strip(),
            "private_key": env["HL_PRIVATE_KEY"].strip(),
            "is_vault": False,
        }
    }
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return str(out)


def prepare(env: dict, argv: list[str]) -> dict:
    """Validate everything and return the env to exec upstream with."""
    if argv:
        raise LockViolation(f"extra CLI args are not allowed (could override locks): {argv}")
    for name in FORBIDDEN_ENV:
        if env.get(name, "").strip():
            raise LockViolation(f"{name} is not allowed; use the baked config and HL_* secrets")

    pb_user = env.get("PB_USER", "").strip()
    if not pb_user:
        raise LockViolation("PB_USER is required")

    env = dict(env)
    config_path = env.setdefault("PB_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    errors = check_config(config, pb_user)

    if env.get("HL_WALLET_ADDRESS", "").strip() and env.get("HL_PRIVATE_KEY", "").strip():
        env["PB_API_KEYS_PATH"] = render_hl_keys(env, pb_user)
    keys_path = env.get("PB_API_KEYS_PATH", "").strip()
    if not keys_path or not Path(keys_path).is_file():
        errors.append("set HL_WALLET_ADDRESS + HL_PRIVATE_KEY, or PB_API_KEYS_PATH to an existing file")
    else:
        errors += check_api_keys(json.loads(Path(keys_path).read_text(encoding="utf-8")), pb_user)

    if errors:
        raise LockViolation("; ".join(errors))
    env.pop("HL_PRIVATE_KEY", None)
    return env


def main() -> int:
    if sys.argv[1:2] == ["--check-config"]:
        path = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("PB_CONFIG_PATH", DEFAULT_CONFIG_PATH)
        errors = check_config(json.loads(Path(path).read_text(encoding="utf-8")))
        if errors:
            print("C48-1 lock check FAILED: " + "; ".join(errors), file=sys.stderr)
            return 2
        print(f"C48-1 lock check OK: {path}")
        return 0
    try:
        env = prepare(dict(os.environ), sys.argv[1:])
    except (LockViolation, OSError, ValueError, KeyError) as e:
        print(f"C48-1 refusing to start: {e}", file=sys.stderr)
        return 2
    print(f"C48-1 locks OK; starting passivbot live for {env['PB_USER']} with {env['PB_CONFIG_PATH']}", flush=True)
    os.execve(UPSTREAM_ENTRYPOINT, [UPSTREAM_ENTRYPOINT], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
