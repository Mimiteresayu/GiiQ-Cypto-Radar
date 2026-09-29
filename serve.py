#!/usr/bin/env python3
"""Own Trend Radar UI — local :8787 or Railway (PORT, COCKPIT_PASSWORD).

Local:  python serve.py  → http://127.0.0.1:8787/  (no password unless set)
Railway: password gate + POST /api/sync; background scanner writes out/gc_radar_*.json
         + in-process APScheduler for auto-execution crons (Asia/Hong_Kong timezone)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request as _url_req
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT") or "8787")
HOST = os.environ.get("HOST") or (
    "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY") else "0.0.0.0"
)
SCAN_SCRIPT = os.path.join(ROOT, "scan_gc_radar.py")
FAILSAFE_SCRIPT = os.path.join(ROOT, "failsafe_exit_worker.py")
UI_PATH = os.path.join(ROOT, "ui.html")
OUT_DIR = os.path.join(ROOT, "out")
RESCAN_TIMEOUT_S = 1800
PASSWORD = (os.environ.get("COCKPIT_PASSWORD") or "").strip()
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY"))
COOKIE_NAME = "otr_session"
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or PASSWORD or "dev-local").encode()
ENTRY_READ_KEY = (os.environ.get("ENTRY_READ_KEY") or "").strip()
AI_DECISION_KEY = (os.environ.get("AI_DECISION_KEY") or ENTRY_READ_KEY or "").strip()
# New auto-execution scheduler (APScheduler in-process, Asia/Hong_Kong timezone)
SCHEDULER_ENABLED = (os.environ.get("SCHEDULER_ENABLED") or ("1" if ON_RAILWAY else "0")).strip() in (
    "1",
    "true",
    "yes",
)
# Legacy scheduler (Railway): UTC :05 after bar close. HKT = UTC+8.
# NOTE: OTR_SCHEDULER is now deprecated in favor of SCHEDULER_ENABLED + APScheduler
SCHEDULER_ENABLE = (os.environ.get("OTR_SCHEDULER") or ("1" if ON_RAILWAY else "0")).strip() in (
    "1",
    "true",
    "yes",
)
SCAN_MAX = int(os.environ.get("OTR_SCAN_MAX") or "280")
SCAN_MAX_1H = int(os.environ.get("OTR_SCAN_MAX_1H") or "200")
SCAN_CONCURRENCY = int(os.environ.get("OTR_SCAN_CONCURRENCY") or "2")
# Radar refresh interval for 1h+4h (minutes). Default 15 for 1H exits.
SCAN_INTERVAL_MIN = max(5, int(os.environ.get("OTR_SCAN_INTERVAL_MIN") or "15"))
_scan_lock = threading.Lock()
# Scheduled jobs WAIT for the scan lock (instead of skipping) so e.g. the 08:05 1D+4H scan
# never makes the 08:07 1H exit job skip.  Manual /api/rescan still returns 409 when busy.
SCHED_LOCK_WAIT_S = int(os.environ.get("OTR_SCHED_LOCK_WAIT_S") or "1200")
# 32000: largest line size verified intact in Railway logs (PR #8 check: 3x~32.8KB lines)
DESK_DATA_CHUNK_BYTES = int(os.environ.get("DESK_DATA_CHUNK_BYTES") or "32000")
# LIVE radar loop (APScheduler cron minutes, HKT). :03/:13/... sits after the :07 1H and
# :10 4H closed-bar jobs so it never races them (it also takes the scan lock).
LIVE_RADAR_MINUTES = (os.environ.get("OTR_LIVE_MINUTES") or "3-59/10").strip()
# GIIQ-SoT-3: at 08:50 HKT, if Claude's ENTRY_DESK POST never arrived, Railway stores fallback decisions
# itself (Base only, 2% margin; Chase vetoed) so the system does not depend on any desktop/agent.
# AUTO_FALLBACK=0 disables it (then no Claude POST = no trade, fail-closed).
AUTO_FALLBACK = (os.environ.get("AUTO_FALLBACK") or "1").strip().lower() not in ("0", "false", "no", "off")
LIVE_LOCK_WAIT_S = int(os.environ.get("OTR_LIVE_LOCK_WAIT_S") or "240")
# DESK_DATA volume: full set on every closed-bar job + at least every DESK_FULL_EVERY_S;
# other 10-min live cycles log a compact set (focus rows only).
DESK_FULL_EVERY_S = int(os.environ.get("DESK_FULL_EVERY_S") or "3300")
# Closed-bar job offset after bar close (min): 1H :07, 4H :10, 1D 08:05 HKT (= 00:05 UTC)
CLOSED_SCAN_OFFSET_MIN = {"1h": 7, "4h": 10, "1d": 5}
TF_BAR_MS = {"1h": 3600_000, "4h": 4 * 3600_000, "1d": 86400_000}
HKT = timezone(timedelta(hours=8))
SCHEDULER_STATUS_PATH = os.path.join(OUT_DIR, "scheduler_status.json")
_last_scan: dict[str, str] = {}  # tf -> slot key
_last_failsafe: str = ""  # last failsafe run slot key (hour)

try:
    from entry_candidates import build_candidates
except ImportError:
    build_candidates = None  # type: ignore

from exec_common import (  # noqa: E402
    SOT_ID,
    clamp_leverage,
    hard_sl_for_tier,
    hkt_date,
    isolated_liq_price_long,
    liq_beyond_sl_long,
    parse_ts,
    sig,
)

try:
    import live_radar  # 10-min LIVE radar (display only; 1 HL request per cycle)
except ImportError:
    live_radar = None  # type: ignore

try:  # GIIQ dimensions + measurement ledger (shadow only, never trades)
    import dim_ledger
    import dimensions as giiq_dims
except ImportError:
    dim_ledger = None  # type: ignore
    giiq_dims = None  # type: ignore

try:
    from decisions import store_decisions, get_decisions_for_today
    from trade_log import get_all_trades
except ImportError:
    store_decisions = None  # type: ignore
    get_decisions_for_today = None  # type: ignore
    get_all_trades = None  # type: ignore

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    pytz = None  # type: ignore

# Desk-data endpoint: HL wallet (public read-only)
HL_ADDRESS = (os.environ.get("HL_ADDRESS") or os.environ.get("HL_WALLET") or "0xcFCda0F8576a268BaA17935368081F4e687dB122").strip()
HL_API_URL = "https://api.hyperliquid.xyz/info"

# Cache for HL data (avoid rate limits)
_hl_cache: dict = {}  # {"ts": timestamp, "data": {...}}
_hl_cache_lock = threading.Lock()
HL_CACHE_TTL_S = 45  # 45s cache to stay fresh but not hammer API

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">
<title>Own Trend Radar</title>
<style>body{font-family:system-ui;background:#0b0f14;color:#e6edf3;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#161b22;padding:24px;border-radius:12px;width:min(360px,92vw);border:1px solid #30363d}
input{width:100%;padding:10px;margin:8px 0 16px;border-radius:8px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;box-sizing:border-box}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#238636;color:#fff;font-weight:600}
.err{color:#f85149;font-size:13px;margin:0 0 8px}</style></head>
<body><form method=POST action=/login>
<h2 style=margin:0 0 8px>Own Trend Radar</h2>
<p style=opacity:.7;font-size:13px;margin:0 0 12px>read-only · auto-refresh</p>
__ERR__
<label>Password</label>
<input type=password name=password autofocus required>
<button type=submit>Enter</button>
</form></body></html>
"""


def _token_for(password: str) -> str:
    return hmac.new(SESSION_SECRET, password.encode(), hashlib.sha256).hexdigest()


def _run_failsafe() -> tuple[bool, str]:
    """Run fail-safe exit worker. Returns (ok, note/error)."""
    if not os.path.isfile(FAILSAFE_SCRIPT):
        return False, "failsafe_exit_worker.py missing"
    cmd = [sys.executable, FAILSAFE_SCRIPT]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min timeout
        )
    except subprocess.TimeoutExpired:
        return False, "failsafe timed out (>300s)"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "failsafe failed")[-1000:]
        return False, err
    # Success; extract status from stdout (JSON)
    try:
        result = json.loads(proc.stdout)
        status = result.get("status", "unknown")
        mode = result.get("mode", "?")
        pos_count = len(result.get("positions", []))
        action_count = len(result.get("actions", []))
        return True, f"failsafe {mode} status={status} pos={pos_count} actions={action_count}"
    except Exception:
        return True, "failsafe ran (status unknown)"


def _run_scan(tfs: list[str], max_symbols: int | None = None) -> tuple[bool, str]:
    """Run scan_gc_radar.py for given TFs. Returns (ok, note/error)."""
    if not os.path.isfile(SCAN_SCRIPT):
        return False, "scan_gc_radar.py missing"
    if not tfs:
        return False, "no tfs"
    mx = max_symbols if max_symbols is not None else SCAN_MAX
    cmd = [
        sys.executable,
        SCAN_SCRIPT,
        "--tf",
        ",".join(tfs),
        "--max",
        str(mx),
        "--concurrency",
        str(max(1, min(8, SCAN_CONCURRENCY))),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=RESCAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"scan timed out (>{RESCAN_TIMEOUT_S}s)"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "scan failed")[-2000:]
        return False, err
    return True, f"scanned {','.join(tfs)}"


