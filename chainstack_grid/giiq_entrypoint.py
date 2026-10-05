#!/usr/bin/env python3
"""C48-3 wrapper around chainstacklabs/hyperliquid-trading-bot (HL TESTNET ONLY).

Startup (any failure -> exit 2, bot never starts):
  1. refuse extra CLI args, mainnet env, endpoint overrides, HYPERLIQUID_TESTNET != true,
     a halt marker left by a previous watchdog stop;
  2. assert_locks.py on the baked yaml;
  3. upstream `run_bot.py --validate` on the baked yaml;
  4. signer must be an approved API/agent wallet of TESTNET_WALLET_ADDRESS (not the master key);
  5. update_leverage(2, BTC, isolated) on testnet, read back, abort if > 3;
  6. GIIQ_NAV_USD = real testnet equity; allocation must be <= 2% NAV and fit >= 2 grid levels
     at the HL $10.5 min notional.
Then the bot runs as a child process. The watchdog polls clearinghouseState and on
lev > 3 / DD >= 4% / BTC notional > 8% NAV / non-BTC position / reconciliation failure
stops the bot, cancels all orders, closes every open position reduce-only (IOC) and confirms
size 0 (CRITICAL + reason in the halt marker if not), writes the halt marker, exits non-zero.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import assert_locks  # noqa: E402

UPSTREAM_DIR = Path(os.environ.get("GIIQ_UPSTREAM_DIR", "/app/upstream"))
LOCKED_YAML = HERE / "bots" / "c48_3_btc_grid_locked.yaml"
DATA_DIR = Path("/data")
HALT_MARKER_NAME = "c48_3_HALTED"

TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
COIN = assert_locks.SYMBOL
TARGET_LEVERAGE = assert_locks.TARGET_LEVERAGE
MAX_LEVERAGE = assert_locks.MAX_LEVERAGE
HL_MIN_NOTIONAL_USD = 10.5  # upstream basic_grid.MIN_NOTIONAL_USD
MIN_GRID_LEVELS = 2  # upstream geometric spacing divides by (levels - 1)
RECONCILE_MAX_FAILURES = 3
CLOSE_ATTEMPTS = 3
CLOSE_SLIPPAGE = 0.05

REQUIRED_ENV = ("HYPERLIQUID_TESTNET_PRIVATE_KEY", "TESTNET_WALLET_ADDRESS")
SECRET_ENV = ("HYPERLIQUID_TESTNET_PRIVATE_KEY",)
# Upstream KeyManager would fall back to these (legacy keys work for either network), and
# HYPERLIQUID_TESTNET_*_URL would let the "testnet" router talk to an arbitrary host.
FORBIDDEN_ENV = (
    "HYPERLIQUID_PRIVATE_KEY",
    "HYPERLIQUID_PRIVATE_KEY_FILE",
    "HYPERLIQUID_TESTNET_KEY_FILE",
)
FORBIDDEN_ENV_PREFIXES = ("HYPERLIQUID_PUBLIC_", "HYPERLIQUID_CHAINSTACK_",
                          "HYPERLIQUID_TESTNET_PUBLIC_", "HYPERLIQUID_TESTNET_CHAINSTACK_")
MAINNET_HOST_MARKER = "hyperliquid.xyz"  # testnet hosts are *.hyperliquid-testnet.xyz

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class LockViolation(Exception):
    pass


_log_file = None
_secrets: list[str] = []


def redact(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 12 else "***"


def scrub(text: str) -> str:
    for s in _secrets:
        if s:
            text = text.replace(s, "***")
    return text


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} [c48-3] {scrub(msg)}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


def check_env(env: dict, argv: list[str]) -> None:
    if argv:
        raise LockViolation(f"extra CLI args are not allowed (could override locks): {argv}")
    if env.get("HYPERLIQUID_TESTNET", "").strip().lower() != "true":
        raise LockViolation("HYPERLIQUID_TESTNET must be 'true'")
    for name, value in env.items():
        if "MAINNET" in name.upper():
            raise LockViolation(f"{name} is not allowed (mainnet)")
        if name in FORBIDDEN_ENV or name.startswith(FORBIDDEN_ENV_PREFIXES):
            raise LockViolation(f"{name} is not allowed (only HYPERLIQUID_TESTNET_PRIVATE_KEY + default testnet endpoints)")
        if MAINNET_HOST_MARKER in str(value).lower():
            raise LockViolation(f"{name} points at a mainnet host")
    missing = [n for n in REQUIRED_ENV if not env.get(n, "").strip()]
    if missing:
        raise LockViolation(f"missing required env: {', '.join(missing)}")
    if not KEY_RE.match(env["HYPERLIQUID_TESTNET_PRIVATE_KEY"].strip()):
        raise LockViolation("HYPERLIQUID_TESTNET_PRIVATE_KEY is not a 32-byte hex key")
    if not ADDR_RE.match(env["TESTNET_WALLET_ADDRESS"].strip()):
        raise LockViolation("TESTNET_WALLET_ADDRESS is not a 0x address")


def check_halt_marker(data_dir: Path) -> None:
    marker = data_dir / HALT_MARKER_NAME
    if marker.exists():
        raise LockViolation(f"{marker} exists (previous watchdog stop); human review, then delete it")


def check_locks(path: Path = LOCKED_YAML) -> None:
    errors = assert_locks.check_config(assert_locks.load(path))
    if errors:
        raise LockViolation("lock check failed: " + "; ".join(errors))


def run_upstream_validate(python: str = sys.executable, upstream: Path = UPSTREAM_DIR,
                          yaml_path: Path = LOCKED_YAML) -> None:
    proc = subprocess.run([python, "src/run_bot.py", "--validate", str(yaml_path)],
                          cwd=upstream, capture_output=True, text=True, timeout=120)
    for line in (proc.stdout + proc.stderr).splitlines():
        log(f"validate: {line}")
    if proc.returncode != 0:
        raise LockViolation(f"upstream --validate exited {proc.returncode}")


class HLTestnetClient:
    """Thin HL SDK wrapper, hardwired to TESTNET_API_URL."""

    def __init__(self, private_key: str, master: str):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        wallet = Account.from_key(private_key)
        self.signer_address = wallet.address
        self.master = master
        self.info = Info(TESTNET_API_URL, skip_ws=True)
        self.exchange = Exchange(wallet, TESTNET_API_URL, account_address=master)

    def update_leverage(self, leverage: int, coin: str, is_cross: bool):
        return self.exchange.update_leverage(leverage, coin, is_cross=is_cross)

    def user_state(self) -> dict:
        return self.info.user_state(self.master)

    def active_asset_data(self, coin: str) -> dict:
        return self.info.post("/info", {"type": "activeAssetData", "user": self.master, "coin": coin})

    def extra_agents(self) -> list:
        return self.info.post("/info", {"type": "extraAgents", "user": self.master}) or []

    def open_orders(self) -> list:
        return self.info.open_orders(self.master)

    def close_position(self, coin: str, szi: float):
        """Reduce-only IOC limit at the SDK's 5% slippage price (what SDK market_close does)."""
        is_buy = szi < 0
        px = self.exchange._slippage_price(coin, is_buy, CLOSE_SLIPPAGE)
        return self.exchange.order(coin, is_buy, abs(szi), px,
                                   {"limit": {"tif": "Ioc"}}, reduce_only=True)

    def cancel_all(self, coin: str) -> int:
        orders = [o for o in self.open_orders() if o.get("coin") == coin]
        if orders:
            self.exchange.bulk_cancel([{"coin": coin, "oid": o["oid"]} for o in orders])
        return len(orders)


