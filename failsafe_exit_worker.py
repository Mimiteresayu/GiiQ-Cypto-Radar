#!/usr/bin/env python3
"""Fail-safe exit worker: deterministic tier-based exits (DORMANT build; not enabled).

LOCKED rules from SIZE_TIER_EXIT_LOCKED.md / HARBOR_AUTOTRADE_PROMPT_v1.md:
- GC periods: 1D=144, 4H=72, 1H=48, Lag/Fast off, closed bars only
- Tiers by mcap: Mega/Large → 4H Filter cross-down; Small/Tiny → 1H Lower cross-down
- Hard SL: Mega/Large → 4H Lower (period 72); Small/Tiny → 4H Filter (mid); re-aligned each run
  (same as exec_common.hard_sl_for_tier / mcap_tiers.HARD_SL_BY_TIER)
- Shorts: report-only (no exit logic locked yet)
- Never opens positions

Mode:
  FAILSAFE_ENABLE=1 + HL_API_WALLET_KEY present → LIVE (places orders)
  Otherwise → DRY_RUN (writes out/failsafe_last.json only)

Staleness guard: if radar/candles stale or unreachable, no destructive actions.
Retry/backoff on HL 429.
Optional ALERT_WEBHOOK_URL: POST short text on live action/error.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request as _url_req
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scan_gc_radar import fetch_candles, compute_gc, hl_post, gc_period_for_tf  # noqa: E402
from mcap_tiers import tier_for, PRIMARY_RULE, HARD_SL_BY_TIER  # noqa: E402

# Hyperliquid SDK imports (only when LIVE_MODE)
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    Exchange = None
    Info = None
    Account = None

OUT = ROOT / "out" / "failsafe_last.json"
RADAR_DIR = ROOT / "out"
STALENESS_THRESHOLD_S = 7200  # 2h; reject radar older than this
LOOKBACK_BARS = 10  # minimal lookback for cross detection

ENABLE = os.environ.get("FAILSAFE_ENABLE", "").strip() == "1"
HL_ADDRESS = (os.environ.get("HL_ADDRESS") or "").strip()
HL_API_WALLET_KEY = (os.environ.get("HL_API_WALLET_KEY") or "").strip()
ALERT_WEBHOOK_URL = (os.environ.get("ALERT_WEBHOOK_URL") or "").strip()

LIVE_MODE = ENABLE and bool(HL_API_WALLET_KEY)

# Initialize SDK exchange client (lazy, only in LIVE_MODE)
_exchange_client = None


def _get_exchange() -> Optional[Any]:
    """Get or create hyperliquid exchange client (LIVE_MODE only)."""
    global _exchange_client
    if not LIVE_MODE or not SDK_AVAILABLE:
        return None
    if _exchange_client is None:
        if not HL_API_WALLET_KEY or not HL_ADDRESS:
            sys.stderr.write("[failsafe] ERROR: LIVE_MODE but missing HL_API_WALLET_KEY or HL_ADDRESS\n")
            return None
        try:
            # Create wallet from private key (API/agent wallet key)
            wallet = Account.from_key(HL_API_WALLET_KEY)
            # Exchange client with wallet and account_address
            _exchange_client = Exchange(
                wallet=wallet,
                base_url=constants.MAINNET_API_URL,
                account_address=HL_ADDRESS,  # trade on behalf of this address
            )
            sys.stderr.write(f"[failsafe] SDK exchange client initialized for {HL_ADDRESS[:8]}...\n")
        except Exception as e:
            sys.stderr.write(f"[failsafe] ERROR: failed to init exchange client: {e}\n")
            return None
    return _exchange_client


def _alert(text: str) -> None:
    """POST short text to ALERT_WEBHOOK_URL if set."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"text": text}).encode()
        req = _url_req.Request(
            ALERT_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _url_req.urlopen(req, timeout=10) as _resp:
            pass
    except Exception as e:
        sys.stderr.write(f"[failsafe] alert webhook error: {e}\n")


def _hl_post_retry(payload: dict, retries=3, backoff_s=2) -> dict:
    """POST to HL with retry/backoff on 429."""
    for attempt in range(retries):
        try:
            return hl_post(payload)
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                sys.stderr.write(f"[failsafe] HL 429 retry {attempt+1}/{retries} in {backoff_s}s\n")
                time.sleep(backoff_s)
                backoff_s *= 2
                continue
            raise
    raise RuntimeError("HL retry exhausted")


def _check_radar_freshness(tf: str) -> tuple[bool, Optional[str]]:
    """Check if radar JSON exists and is fresh. Returns (ok, error_msg)."""
    fp = RADAR_DIR / f"gc_radar_{tf}.json"
    if not fp.is_file():
        return False, f"radar_{tf}_missing"
    try:
        with open(fp) as f:
            data = json.load(f)
        ts_str = data.get("ts", "")
        if not ts_str:
            return False, f"radar_{tf}_no_timestamp"
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_s > STALENESS_THRESHOLD_S:
            return False, f"radar_{tf}_stale_{int(age_s)}s"
        return True, None
    except Exception as e:
        return False, f"radar_{tf}_read_error:{e}"


def _gc_closed(coin: str, tf: str) -> Dict[str, Any]:
    """Last fully closed bar GC levels + prev for cross detection."""
    period = gc_period_for_tf(tf)
    try:
        bars = fetch_candles(coin, tf)
    except Exception as e:
        return {"ok": False, "tf": tf, "error": f"fetch_candles:{e}"}
    if len(bars) < period + 20:
        return {"ok": False, "tf": tf, "error": "not_enough_bars"}
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=period)
    i = len(bars) - 2  # last closed
    if i < 1:
        return {"ok": False, "tf": tf, "error": "no_prev_bar"}
    return {
        "ok": True,
        "tf": tf,
        "period": period,
        "bar_utc": datetime.fromtimestamp(bars[i]["t"] / 1000, tz=timezone.utc).isoformat(),
        "close": float(c[i]),
        "prev_close": float(c[i - 1]),
        "upper": float(gc[i]["upper"]),
        "filter": float(gc[i]["filter"]),
        "prev_filter": float(gc[i - 1]["filter"]),
        "lower": float(gc[i]["lower"]),
        "prev_lower": float(gc[i - 1]["lower"]),
    }