def _slot_key(now: datetime, kind: str) -> str:
    """Dedup key so each schedule window runs once."""
    if kind == "1d":
        return now.strftime("%Y-%m-%d") + ":1d"
    # 1h / 4h share the same 15-minute radar slot (floor minute)
    slot_min = (now.minute // SCAN_INTERVAL_MIN) * SCAN_INTERVAL_MIN
    return now.strftime("%Y-%m-%dT%H") + f":{slot_min:02d}:{kind}"


def _due_tfs(now: datetime) -> list[tuple[str, int]]:
    """Return (tf, max_symbols) due now.

    - 1h + 4h every SCAN_INTERVAL_MIN minutes (default 15 → :00/:15/:30/:45)
    - 1d daily at 00:05 UTC only (daily bars; not on 15m tick)
    """
    due: list[tuple[str, int]] = []
    # Intraday radar (exits): every N minutes on the clock
    if now.minute % SCAN_INTERVAL_MIN == 0:
        if _last_scan.get("1h") != _slot_key(now, "1h"):
            due.append(("1h", SCAN_MAX_1H))
        if _last_scan.get("4h") != _slot_key(now, "4h"):
            due.append(("4h", SCAN_MAX))
    # 1D daily at 00:05 UTC (= 08:05 HKT)
    if now.hour == 0 and now.minute == 5 and _last_scan.get("1d") != _slot_key(now, "1d"):
        due.append(("1d", SCAN_MAX))
    return due


def _generate_entry_candidates() -> int | None:
    """Generate entry_candidates_latest.json and dated snapshot (non-fatal). Returns count."""
    if not build_candidates:
        return None
    try:
        radar_1d_path = os.path.join(OUT_DIR, "gc_radar_1d.json")
        radar_4h_path = os.path.join(OUT_DIR, "gc_radar_4h.json")
        with open(radar_1d_path) as f:
            radar_1d = json.load(f)
        with open(radar_4h_path) as f:
            radar_4h = json.load(f)
        result = build_candidates(radar_1d, radar_4h)
        # Write latest
        latest_path = os.path.join(OUT_DIR, "entry_candidates_latest.json")
        with open(latest_path, "w") as f:
            json.dump(result, f, indent=2)
        # Write dated (HKT = UTC+8)
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        hkt = now + timedelta(hours=8)
        dated_name = f"entry_candidates_{hkt.strftime('%Y%m%d')}.json"
        dated_path = os.path.join(OUT_DIR, dated_name)
        with open(dated_path, "w") as f:
            json.dump(result, f, indent=2)
        sys.stderr.write(f"[entry_candidates] generated count={result['count']} → {dated_name}\n")
        return result["count"]
    except Exception as e:
        sys.stderr.write(f"[entry_candidates] error (non-fatal): {e}\n")
        return None


def _fetch_hl_live() -> dict:
    """Fetch fresh HL clearinghouse + spot state for HL_ADDRESS (no cache).
    
    Returns: {
        "hl_perp": clearinghouseState (or {"error": "..."}),
        "hl_spot": spotClearinghouseState (or {"error": "..."}),
        "ts": ISO timestamp,
        "address": HL_ADDRESS,
        "fetch_error": bool (True if any fetch failed, PR #6 fix)
    }
    """
    result: dict = {
        "address": HL_ADDRESS,
        "ts": datetime.now(timezone.utc).isoformat(),
        "fetch_error": False,
    }
    
    for req_type, key in [
        ("clearinghouseState", "hl_perp"),
        ("spotClearinghouseState", "hl_spot"),
    ]:
        try:
            payload = json.dumps({"type": req_type, "user": HL_ADDRESS}).encode()
            req = _url_req.Request(
                HL_API_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _url_req.urlopen(req, timeout=15) as resp:
                result[key] = json.loads(resp.read())
        except Exception as e:
            result[key] = {"error": str(e)}
            result["fetch_error"] = True
            sys.stderr.write(f"[HL_FETCH] {req_type} failed: {e}\n")

    # Open orders incl. trigger/reduce-only stops (Hard SL verification). Non-fatal.
    try:
        req = _url_req.Request(
            HL_API_URL,
            data=json.dumps({"type": "frontendOpenOrders", "user": HL_ADDRESS}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _url_req.urlopen(req, timeout=15) as resp:
            result["hl_open_orders"] = json.loads(resp.read())
    except Exception as e:
        result["hl_open_orders"] = {"error": str(e)}
        sys.stderr.write(f"[HL_FETCH] frontendOpenOrders failed: {e}\n")

    return result


def _get_hl_cached() -> dict:
    """Get cached HL data or fetch fresh if cache expired.
    
    Returns cached data if valid and fresh, or fetches new data.
    Does NOT cache data with fetch errors (PR #6 fix).
    """
    global _hl_cache
    now = time.time()
    
    with _hl_cache_lock:
        cached = _hl_cache.get("data")
        ts = _hl_cache.get("ts", 0)
        
        # Return cached if valid and fresh
        if cached and (now - ts) < HL_CACHE_TTL_S and not cached.get("fetch_error"):
            return cached
        
        # Fetch fresh
        fresh = _fetch_hl_live()
        
        # Only cache if fetch succeeded (no errors)
        if not fresh.get("fetch_error"):
            _hl_cache = {"ts": now, "data": fresh}
        else:
            # Don't cache errors, but return the fresh attempt
            sys.stderr.write("[HL_CACHE] Not caching data with fetch errors\n")
        
        return fresh


def _compute_unified_equity(hl_data: dict) -> dict:
    """Compute Unified mode equity from HL API response.
    
    Unified mode: equity = spot USDC total; perp accountValue ≈ 0 even when funded.
    Free USDC = spot USDC - margin used.
    
    Returns: {
        "equity": float,
        "spot_usdc": float,
        "margin_used": float,
        "spot_usdc_free": float,
        "uPnL_sum": float,
        "ts": ISO string,
        "error": str (if HL fetch failed, PR #6 fix)
    }
    """
    # Check for fetch errors (PR #6 fix)
    if hl_data.get("fetch_error"):
        hl_spot = hl_data.get("hl_spot", {})
        hl_perp = hl_data.get("hl_perp", {})
        errors = []
        if "error" in hl_spot:
            errors.append(f"spot: {hl_spot['error']}")
        if "error" in hl_perp:
            errors.append(f"perp: {hl_perp['error']}")
        return {
            "equity": 0.0,
            "spot_usdc": 0.0,
            "margin_used": 0.0,
            "spot_usdc_free": 0.0,
            "uPnL_sum": 0.0,
            "ts": hl_data.get("ts", ""),
            "error": "HL fetch failed: " + "; ".join(errors),
        }
    
    hl_spot = hl_data.get("hl_spot", {})
    hl_perp = hl_data.get("hl_perp", {})
    
    # Spot USDC balance (source of truth in Unified)
    balances = hl_spot.get("balances", [])
    spot_usdc = 0.0
    for bal in balances:
        if bal.get("coin") == "USDC":
            try:
                spot_usdc = float(bal.get("total", 0))
            except (TypeError, ValueError):
                pass
            break
    
    # Margin used from perp state
    margin_summary = hl_perp.get("marginSummary", {})
    try:
        margin_used = float(margin_summary.get("totalMarginUsed", 0))
    except (TypeError, ValueError):
        margin_used = 0.0
    
    # uPnL from perp positions
    upnl_sum = 0.0
    positions = hl_perp.get("assetPositions", [])
    for pos_group in positions:
        position = pos_group.get("position", {})
        if not position:
            continue
        try:
            upnl = float(position.get("unrealizedPnl", 0))
            upnl_sum += upnl
        except (TypeError, ValueError):
            pass
    
    # Equity = spot USDC (in Unified mode)
    equity = spot_usdc
    
    # Free USDC = spot USDC - margin used
    free_usdc = max(0, spot_usdc - margin_used)
    
    return {
        "equity": equity,
        "spot_usdc": spot_usdc,
        "margin_used": margin_used,
        "spot_usdc_free": free_usdc,
        "uPnL_sum": upnl_sum,
        "ts": hl_data.get("ts", ""),
    }


def _compute_positions_with_stops(hl_data: dict, radar_1h: dict, radar_4h: dict) -> list:
    """Compute position rows with tier-based stops from radar.
    
    Returns list of dicts with:
        coin, side, size, entry, positionValue, uPnL, leverage, liquidation_px,
        tier, primary_exit, hard_sl, sl_dist_pct, exit_signal, liq_beyond_sl
    """
    try:
        from mcap_tiers import tier_for
    except ImportError:
        tier_for = None  # type: ignore
    
    hl_perp = hl_data.get("hl_perp", {})
    positions_raw = hl_perp.get("assetPositions", [])
    
    # Build radar maps
    r1h_map = {}
    r4h_map = {}
    if radar_1h and radar_1h.get("rows"):
        for r in radar_1h["rows"]:
            r1h_map[r["symbol"]] = r
    if radar_4h and radar_4h.get("rows"):
        for r in radar_4h["rows"]:
            r4h_map[r["symbol"]] = r
    
    result = []
    
    for pos_group in positions_raw:
        position = pos_group.get("position", {})
        if not position:
            continue
        
        coin = position.get("coin", "")
        if not coin:
            continue
        
        # Extract position data
        try:
            szi = float(position.get("szi", 0))
            entry_px = float(position.get("entryPx", 0))
            position_value = float(position.get("positionValue", 0))
            unrealized_pnl = float(position.get("unrealizedPnl", 0))
            leverage_val = position.get("leverage", {})
            leverage = float(leverage_val.get("value", 0)) if isinstance(leverage_val, dict) else 0.0
            liquidation_px = float(position.get("liquidationPx") or 0)
        except (TypeError, ValueError):
            continue
        
        if abs(szi) < 1e-8:  # Skip zero positions
            continue
        
        side = "LONG" if szi > 0 else "SHORT"
        size = abs(szi)
        
        # Tier
        tier = "tiny"
        if tier_for:
            tier = tier_for(coin)
        
        # Get radar rows
        r1h = r1h_map.get(coin) or r1h_map.get(f"{coin}-PERP")
        r4h = r4h_map.get(coin) or r4h_map.get(f"{coin}-PERP")
        
        # Tier-based stops (Mega/Large: 4H Filter primary, 4H Lower hard SL)
        # (Small/Tiny: 1H Lower primary, 4H Filter hard SL)
        # NOTE: Current spec says ALL tiers use 4H Filter as Hard SL (unified 2026-09-21)
        # but primary exit differs by tier
        primary_exit_level = None
        primary_exit_label = ""
        hard_sl_level = None
        hard_sl_label = "4H Filter"
        
        if tier in ("mega", "large"):
            # Primary exit: 4H close < 4H Filter
            if r4h:
                primary_exit_level = r4h.get("filter")
                primary_exit_label = "4H Filter"
                hard_sl_level = r4h.get("lower")  # 4H Lower is hard SL for Mega/Large
                hard_sl_label = "4H Lower"
        else:  # small, tiny
            # Primary exit: 1H close < 1H Lower
            if r1h:
                primary_exit_level = r1h.get("lower")
                primary_exit_label = "1H Lower"
            # Hard SL: 4H Filter (mid)
            if r4h:
                hard_sl_level = r4h.get("filter")
                hard_sl_label = "4H Filter"
        
        # SL distance %
        sl_dist_pct = None
        if hard_sl_level and entry_px:
            sl_dist_pct = round((hard_sl_level - entry_px) / entry_px * 100, 2)
        
        # Exit signal logic
        exit_signal = "HOLD"
        if tier in ("mega", "large") and r4h:
            # Check if 4H close < 4H filter
            close_4h = r4h.get("close")
            filt_4h = r4h.get("filter")
            trend_4h = r4h.get("trend")
            if close_4h and filt_4h and close_4h < filt_4h:
                exit_signal = "EXIT 4H"
            elif trend_4h == "Red":
                exit_signal = "WATCH"
        elif tier in ("small", "tiny") and r1h:
            # Check if 1H close < 1H lower
            close_1h = r1h.get("close")
            lower_1h = r1h.get("lower")
            trend_1h = r1h.get("trend")
            if close_1h and lower_1h and close_1h < lower_1h:
                exit_signal = "EXIT 1H"
            elif trend_1h == "Red":
                exit_signal = "WATCH"
        else:
            # Fallback: check both TFs for red trend
            if (r4h and r4h.get("trend") == "Red") or (r1h and r1h.get("trend") == "Red"):
                exit_signal = "WATCH"
        
        # Check if liquidation price is beyond hard SL (safe if true)
        liq_beyond_sl = None
        if liquidation_px and hard_sl_level:
            if side == "LONG":
                liq_beyond_sl = liquidation_px < hard_sl_level  # liq lower than SL = safe
            else:
                liq_beyond_sl = liquidation_px > hard_sl_level  # liq higher than SL = safe
        
        result.append({
            "coin": coin,
            "side": side,
            "size": size,
            "entry": entry_px,
            "positionValue": position_value,
            "uPnL": unrealized_pnl,
            "leverage": leverage,
            "liquidation_px": liquidation_px,
            "tier": tier,
            "primary_exit": primary_exit_level,
            "primary_exit_label": primary_exit_label,
            "hard_sl": hard_sl_level,
            "hard_sl_label": hard_sl_label,
            "sl_dist_pct": sl_dist_pct,
            "exit_signal": exit_signal,
            "liq_beyond_sl": liq_beyond_sl,
            # Include radar trends for UI
            "trend_1h": r1h.get("trend") if r1h else None,
            "trend_4h": r4h.get("trend") if r4h else None,
        })
    
    return result


# =====================================================================
# [DESK_DATA] single-line JSON for log readers (Claude jobs read Railway logs via MCP)
# =====================================================================

_hl_meta_cache: dict = {}
_hl_meta_lock = threading.Lock()
HL_META_TTL_S = 3600


def _get_hl_meta_cached() -> dict:
    """coin -> {szDecimals, maxLeverage} from HL meta (1h cache; {} on failure)."""
    now = time.time()
    with _hl_meta_lock:
        if _hl_meta_cache.get("data") and now - _hl_meta_cache.get("ts", 0) < HL_META_TTL_S:
            return _hl_meta_cache["data"]
        try:
            req = _url_req.Request(
                HL_API_URL,
                data=json.dumps({"type": "meta"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _url_req.urlopen(req, timeout=15) as resp:
                m = json.loads(resp.read())
            data = {
                a["name"]: {"szDecimals": a.get("szDecimals", 0), "maxLeverage": a.get("maxLeverage")}
                for a in (m.get("universe") or [])
            }
            _hl_meta_cache.update(ts=now, data=data)
            return data
        except Exception as e:
            sys.stderr.write(f"[HL_META] fetch failed: {e}\n")
            return _hl_meta_cache.get("data") or {}


def _suggested_size_pct(entry_type: str) -> float:
    if entry_type == "Continuation":
        return 3.0
    if entry_type == "Chase":
        return 8.0
    return 6.0


def _enhance_candidates(candidates: list, account: dict, meta: dict) -> list:
    """Add suggested size/leverage, tier Hard SL and ISOLATED liq estimate to each candidate.

    size_pct = margin % of equity; notional = margin x leverage.
    Liq = HL isolated-long formula (exec_common.isolated_liq_price_long) -> never negative.
    """
    equity = 0.0
    try:
        equity = float(account.get("equity") or 0)
    except (TypeError, ValueError, AttributeError):
        equity = 0.0
    out = []
    from exec_common import size_by_margin
    for c in candidates:
        coin_max = (meta.get(c.get("symbol")) or {}).get("maxLeverage")
        hard_sl, sl_label = hard_sl_for_tier(c.get("tier", ""), {"lower": c.get("lower_4h"), "filter": c.get("filter_4h")})
        close_1d = c.get("close_1d") or 0
        # GIIQ-SoT-2 suggestion (the executor re-sizes at order time with live mid + open risk)
        sz = size_by_margin(equity, close_1d, hard_sl, coin_max) if (equity and close_1d and hard_sl) else {"ok": False}
        if sz.get("ok"):
            size_pct, lev = round(sz["margin_pct"], 2), int(sz["leverage"])
        else:
            size_pct, lev = 2.0, clamp_leverage(3.0, coin_max)
        liq = isolated_liq_price_long(close_1d, lev, coin_max) if close_1d else None
        margin = equity * size_pct / 100.0 if equity else None
        out.append({
            **c,
            "suggested_size_pct": size_pct,
            "suggested_leverage": lev,
            "coin_max_leverage": coin_max,
            "margin_mode": "isolated",
            "suggested_margin_usd": round(margin, 2) if margin else None,
            "suggested_notional_usd": round(margin * lev, 2) if margin else None,
            "hard_sl": hard_sl,
            "hard_sl_label": sl_label,
            "estimated_liq_price": liq,
            "liq_beyond_sl": liq_beyond_sl_long(liq, hard_sl) if liq is not None else None,
            "sot2_sizing": ({"ok": True, "risk_margin_pct": sz.get("margin_pct")} if sz.get("ok")
                            else {"ok": False, "reason": sz.get("reason") or "no NAV / price / Hard SL"}),
        })
    return out


def _trim_hl_perp(perp: dict) -> dict:
    if not isinstance(perp, dict) or "error" in perp:
        return {"error": (perp or {}).get("error", "missing") if isinstance(perp, dict) else "missing"}
    ms = perp.get("marginSummary") or {}
    positions = []
    for g in perp.get("assetPositions") or []:
        p = g.get("position") or {}
        try:
            if abs(float(p.get("szi") or 0)) < 1e-12:
                continue
        except (TypeError, ValueError):
            continue
        lev = p.get("leverage") or {}
        positions.append({
            "coin": p.get("coin"),
            "szi": sig(float(p.get("szi") or 0)),
            "entryPx": sig(float(p.get("entryPx") or 0)),
            "positionValue": sig(float(p.get("positionValue") or 0)),
            "unrealizedPnl": sig(float(p.get("unrealizedPnl") or 0)),
            "liquidationPx": sig(float(p.get("liquidationPx") or 0)) if p.get("liquidationPx") else None,
            "marginUsed": sig(float(p.get("marginUsed") or 0)),
            "leverage": {"type": lev.get("type"), "value": lev.get("value")},
        })
    return {
        "marginSummary": {k: sig(float(ms.get(k) or 0)) for k in ("accountValue", "totalMarginUsed", "totalNtlPos")},
        "withdrawable": sig(float(perp.get("withdrawable") or 0)),
        "positions": positions,
    }


def _trim_hl_spot(spot: dict) -> dict:
    if not isinstance(spot, dict) or "error" in spot:
        return {"error": (spot or {}).get("error", "missing") if isinstance(spot, dict) else "missing"}
    for b in spot.get("balances") or []:
        if b.get("coin") == "USDC":
            return {"usdc_total": sig(float(b.get("total") or 0)), "usdc_hold": sig(float(b.get("hold") or 0))}
    return {"usdc_total": 0.0, "usdc_hold": 0.0}


_DESK_ROW_KEYS = ("symbol", "trend", "close", "filter", "upper", "lower")
_DESK_LIVE_KEYS = ("close", "filter", "upper", "lower", "trend", "above_upper", "cross_up")


def _hkt_iso(ms) -> str | None:
    """epoch ms (UTC) -> ISO string in HKT (+08:00)."""
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=HKT).isoformat(timespec="seconds")


def _next_live_update(now: datetime) -> str:
    """Next LIVE radar cron slot (default :03/:13/…/:53 HKT)."""
    try:
        start, step = LIVE_RADAR_MINUTES.split("/")[0].split("-")[0], LIVE_RADAR_MINUTES.split("/")[1]
        mins = list(range(int(start), 60, int(step)))
    except Exception:
        mins = list(range(3, 60, 10))
    t = now.astimezone(HKT).replace(second=0, microsecond=0)
    for _ in range(0, 120):
        t += timedelta(minutes=1)
        if t.minute in mins:
            return t.isoformat(timespec="seconds")
    return t.isoformat(timespec="seconds")


def _tf_timing(tf: str, radar: dict, now: datetime) -> dict:
    """Per-TF timestamps (HKT ISO): closed-bar scan + bar, live scan, next close / updates."""
    bar_ms = TF_BAR_MS[tf]
    now_ms = int(now.timestamp() * 1000)
    closed_open = radar.get("closed_bar_open_ms") if isinstance(radar, dict) else None
    if closed_open is None and isinstance(radar, dict):
        bts = [r.get("bar_time") for r in radar.get("rows") or [] if isinstance(r.get("bar_time"), int)]
        closed_open = max(bts) if bts else None
    forming = now_ms - now_ms % bar_ms
    nxt = forming + bar_ms
    ts_dt = parse_ts((radar or {}).get("ts")) if isinstance(radar, dict) else None
    live_dt = parse_ts((radar or {}).get("live_ts")) if isinstance(radar, dict) else None
    return {
        "closed_scan_ts": ts_dt.astimezone(HKT).isoformat(timespec="seconds") if ts_dt else None,
        "last_closed_bar_open": _hkt_iso(closed_open),
        "last_closed_bar_close": _hkt_iso(closed_open + bar_ms) if closed_open else None,
        "live_ts": live_dt.astimezone(HKT).isoformat(timespec="seconds") if live_dt else None,
        "forming_bar_open": _hkt_iso(forming),
        "next_close": _hkt_iso(nxt),
        "next_closed_scan": _hkt_iso(nxt + CLOSED_SCAN_OFFSET_MIN[tf] * 60_000),
        "next_live_update": _next_live_update(now),
    }


_CAT_ORDER = ("N", "C", "V")
_CAT_LEGACY = {"NARRATIVE": "N", "Narrative": "N", "CEMETERY": "C", "Cemetery": "C"}


def _cat_label(r: dict) -> str:
    """CAT short codes, space-separated, fixed order N C V (blank if none). Legacy rows mapped."""
    raw = r.get("cat_tags")
    if raw is None:
        raw = r.get("categories") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    got = {_CAT_LEGACY.get(t, t) for t in raw}
    return " ".join(c for c in _CAT_ORDER if c in got)


def _trim_radar(radar: dict, focus: set | None = None) -> dict:
    """Radar for DESK_DATA. Row keys w/o prefix = LAST CLOSED bar (signal SoT);
    live_* = forming bar (display only). focus: keep only these symbols (compact log)."""
    if not isinstance(radar, dict) or "rows" not in radar:
        return {"error": (radar or {}).get("error", "missing") if isinstance(radar, dict) else "missing"}
    rows = []
    for r in radar.get("rows") or []:
        if focus is not None and r.get("symbol") not in focus:
            continue
        row = {k: sig(r.get(k)) for k in _DESK_ROW_KEYS}
        row["dual_cross_up"] = bool(r.get("dual_cross_up"))
        row["dual_cross_down"] = bool(r.get("dual_cross_down_filter", r.get("dual_cross_down")))
        row["tier"] = r.get("tier")
        row["cat"] = _cat_label(r)
        mc = r.get("mcap_usd", r.get("mcap"))
        row["mcap"] = int(mc) if isinstance(mc, (int, float)) else None
        lv = r.get("live") if isinstance(r.get("live"), dict) else {}
        for k in _DESK_LIVE_KEYS:
            v = lv.get(k)
            row[f"live_{k}"] = sig(v) if isinstance(v, float) else v
        rows.append(row)
    out = {
        "ts": radar.get("ts"),
        "closed_bar_open": _hkt_iso(radar.get("closed_bar_open_ms")),
        "live_ts": radar.get("live_ts"),
        "n": len(rows),
        "breadth": radar.get("breadth"),
        "live_breadth": radar.get("live_breadth"),
        "flags": {"dual_cross_up": (radar.get("flags") or {}).get("dual_cross_up", [])},
        "live_flags": {"cross_up": (radar.get("live_flags") or {}).get("cross_up", [])},
        "rows": rows,
    }
    if focus is not None:
        out["focus_only"] = True
        out["n_total"] = len(radar.get("rows") or [])
    return out


def _trim_open_orders(orders) -> list | dict:
    """HL frontendOpenOrders -> compact list (trigger/reduce-only stops kept for Hard SL checks)."""
    if isinstance(orders, dict):
        return {"error": orders.get("error", "missing")}
    out = []
    for o in orders or []:
        out.append({
            "coin": o.get("coin"),
            "side": o.get("side"),
            "sz": o.get("sz"),
            "limitPx": o.get("limitPx"),
            "orderType": o.get("orderType"),
            "isTrigger": o.get("isTrigger"),
            "triggerPx": o.get("triggerPx"),
            "triggerCondition": o.get("triggerCondition"),
            "reduceOnly": o.get("reduceOnly"),
            "isPositionTpsl": o.get("isPositionTpsl"),
            "tif": o.get("tif"),
            "oid": o.get("oid"),
            "timestamp": o.get("timestamp"),
        })
    return out


def _load_candidates_file() -> dict:
    try:
        with open(os.path.join(OUT_DIR, "entry_candidates_latest.json")) as f:
            return json.load(f)
    except Exception:
        return {}


_ENTRY_TAB_KEYS = ("symbol", "tier", "category", "category_label", "cat_tags", "trend_1d", "trend_4h", "close_1d", "upper_1d",
                   "filter_1d", "close_4h", "upper_4h", "filter_4h", "lower_4h", "entry_ref", "hard_sl_dist_pct")


def _entry_tab(cd: dict) -> dict:
    """ENTRY tab lists (same source as /api/ai/candidates: entry_candidates_latest.json)."""
    base, chase = [], []
    for c in cd.get("candidates") or []:
        row = {k: (sig(c.get(k)) if isinstance(c.get(k), float) else c.get(k)) for k in _ENTRY_TAB_KEYS}
        is_chase = c.get("is_chase", c.get("type") == "Chase")
        is_base = c.get("is_base", c.get("type") == "Base")
        if is_base:
            base.append(row)
        if is_chase:
            chase.append(row)
    # AI decision per candidate (today's HKT decisions file): APPROVED / VETO + reason
    try:
        decs = get_decisions_for_today() if get_decisions_for_today else {}
    except Exception:
        decs = {}
    for row in base + chase:
        d = (decs or {}).get(row.get("symbol")) or {}
        row["ai_decision"] = ({"decision": str(d.get("decision") or "").upper(), "reason": d.get("reason") or "",
                               "size_pct": d.get("size_pct"), "leverage": d.get("leverage"),
                               "timestamp": d.get("timestamp")} if d else None)
    return {"generated_at": cd.get("generated_at"), "base": base, "chase": chase}


def _exec_mode() -> dict:
    """Real executor mode from the service env (same rule as exec_common.is_live_mode)."""
    try:
        from exec_common import is_live_mode
        live = is_live_mode()
    except Exception:
        live = False
    return {"mode": "LIVE" if live else "DRY_RUN", "sot": SOT_ID,
            "exec_dry_run": (os.environ.get("EXEC_DRY_RUN") or "1").strip(),
            "key_present": bool((os.environ.get("HL_API_PRIVATE_KEY") or "").strip())}


def _load_narrative(radar_1d: dict | None = None) -> dict:
    """NARRATIVE tab watchlist (out/narrative_watchlist.json) + Bitunix 1D GC / HL 1D radar row."""
    try:
        with open(os.path.join(OUT_DIR, "narrative_watchlist.json")) as f:
            wl = json.load(f)
    except Exception as e:
        return {"error": str(e), "items": []}
    gc_by: dict = {}
    try:
        with open(os.path.join(OUT_DIR, "bitunix_gc_1d.json")) as f:
            for g in (json.load(f) or {}).get("items") or []:
                if g.get("ticker"):
                    gc_by[str(g["ticker"]).upper()] = g
    except Exception:
        pass
    hl_by = {r.get("symbol"): r for r in (radar_1d or {}).get("rows") or []} if isinstance(radar_1d, dict) else {}
    nmap = {str(k).upper(): v for k, v in ((radar_1d or {}).get("narrative_map") or {}).items()} if isinstance(radar_1d, dict) else {}
    items = []
    for it in wl.get("items") or []:
        t = str(it.get("ticker") or "").upper()
        g = gc_by.get(t) or {}
        hl_sym = nmap.get(t) or (t if t in hl_by else None)
        h = hl_by.get(hl_sym) or {}
        items.append({
            "ticker": t,
            "sector": it.get("sector"),
            "venue": "HL" if h else (it.get("venue") or "unknown"),  # HL or spot venue; non-HL kept
            "gc_scan": it.get("gc_scan"),
            "narrative": (it.get("narrative") or "")[:80],
            "last_seen": it.get("last_seen"),
            "on_hl": bool(h),
            "hl_symbol": hl_sym if h else None,
            "trend_1d": h.get("trend") or g.get("trend"),
            "close_1d": sig(h.get("close") if h else g.get("close")),
            "upper_1d": sig(h.get("upper") if h else g.get("upper")),
            "dual_cross_up_1d": bool(h.get("dual_cross_up") if h else g.get("dual_cross_up")),
        })
    return {"updated": wl.get("updated"), "n": len(items), "items": items}


NARRATIVE_MAX_ITEMS = 300
_narrative_lock = threading.Lock()


def _read_narrative_watchlist() -> dict:
    try:
        with open(os.path.join(OUT_DIR, "narrative_watchlist.json")) as f:
            wl = json.load(f)
        return wl if isinstance(wl, dict) else {"items": wl if isinstance(wl, list) else []}
    except Exception:
        return {"items": []}


def _clean_str(v, n: int) -> str:
    return str(v or "").strip()[:n]


def update_narrative_watchlist(body: dict, now: datetime | None = None) -> dict:
    """Merge/replace out/narrative_watchlist.json (atomic). Raises ValueError on bad input.
    merge: upsert items by ticker (keeps first_seen/notes/other fields), then drop `remove`.
    replace: list becomes exactly `items`. Marks managed_by="api" so the scanner uses only this file."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    mode = str(body.get("mode") or "merge").lower()
    if mode not in ("merge", "replace"):
        raise ValueError("mode must be merge|replace")
    items = body.get("items") or []
    remove = body.get("remove") or []
    if not isinstance(items, list) or not isinstance(remove, list):
        raise ValueError("items/remove must be lists")
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(HKT).strftime("%Y-%m-%d")
    clean = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("each item must be an object")
        t = _clean_str(it.get("ticker") or it.get("symbol"), 20).lstrip("$")  # keep case (HL kPEPE)
        if not t:
            raise ValueError("item missing ticker")
        row = {"ticker": t}
        for k, n in (("sector", 40), ("venue", 40), ("narrative", 300), ("asset_type", 20), ("notes", 300)):
            if it.get(k) is not None:
                row[k] = _clean_str(it.get(k), n)
        clean.append(row)
    with _narrative_lock:
        wl = _read_narrative_watchlist()
        old = {str(i.get("ticker") or "").upper(): i for i in wl.get("items") or [] if isinstance(i, dict)}
        merged = {} if mode == "replace" else dict(old)
        for row in clean:
            prev = old.get(row["ticker"].upper(), {})
            merged[row["ticker"].upper()] = {**prev, **row, "first_seen": prev.get("first_seen") or today, "last_seen": today}
        for t in remove:
            merged.pop(_clean_str(t, 20).upper().lstrip("$"), None)
        if len(merged) > NARRATIVE_MAX_ITEMS:
            raise ValueError(f"max {NARRATIVE_MAX_ITEMS} items")
        wl["items"] = list(merged.values())
        wl["updated"] = now.isoformat()
        wl["managed_by"] = "api"
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "narrative_watchlist.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(wl, f, indent=2)
        os.replace(tmp, path)
    return {"mode": mode, "count": len(wl["items"]), "updated": wl["updated"],
            "tickers": [i["ticker"] for i in wl["items"]]}


def _build_desk_data_payload(event: str = "", now: datetime | None = None, kind: str = "full") -> dict:
    """Desk snapshot for the [DESK_DATA] log line(s).

    kind="full": all radar rows (closed + live_*) + narrative.
    kind="live": compact — radar rows only for focus symbols (candidates, positions, open
    orders, BTC/ETH, narrative tickers on HL); everything else identical.
    """
    now = now or datetime.now(timezone.utc)
    radars = {}
    for tf in ("1d", "4h", "1h"):
        try:
            with open(os.path.join(OUT_DIR, f"gc_radar_{tf}.json")) as f:
                radars[tf] = json.load(f)
        except Exception as e:
            radars[tf] = {"error": str(e)}
    try:
        hl_data = _get_hl_cached()
    except Exception as e:
        hl_data = {"hl_perp": {"error": str(e)}, "hl_spot": {"error": str(e)}}
    account = _compute_unified_equity(hl_data) if not hl_data.get("fetch_error") else {}

    cands: list = []
    cand_meta: dict = {"generated_at": None, "stale": None, "today": False}
    cd = _load_candidates_file()
    try:
        if cd:
            gen = parse_ts(cd.get("generated_at"))
            cand_meta = {"generated_at": cd.get("generated_at"), "stale": cd.get("stale"),
                         "today": bool(gen and hkt_date(gen) == hkt_date(now)),
                         "radar_1d_asof": cd.get("radar_1d_asof"), "radar_4h_asof": cd.get("radar_4h_asof"),
                         "count": cd.get("count")}
            if cand_meta["today"]:
                for c in _enhance_candidates(cd.get("candidates") or [], account, _get_hl_meta_cached()):
                    cands.append({
                        "symbol": c.get("symbol"), "type": c.get("type"), "tier": c.get("tier"),
                        "is_base": c.get("is_base"), "is_chase": c.get("is_chase"),
                        "trend_1d": c.get("trend_1d"), "trend_4h": c.get("trend_4h"),
                        "close": sig(c.get("close_1d")), "entry_ref": sig(c.get("entry_ref")),
                        "hard_sl": sig(c.get("hard_sl")),
                        "hard_sl_label": c.get("hard_sl_label"), "sl_pct": sig(c.get("hard_sl_dist_pct"), 4),
                        "size_pct": c.get("suggested_size_pct"), "lev": c.get("suggested_leverage"),
                        "max_lev": c.get("coin_max_leverage"), "liq": sig(c.get("estimated_liq_price")),
                        "liq_beyond_sl": c.get("liq_beyond_sl"), "held": c.get("already_held"),
                        "sot2": c.get("sot2_sizing"),
                    })
    except Exception as e:
        cand_meta["error"] = str(e)

    perp = _trim_hl_perp(hl_data.get("hl_perp", {}))
    orders = _trim_open_orders(hl_data.get("hl_open_orders", {"error": "not fetched"}))
    narrative = _load_narrative(radars.get("1d"))
    focus = None
    if kind != "full":
        focus = {"BTC", "ETH"} | {c.get("symbol") for c in cd.get("candidates") or []}
        focus |= {p.get("coin") for p in perp.get("positions") or []} if isinstance(perp, dict) else set()
        focus |= {o.get("coin") for o in orders} if isinstance(orders, list) else set()
        focus |= {i["ticker"] for i in narrative.get("items") or [] if i.get("on_hl")}

    payload = {
        "ts": now.isoformat(),
        "event": event,
        "kind": kind,
        "timing": {tf: _tf_timing(tf, radars[tf], now) for tf in ("1d", "4h", "1h")},
        "candidates_meta": cand_meta,
        "candidates": cands,
        "entry_tab": _entry_tab(cd) if cand_meta.get("today") else {"generated_at": cd.get("generated_at"), "base": [], "chase": []},
        "hl_spot": _trim_hl_spot(hl_data.get("hl_spot", {})),
        "hl_perp": perp,
        "hl_open_orders": orders,
        "sot": SOT_ID,
        "exec_mode": _exec_mode(),
        "run_report": _today_run_report(now),
        "pending_entries": _pending_view(),
        "dimensions": _dims_compact(now),
        "exit_health": _exit_health(now, hl_data),
    }
    if kind == "full":
        payload["narrative"] = narrative
    else:
        payload["narrative"] = {"updated": narrative.get("updated"), "n": narrative.get("n"),
                                "dual_cross_up_1d": [i["ticker"] for i in narrative.get("items") or [] if i.get("dual_cross_up_1d")]}
    for tf in ("1d", "4h", "1h"):
        payload[f"gc_radar_{tf}"] = _trim_radar(radars[tf], focus)
    return payload


def _exit_health(now: datetime | None = None, hl_data: dict | None = None) -> dict:
    """EXIT_DESK health (report only): NO_SL / EXIT_NOT_DONE / JOB_FAILED / MARGIN_HIGH / ... (exit_health.py)."""
    try:
        import exit_health
        from mcap_tiers import tier_for as _tier_for
        hl_data = hl_data if hl_data is not None else _get_hl_cached()
        account = _compute_unified_equity(hl_data) if not hl_data.get("fetch_error") else {}
        radars = {tf: _read_out_json(f"gc_radar_{tf}.json") for tf in ("1h", "4h")}
        with _scheduler_lock:
            jobs = dict(_scheduler_jobs_status)
        try:
            from pending_entries import load_pending
            pend = load_pending()
        except Exception:
            pend = []
        return exit_health.check(perp=hl_data.get("hl_perp"), open_orders=hl_data.get("hl_open_orders"),
                                 nav=account.get("equity"), radar_1h=radars["1h"], radar_4h=radars["4h"],
                                 tier_for=_tier_for, job_status=jobs, pending=pend, now=now,
                                 pending_skips=(_read_out_json("pending_skips.json").get("skips") or []))
    except Exception as e:
        return {"ok": False, "problems": [{"code": "HEALTH_ERROR", "coin": None, "msg": str(e)}],
                "summary": f"health check error: {e}"}


# ---------------------------------------------------------------- cockpit views (display only)
_view_cache: dict = {}
_view_lock = threading.Lock()


def _hl_info(body: dict, timeout: int = 15):
    req = _url_req.Request(HL_API_URL, data=json.dumps(body).encode(),
                           headers={"Content-Type": "application/json"}, method="POST")
    with _url_req.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _account_view() -> dict:
    """Cockpit: PnL windows, equity curve, fills, funding, orders split, open risk, pipeline inputs (60s cache)."""
    import account_view as AV
    with _view_lock:
        c = _view_cache.get("account")
        if c and time.time() - c[0] < 60:
            return c[1]
    now_ms = int(time.time() * 1000)
    out: dict = {"ts": datetime.now(timezone.utc).isoformat(), "errors": []}
    hl = _get_hl_cached()
    perp = hl.get("hl_perp") or {}
    pos = AV.positions(perp)
    orders = AV.classify_orders(hl.get("hl_open_orders"), pos)
    out.update(positions=pos, orders=orders, risk=AV.risk_to_sl(pos, orders["sl"]),
               account=_compute_unified_equity(hl) if not hl.get("fetch_error") else {})
    for key, body, fn in (
        ("portfolio", {"type": "portfolio", "user": HL_ADDRESS}, None),
        ("fills", {"type": "userFillsByTime", "user": HL_ADDRESS, "startTime": now_ms - 30 * 86_400_000}, None),
        ("funding", {"type": "userFunding", "user": HL_ADDRESS, "startTime": now_ms - 7 * 86_400_000}, None),
    ):
        try:
            raw = _hl_info(body)
            if key == "portfolio":
                pf = AV.parse_portfolio(raw)
                out["pnl"] = AV.pnl_windows(pf)
                out["equity_curve"] = {w: (pf.get(w) or {}).get("av") for w in ("week", "month", "allTime")}
            elif key == "fills":
                out["fills"] = AV.summarize_fills(raw, now_ms)
            else:
                out["funding_7d"] = AV.summarize_funding(raw)
        except Exception as e:
            out["errors"].append(f"{key}: {e}")
    with _scheduler_lock:
        out["jobs"] = dict(_scheduler_jobs_status)
    out["next_runs"] = _next_runs()
    now = datetime.now(timezone.utc)
    out["run_report"] = _today_run_report(now)
    out["pending"] = _pending_view()
    out["exit_health"] = _exit_health(now, hl)
    try:
        out["decisions_today"] = get_decisions_for_today() if get_decisions_for_today else {}
    except Exception:
        out["decisions_today"] = {}
    cd = _load_candidates_file()
    gen = parse_ts(cd.get("generated_at")) if cd else None
    out["candidates"] = {"today": bool(gen and hkt_date(gen) == hkt_date(now)), "generated_at": cd.get("generated_at"),
                         "list": cd.get("candidates") or [], "entry_tab": _entry_tab(cd) if cd else {}}
    out["dimensions"] = _dims_compact(now)
    out["exec_mode"] = _exec_mode()
    out["sot"] = SOT_ID
    with _view_lock:
        _view_cache["account"] = (time.time(), out)
    return out


def _market_view() -> dict:
    """Market tab: sentiment, regime, breadth history, heatmap tiles, fresh crosses, sectors (5-min cache)."""
    import gzip
    import market_view as MV
    radars = {tf: _read_out_json(f"gc_radar_{tf}.json") for tf in ("1h", "4h", "1d")}
    key = tuple((radars[tf] or {}).get("live_ts") or (radars[tf] or {}).get("ts") for tf in ("1h", "4h", "1d"))
    with _view_lock:
        c = _view_cache.get("market")
        if c and c[2] == key and time.time() - c[0] < 300:
            return c[1]
    import scan_gc_radar as sgr
    bars = {}
    for tf in ("1h", "4h", "1d"):
        try:
            with gzip.open(os.path.join(OUT_DIR, f"candles_{tf}.json.gz"), "rt", encoding="utf-8") as f:
                bars[tf] = (json.load(f) or {}).get("bars") or {}
        except Exception:
            bars[tf] = {}
    sectors = {}
    if giiq_dims:
        try:
            sectors = giiq_dims.narrative_index(_read_out_json("narrative_watchlist.json"), radars.get("1d"))
        except Exception:
            sectors = {}
    out = MV.build(radars, bars, sectors, {tf: sgr.gc_period_for_tf(tf) for tf in ("1h", "4h", "1d")},
                   int(time.time() * 1000), sgr.compute_gc)
    out["ts"] = datetime.now(timezone.utc).isoformat()
    with _view_lock:
        _view_cache["market"] = (time.time(), out, key)
    return out


def _read_out_json(name: str) -> dict:
    try:
        with open(os.path.join(OUT_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def _dims_compact(now: datetime | None = None) -> dict:
    """Today's dimension scores (shadow) for Claude's ENTRY_DESK. {} if not computed today."""
    if not giiq_dims:
        return {}
    d = _read_out_json("dimensions_latest.json")
    if not d or d.get("signal_date") != hkt_date(now or datetime.now(timezone.utc)):
        return {"today": False, "note": "dimensions not computed yet today (08:08 HKT job)"}
    try:
        return {"today": True, **giiq_dims.compact(d)}
    except Exception as e:
        return {"error": str(e)}


def _dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _desk_data_lines(payload: dict, max_bytes: int | None = None) -> list:
    """Render payload as ONE line `[DESK_DATA] {json}` or, if larger than max_bytes,
    as `[DESK_DATA i/n] {json}` chunks. Every chunk is self-contained JSON with the same
    `ts` plus `part`/`parts`; radars too big for one chunk are split by rows with
    `rows_offset` (concatenate rows in part order to rebuild)."""
    max_bytes = max_bytes or DESK_DATA_CHUNK_BYTES
    full = _dumps(payload)
    if len(full.encode()) <= max_bytes:
        return [f"[DESK_DATA] {full}"]

    ts = payload.get("ts")
    budget = max_bytes - 200  # room for ts/part/parts wrapper
    sections: list = []  # list of dicts (each becomes part of a chunk)
    head = {k: v for k, v in payload.items() if not k.startswith("gc_radar_") and k != "ts"}
    sections.append(head)
    for key in ("gc_radar_1d", "gc_radar_4h", "gc_radar_1h"):
        r = payload.get(key)
        if r is None:
            continue
        if len(_dumps({key: r}).encode()) <= budget or not isinstance(r.get("rows"), list):
            sections.append({key: r})
            continue
        base = {k: v for k, v in r.items() if k != "rows"}
        rows = r["rows"]
        piece: list = []
        offset = 0
        first = True
        for i, row in enumerate(rows):
            trial = dict(base if first else {"ts": r.get("ts")}, rows_offset=offset, rows=piece + [row])
            if piece and len(_dumps({key: trial}).encode()) > budget:
                sections.append({key: dict(base if first else {"ts": r.get("ts")}, rows_offset=offset, rows=piece)})
                first = False
                offset = i
                piece = [row]
            else:
                piece.append(row)
        sections.append({key: dict(base if first else {"ts": r.get("ts")}, rows_offset=offset, rows=piece)})

    chunks: list = []
    cur: dict = {}
    for sec in sections:
        trial = {**cur, **sec}
        if cur and (len(_dumps(trial).encode()) > budget or any(k in cur for k in sec)):
            chunks.append(cur)
            cur = dict(sec)
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [f"[DESK_DATA {i}/{n}] " + _dumps({"ts": ts, "part": i, "parts": n, **c}) for i, c in enumerate(chunks, 1)]


_last_full_desk = {"t": 0.0}


def _log_desk_data(event: str = "", kind: str = "full") -> None:
    """Emit the [DESK_DATA] JSON line(s) to stdout (Railway deploy logs).
    kind="live" is upgraded to "full" if no full set was logged for DESK_FULL_EVERY_S."""
    try:
        if kind != "full" and time.time() - _last_full_desk["t"] > DESK_FULL_EVERY_S:
            kind = "full"
        if kind == "full":
            _last_full_desk["t"] = time.time()
        for line in _desk_data_lines(_build_desk_data_payload(event, kind=kind)):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stderr.write(f"[DESK_DATA_ERROR] {e}\n")


# =====================================================================
# APScheduler: In-process cron jobs for auto-execution
# =====================================================================

_scheduler_jobs_status: dict[str, dict] = {}
_scheduler_lock = threading.Lock()


def _load_job_status() -> None:
    """Restore last job status from the volume so restarts don't wipe it."""
    try:
        with open(SCHEDULER_STATUS_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _scheduler_lock:
                _scheduler_jobs_status.update(data)
    except Exception:
        pass


def _update_job_status(job_name: str, status: str, message: str = "", error: str = "") -> None:
    with _scheduler_lock:
        _scheduler_jobs_status[job_name] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "message": message,
            "error": error[-1500:] if error else "",
        }
        snapshot = dict(_scheduler_jobs_status)
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        tmp = SCHEDULER_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, SCHEDULER_STATUS_PATH)
    except Exception as e:
        sys.stderr.write(f"[SCHEDULER] status persist failed: {e}\n")


def _get_scheduler_status() -> dict:
    with _scheduler_lock:
        return dict(_scheduler_jobs_status)


def _run_worker(script: str, args: list, timeout: int, force_dry_run: bool = False) -> tuple[int, str, str]:
    """Run a worker script; forward its stderr to our logs. force_dry_run strips live creds."""
    env = dict(os.environ)
    if force_dry_run:
        env["EXEC_DRY_RUN"] = "1"
        env.pop("HL_API_PRIVATE_KEY", None)
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, script), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env,
    )
    if proc.stderr:
        for line in proc.stderr.strip().splitlines()[-60:]:
            sys.stderr.write(f"  [{script}] {line}\n")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _acquire_scan_lock(job_name: str) -> bool:
    if _scan_lock.acquire(timeout=SCHED_LOCK_WAIT_S):
        return True
    _update_job_status(job_name, "skipped", f"scan lock busy > {SCHED_LOCK_WAIT_S}s")
    sys.stderr.write(f"[SCHEDULER] {job_name} skipped: lock busy > {SCHED_LOCK_WAIT_S}s\n")
    return False


def _scheduled_1d_scan(manual: bool = False) -> None:
    """08:05 HKT: 1D + 4H scan (fresh 4H bar for Chase/stale check) -> candidates -> [DESK_DATA]."""
    job_name = "manual_1d_scan_candidates" if manual else "1d_scan_candidates"
    if not _acquire_scan_lock(job_name):
        return
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        ok, note = _run_scan(["1d", "4h"], max_symbols=SCAN_MAX)
        if not ok:
            _update_job_status(job_name, "error", "", note)
            sys.stderr.write(f"[SCHEDULER] {job_name} scan failed: {note[:200]}\n")
            return
        count = _generate_entry_candidates()
        _update_job_status(job_name, "success", f"{note}; candidates={count}")
        sys.stderr.write(f"[SCHEDULER] {job_name} completed: {note}; candidates={count}\n")
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] {job_name} exception: {e}\n")
    finally:
        _scan_lock.release()
    _log_desk_data(job_name)


def _scan_and_exits(job_name: str, tf: str, max_symbols: int, exit_arg: str, manual: bool) -> dict:
    """Closed-bar scan -> candidates -> exit worker. Returns {"exits_ok": bool, "at": iso, "why": str}
    so a caller that also enters (4H job -> pending entries) can enforce exits -> re-fetch -> entries."""
    done = {"exits_ok": False, "at": None, "why": "not run"}
    if not _acquire_scan_lock(job_name):
        done["why"] = "scan lock busy"
        return done
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        ok, note = _run_scan([tf], max_symbols=max_symbols)
        if not ok:
            _update_job_status(job_name, "error", "", note)
            sys.stderr.write(f"[SCHEDULER] {job_name} scan failed: {note[:200]}\n")
            done["why"] = f"scan failed: {note[:200]}"
            return done
        # Keep ENTRY tab == AI candidates list (never stale/empty while radar is fresh)
        count = _generate_entry_candidates()
        note = f"{note}; candidates={count}"
        try:
            rc, out, err = _run_worker("exit_worker.py", [exit_arg], timeout=300, force_dry_run=manual)
        except Exception as e:
            rc, out, err = 99, "", str(e)
        if rc == 0:
            try:
                res = json.loads(out)
                msg = (f"{note}; exits {res.get('mode')} ok: exits={len(res.get('exits', []))} "
                       f"actions={len(res.get('actions', []))} sl_actions={len(res.get('sl_actions', []))} "
                       f"holds={len(res.get('holds', []))}")
            except Exception:
                msg = f"{note}; exits ok"
            _update_job_status(job_name, "success", msg)
            done.update(exits_ok=True, at=datetime.now(timezone.utc).isoformat(), why="ok")
        else:
            msg = f"{note}; exit_worker rc={rc}"
            _update_job_status(job_name, "error", msg, (err or out)[-1500:])
            done["why"] = f"exit_worker rc={rc}"
        sys.stderr.write(f"[SCHEDULER] {job_name} completed: {msg}\n")
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] {job_name} exception: {e}\n")
        done["why"] = f"exception: {e}"
    finally:
        _scan_lock.release()
    _log_desk_data(job_name)
    return done


def _scheduled_1h_scan_exits(manual: bool = False) -> None:
    """Hourly :07 HKT: 1H scan + Small/Tiny exits + SL align."""
    _scan_and_exits("manual_1h_scan_exits" if manual else "1h_scan_exits", "1h", SCAN_MAX_1H, "hourly", manual)


PENDING_SKIPS_PATH = os.path.join(OUT_DIR, "pending_skips.json")


def _note_pending_skip(why: str, now: datetime | None = None) -> None:
    """Keep the last 7 days of 'pending check skipped because exits failed' events for EXIT_DESK's daily line."""
    now = now or datetime.now(timezone.utc)
    try:
        try:
            with open(PENDING_SKIPS_PATH) as f:
                items = json.load(f).get("skips") or []
        except Exception:
            items = []
        cutoff = (now - timedelta(days=7)).isoformat()
        items = [x for x in items if str(x.get("ts")) >= cutoff] + [{"ts": now.isoformat(), "why": why[:300]}]
        tmp = PENDING_SKIPS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"skips": items}, f, indent=1)
        os.replace(tmp, PENDING_SKIPS_PATH)
    except Exception as e:
        sys.stderr.write(f"[PENDING] skip log failed: {e}\n")