def check_api_wallet(client, master: str) -> None:
    signer = client.signer_address
    if signer.lower() == master.lower():
        raise LockViolation("HYPERLIQUID_TESTNET_PRIVATE_KEY is the master key; use an API/agent wallet key")
    approved = {str(a.get("address", "")).lower() for a in client.extra_agents()}
    if signer.lower() not in approved:
        raise LockViolation(f"API wallet {redact(signer)} is not approved on master {redact(master)}")
    log(f"API wallet {redact(signer)} approved on master {redact(master)}")


def _btc_position(state: dict, coin: str = COIN) -> dict | None:
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position") or {}
        if pos.get("coin") == coin:
            return pos
    return None


def measure_leverage(client, coin: str = COIN, state: dict | None = None) -> tuple[float, str]:
    """Max of the asset leverage setting and any open position's leverage."""
    state = client.user_state() if state is None else state
    readings: list[tuple[float, str]] = []
    lev = (client.active_asset_data(coin) or {}).get("leverage") or {}
    if lev.get("value") is not None:
        readings.append((float(lev["value"]), str(lev.get("type", "?"))))
    pos = _btc_position(state, coin)
    if pos and (pos.get("leverage") or {}).get("value") is not None:
        readings.append((float(pos["leverage"]["value"]), str(pos["leverage"].get("type", "?"))))
    if not readings:
        raise LockViolation(f"could not read back {coin} leverage")
    return max(readings)