def _round_trigger(px: float) -> float:
    """Round trigger price per HL precision."""
    if px >= 1000:
        return round(px, 1)
    if px >= 100:
        return round(px, 2)
    if px >= 1:
        return round(px, 4)
    if px >= 0.01:
        return round(px, 6)
    return round(px, 8)


def _compute_long_signal(coin: str, tier: str, entry: float, size: float) -> Dict[str, Any]:
    """Compute tier-based exit signal + hard SL for a long position."""
    primary_rule = PRIMARY_RULE[tier]
    hard_sl_rule = HARD_SL_BY_TIER.get(tier, "4h_filter")

    # Always need 4H for hard SL (Mega/Large: 4H Lower; Small/Tiny: 4H Filter)
    g4 = _gc_closed(coin, "4h")
    if not g4.get("ok"):
        return {
            "ok": False,
            "tier": tier,
            "error": f"4h_gc_error:{g4.get('error')}",
            "exit_signal": None,
            "hard_sl_px": None,
        }

    # Hard SL per tier (mcap_tiers.HARD_SL_BY_TIER)
    hard_sl_px = _round_trigger(g4["lower"] if hard_sl_rule == "4h_lower" else g4["filter"])

    exit_signal = None
    primary_tf = "4h"

    if tier in ("mega", "large"):
        # 4H Filter cross-down
        cross = g4["close"] < g4["filter"] and g4["prev_close"] >= g4["prev_filter"]
        if cross:
            exit_signal = "EXIT_LONG_4H_FILTER_CROSS"
        primary_tf = "4h"

    elif tier in ("small", "tiny"):
        # 1H Lower cross-down = out of channel
        g1 = _gc_closed(coin, "1h")
        if not g1.get("ok"):
            return {
                "ok": False,
                "tier": tier,
                "error": f"1h_gc_error:{g1.get('error')}",
                "exit_signal": None,
                "hard_sl_px": hard_sl_px,
            }
        cross = g1["close"] < g1["lower"] and g1["prev_close"] >= g1["prev_lower"]
        if cross:
            exit_signal = "EXIT_LONG_1H_LOWER_CROSS"
        primary_tf = "1h"

    else:
        return {
            "ok": False,
            "tier": tier,
            "error": f"unknown_tier:{tier}",
            "exit_signal": None,
            "hard_sl_px": hard_sl_px,
        }

    return {
        "ok": True,
        "tier": tier,
        "primary_rule": primary_rule,
        "hard_sl_rule": hard_sl_rule,
        "primary_tf": primary_tf,
        "exit_signal": exit_signal,
        "hard_sl_px": hard_sl_px,
        "error": None,
    }