def _scheduled_4h_scan_exits(manual: bool = False) -> None:
    """Every 4h :10 HKT: 4H scan + Mega/Large exits + SL align, then pending pullback entries
    on the just-closed 4H bar (manual runs: pending forced DRY_RUN).
    Order is enforced (GIIQ-SoT-1): exits first; entries only if the exit step completed OK;
    pending_worker re-fetches positions itself after the exits."""
    done = _scan_and_exits("manual_4h_scan_exits" if manual else "4h_scan_exits", "4h", SCAN_MAX, "4h", manual)
    if not done.get("exits_ok"):
        why = f"exits step did not complete ({done.get('why')}) -> pending entries skipped (exits -> re-fetch -> entries)"
        _update_job_status("manual_pending_entries" if manual else "pending_entries", "skipped", why)
        if not manual:  # Harbor: accepted 4h delay, but it must show in the daily summary
            _note_pending_skip(why)
            _record_run_report("pending_entries", {"status": "skipped_exits_failed", "message": why})
        sys.stderr.write(f"[PENDING] skipped: {why}\n")
        return
    try:
        _scheduled_pending(manual=manual, after_exits=done.get("at"))
    except Exception as e:
        _update_job_status("pending_entries", "error", "", f"pending exception: {e}")
        sys.stderr.write(f"[PENDING] !!!!!!!! exception: {e}\n")