def enforce_leverage(client, coin: str = COIN) -> float:
    resp = client.update_leverage(TARGET_LEVERAGE, coin, is_cross=False)
    status = (resp or {}).get("status")
    if status != "ok":
        log(f"update_leverage({TARGET_LEVERAGE}, {coin}, isolated) returned status={status!r}: {resp}")
    else:
        log(f"update_leverage({TARGET_LEVERAGE}, {coin}, isolated) ok")
    value, mode = measure_leverage(client, coin)
    log(f"measured {coin} leverage {value:g}x {mode}")
    if value > MAX_LEVERAGE:
        raise LockViolation(f"{coin} leverage {value:g}x > {MAX_LEVERAGE}x")
    if mode != "isolated":
        log(f"WARNING: {coin} leverage mode is {mode!r}, not isolated")
    return value


def account_equity(state: dict) -> float:
    return float((state.get("marginSummary") or {}).get("accountValue", 0) or 0)


def size_allocation(equity: float, cfg: dict) -> tuple[float, int]:
    pct = float(cfg["account"]["max_allocation_pct"])
    levels = int(cfg["grid"]["levels"])
    if not equity > 0:
        raise LockViolation(f"testnet equity is {equity!r}; fund the master via the testnet faucet")
    alloc = equity * pct / 100.0
    nav_pct = alloc / equity * 100.0
    if nav_pct > assert_locks.MAX_ALLOCATION_PCT + 1e-9:
        raise LockViolation(f"allocation ${alloc:.2f} is {nav_pct:.2f}% NAV > {assert_locks.MAX_ALLOCATION_PCT}%")
    effective = min(levels, int(alloc // HL_MIN_NOTIONAL_USD))
    log(f"NAV ${equity:.2f}; allocation {pct:g}% = ${alloc:.2f} ({nav_pct:.2f}% NAV); "
        f"grid levels {levels} -> {effective} at HL min notional ${HL_MIN_NOTIONAL_USD}")
    if effective < MIN_GRID_LEVELS:
        need = MIN_GRID_LEVELS * HL_MIN_NOTIONAL_USD * 100.0 / pct
        raise LockViolation(f"allocation ${alloc:.2f} fits {effective} level(s); need equity >= ${need:.0f}")
    return alloc, effective


def check_risk(state: dict, lev: float, peak_equity: float, open_orders: list,
               max_orders: int, cfg: dict) -> list[str]:
    rm = cfg["risk_management"]
    breaches: list[str] = []
    equity = account_equity(state)
    if lev > MAX_LEVERAGE:
        breaches.append(f"leverage {lev:g}x > {MAX_LEVERAGE}x")
    if peak_equity > 0:
        dd = (peak_equity - equity) / peak_equity * 100.0
        if dd >= float(rm["max_drawdown_pct"]):
            breaches.append(f"account drawdown {dd:.2f}% >= {rm['max_drawdown_pct']}% (peak ${peak_equity:.2f}, now ${equity:.2f})")
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        if float(pos.get("szi", 0) or 0) == 0:
            continue
        if coin != COIN:
            breaches.append(f"unexpected position in {coin!r}")
        notional = abs(float(pos.get("positionValue", 0) or 0))
        if equity > 0 and notional / equity * 100.0 > float(rm["max_position_size_pct"]):
            breaches.append(f"{coin} notional ${notional:.2f} > {rm['max_position_size_pct']}% of NAV ${equity:.2f}")
    others = [o for o in open_orders if o.get("coin") != COIN]
    if others:
        breaches.append(f"open orders on non-{COIN} coins: {sorted({o.get('coin') for o in others})}")
    if len(open_orders) > max_orders:
        breaches.append(f"{len(open_orders)} open orders > expected max {max_orders}")
    return breaches


def startup(env: dict, argv: list[str], client_factory=HLTestnetClient,
            validate=run_upstream_validate, data_dir: Path = DATA_DIR):
    """All pre-launch gates. Returns (client, cfg, child_env, equity, levels)."""
    check_env(env, argv)
    check_halt_marker(data_dir)
    check_locks(LOCKED_YAML)
    log("lock check OK: " + LOCKED_YAML.name)
    validate()
    master = env["TESTNET_WALLET_ADDRESS"].strip()
    client = client_factory(env["HYPERLIQUID_TESTNET_PRIVATE_KEY"].strip(), master)
    check_api_wallet(client, master)
    enforce_leverage(client)
    cfg = assert_locks.load(LOCKED_YAML)
    equity = account_equity(client.user_state())
    if env.get("GIIQ_NAV_USD", "").strip():
        log("ignoring preset GIIQ_NAV_USD; using measured testnet equity")
    _, levels = size_allocation(equity, cfg)
    child_env = dict(env, HYPERLIQUID_TESTNET="true", GIIQ_NAV_USD=f"{equity:.6f}", PYTHONUNBUFFERED="1")
    return client, cfg, child_env, equity, levels


def _tee(stream, prefix: str) -> None:
    for line in iter(stream.readline, ""):
        log(f"{prefix}{line.rstrip()}")


def _open_positions(state: dict) -> dict[str, float]:
    out = {}
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position") or {}
        szi = float(pos.get("szi", 0) or 0)
        if szi != 0:
            out[pos.get("coin")] = szi
    return out


def _fill_summary(resp) -> str:
    try:
        st = resp["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return f"unexpected response {resp!r}"
    if "filled" in st:
        return f"filled {st['filled'].get('totalSz')} @ {st['filled'].get('avgPx')}"
    return f"not filled: {st}"


def flatten(client) -> list[str]:
    """Cancel every open order, then reduce-only IOC close every open position and confirm
    size 0 via a fresh clearinghouseState read. Returns problems (empty = flat)."""
    problems: list[str] = []
    try:
        coins = {COIN} | {o.get("coin") for o in client.open_orders()}
        for coin in sorted(coins):
            log(f"cancelled {client.cancel_all(coin)} open {coin} orders")
    except Exception as e:  # noqa: BLE001
        problems.append(f"cancel failed: {e}")
    try:
        positions = _open_positions(client.user_state())
    except Exception as e:  # noqa: BLE001
        return problems + [f"cannot read positions: {e}"]
    for coin, szi in sorted(positions.items()):
        size = szi
        for attempt in range(1, CLOSE_ATTEMPTS + 1):
            try:
                resp = client.close_position(coin, size)
                summary = _fill_summary(resp)
                after = _open_positions(client.user_state()).get(coin, 0.0)
            except Exception as e:  # noqa: BLE001
                log(f"close {coin} attempt {attempt}/{CLOSE_ATTEMPTS} failed: {e}")
                continue
            log(f"close {coin} attempt {attempt}/{CLOSE_ATTEMPTS}: size {size:g} -> {after:g} ({summary})")
            size = after
            if size == 0:
                break
        if size != 0:
            problems.append(f"{coin} position still {size:g} after {CLOSE_ATTEMPTS} reduce-only closes (was {szi:g})")
    return problems


def stop_bot(proc, client, data_dir: Path, reason: str, halt: bool = True) -> list[str]:
    log(f"STOP: {reason}")
    marker = data_dir / HALT_MARKER_NAME
    if halt and data_dir.is_dir():
        marker.write_text(f"{datetime.now(timezone.utc).isoformat()} {scrub(reason)}\n", encoding="utf-8")
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    problems = flatten(client)
    if problems:
        msg = "CRITICAL: flatten failed, close manually in the HL testnet UI: " + "; ".join(problems)
        log(msg)
        if halt and data_dir.is_dir():
            with marker.open("a", encoding="utf-8") as f:
                f.write(scrub(msg) + "\n")
    else:
        log("flatten OK: no open orders or positions")
    return problems


def watchdog(proc, client, cfg: dict, equity: float, levels: int, interval: float,
             data_dir: Path = DATA_DIR) -> int:
    peak = equity
    failures = 0
    max_orders = 2 * int(cfg["grid"]["levels"])
    while True:
        time.sleep(interval)
        if proc.poll() is not None:
            stop_bot(proc, client, data_dir, f"bot exited with code {proc.returncode}")
            return 3
        try:
            state = client.user_state()
            lev, _ = measure_leverage(client, COIN, state)
            orders = client.open_orders()
            failures = 0
        except Exception as e:  # noqa: BLE001
            failures += 1
            log(f"reconciliation read failed ({failures}/{RECONCILE_MAX_FAILURES}): {e}")
            if failures >= RECONCILE_MAX_FAILURES:
                stop_bot(proc, client, data_dir, "reconciliation failure: cannot read HL testnet state")
                return 4
            continue
        breaches = check_risk(state, lev, peak, orders, max_orders, cfg)
        if breaches:
            stop_bot(proc, client, data_dir, "; ".join(breaches))
            return 4
        peak = max(peak, account_equity(state))


def _open_log(data_dir: Path) -> None:
    global _log_file
    if data_dir.is_dir():
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        name = f"c48_3_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
        _log_file = open(logs / name, "a", encoding="utf-8")


def main() -> int:
    if sys.argv[1:2] == ["--check-config"]:
        try:
            check_locks(LOCKED_YAML)
        except LockViolation as e:
            print(f"C48-3 {e}", file=sys.stderr)
            return 2
        print(f"C48-3 lock check OK: {LOCKED_YAML}")
        return 0

    env = dict(os.environ)
    _secrets.extend(env.get(n, "").strip() for n in SECRET_ENV)
    _open_log(DATA_DIR)
    try:
        client, cfg, child_env, equity, levels = startup(env, sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        log(f"refusing to start: {e}")
        return 2

    interval = float(env.get("GIIQ_WATCHDOG_SEC", "60") or 60)
    log(f"starting upstream bot on HL testnet: {LOCKED_YAML.name} (watchdog every {interval:g}s)")
    proc = subprocess.Popen([sys.executable, "src/run_bot.py", str(LOCKED_YAML)], cwd=UPSTREAM_DIR,
                            env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=_tee, args=(proc.stdout, "bot: "), daemon=True).start()

    def _forward(signum, _frame):
        log(f"received signal {signum}; stopping bot")
        stop_bot(proc, client, DATA_DIR, f"signal {signum}", halt=False)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)
    return watchdog(proc, client, cfg, equity, levels, interval)


if __name__ == "__main__":
    raise SystemExit(main())