def _current_stop_trigger(orders: list, coin: str) -> Optional[float]:
    """Find current stop trigger for coin."""
    for o in orders:
        if (o.get("coin") or o.get("symbol")) != coin:
            continue
        if not o.get("isTrigger") and "stop" not in str(o.get("orderType") or "").lower():
            continue
        try:
            t = float(o.get("triggerPx") or o.get("limitPx") or 0)
            if t > 0:
                return t
        except (TypeError, ValueError):
            continue
    return None


def _get_asset_info(coin: str) -> Optional[Dict[str, Any]]:
    """Fetch asset metadata (szDecimals) from HL."""
    try:
        result = hl_post({"type": "meta"})
        universe = result.get("universe") or []
        for asset in universe:
            if asset.get("name") == coin:
                return asset
        return None
    except Exception as e:
        sys.stderr.write(f"[failsafe] get_asset_info {coin} error: {e}\n")
        return None


def _round_size(size: float, sz_decimals: int) -> float:
    """Round size to lot-size precision (szDecimals)."""
    if sz_decimals <= 0:
        return float(int(size))
    return round(size, sz_decimals)


def _round_price(price: float, sz_decimals: int) -> float:
    """Round price to HL tick rules: max 5 sig figs, price decimals = 6 - szDecimals."""
    if price <= 0:
        return price
    # Price decimals = 6 - szDecimals for perps
    price_decimals = max(0, 6 - sz_decimals)
    rounded = round(price, price_decimals)
    # Max 5 significant figures
    if rounded == 0:
        return rounded
    # Count sig figs and truncate if needed
    import math
    magnitude = math.floor(math.log10(abs(rounded)))
    sig_figs = 5
    scale = 10 ** (sig_figs - 1 - magnitude)
    return round(rounded * scale) / scale


def _slippage_price(mid: float, is_buy: bool, slippage_pct: float, sz_decimals: int) -> float:
    """Compute slippage price from mid (for market orders)."""
    if is_buy:
        # Buy: pay up to mid * (1 + slippage)
        slipped = mid * (1.0 + slippage_pct / 100.0)
    else:
        # Sell: accept down to mid * (1 - slippage)
        slipped = mid * (1.0 - slippage_pct / 100.0)
    return _round_price(slipped, sz_decimals)


def _get_mid_price(coin: str) -> Optional[float]:
    """Fetch current mid price for coin from HL."""
    try:
        result = hl_post({"type": "allMids"})
        mids = result if isinstance(result, dict) else {}
        mid_str = mids.get(coin)
        if mid_str:
            return float(mid_str)
        return None
    except Exception as e:
        sys.stderr.write(f"[failsafe] get_mid_price {coin} error: {e}\n")
        return None


def _place_market_close(coin: str, size: float, is_long: bool, slippage_pct: float = 2.0) -> Dict[str, Any]:
    """Place reduce-only market close via hyperliquid-python-sdk."""
    exchange = _get_exchange()
    if not exchange:
        sys.stderr.write(f"[failsafe] DRY_RUN: market close {coin} size={size} is_long={is_long}\n")
        return {"action": "market_close", "coin": coin, "size": size, "is_long": is_long, "status": "dry_run"}

    # Get asset metadata for size rounding
    asset_info = _get_asset_info(coin)
    if not asset_info:
        return {
            "action": "market_close",
            "coin": coin,
            "size": size,
            "error": "asset_info_unavailable",
            "status": "error",
        }

    sz_decimals = asset_info.get("szDecimals", 0)
    rounded_size = _round_size(size, sz_decimals)

    try:
        # Use Exchange.market_close convenience method (handles slippage internally)
        result = exchange.market_close(coin, sz=rounded_size, slippage=slippage_pct / 100.0)
        sys.stderr.write(f"[failsafe] LIVE: market_close {coin} sz={rounded_size} slippage={slippage_pct}% result={result}\n")
        return {
            "action": "market_close",
            "coin": coin,
            "size": rounded_size,
            "is_long": is_long,
            "slippage_pct": slippage_pct,
            "result": result,
            "status": "placed",
        }
    except Exception as e:
        sys.stderr.write(f"[failsafe] ERROR: market close {coin} failed: {e}\n")
        return {
            "action": "market_close",
            "coin": coin,
            "size": rounded_size,
            "error": str(e),
            "status": "error",
        }