def _candles_cache_missing() -> list:
    return [tf for tf in ("1d", "4h", "1h") if not os.path.isfile(os.path.join(OUT_DIR, f"candles_{tf}.json.gz"))]


def _scheduled_live_radar(manual: bool = False) -> None:
    """Every 10 min (:03/:13/… HKT): LIVE radar for 1D/4H/1H from allMids (1 HL request) on
    top of the cached closed bars, then regenerate entry candidates (ENTRY == AI list) and log
    [DESK_DATA] (compact, or full if a boot scan ran / none logged for DESK_FULL_EVERY_S).
    Boot: any TF without a candle cache gets one closed-bar scan first (no exits/orders).
    Closed-bar signals are untouched (top-level row fields)."""
    job_name = "manual_live_radar" if manual else "live_radar"
    if live_radar is None:
        _update_job_status(job_name, "error", "", "live_radar module missing")
        return
    if not _scan_lock.acquire(timeout=LIVE_LOCK_WAIT_S):
        _update_job_status(job_name, "skipped", f"scan lock busy > {LIVE_LOCK_WAIT_S}s")
        return
    kind = "live"
    try:
        boot = []
        for tf in _candles_cache_missing():
            ok, note = _run_scan([tf], max_symbols=SCAN_MAX_1H if tf == "1h" else SCAN_MAX)
            boot.append(f"{tf}:{'ok' if ok else 'fail'}")
            sys.stderr.write(f"[LIVE] boot closed-bar scan {tf} ok={ok} {note[:200]}\n")
            kind = "full"
        res = live_radar.refresh_live()
        count = _generate_entry_candidates()
        parts = [f"{tf}={r.get('n_live', 0)}/{r.get('n_rows', 0)}" if r.get("ok") else f"{tf}=ERR({r.get('error', '')[:60]})"
                 for tf, r in (res.get("tfs") or {}).items()]
        msg = (f"live mids={res.get('n_mids')} {' '.join(parts)} in {res.get('elapsed_s')}s; candidates={count}"
               + (f"; boot_scan={','.join(boot)}" if boot else ""))
        _update_job_status(job_name, "success" if res.get("ok") else "partial", msg)
        sys.stderr.write(f"[LIVE] {msg}\n")
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[LIVE] exception: {e}\n")
    finally:
        _scan_lock.release()
    _log_desk_data(job_name, kind=kind)