def _place_stop_trigger(coin: str, trigger_px: float, size: float, slippage_pct: float = 2.0) -> Dict[str, Any]:
    """Place reduce-only stop-market trigger order for hard SL."""
    exchange = _get_exchange()
    if not exchange:
        return {"action": "place_sl", "coin": coin, "trigger": trigger_px, "status": "dry_run"}

    # Get asset metadata
    asset_info = _get_asset_info(coin)
    if not asset_info:
        return {"action": "place_sl", "coin": coin, "trigger": trigger_px, "error": "asset_info_unavailable", "status": "error"}

    sz_decimals = asset_info.get("szDecimals", 0)
    rounded_size = _round_size(size, sz_decimals)
    
    # Round trigger_px to HL tick rules
    rounded_trigger = _round_price(trigger_px, sz_decimals)
    
    # Compute limit_px with slippage below trigger (longs sell on stop)
    limit_px = _round_price(rounded_trigger * (1.0 - slippage_pct / 100.0), sz_decimals)

    try:
        # Stop-market: trigger below for longs (sell when price drops)
        # OrderType: {"trigger": {"triggerPx": float, "isMarket": bool, "tpsl": "sl"}}
        order_type: Dict[str, Any] = {
            "trigger": {
                "triggerPx": rounded_trigger,
                "isMarket": True,
                "tpsl": "sl",
            }
        }
        result = exchange.order(
            name=coin,
            is_buy=False,  # longs: sell on stop
            sz=rounded_size,
            limit_px=limit_px,
            order_type=order_type,
            reduce_only=True,
        )
        sys.stderr.write(f"[failsafe] LIVE: place SL trigger {coin} @ {rounded_trigger} limit_px={limit_px} size={rounded_size} result={result}\n")
        return {
            "action": "place_sl",
            "coin": coin,
            "trigger": rounded_trigger,
            "limit_px": limit_px,
            "size": rounded_size,
            "result": result,
            "status": "placed",
        }
    except Exception as e:
        sys.stderr.write(f"[failsafe] ERROR: place SL trigger {coin} failed: {e}\n")
        return {"action": "place_sl", "coin": coin, "trigger": rounded_trigger, "error": str(e), "status": "error"}


def _cancel_stop_triggers(coin: str, orders: list) -> List[Dict[str, Any]]:
    """Cancel existing stop triggers for coin."""
    exchange = _get_exchange()
    if not exchange:
        return []

    cancels = []
    for o in orders:
        if (o.get("coin") or o.get("symbol")) != coin:
            continue
        if not o.get("isTrigger") and "stop" not in str(o.get("orderType") or "").lower():
            continue
        oid = o.get("oid")
        if not oid:
            continue
        try:
            result = exchange.cancel(name=coin, oid=int(oid))
            sys.stderr.write(f"[failsafe] LIVE: cancel SL {coin} oid={oid} result={result}\n")
            cancels.append({"coin": coin, "oid": oid, "result": result, "status": "cancelled"})
        except Exception as e:
            sys.stderr.write(f"[failsafe] ERROR: cancel SL {coin} oid={oid} failed: {e}\n")
            cancels.append({"coin": coin, "oid": oid, "error": str(e), "status": "error"})
    return cancels


def _align_hard_sl(coin: str, hard_sl_px: float, size: float, orders: list) -> Optional[Dict[str, Any]]:
    """Align hard SL trigger order: cancel existing and place new if drift > 0.3%."""
    DRIFT_PCT = 0.3
    current_trigger = _current_stop_trigger(orders, coin)

    if current_trigger is None:
        # No existing SL: place new
        sys.stderr.write(f"[failsafe] {coin}: place hard SL trigger @ {hard_sl_px} (no existing)\n")
        if LIVE_MODE:
            # Cancel any orphan triggers first, then place
            cancels = _cancel_stop_triggers(coin, orders)
            sl_result = _place_stop_trigger(coin, hard_sl_px, size)
            return {"action": "place_sl", "coin": coin, "trigger": hard_sl_px, "current": None, "cancels": cancels, "place": sl_result}
        else:
            return {"action": "place_sl", "coin": coin, "trigger": hard_sl_px, "current": None, "mode": "dry_run"}

    drift = abs(current_trigger - hard_sl_px) / hard_sl_px * 100.0
    if drift >= DRIFT_PCT:
        sys.stderr.write(f"[failsafe] {coin}: update hard SL {current_trigger} → {hard_sl_px} (drift={drift:.2f}%)\n")
        if LIVE_MODE:
            # Cancel old, place new
            cancels = _cancel_stop_triggers(coin, orders)
            sl_result = _place_stop_trigger(coin, hard_sl_px, size)
            return {
                "action": "update_sl",
                "coin": coin,
                "trigger": hard_sl_px,
                "current": current_trigger,
                "drift_pct": drift,
                "cancels": cancels,
                "place": sl_result,
            }
        else:
            return {"action": "update_sl", "coin": coin, "trigger": hard_sl_px, "current": current_trigger, "drift_pct": drift, "mode": "dry_run"}
    return None