_exec_lock = threading.Lock()  # one executor pass at a time (08:55 cron vs keyed /api/exec/run)
EXEC_PREFLIGHT_PATH = os.path.join(OUT_DIR, "exec_preflight.json")


def _read_exec_preflight() -> dict:
    try:
        with open(EXEC_PREFLIGHT_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _scheduled_preflight(manual: bool = False) -> dict:
    """Boot + 08:45 HKT: LIVE preflight (mode, agent approval on HL, account, signed probe,
    today's approvals). Never trades. Result -> out/exec_preflight.json + scheduler status
    'exec_preflight' (shown as a red banner in the cockpit when it fails)."""
    job_name = "exec_preflight"
    res: dict = {}
    try:
        rc, out, err = _run_worker("exec_preflight.py", [], timeout=120)
        try:
            res = json.loads(out)
        except Exception:
            res = {"ok": False, "mode": "?", "checks": [], "error": (err or out)[-800:]}
        res["ran_at"] = datetime.now(timezone.utc).isoformat()
        failed = [f"{c['check']}: {c['detail']}" for c in res.get("checks", []) if not c.get("ok") and c.get("blocking")]
        warns = res.get("warnings") or []
        if res.get("ok"):
            status = "warning" if warns else "success"
            msg = f"{res.get('mode')} preflight OK: agent={((res.get('agent') or {}).get('name'))} " \
                  f"approved={res.get('approved')}" + (f" | WARN {'; '.join(warns)}" if warns else "")
        else:
            status = "error"
            msg = f"{res.get('mode')} preflight FAILED: " + "; ".join(failed or [res.get("error") or "unknown"])
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            tmp = EXEC_PREFLIGHT_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(res, f, indent=2, default=str)
            os.replace(tmp, EXEC_PREFLIGHT_PATH)
        except Exception as e:
            sys.stderr.write(f"[PREFLIGHT] persist failed: {e}\n")
        _update_job_status(job_name, status, msg[:1500], "" if status != "error" else msg[:1500])
        banner = "!!!!!!!! " if status == "error" else ""
        sys.stderr.write(f"[PREFLIGHT] {banner}{status.upper()}: {msg}\n")
    except Exception as e:
        _update_job_status(job_name, "error", "", f"preflight exception: {e}")
        sys.stderr.write(f"[PREFLIGHT] !!!!!!!! exception: {e}\n")
        res = {"ok": False, "error": str(e)}
    return res


def _scheduled_executor(manual: bool = False, live_api: bool = False) -> dict:
    """08:55 HKT: execute AI-approved candidates.

    manual=True (password /api/jobs/run) is always DRY_RUN. live_api=True is the keyed
    POST /api/exec/run re-run: same executor.py, same env (LIVE iff EXEC_DRY_RUN=0), same
    fail-closed checks; idempotent because executor skips coins already held."""
    job_name = "api_exec_run" if live_api else ("manual_executor" if manual else "executor")
    res: dict = {}
    if not _exec_lock.acquire(timeout=5):
        _update_job_status(job_name, "skipped", "another executor pass is running")
        return {"status": "busy", "message": "another executor pass is running"}
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        rc, out, err = _run_worker("executor.py", [], timeout=600, force_dry_run=manual and not live_api)
        try:
            res = json.loads(out)
        except Exception:
            res = {}
        status = res.get("status", "unknown")
        _record_run_report(job_name, res)
        msg = (f"[{SOT_ID}] {res.get('mode', '?')} {status}: executed={len(res.get('executed', []))} "
               f"actions={len(res.get('actions', []))} skipped={len(res.get('skipped', []))}"
               + (f" | {res.get('message')}" if res.get("message") else ""))
        if res.get("skipped"):
            msg += " | skips: " + "; ".join(f"{x.get('symbol')}: {x.get('reason')}" for x in res["skipped"])[:900]
        if res.get("alerts"):
            msg += " | ALERTS: " + "; ".join(map(str, res["alerts"]))[:600]
        if rc == 0 and status in ("success", "fail_closed"):
            _update_job_status(job_name, status, msg)
        else:
            _update_job_status(job_name, "error", msg, (err or out)[-1500:])
        banner = "!!!!!!!! " if status not in ("success", "fail_closed") else ""
        sys.stderr.write(f"[SCHEDULER] {banner}{job_name} {status}: {msg}\n")
    except subprocess.TimeoutExpired:
        _update_job_status(job_name, "error", "", "executor timed out (>600s)")
        res = {"status": "error", "message": "executor timed out (>600s)"}
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] !!!!!!!! {job_name} exception: {e}\n")
        res = {"status": "error", "message": str(e)}
    finally:
        _exec_lock.release()
    return res


def _scheduled_pending(manual: bool = False, after_exits: str | None = None) -> dict:
    """Every 4h at :10 HKT, right after the 4H closed-bar scan (called from the 4H job):
    pending pullback entries (ADD_ON / CONTINUATION) -> pending_worker.py.
    Same env as the executor (LIVE iff EXEC_DRY_RUN=0); manual (password) runs are forced DRY_RUN."""
    job_name = "manual_pending_entries" if manual else "pending_entries"
    res: dict = {}
    if not _exec_lock.acquire(timeout=120):
        _update_job_status(job_name, "skipped", "executor/pending pass already running")
        return {"status": "busy"}
    try:
        args = ["--after-exits", after_exits] if after_exits else []
        rc, out, err = _run_worker("pending_worker.py", args, timeout=300, force_dry_run=manual)
        try:
            res = json.loads(out)
        except Exception:
            res = {}
        status = res.get("status", "unknown")
        if res.get("checked") or res.get("filled") or res.get("cancelled") or status != "success":
            _record_run_report(job_name, res)
        checks = "; ".join(f"{c.get('symbol')} {c.get('kind')}: {c.get('result') or c.get('action')} ({c.get('reason')})"
                           for c in res.get("checked", []))
        msg = (f"[{SOT_ID}] {res.get('mode', '?')} {status}: active={len(res.get('pending_active', []))} "
               f"filled={len(res.get('filled', []))} cancelled={len(res.get('cancelled', []))}"
               + (f" | {res.get('message')}" if res.get("message") else "")
               + (f" | {checks[:900]}" if checks else "")
               + (f" | ALERTS: {'; '.join(map(str, res['alerts']))[:500]}" if res.get("alerts") else ""))
        if rc == 0 and status in ("success", "fail_closed"):
            _update_job_status(job_name, status, msg)
        else:
            _update_job_status(job_name, "error", msg, (err or out)[-1500:])
        sys.stderr.write(f"[PENDING] {'!!!!!!!! ' if status != 'success' else ''}{job_name} {status}: {msg}\n")
    except subprocess.TimeoutExpired:
        _update_job_status(job_name, "error", "", "pending worker timed out (>300s)")
        res = {"status": "error"}
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        res = {"status": "error", "message": str(e)}
    finally:
        _exec_lock.release()
    return res


RUN_REPORT_DIR = os.environ.get("RUN_REPORT_DIR") or os.path.join(OUT_DIR, "run_reports")
RUN_REPORT_MAX_RUNS = 30
_run_report_lock = threading.Lock()


def _run_report_path(day: str) -> str:
    return os.path.join(RUN_REPORT_DIR, f"{day}.json")


def _record_run_report(job_name: str, res: dict, now: datetime | None = None) -> None:
    """Append this run's report (executor / pending) to out/run_reports/<HKT date>.json."""
    try:
        rep = dict((res or {}).get("run_report") or {})
        if not rep:
            rep = {"sot": SOT_ID, "run": job_name, "mode": (res or {}).get("mode"),
                   "status": (res or {}).get("status") or "unknown", "message": (res or {}).get("message"),
                   "executed": [], "skipped": [], "downsized": [], "failed": []}
        rep["job"] = job_name
        now = now or datetime.now(timezone.utc)
        rep.setdefault("ts", now.isoformat())
        day = now.astimezone(HKT).strftime("%Y-%m-%d")
        with _run_report_lock:
            os.makedirs(RUN_REPORT_DIR, exist_ok=True)
            path = _run_report_path(day)
            try:
                with open(path) as f:
                    doc = json.load(f)
            except Exception:
                doc = {"date": day, "runs": []}
            doc["sot"] = SOT_ID
            doc["runs"] = (doc.get("runs") or [])[-(RUN_REPORT_MAX_RUNS - 1):] + [rep]
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(doc, f, indent=1, default=str)
            os.replace(tmp, path)
    except Exception as e:
        sys.stderr.write(f"[RUN_REPORT] persist failed: {e}\n")


def _today_run_report(now: datetime | None = None) -> dict:
    """Daily run report (HKT day): every executor / pending run + merged executed / skipped /
    downsized / failed lists with reasons (cockpit + [DESK_DATA])."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(HKT).strftime("%Y-%m-%d")
    try:
        with open(_run_report_path(day)) as f:
            doc = json.load(f)
    except Exception:
        doc = {"date": day, "runs": []}
    out = {"date": day, "sot": SOT_ID, "runs": [], "executed": [], "skipped": [], "downsized": [], "failed": []}
    for r in doc.get("runs") or []:
        tag = {"job": r.get("job") or r.get("run"), "mode": r.get("mode"), "ts": r.get("ts")}
        out["runs"].append({**tag, "status": r.get("status"), "message": r.get("message"), "nav": r.get("nav"),
                            "sequence": r.get("sequence"),
                            "counts": {k: len(r.get(k) or []) for k in ("executed", "skipped", "downsized", "failed")}})
        for k in ("executed", "skipped", "downsized", "failed"):
            for item in r.get(k) or []:
                out[k].append({**item, **{f"run_{a}": b for a, b in tag.items()}})
    return out


def _pending_view() -> list:
    """Active pending pullback entries + live trigger zones (cockpit / API)."""
    try:
        from pending_entries import load_pending, summary as _psum
        def _rows(tf: str) -> dict:
            try:
                with open(os.path.join(OUT_DIR, f"gc_radar_{tf}.json")) as f:
                    return {r.get("symbol"): r for r in (json.load(f).get("rows") or [])}
            except Exception:
                return {}
        entries = load_pending()
        mids = {}
        live = {}
        for tf in ("1d", "4h"):
            for sym, r in _rows(tf).items():
                if isinstance(r.get("live"), dict) and r["live"].get("close"):
                    live.setdefault(sym, r["live"]["close"])
        mids = live
        return _psum(entries, _rows("1d"), _rows("4h"), mids)
    except Exception as e:
        return [{"error": str(e)}]


def _scheduled_dims(manual: bool = False, mode: str = "snapshot") -> dict:
    """GIIQ dimensions (shadow): 08:08 snapshot (whales + HL ctx + scores + ledger rows);
    08:30 outcomes + report. Never places orders; failures only affect the measurement."""
    job_name = ("manual_" if manual else "") + ("dims_" + mode)
    res: dict = {}
    try:
        steps = [mode] if mode != "outcomes" else ["outcomes", "report"]
        for step in steps:
            rc, out, err = _run_worker("dims_job.py", [step], timeout=900, force_dry_run=True)
            try:
                res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
            except Exception:
                res = {"status": "error", "message": (err or out)[-300:]}
            ok = rc == 0 and res.get("status") in ("success", "skipped")
            _update_job_status(job_name, res.get("status", "error") if ok else "error",
                               f"{step}: {json.dumps(res, default=str)[:600]}", "" if ok else (err or out)[-800:])
            sys.stderr.write(f"[SCHEDULER] {job_name} {step}: {json.dumps(res, default=str)[:400]}\n")
            if not ok:
                break
        if mode == "snapshot":
            _log_desk_data(job_name)
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        res = {"status": "error", "message": str(e)}
    return res


def build_fallback_decisions(cd: dict, now: datetime | None = None) -> tuple[list, str]:
    """Fallback decisions from today's candidates: Base -> approve at 2% margin, Chase -> veto.
    Returns (decisions, why_not) — decisions empty with a reason when candidates are unusable."""
    now = now or datetime.now(timezone.utc)
    gen = parse_ts((cd or {}).get("generated_at"))
    if not gen or hkt_date(gen) != hkt_date(now):
        return [], "candidates not generated today"
    if cd.get("stale"):
        return [], "candidates stale"
    out = []
    for c in cd.get("candidates") or []:
        sym = str(c.get("symbol") or "").upper()
        if not sym:
            continue
        base = bool(c.get("is_base")) or c.get("type") == "Base"
        out.append({"symbol": sym, "decision": "approve" if base else "veto", "type": "BASE" if base else "CHASE",
                    "size_pct": 2.0 if base else 0, "leverage": None if base else 0,
                    "reason": ("RAILWAY_FALLBACK: no Claude POST by 08:50 - Base only, 2% margin" if base
                               else "RAILWAY_FALLBACK: no Claude POST - Chase not allowed in fallback")})
    return out, ""


def _scheduled_fallback(manual: bool = False) -> dict:
    """08:50 HKT: if no Claude decision is stored today, store fallback decisions (source=fallback).
    Manual (password) runs only preview; they never store."""
    job_name = "manual_decision_fallback" if manual else "decision_fallback"
    res: dict = {}
    try:
        from decisions import _rec_source
        decs = get_decisions_for_today() if get_decisions_for_today else {}
        if any(_rec_source(r) == "claude" for r in (decs or {}).values()):
            res = {"status": "skipped", "message": "Claude decisions present - no fallback"}
        elif decs:
            res = {"status": "skipped", "message": f"fallback already stored ({len(decs)} decisions)"}
        else:
            fb, why = build_fallback_decisions(_load_candidates_file())
            if why:
                res = {"status": "skipped", "message": f"no fallback: {why}"}
            elif manual or not AUTO_FALLBACK:
                res = {"status": "preview", "would_store": fb,
                       "message": "preview only" if manual else "AUTO_FALLBACK=0 - not stored (no trade today)"}
            else:
                stored = store_decisions(fb, source="fallback")
                if dim_ledger and stored.get("stored"):
                    try:
                        conn = dim_ledger.connect()
                        dim_ledger.record_decisions(conn, hkt_date(datetime.now(timezone.utc)), stored["stored"],
                                                    source="fallback")
                        conn.close()
                    except Exception as e:
                        stored["ledger_error"] = str(e)
                stored.pop("stored", None)
                res = {"status": "success" if stored.get("ok") else "error",
                       "message": f"RAILWAY_FALLBACK stored: {stored.get('stored_count')} decisions "
                                  f"({sum(1 for d in fb if d['decision'] == 'approve')} Base approvals @2%)",
                       "result": stored}
        _update_job_status(job_name, res.get("status", "error") if res.get("status") != "preview" else "success",
                           res.get("message", ""))
        banner = "!!!!!!!! " if res.get("status") == "success" and not manual else ""
        sys.stderr.write(f"[SCHEDULER] {banner}{job_name}: {res.get('message')}\n")
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        res = {"status": "error", "message": str(e)}
    return res


def _scheduled_dims_outcomes(manual: bool = False) -> dict:
    return _scheduled_dims(manual=manual, mode="outcomes")


def _scheduled_dims_backfill(manual: bool = False) -> dict:
    """Historical replay into the separate backfill ledger (shadow; manual trigger only).
    Refused inside trading windows (heavy_job_blocked)."""
    why = heavy_job_blocked()
    if why:
        _update_job_status("manual_dims_backfill" if manual else "dims_backfill", "skipped", why)
        return {"status": "skipped", "message": why}
    return _scheduled_dims(manual=manual, mode="backfill")


# Shadow-only jobs Claude may trigger with the AI key (they never place, change or cancel orders).
AI_SHADOW_JOBS = ("dims", "dims_outcomes", "dims_backfill")


def heavy_job_blocked(now: datetime | None = None) -> str:
    """The CPU-heavy backfill must not share the box with trading jobs: blocked 07:55-09:05 HKT
    (scan / decisions / preflight / executor) and every hour :05-:12 (1H/4H scans + exits)."""
    h = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    mins = h.hour * 60 + h.minute
    if 7 * 60 + 55 <= mins <= 9 * 60 + 5:
        return "blocked 07:55-09:05 HKT (entry window)"
    if 5 <= h.minute <= 12:
        return "blocked :05-:12 every hour (1H/4H scans + exits)"
    return ""


# ---------------------------------------------------------------- Bitunix shadow radar (display/shadow only)
# Harbor-approved 2026-09-29. Own lock (never _scan_lock / _exec_lock), subprocess with a hard timeout and
# force_dry_run=True, so the BX worker never even sees HL_API_PRIVATE_KEY. No Bitunix key, no orders.
# Nothing on the HL order path reads bx_* files (test_bx_isolation.py). BX status goes to its own
# [BX_DATA] log line, never into [DESK_DATA] / ENTRY_DESK candidates.
BX_ENABLED = (os.environ.get("BX_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"))
BX_TIMEOUT_S = int(os.environ.get("BX_TIMEOUT_S", "480"))
BX_PROBE_LOG_DAYS = 60
_bx_lock = threading.Lock()


def _bx_nav_usd() -> float | None:
    """HL NAV for shadow sizing (read only, cached HL data; None if unavailable)."""
    try:
        hl = _get_hl_cached()
        if hl.get("fetch_error"):
            return None
        eq = _compute_unified_equity(hl).get("equity")
        return float(eq) if eq else None
    except Exception:
        return None


def _bx_probe_log(job: str, res: dict) -> None:
    """Daily Bitunix API health for the 30-day go-live bar: calls, errors, 429s, mean latency per run."""
    try:
        api = (res or {}).get("api") or {}
        path = os.path.join(OUT_DIR, "bx_probe_log.json")
        try:
            with open(path) as f:
                log = json.load(f)
        except Exception:
            log = {"runs": []}
        calls = int(api.get("calls") or 0)
        log["runs"] = (log.get("runs") or [])[-(BX_PROBE_LOG_DAYS * 8):] + [{
            "ts": datetime.now(timezone.utc).isoformat(), "job": job, "status": (res or {}).get("status"),
            "calls": calls, "errors": int(api.get("errors") or 0), "n429": int(api.get("n429") or 0),
            "mean_ms": round(float(api.get("ms_total") or 0) / calls, 1) if calls else None}]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(log, f, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        sys.stderr.write(f"[BX] probe log failed: {e}\n")


def _scheduled_bx(manual: bool = False, job: str = "daily") -> dict:
    """BX shadow radar: daily 08:20 (catalog + 1D/4H) · every 4h :25 (4H) · hourly :27 (1H, new tokens + open
    shadow positions). Then the shadow book for the same job. Skips if another BX run holds the lock."""
    job_name = ("manual_" if manual else "") + f"bx_{job}"
    if not BX_ENABLED:
        _update_job_status(job_name, "skipped", "BX_ENABLED=0")
        return {"status": "skipped"}
    if not _bx_lock.acquire(blocking=False):
        _update_job_status(job_name, "skipped", "another BX run in progress")
        return {"status": "busy"}
    res: dict = {}
    try:
        rc, out, err = _run_worker("bx_radar.py", [job], timeout=BX_TIMEOUT_S, force_dry_run=True)
        try:
            res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except Exception:
            res = {"status": "error", "message": (err or out)[-300:]}
        _bx_probe_log(job, res)
        radar_ok = rc == 0 and res.get("status") in ("success", "partial", "skipped")
        shadow: dict = {}
        if radar_ok and res.get("status") != "skipped":
            nav = _bx_nav_usd()
            rc2, out2, err2 = _run_worker("bx_shadow.py", [job] + (["--nav", str(nav)] if nav else []),
                                          timeout=BX_TIMEOUT_S, force_dry_run=True)
            try:
                shadow = json.loads(out2.strip().splitlines()[-1]) if out2.strip() else {}
            except Exception:
                shadow = {"error": (err2 or out2)[-300:]}
            if rc2 != 0:
                radar_ok = False
                res["message"] = f"shadow rc={rc2}: {(err2 or out2)[-200:]}"
        msg = (f"{res.get('status')}: " + json.dumps({k: res.get(k) for k in
               ("n_contracts", "n_scanned", "n_1d", "n_4h", "n_tradfi", "n", "review", "elapsed_s", "message")
               if res.get(k) is not None}, default=str)
               + (f" | shadow signals={len(shadow.get('signals') or [])} opened={len(shadow.get('opened') or [])} "
                  f"closed={len(shadow.get('closed') or [])} open={shadow.get('open')}" if shadow else ""))
        _update_job_status(job_name, "success" if radar_ok else "error", msg[:900], "" if radar_ok else (err or out)[-800:])
        sys.stderr.write(f"[BX_DATA] {job_name} {msg[:700]}\n")
        res["shadow"] = shadow
    except subprocess.TimeoutExpired:
        _update_job_status(job_name, "error", "", f"BX worker timed out (> {BX_TIMEOUT_S}s)")
        res = {"status": "error", "message": "timeout"}
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        res = {"status": "error", "message": str(e)}
    finally:
        _bx_lock.release()
    return res


def _scheduled_bx_daily(manual: bool = False) -> dict:
    return _scheduled_bx(manual=manual, job="daily")


def _scheduled_bx_4h(manual: bool = False) -> dict:
    return _scheduled_bx(manual=manual, job="4h")


def _scheduled_bx_1h(manual: bool = False) -> dict:
    return _scheduled_bx(manual=manual, job="1h")


def _bx_view() -> dict:
    """Cockpit Market tab (BX filter) + TradFi tab + BX shadow panel. Display only."""
    meta = _read_out_json("bx_meta.json")
    keep = ("symbol", "bx_symbol", "ex", "asset_class", "close", "price", "trend", "filter", "upper", "lower",
            "dual_cross_up", "above_upper", "bar_time", "vol24h_usd", "ign_x", "ign", "spread_bp", "liq_tier",
            "contract_age_days", "new_contract", "asset_age", "cat_tags", "drop_from_ath_pct", "tier", "gc_tf",
            "session_gap", "narrative")
    out: dict = {"ok": bool(meta), "ts": meta.get("ts"), "counts": meta.get("counts"),
                 "n_contracts": meta.get("n_contracts"), "n_scanned": meta.get("n_scanned"),
                 "review": (meta.get("review") or [])[:200],
                 # HL names also listed on Bitunix -> the Bitunix tab labels those HL rows HL+BX
                 "overlap": sorted({r.get("hl_name") for r in meta.get("catalog") or []
                                    if r.get("ex") == "HL+BX" and r.get("hl_name")})}
    for tf in ("1d", "4h", "1h"):
        r = _read_out_json(f"bx_radar_{tf}.json")
        out[f"radar_{tf}"] = {"ts": r.get("ts"), "breadth": r.get("breadth"),
                              "rows": [{k: x.get(k) for k in keep} for x in r.get("rows") or []]}
    tf_r = _read_out_json("bx_tradfi_radar.json")
    out["tradfi"] = {"ts": tf_r.get("ts"), "rows": [{k: x.get(k) for k in keep + ("tf",)} for x in tf_r.get("rows") or []]}
    try:
        import bx_shadow
        conn = bx_shadow.connect()
        try:
            out["shadow"] = {"compare": bx_shadow.compare(conn),
                             "open": [dict(r) for r in conn.execute(
                                 "SELECT symbol, kind, gc_tf, counted, entry_time, entry_px, size_pct_nav, hard_sl, exit_rule "
                                 "FROM shadow_trades WHERE status='open' ORDER BY entry_time DESC")],
                             "closed": [dict(r) for r in conn.execute(
                                 "SELECT symbol, kind, gc_tf, counted, entry_time, exit_time, exit_reason, ret_pct, pnl_nav_pct "
                                 "FROM shadow_trades WHERE status='closed' ORDER BY exit_time DESC LIMIT 20")]}
        finally:
            conn.close()
    except Exception as e:
        out["shadow"] = {"error": str(e)[:200]}
    return out


MANUAL_JOBS = {
    "1d": _scheduled_1d_scan,
    "1h": _scheduled_1h_scan_exits,
    "4h": _scheduled_4h_scan_exits,
    "executor": _scheduled_executor,
    "live": _scheduled_live_radar,
    "pending": _scheduled_pending,
    "dims": _scheduled_dims,
    "dims_outcomes": _scheduled_dims_outcomes,
    "dims_backfill": _scheduled_dims_backfill,
    "fallback": _scheduled_fallback,
    "bx": _scheduled_bx_daily,
    "bx_4h": _scheduled_bx_4h,
    "bx_1h": _scheduled_bx_1h,
}
_manual_running: set = set()
_manual_lock = threading.Lock()


def _start_manual_job(job: str) -> tuple[bool, str]:
    fn = MANUAL_JOBS.get(job)
    if not fn:
        return False, f"unknown job {job!r}; use one of {sorted(MANUAL_JOBS)}"
    with _manual_lock:
        if job in _manual_running:
            return False, f"{job} already running"
        _manual_running.add(job)

    def _run() -> None:
        try:
            fn(manual=True)
        finally:
            with _manual_lock:
                _manual_running.discard(job)

    threading.Thread(target=_run, name=f"manual-{job}", daemon=True).start()
    return True, "started"


_SCHED_REF: dict = {}


def _next_runs() -> dict:
    s = _SCHED_REF.get("s")
    if not s:
        return {}
    out = {}
    try:
        for j in s.get_jobs():
            nrt = getattr(j, "next_run_time", None)
            out[j.id] = nrt.astimezone(HKT).isoformat(timespec="seconds") if nrt else None
    except Exception:
        pass
    return out


def _init_scheduler() -> BackgroundScheduler | None:
    """APScheduler cron jobs (Asia/Hong_Kong):
    08:05 1D+4H scan + candidates · hourly :07 1H + Small/Tiny exits ·
    every 4h :10 4H + Mega/Large exits · boot + 08:45 preflight · 08:55 executor.
    Jobs wait for the scan lock."""
    if not HAS_APSCHEDULER:
        sys.stderr.write("[SCHEDULER] APScheduler not available, skipping\n")
        return None
    if not SCHEDULER_ENABLED:
        sys.stderr.write("[SCHEDULER] SCHEDULER_ENABLED=0, skipping\n")
        return None
    try:
        _load_job_status()
        hkt = pytz.timezone("Asia/Hong_Kong")
        scheduler = BackgroundScheduler(timezone=hkt)
        common = dict(max_instances=1, coalesce=True, misfire_grace_time=600)
        scheduler.add_job(_scheduled_1d_scan, CronTrigger(hour=8, minute=5, timezone=hkt),
                          id="1d_scan_candidates", name="1D+4H Scan + Entry Candidates", **common)
        scheduler.add_job(_scheduled_1h_scan_exits, CronTrigger(minute=7, timezone=hkt),
                          id="1h_scan_exits", name="1H Scan + Small/Tiny Exits", **common)
        scheduler.add_job(_scheduled_4h_scan_exits, CronTrigger(hour="0,4,8,12,16,20", minute=10, timezone=hkt),
                          id="4h_scan_exits", name="4H Scan + Mega/Large Exits", **common)
        scheduler.add_job(_scheduled_dims, CronTrigger(hour=8, minute=8, timezone=hkt),
                          id="dims_snapshot", name="GIIQ dimensions snapshot (shadow)", **common)
        scheduler.add_job(_scheduled_dims_outcomes, CronTrigger(hour=8, minute=30, timezone=hkt),
                          id="dims_outcomes", name="GIIQ dimension outcomes + report (shadow)", **common)
        scheduler.add_job(_scheduled_fallback, CronTrigger(hour=8, minute=50, timezone=hkt),
                          id="decision_fallback", name="Decision fallback if no Claude POST (Base only, 2%)", **common)
        scheduler.add_job(_scheduled_preflight, CronTrigger(hour=8, minute=45, timezone=hkt),
                          id="exec_preflight", name="LIVE executor preflight (agent/key/signing)",
                          **common)
        scheduler.add_job(_scheduled_preflight, "date", run_date=datetime.now(hkt) + timedelta(seconds=20),
                          id="exec_preflight_boot", name="LIVE preflight at boot", **common)
        scheduler.add_job(_scheduled_executor, CronTrigger(hour=8, minute=55, timezone=hkt),
                          id="executor", name="Auto-Executor", **common)
        # LIVE radar every 10 min; first run ~30s after boot (does boot scans if cache missing)
        scheduler.add_job(_scheduled_live_radar, CronTrigger(minute=LIVE_RADAR_MINUTES, timezone=hkt),
                          id="live_radar", name="LIVE radar 1D/4H/1H + candidates sync",
                          next_run_time=datetime.now(hkt) + timedelta(seconds=30), **common)
        if BX_ENABLED:  # Bitunix shadow radar: own lock, never blocks an HL job
            scheduler.add_job(_scheduled_bx_daily, CronTrigger(hour=8, minute=20, timezone=hkt),
                              id="bx_daily", name="Bitunix shadow radar daily (display/shadow only)", **common)
            scheduler.add_job(_scheduled_bx_4h, CronTrigger(hour="0,4,12,16,20", minute=25, timezone=hkt),
                              id="bx_4h", name="Bitunix shadow 4H (display/shadow only)", **common)
            scheduler.add_job(_scheduled_bx_1h, CronTrigger(minute=27, timezone=hkt),
                              id="bx_1h", name="Bitunix shadow 1H (new tokens + open shadow)", **common)
        scheduler.start()
        _SCHED_REF["s"] = scheduler
        sys.stderr.write(
            "[SCHEDULER] APScheduler started (Asia/Hong_Kong)\n"
            "  - 08:05 HKT: 1D+4H scan + entry candidates\n"
            "  - 08:08 HKT: GIIQ dimensions snapshot (shadow; whales + HL ctx)\n"
            "  - 08:30 HKT: GIIQ dimension outcomes + report (shadow)\n"
            f"  - 08:50 HKT: decision fallback if no Claude POST (AUTO_FALLBACK={'on' if AUTO_FALLBACK else 'off'})\n"
            "  - Hourly :07: 1H scan + Small/Tiny exits\n"
            "  - Every 4h :10: 4H scan + Mega/Large exits\n"
            "  - boot + 08:45 HKT: LIVE executor preflight\n"
            "  - 08:55 HKT: Auto-executor (Base now; Chase -> pending pullback)\n"
            "  - Every 4h :10 (after 4H scan): pending pullback entries\n"
            f"  - cron minute {LIVE_RADAR_MINUTES}: LIVE radar 1D/4H/1H + candidates sync + DESK_DATA\n"
            + ("  - BX shadow (no orders): 08:20 daily, 4h :25 (08:20 run covers 08), hourly :27\n" if BX_ENABLED else "")
        )
        return scheduler
    except Exception as e:
        sys.stderr.write(f"[SCHEDULER] Failed to initialize: {e}\n")
        return None


def _scheduler_loop() -> None:
    global _last_failsafe
    sys.stderr.write(
        f"[scheduler] started UTC: 1h+4h every {SCAN_INTERVAL_MIN}m; 1d@00:05 "
        f"(concurrency={SCAN_CONCURRENCY})\n"
    )
    # Soft boot: if radar files missing, scan after short delay (volume may be empty)
    time.sleep(15)
    missing = [
        tf
        for tf in ("1d", "4h", "1h")
        if not os.path.isfile(os.path.join(OUT_DIR, f"gc_radar_{tf}.json"))
    ]
    if missing and _scan_lock.acquire(blocking=False):
        try:
            sys.stderr.write(f"[scheduler] boot scan missing={missing}\n")
            for tf in missing:
                mx = SCAN_MAX_1H if tf == "1h" else SCAN_MAX
                ok, note = _run_scan([tf], max_symbols=mx)
                sys.stderr.write(f"[scheduler] boot tf={tf} ok={ok} {note[:200]}\n")
                if ok:
                    _last_scan[tf] = "boot"
        finally:
            _scan_lock.release()
        _log_desk_data()  # emit after boot scans for MCP relay

    while True:
        try:
            now = datetime.now(timezone.utc)
            due = _due_tfs(now)
            if due and _scan_lock.acquire(blocking=False):
                try:
                    # Mark slots first so we don't re-fire if scan spans the next minute tick
                    for tf, _mx in due:
                        _last_scan[tf] = _slot_key(now, tf)
                    # 1d alone when due; 1h+4h together on the 15m tick (4h uses SCAN_MAX)
                    daily = [tf for tf, _ in due if tf == "1d"]
                    intraday = [tf for tf, _ in due if tf in ("1h", "4h")]
                    if daily:
                        ok, note = _run_scan(daily, max_symbols=SCAN_MAX)
                        sys.stderr.write(f"[scheduler] {now.isoformat()} 1d ok={ok} {note[:300]}\n")
                        if ok:
                            _generate_entry_candidates()
                    if intraday:
                        # Prefer scanning 4h then 1h sequentially via one or two calls
                        if "4h" in intraday:
                            ok, note = _run_scan(["4h"], max_symbols=SCAN_MAX)
                            sys.stderr.write(f"[scheduler] {now.isoformat()} 4h ok={ok} {note[:300]}\n")
                        if "1h" in intraday:
                            ok, note = _run_scan(["1h"], max_symbols=SCAN_MAX_1H)
                            sys.stderr.write(f"[scheduler] {now.isoformat()} 1h ok={ok} {note[:300]}\n")
                            _log_desk_data()  # emit after each scan slot for MCP relay
                finally:
                    _scan_lock.release()

            # Fail-safe worker: hourly when minute >= 5 (after 1H close at :00)
            # Slot-based: run once per UTC hour (avoid missing :05 if loop skips that minute)
            if now.minute >= 5:
                slot = now.strftime("%Y-%m-%dT%H")
                if _last_failsafe != slot:
                    _last_failsafe = slot
                    try:
                        ok, note = _run_failsafe()
                        sys.stderr.write(f"[scheduler] {now.isoformat()} failsafe ok={ok} {note[:300]}\n")
                    except Exception as e:
                        sys.stderr.write(f"[scheduler] failsafe exception (non-fatal): {e}\n")

        except Exception as e:
            sys.stderr.write(f"[scheduler] error: {e}\n")
        time.sleep(15)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), PASSWORD):
            return True
        if self.headers.get("X-Cockpit-Password") == PASSWORD:
            return True
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth[6:].strip()).decode()
                _u, _, pw = raw.partition(":")
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        cookie = SimpleCookie()
        if self.headers.get("Cookie"):
            cookie.load(self.headers.get("Cookie"))
        if COOKIE_NAME in cookie:
            return hmac.compare_digest(cookie[COOKIE_NAME].value, _token_for(PASSWORD))
        return False

    def _need_auth(self) -> bool:
        if self._authed():
            return False
        path = urlparse(self.path).path
        if path == "/login" and self.command == "POST":
            return False
        if path in ("/health", "/healthz"):
            return False
        self._send_login()
        return True

    def _send_login(self, err: str = "") -> None:
        html = LOGIN_HTML.replace("__ERR__", f'<p class=err>{err}</p>' if err else "")
        body = html.encode()
        self.send_response(200)  # Return 200 for login form (not 401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/healthz"):
            self._send_json(
                200,
                {
                    "ok": True,
                    "scheduler": SCHEDULER_ENABLE,
                    "scheduler_enabled": SCHEDULER_ENABLED,
                    "last_scan": dict(_last_scan),
                    "scanner": os.path.isfile(SCAN_SCRIPT),
                },
            )
            return
        if path == "/api/entry-candidates":
            self._entry_candidates(parsed.query)
            return
        if path == "/api/ai/narrative":
            self._ai_narrative()
            return
        if path == "/api/ai/candidates":
            self._ai_candidates(parsed.query)
            return
        if path == "/api/public/radar":
            self._public_radar()
            return
        if path == "/api/scheduler/status":
            self._scheduler_status()
            return
        if path == "/api/exec/preflight":
            self._exec_preflight()
            return
        if path == "/api/exit/health":
            if self._ai_key_ok():
                self._send_json(200, _exit_health())
            return
        if path == "/api/ai/dimensions":
            if self._ai_key_ok():
                d = _read_out_json("dimensions_latest.json")
                self._send_json(200 if d else 404, d or {"ok": False, "error": "dimensions not computed yet"})
            return
        if path == "/api/dimensions/report":
            if self._ai_key_ok():
                self._dims_report(parsed.query)
            return
        if path == "/api/whales":
            if self._ai_key_ok():
                snap = _read_out_json(os.path.join("whales", "latest.json"))
                wl = _read_out_json("whales_watchlist.json")
                self._send_json(200, {"ok": bool(snap), "snapshot": {k: snap.get(k) for k in
                                      ("ts", "n_wallets", "n_manual", "errors", "wallets")} if snap else None,
                                      "coins": snap.get("coins") if snap else None, "watchlist": wl})
            return
        if path in ("/api/bx/radar", "/api/bx/shadow", "/api/bx/review"):
            # keyed, read-only BX shadow data (Claude weekly review of unknown-class contracts, reports)
            if self._ai_key_ok():
                try:
                    if path == "/api/bx/radar":
                        tf = (parse_qs(parsed.query).get("tf") or ["1d"])[0]
                        name = "bx_tradfi_radar.json" if tf == "tradfi" else f"bx_radar_{tf if tf in ('1d', '4h', '1h') else '1d'}.json"
                        d = _read_out_json(name)
                        self._send_json(200 if d else 404, d or {"ok": False, "error": "BX radar not built yet"})
                    elif path == "/api/bx/review":
                        m = _read_out_json("bx_meta.json")
                        self._send_json(200, {"ok": bool(m), "ts": m.get("ts"), "review": m.get("review") or [],
                                              "how_to_fix": "classify in data/bx_asset_class.json or map in "
                                                            "data/bx_symbol_map.json (Harbor reviews Sundays)"})
                    else:
                        import bx_shadow
                        probe = _read_out_json("bx_probe_log.json")
                        self._send_json(200, {"ok": True, "compare": bx_shadow.compare(),
                                              "probe_runs": (probe.get("runs") or [])[-40:]})
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        if path == "/api/exec/pending":
            if self._ai_key_ok():
                try:
                    from pending_entries import load_pending
                    allrec = load_pending()
                except Exception:
                    allrec = []
                self._send_json(200, {"ok": True, "active": _pending_view(), "all": allrec[-50:]})
            return
        if self._need_auth():
            return
        if path == "/api/desk-data":
            self._desk_data()
            return
        if path == "/api/dims-ui":
            self._dims_ui()
            return
        if path in ("/api/account-ui", "/api/market-ui"):
            try:
                self._send_json(200, _account_view() if path == "/api/account-ui" else _market_view())
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        if path == "/api/bx-ui":  # password-gated like the other views; BX shadow data, display only
            try:
                self._send_json(200, _bx_view())
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        if path == "/api/trades":
            self._get_trades()
            return
        if path in ("/", "/index.html"):
            self._send_file(UI_PATH, "text/html; charset=utf-8")
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self._login()
            return
        if path == "/api/sync":
            if not self._authed():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self._sync()
            return
        if path == "/api/rescan":
            # Password-gated (same as UI); allowed on Railway so Harbor is optional
            if self._need_auth():
                return
            self._rescan()
            return
        if path == "/api/ai/decision":
            self._ai_decision()
            return
        if path == "/api/whales/watchlist":
            self._whales_watchlist()
            return
        if path == "/api/ai/jobs/run":
            self._ai_shadow_job()
            return
        if path == "/api/ai/narrative":
            self._ai_narrative()
            return
        if path == "/api/exec/run":
            self._exec_run()
            return
        if path == "/api/exec/preflight":
            self._exec_preflight()
            return
        if path == "/api/jobs/run":
            # Password-gated manual trigger. executor/exit workers are FORCED DRY_RUN here.
            if self._need_auth():
                return
            self._run_job()
            return
        self.send_error(404, "Not Found")

    def _login(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        password = ""
        if "application/json" in ctype:
            try:
                password = str(json.loads(raw.decode()).get("password") or "")
            except Exception:
                password = ""
        else:
            qs = parse_qs(raw.decode(errors="ignore"))
            password = (qs.get("password") or [""])[0]
        if not PASSWORD or not hmac.compare_digest(password, PASSWORD):
            self._send_login("Wrong password")
            return
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={_token_for(PASSWORD)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000",
        )
        self.end_headers()

    def _safe_rel(self, rel: str) -> str | None:
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return None
        if not (rel.startswith("out/") or rel.startswith("narrative/")):
            return None
        return rel

    def _sync(self) -> None:
        """Merge-write: only replaces keys present in payload; never wipes omitted radar files.
        
        Staleness guard: reject gc_radar_*.json if incoming scan timestamp is older than existing.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            self._send_json(400, {"ok": False, "error": "need {files:{path: content}}"})
            return
        written = []
        skipped_stale = []
        for rel, content in files.items():
            safe = self._safe_rel(str(rel))
            if not safe:
                self._send_json(400, {"ok": False, "error": f"bad path: {rel}"})
                return
            dest = os.path.join(ROOT, safe)
            
            # Staleness check for radar JSONs
            if safe.startswith("out/gc_radar_") and safe.endswith(".json"):
                if isinstance(content, str):
                    try:
                        incoming = json.loads(content)
                        incoming_ts = incoming.get("ts", "")
                        if os.path.isfile(dest):
                            with open(dest) as f:
                                existing = json.load(f)
                                existing_ts = existing.get("ts", "")
                            if existing_ts and incoming_ts and incoming_ts < existing_ts:
                                skipped_stale.append(f"{safe} (incoming={incoming_ts[:19]} < existing={existing_ts[:19]})")
                                continue
                    except Exception as e:
                        sys.stderr.write(f"[sync] staleness check failed for {safe}: {e}\n")
            
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if isinstance(content, str):
                data = content.encode("utf-8")
            else:
                self._send_json(400, {"ok": False, "error": f"content must be string: {rel}"})
                return
            tmp = dest + ".tmp." + secrets.token_hex(4)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            written.append(safe)
        result = {"ok": True, "written": written}
        if skipped_stale:
            result["skipped_stale"] = skipped_stale
        
        # Generate entry candidates if 1D/4H radars were synced
        if any("out/gc_radar_1d.json" in w for w in written):
            _generate_entry_candidates()
        
        self._send_json(200, result)

    def _desk_data(self) -> None:
        """Password-gated endpoint — combined radar + HL live data (cached 45s).

        Returns: {
            gc_radar_1h, gc_radar_4h, gc_radar_1d (from volume),
            account (computed from Unified mode),
            positions (with tier-based stops),
            scheduler (APScheduler job status if enabled),
            ts, address
        }
        """
        result: dict = {}

        # 1) Radar JSONs from volume mount
        radar_1h = None
        radar_4h = None
        for tf in ("1h", "4h", "1d"):
            fp = os.path.join(OUT_DIR, f"gc_radar_{tf}.json")
            try:
                with open(fp) as f:
                    data = json.load(f)
                    result[f"gc_radar_{tf}"] = data
                    if tf == "1h":
                        radar_1h = data
                    elif tf == "4h":
                        radar_4h = data
            except Exception as e:
                result[f"gc_radar_{tf}"] = {"error": str(e)}

        # 2) HL data (cached)
        hl_data = _get_hl_cached()
        result["hl_data"] = hl_data
        
        # 3) Compute Unified equity
        account = _compute_unified_equity(hl_data)
        result["account"] = account
        
        # 4) Compute positions with tier-based stops
        positions = _compute_positions_with_stops(hl_data, radar_1h or {}, radar_4h or {})
        result["positions"] = positions
        
        # 5) ENTRY tab list = entry_candidates_latest.json (same source as /api/ai/candidates)
        cd = _load_candidates_file()
        result["entry_candidates"] = cd
        result["entry_tab"] = _entry_tab(cd)

        # 6) Scheduler status (if enabled)
        if SCHEDULER_ENABLED:
            result["scheduler"] = _get_scheduler_status()
        result["exec_preflight"] = _read_exec_preflight()
        result["pending_entries"] = _pending_view()
        result["exec_mode"] = _exec_mode()
        result["sot"] = SOT_ID
        result["run_report"] = _today_run_report()
        
        result["ts"] = datetime.now(timezone.utc).isoformat()
        result["address"] = HL_ADDRESS
        self._send_json(200, result)

    def _send_file(self, filepath: str, content_type: str) -> None:
        try:
            with open(filepath, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ai_candidates(self, query_str: str) -> None:
        """GET /api/ai/candidates: keyed endpoint for AI to fetch today's candidates.
        
        Auth: query param key compared with AI_DECISION_KEY (fallback ENTRY_READ_KEY).
        Returns 404 if AI_DECISION_KEY unset.
        Returns 403 if key missing or invalid.
        Returns 200 with enhanced candidate data for AI decision.
        """
        if not AI_DECISION_KEY:
            self._send_json(404, {"ok": False, "error": "AI candidates endpoint disabled"})
            return
        
        qs = parse_qs(query_str)
        provided_key = (qs.get("key") or [""])[0]
        
        if not provided_key or not hmac.compare_digest(provided_key, AI_DECISION_KEY):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        
        # Load entry candidates
        latest_path = os.path.join(OUT_DIR, "entry_candidates_latest.json")
        try:
            with open(latest_path) as f:
                candidates_data = json.load(f)
        except FileNotFoundError:
            self._send_json(404, {"ok": False, "error": "entry candidates not yet generated"})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        
        # Get HL account state
        try:
            hl_data = _get_hl_cached()
            account = _compute_unified_equity(hl_data)
        except Exception as e:
            account = {"error": str(e)}
        
        # Enhance: suggested size/leverage (<= coin max), tier Hard SL, isolated liq estimate
        enhanced = _enhance_candidates(candidates_data.get("candidates", []), account, _get_hl_meta_cached())

        # Build response
        response = {
            "generated_at": candidates_data.get("generated_at"),
            "radar_1d_asof": candidates_data.get("radar_1d_asof"),
            "radar_4h_asof": candidates_data.get("radar_4h_asof"),
            "stale": candidates_data.get("stale", False),
            "btc": candidates_data.get("btc", {}),
            "count": len(enhanced),
            "candidates": enhanced,
            "account": account,
        }
        
        self._send_json(200, response)

    def _ai_narrative(self) -> None:
        """GET/POST /api/ai/narrative — keyed (X-AI-Key header or ?key=, same key as /api/ai/candidates).
        GET: current watchlist. POST: {"mode": "merge"|"replace", "items": [{ticker, sector, venue,
        narrative}], "remove": [tickers]} -> persisted to out/narrative_watchlist.json (used by next scan)."""
        provided = self.headers.get("X-AI-Key") or (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
        if not AI_DECISION_KEY:
            self._send_json(404, {"ok": False, "error": "AI endpoints disabled"})
            return
        if not provided or not hmac.compare_digest(provided, AI_DECISION_KEY):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        if self.command == "GET":
            self._send_json(200, {"ok": True, **_read_narrative_watchlist()})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 512_000:
            self._send_json(413, {"ok": False, "error": "body too large"})
            return
        try:
            body = json.loads((self.rfile.read(length) if length else b"{}").decode())
            res = update_narrative_watchlist(body)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, **res})

    def _dims_report(self, query_str: str) -> None:
        """GET /api/dimensions/report?since=YYYY-MM-DD&type=Base|Chase&metric=ret_7d_sl (keyed)."""
        if not dim_ledger:
            self._send_json(500, {"ok": False, "error": "dim_ledger not available"})
            return
        qs = parse_qs(query_str)
        metric = (qs.get("metric") or ["ret_7d_sl"])[0]
        if metric not in ("ret_7d_sl", "ret_7d", "ret_3d", "ret_1d", "mfe_7d"):
            self._send_json(400, {"ok": False, "error": "bad metric"})
            return
        try:
            backfill = (qs.get("db") or [""])[0] == "backfill"
            conn = dim_ledger.connect(dim_ledger.backfill_db_path() if backfill else None)
            rep = dim_ledger.report(conn, metric=metric, since=(qs.get("since") or [None])[0],
                                    sig_type=(qs.get("type") or [None])[0])
            conn.close()
            if backfill:
                rep["note"] = (_read_out_json("dimensions_report_backfill.json") or {}).get("note")
            self._send_json(200, {"ok": True, "db": "backfill" if backfill else "live", **rep})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _dims_ui(self) -> None:
        """GET /api/dims-ui (cockpit password): everything the 維度 tab shows in one call."""
        out: dict = {"ok": True, "now": datetime.now(timezone.utc).isoformat()}
        out["latest"] = _read_out_json("dimensions_latest.json") or None
        try:
            decs = get_decisions_for_today() if get_decisions_for_today else {}
        except Exception:
            decs = {}
        out["decisions_today"] = decs
        snap = _read_out_json(os.path.join("whales", "latest.json"))
        out["whales"] = ({k: snap.get(k) for k in ("ts", "n_wallets", "n_manual", "errors")} if snap else None)
        if dim_ledger:
            for key, path_ in (("live", None), ("backfill", dim_ledger.backfill_db_path())):
                try:
                    if path_ and not os.path.isfile(path_):
                        out[key] = None
                        continue
                    conn = dim_ledger.connect(path_)
                    out[key] = {"report": dim_ledger.report(conn),
                                "recent": dim_ledger.recent_signals(conn, 60 if key == "live" else 40)}
                    conn.close()
                except Exception as e:
                    out[key] = {"error": str(e)}
            if out.get("backfill") and isinstance(out["backfill"], dict) and out["backfill"].get("report"):
                out["backfill"]["report"]["note"] = (_read_out_json("dimensions_report_backfill.json") or {}).get("note")
        self._send_json(200, out)

    def _ai_shadow_job(self) -> None:
        """POST /api/ai/jobs/run (keyed) {"job": "dims"|"dims_outcomes"|"dims_backfill"} -> 202.
        Shadow measurement jobs only; trading jobs stay password-gated."""
        if not self._ai_key_ok():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            job = str((json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}") or {}).get("job") or "")
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        if job not in AI_SHADOW_JOBS:
            self._send_json(400, {"ok": False, "error": f"job must be one of {list(AI_SHADOW_JOBS)}"})
            return
        if job == "dims_backfill" and heavy_job_blocked():
            self._send_json(409, {"ok": False, "job": job, "error": heavy_job_blocked()})
            return
        ok, msg = _start_manual_job(job)
        self._send_json(202 if ok else 409, {"ok": ok, "job": job, "message": msg,
                                             "status_key": f"manual_{job}_*", "see": "/api/scheduler/status"})

    def _whales_watchlist(self) -> None:
        """POST /api/whales/watchlist (keyed) {"wallets": [{"address","label","source"}]} — replaces
        the manual smart-money list (e.g. traders found on fomo.family / HyperDash). Public addresses only."""
        if not self._ai_key_ok():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        try:
            import whales
            res = whales.save_manual_watchlist(OUT_DIR, body.get("wallets") or [])
            self._send_json(200 if res.get("ok") else 400, res)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _ai_key_ok(self) -> bool:
        provided = self.headers.get("X-AI-Key") or (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
        if not AI_DECISION_KEY:
            self._send_json(404, {"ok": False, "error": "AI endpoints disabled"})
            return False
        if not provided or not hmac.compare_digest(provided, AI_DECISION_KEY):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return False
        return True

    def _exec_preflight(self) -> None:
        """GET /api/exec/preflight (keyed): last preflight result. POST: run it now (never trades)."""
        if not self._ai_key_ok():
            return
        if self.command == "POST":
            res = _scheduled_preflight(manual=True)
        else:
            res = _read_exec_preflight()
        self._send_json(200, {"ok": True, "preflight": res})

    def _exec_run(self) -> None:
        """POST /api/exec/run (keyed, X-AI-Key): run executor.py ONCE for today's (HKT) stored
        decisions with the service env (LIVE iff EXEC_DRY_RUN=0). Same fail-closed checks as the
        08:55 cron (fresh candidates, liq beyond Hard SL, 80% margin cap, SoT bands); idempotent
        (coins already held are skipped). Optional body {"date": "YYYY-MM-DD"} must equal today HKT."""
        if not self._ai_key_ok():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        today = datetime.now(HKT).strftime("%Y-%m-%d")
        want = str((body or {}).get("date") or today)
        if want != today:
            self._send_json(400, {"ok": False, "error": f"only today's decisions can run (today HKT={today})"})
            return
        res = _scheduled_executor(live_api=True)
        if res.get("status") == "busy":
            self._send_json(409, {"ok": False, **res})
            return
        self._send_json(200, {"ok": res.get("status") in ("success", "fail_closed"), "date": today, "result": res})

    def _ai_decision(self) -> None:
        """POST /api/ai/decision: store AI approval/veto decisions.
        
        Auth: header X-AI-Key or query param key.
        Body: {decisions: [{symbol, decision: approve|veto, size_pct, leverage, reason}, ...]}

        size_pct/leverage are stored as submitted; the executor clamps them to SoT bands
        (size band, 1-5x, coin maxLeverage) at execution time.
        """
        # Auth check
        auth_header = self.headers.get("X-AI-Key") or ""
        query_str = urlparse(self.path).query
        qs = parse_qs(query_str)
        query_key = (qs.get("key") or [""])[0]
        
        provided_key = auth_header or query_key
        
        if not AI_DECISION_KEY:
            self._send_json(404, {"ok": False, "error": "AI decision endpoint disabled"})
            return
        
        if not provided_key or not hmac.compare_digest(provided_key, AI_DECISION_KEY):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        
        # Read body
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        
        try:
            body = json.loads(raw.decode())
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        
        decisions = body.get("decisions")
        if not isinstance(decisions, list):
            self._send_json(400, {"ok": False, "error": "need {decisions: [...]}"})
            return
        
        # Store decisions
        if not store_decisions:
            self._send_json(500, {"ok": False, "error": "decisions module not available"})
            return
        
        src = "fallback" if str(body.get("source") or "").lower() == "fallback" else "claude"
        try:
            result = store_decisions(decisions, source=src)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        # Measurement ledger (Claude dims + decision per signal). Never blocks the decision itself.
        if dim_ledger and result.get("stored"):
            try:
                conn = dim_ledger.connect()
                today = hkt_date(datetime.now(timezone.utc))
                result["ledger_recorded"] = dim_ledger.record_decisions(conn, today, result["stored"], source=src)
                conn.close()
            except Exception as e:
                result["ledger_error"] = str(e)
        result.pop("stored", None)
        if not result.get("ok"):
            sys.stderr.write(f"[AI_DECISION] !!!!!!!! nothing stored: {json.dumps(result)[:500]}\n")
        code = 200 if result.get("ok") else (409 if "fallback ignored" in str(result.get("error")) else 422)
        self._send_json(code, result)

    def _public_radar(self) -> None:
        """GET /api/public/radar: public trimmed radar feed (no auth, no positions).
        
        Returns:
            gc_radar_1h, gc_radar_4h, gc_radar_1d (from volume mount)
            No positions, no account data, no keys required.
        """
        result: dict = {}
        
        # Radar JSONs from volume mount
        for tf in ("1h", "4h", "1d"):
            fp = os.path.join(OUT_DIR, f"gc_radar_{tf}.json")
            try:
                with open(fp) as f:
                    data = json.load(f)
                    result[f"gc_radar_{tf}"] = data
            except Exception as e:
                result[f"gc_radar_{tf}"] = {"error": str(e)}
        
        result["ts"] = datetime.now(timezone.utc).isoformat()
        self._send_json(200, result)

    def _scheduler_status(self) -> None:
        """GET /api/scheduler/status: password-gated endpoint for scheduler job status.
        
        Returns:
            enabled: bool
            jobs: dict of job_name -> {last_run, status, message, error}
        """
        if not SCHEDULER_ENABLED:
            self._send_json(200, {
                "enabled": False,
                "message": "Scheduler is disabled (SCHEDULER_ENABLED=0)",
            })
            return
        
        status = _get_scheduler_status()
        self._send_json(200, {
            "enabled": True,
            "jobs": status,
            "next_runs_hkt": _next_runs(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def _get_trades(self) -> None:
        """GET /api/trades: get all trades from trade log (password-gated).
        
        Returns:
            trades: list of trade dicts
        """
        if not get_all_trades:
            self._send_json(500, {"ok": False, "error": "trade_log module not available"})
            return
        
        try:
            trades = get_all_trades()
            self._send_json(200, {"ok": True, "count": len(trades), "trades": trades})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _entry_candidates(self, query_str: str) -> None:
        """Read-only endpoint for entry_candidates_latest.json (bypasses password gate).
        
        Auth: query param key compared with ENTRY_READ_KEY (constant-time).
        Returns 404 if ENTRY_READ_KEY unset (disabled).
        Returns 403 if key missing or invalid.
        Returns 200 with JSON if key matches.
        """
        if not ENTRY_READ_KEY:
            self._send_json(404, {"ok": False, "error": "entry candidates endpoint disabled"})
            return
        
        qs = parse_qs(query_str)
        provided_key = (qs.get("key") or [""])[0]
        
        if not provided_key or not hmac.compare_digest(provided_key, ENTRY_READ_KEY):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        
        # Read entry_candidates_latest.json
        latest_path = os.path.join(OUT_DIR, "entry_candidates_latest.json")
        try:
            with open(latest_path) as f:
                data = json.load(f)
            self._send_json(200, data)
        except FileNotFoundError:
            self._send_json(404, {"ok": False, "error": "entry candidates not yet generated"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _run_job(self) -> None:
        """POST /api/jobs/run {"job": "1d"|"1h"|"4h"|"executor"} -> 202, runs in background.

        Same code path as the scheduled job, but executor / exit worker are always DRY_RUN
        (EXEC_DRY_RUN=1 and no key in the subprocess env). Status: /api/scheduler/status
        under manual_<job>.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            job = str((json.loads(raw.decode() or "{}") or {}).get("job") or "")
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        ok, msg = _start_manual_job(job)
        self._send_json(202 if ok else 409 if "running" in msg else 400,
                        {"ok": ok, "job": job, "message": msg, "dry_run_forced": job in ("executor", "1h", "4h")})

    def _rescan(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        tfs = ["1h", "4h", "1d"]
        max_symbols = None
        if raw:
            try:
                body = json.loads(raw.decode())
                if isinstance(body, dict):
                    if body.get("tf"):
                        tfs = [t.strip() for t in str(body["tf"]).split(",") if t.strip()]
                    if body.get("max") is not None:
                        max_symbols = int(body["max"])
            except Exception:
                pass
        if not _scan_lock.acquire(blocking=False):
            self._send_json(409, {"ok": False, "error": "scan already running"})
            return
        try:
            ok, note = _run_scan(tfs, max_symbols=max_symbols)
            if ok and "1d" in tfs:
                count = _generate_entry_candidates()
                note = f"{note}; candidates={count}"
        finally:
            _scan_lock.release()
        if ok:
            _log_desk_data("manual_rescan")  # emit updated desk data after manual rescan
        if not ok:
            self._send_json(500, {"ok": False, "error": note})
            return
        self._send_json(200, {"ok": True, "note": note})


def main() -> None:
    os.chdir(ROOT)
    os.makedirs(OUT_DIR, exist_ok=True)
    
    # Initialize APScheduler (new in-process scheduler)
    scheduler = None
    if SCHEDULER_ENABLED:
        scheduler = _init_scheduler()
    
    # Start legacy scheduler thread (deprecated, only if OTR_SCHEDULER=1 and SCHEDULER_ENABLED=0)
    if SCHEDULER_ENABLE and not SCHEDULER_ENABLED:
        t = threading.Thread(target=_scheduler_loop, name="otr-scheduler", daemon=True)
        t.start()
    
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Own Trend Radar UI → http://0.0.0.0:{PORT}/", flush=True)
    print(
        f"password_gate={'on' if PASSWORD else 'off'} railway={ON_RAILWAY} "
        f"scheduler_enabled={SCHEDULER_ENABLED} legacy_scheduler={SCHEDULER_ENABLE}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        if scheduler:
            scheduler.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