def run_failsafe() -> Dict[str, Any]:
    """Main fail-safe logic: check positions, compute signals, place/align orders if live."""
    ts = datetime.now(timezone.utc).isoformat()
    mode = "LIVE" if LIVE_MODE else "DRY_RUN"
    sys.stderr.write(f"[failsafe] {ts} mode={mode}\n")

    errors = []

    # Check radar freshness for 1h/4h
    for tf in ("1h", "4h"):
        ok, err = _check_radar_freshness(tf)
        if not ok:
            errors.append(f"staleness_guard:{err}")
            sys.stderr.write(f"[failsafe] staleness guard FAIL: {err}\n")
            return {
                "ts": ts,
                "mode": mode,
                "errors": errors,
                "actions": [],
                "positions": [],
                "status": "aborted_stale_radar",
            }

    if not HL_ADDRESS or not HL_ADDRESS.startswith("0x"):
        errors.append("missing_HL_ADDRESS")
        return {"ts": ts, "mode": mode, "errors": errors, "actions": [], "positions": [], "status": "aborted_no_address"}

    # Fetch positions
    try:
        state = _hl_post_retry({"type": "clearinghouseState", "user": HL_ADDRESS})
    except Exception as e:
        errors.append(f"fetch_positions:{e}")
        sys.stderr.write(f"[failsafe] fetch positions error: {e}\n")
        if ALERT_WEBHOOK_URL:
            _alert(f"[failsafe] ERROR: {e}")
        return {"ts": ts, "mode": mode, "errors": errors, "actions": [], "positions": [], "status": "error"}

    try:
        orders = _hl_post_retry({"type": "openOrders", "user": HL_ADDRESS})
    except Exception as e:
        errors.append(f"fetch_orders:{e}")
        orders = []

    positions = []
    actions = []

    for a in state.get("assetPositions") or []:
        pos = a.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if abs(szi) < 1e-12:
            continue
        coin = pos.get("coin") or ""
        side = "LONG" if szi > 0 else "SHORT"
        size = abs(szi)
        entry = float(pos.get("entryPx") or 0)

        if side == "SHORT":
            # Shorts: report-only (no exit logic locked)
            positions.append({
                "coin": coin,
                "side": side,
                "size": size,
                "entry": entry,
                "signal": None,
                "hard_sl_px": None,
                "note": "shorts_report_only",
            })
            continue

        # Longs: tier-based exits
        tier = tier_for(coin)
        signal_result = _compute_long_signal(coin, tier, entry, size)

        current_trigger = _current_stop_trigger(orders, coin)
        hard_sl_px = signal_result.get("hard_sl_px")

        pos_info = {
            "coin": coin,
            "side": side,
            "size": size,
            "entry": entry,
            "tier": tier,
            "signal": signal_result.get("exit_signal"),
            "hard_sl_px": hard_sl_px,
            "current_trigger": current_trigger,
            "error": signal_result.get("error"),
        }
        positions.append(pos_info)

        if not signal_result.get("ok"):
            errors.append(f"{coin}:{signal_result.get('error')}")
            continue

        # Exit signal → market close
        if signal_result.get("exit_signal"):
            if LIVE_MODE:
                action = _place_market_close(coin, size, is_long=True)
                actions.append(action)
                _alert(f"[failsafe] EXIT {coin} {signal_result['exit_signal']} size={size}")
            else:
                actions.append({
                    "action": "market_close",
                    "coin": coin,
                    "size": size,
                    "signal": signal_result["exit_signal"],
                    "mode": "dry_run",
                })

        # Align hard SL
        if hard_sl_px:
            sl_action = _align_hard_sl(coin, hard_sl_px, size, orders)
            if sl_action:
                actions.append(sl_action)
                if LIVE_MODE:
                    _alert(f"[failsafe] SL align {coin} → {hard_sl_px}")

    result = {
        "ts": ts,
        "mode": mode,
        "errors": errors,
        "actions": actions,
        "positions": positions,
        "status": "ok" if not errors else "partial_error",
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    sys.stderr.write(f"[failsafe] wrote {OUT}\n")

    return result


def main() -> None:
    result = run_failsafe()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] in ("ok", "partial_error") else 1)


if __name__ == "__main__":
    main()
