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
from datetime import datetime, timezone
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
_last_scan: dict[str, str] = {}  # tf -> slot key
_last_failsafe: str = ""  # last failsafe run slot key (hour)

try:
    from entry_candidates import build_candidates
except ImportError:
    build_candidates = None  # type: ignore

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


def _generate_entry_candidates() -> None:
    """Generate entry_candidates_latest.json and dated snapshot (non-fatal)."""
    if not build_candidates:
        return
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
    except Exception as e:
        sys.stderr.write(f"[entry_candidates] error (non-fatal): {e}\n")


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


def _log_desk_data() -> None:
    """One-line summary of scan completion (reduced from full JSON dump)."""
    try:
        radar_ts = {}
        pos_count = {}
        for tf in ("1h", "4h", "1d"):
            fp = os.path.join(OUT_DIR, f"gc_radar_{tf}.json")
            try:
                with open(fp) as f:
                    data = json.load(f)
                    radar_ts[tf] = data.get("ts", "")[:19]
                    rows = data.get("rows") or []
                    pos_count[tf] = len(rows)
            except Exception:
                radar_ts[tf] = "error"
                pos_count[tf] = 0
        sys.stderr.write(
            f"[DESK_DATA] {datetime.now(timezone.utc).isoformat()[:19]} "
            f"radar_1h={pos_count['1h']}@{radar_ts['1h']} "
            f"radar_4h={pos_count['4h']}@{radar_ts['4h']} "
            f"radar_1d={pos_count['1d']}@{radar_ts['1d']}\n"
        )
    except Exception as e:
        sys.stderr.write(f"[DESK_DATA] error: {e}\n")


# =====================================================================
# APScheduler: In-process cron jobs for auto-execution
# =====================================================================

# Scheduler state tracking
_scheduler_jobs_status: dict[str, dict] = {}
_scheduler_lock = threading.Lock()


def _update_job_status(job_name: str, status: str, message: str = "", error: str = "") -> None:
    """Update scheduler job status."""
    with _scheduler_lock:
        _scheduler_jobs_status[job_name] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "message": message,
            "error": error,
        }


def _get_scheduler_status() -> dict:
    """Get current scheduler status for all jobs."""
    with _scheduler_lock:
        return dict(_scheduler_jobs_status)


def _scheduled_1d_scan() -> None:
    """Scheduled job: 1D scan + generate entry candidates (08:05 HKT daily)."""
    job_name = "1d_scan_candidates"
    
    # Lock guard
    if not _scan_lock.acquire(blocking=False):
        _update_job_status(job_name, "skipped", "scan already running")
        return
    
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        
        # 1D scan
        ok, note = _run_scan(["1d"], max_symbols=SCAN_MAX)
        if not ok:
            _update_job_status(job_name, "error", "", note)
            sys.stderr.write(f"[SCHEDULER] {job_name} scan failed: {note[:200]}\n")
            return
        
        # Generate entry candidates
        if build_candidates:
            _generate_entry_candidates()
        
        _update_job_status(job_name, "success", f"1D scan complete, {note}")
        sys.stderr.write(f"[SCHEDULER] {job_name} completed: {note[:200]}\n")
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] {job_name} exception: {e}\n")
    finally:
        _scan_lock.release()


def _scheduled_1h_scan_exits() -> None:
    """Scheduled job: 1H scan + Small/Tiny exits (hourly :05)."""
    job_name = "1h_scan_exits"
    
    # Lock guard
    if not _scan_lock.acquire(blocking=False):
        _update_job_status(job_name, "skipped", "scan already running")
        return
    
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        
        # 1H scan
        ok, note = _run_scan(["1h"], max_symbols=SCAN_MAX_1H)
        if not ok:
            _update_job_status(job_name, "error", "", note)
            sys.stderr.write(f"[SCHEDULER] {job_name} scan failed: {note[:200]}\n")
            return
        
        # Small/Tiny exits
        exit_script = os.path.join(ROOT, "exit_worker.py")
        if os.path.isfile(exit_script):
            try:
                proc = subprocess.run(
                    [sys.executable, exit_script, "hourly"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                exit_msg = "exits OK" if proc.returncode == 0 else f"exits failed: {proc.returncode}"
            except Exception as e:
                exit_msg = f"exits error: {e}"
        else:
            exit_msg = "exit_worker.py missing"
        
        _update_job_status(job_name, "success", f"1H scan + {exit_msg}")
        sys.stderr.write(f"[SCHEDULER] {job_name} completed: {note[:200]}, {exit_msg}\n")
        _log_desk_data()
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] {job_name} exception: {e}\n")
    finally:
        _scan_lock.release()


def _scheduled_4h_scan_exits() -> None:
    """Scheduled job: 4H scan + Mega/Large exits (every 4h :05)."""
    job_name = "4h_scan_exits"
    
    # Lock guard
    if not _scan_lock.acquire(blocking=False):
        _update_job_status(job_name, "skipped", "scan already running")
        return
    
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        
        # 4H scan
        ok, note = _run_scan(["4h"], max_symbols=SCAN_MAX)
        if not ok:
            _update_job_status(job_name, "error", "", note)
            sys.stderr.write(f"[SCHEDULER] {job_name} scan failed: {note[:200]}\n")
            return
        
        # Mega/Large exits
        exit_script = os.path.join(ROOT, "exit_worker.py")
        if os.path.isfile(exit_script):
            try:
                proc = subprocess.run(
                    [sys.executable, exit_script, "4h"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                exit_msg = "exits OK" if proc.returncode == 0 else f"exits failed: {proc.returncode}"
            except Exception as e:
                exit_msg = f"exits error: {e}"
        else:
            exit_msg = "exit_worker.py missing"
        
        _update_job_status(job_name, "success", f"4H scan + {exit_msg}")
        sys.stderr.write(f"[SCHEDULER] {job_name} completed: {note[:200]}, {exit_msg}\n")
        _log_desk_data()
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] {job_name} exception: {e}\n")
    finally:
        _scan_lock.release()


def _scheduled_executor() -> None:
    """Scheduled job: Execute approved candidates (08:55 HKT daily)."""
    job_name = "executor"
    
    try:
        sys.stderr.write(f"[SCHEDULER] Starting {job_name}\n")
        
        executor_script = os.path.join(ROOT, "executor.py")
        if not os.path.isfile(executor_script):
            _update_job_status(job_name, "error", "", "executor.py missing")
            return
        
        proc = subprocess.run(
            [sys.executable, executor_script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min timeout
        )
        
        if proc.returncode == 0:
            try:
                result = json.loads(proc.stdout)
                executed = len(result.get("executed", []))
                actions = len(result.get("actions", []))
                skipped = len(result.get("skipped", []))
                mode = result.get("mode", "?")
                status = result.get("status", "unknown")
                msg = f"{mode} {status}: executed={executed} actions={actions} skipped={skipped}"
                _update_job_status(job_name, "success", msg)
                sys.stderr.write(f"[SCHEDULER] {job_name} completed: {msg}\n")
            except Exception:
                _update_job_status(job_name, "success", "executor ran (status unknown)")
        else:
            err = (proc.stderr or proc.stdout or "executor failed")[-1000:]
            _update_job_status(job_name, "error", "", err)
            sys.stderr.write(f"[SCHEDULER] {job_name} failed: {err[:200]}\n")
    except subprocess.TimeoutExpired:
        _update_job_status(job_name, "error", "", "executor timed out (>600s)")
    except Exception as e:
        _update_job_status(job_name, "error", "", str(e))
        sys.stderr.write(f"[SCHEDULER] {job_name} exception: {e}\n")


def _init_scheduler() -> BackgroundScheduler | None:
    """Initialize APScheduler with cron jobs (Asia/Hong_Kong timezone).
    
    Staggered times to avoid lock collisions (PR #6 fix):
    - 08:05 HKT: 1D scan + candidates
    - Every hour :07: 1H scan + Small/Tiny exits
    - Every 4h :10: 4H scan + Mega/Large exits  
    - 08:55 HKT: Executor
    """
    if not HAS_APSCHEDULER:
        sys.stderr.write("[SCHEDULER] APScheduler not available, skipping\n")
        return None
    
    if not SCHEDULER_ENABLED:
        sys.stderr.write("[SCHEDULER] SCHEDULER_ENABLED=0, skipping\n")
        return None
    
    try:
        hkt = pytz.timezone("Asia/Hong_Kong")
        scheduler = BackgroundScheduler(timezone=hkt)
        
        # 08:05 HKT daily: 1D scan + generate entry candidates
        scheduler.add_job(
            _scheduled_1d_scan,
            CronTrigger(hour=8, minute=5, timezone=hkt),
            id="1d_scan_candidates",
            name="1D Scan + Entry Candidates",
            max_instances=1,
            coalesce=True,
        )
        
        # Hourly :07 (staggered +2min from 1D): 1H scan + Small/Tiny exits
        scheduler.add_job(
            _scheduled_1h_scan_exits,
            CronTrigger(minute=7, timezone=hkt),
            id="1h_scan_exits",
            name="1H Scan + Small/Tiny Exits",
            max_instances=1,
            coalesce=True,
        )
        
        # Every 4h :10 (staggered +5min from 1D/1H): 4H scan + Mega/Large exits
        scheduler.add_job(
            _scheduled_4h_scan_exits,
            CronTrigger(hour="0,4,8,12,16,20", minute=10, timezone=hkt),
            id="4h_scan_exits",
            name="4H Scan + Mega/Large Exits",
            max_instances=1,
            coalesce=True,
        )
        
        # 08:55 HKT daily: Execute approved candidates
        scheduler.add_job(
            _scheduled_executor,
            CronTrigger(hour=8, minute=55, timezone=hkt),
            id="executor",
            name="Auto-Executor",
            max_instances=1,
            coalesce=True,
        )
        
        scheduler.start()
        sys.stderr.write(
            "[SCHEDULER] APScheduler started (Asia/Hong_Kong timezone, staggered)\n"
            "  - 08:05 HKT: 1D scan + entry candidates\n"
            "  - Hourly :07: 1H scan + Small/Tiny exits\n"
            "  - Every 4h :10: 4H scan + Mega/Large exits\n"
            "  - 08:55 HKT: Auto-executor\n"
        )
        return scheduler
    except Exception as e:
        sys.stderr.write(f"[SCHEDULER] Failed to initialize: {e}\n")
        return None


def _log_desk_data() -> None:
    """One-line summary of scan completion (reduced from full JSON dump)."""
    try:
        radar_ts = {}
        pos_count = {}
        for tf in ("1h", "4h", "1d"):
            fp = os.path.join(OUT_DIR, f"gc_radar_{tf}.json")
            try:
                with open(fp) as f:
                    data = json.load(f)
                    radar_ts[tf] = data.get("ts", "")[:19]
                    rows = data.get("rows") or []
                    pos_count[tf] = len(rows)
            except Exception:
                radar_ts[tf] = "error"
                pos_count[tf] = 0
        sys.stderr.write(
            f"[DESK_DATA] {datetime.now(timezone.utc).isoformat()[:19]} "
            f"radar_1h={pos_count['1h']}@{radar_ts['1h']} "
            f"radar_4h={pos_count['4h']}@{radar_ts['4h']} "
            f"radar_1d={pos_count['1d']}@{radar_ts['1d']}\n"
        )
    except Exception as e:
        sys.stderr.write(f"[DESK_DATA] error: {e}\n")


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
        if path == "/api/ai/candidates":
            self._ai_candidates(parsed.query)
            return
        if path == "/api/public/radar":
            self._public_radar()
            return
        if path == "/api/scheduler/status":
            self._scheduler_status()
            return
        if self._need_auth():
            return
        if path == "/api/desk-data":
            self._desk_data()
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
        
        # 5) Scheduler status (if enabled)
        if SCHEDULER_ENABLED:
            result["scheduler"] = _get_scheduler_status()
        
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
        
        # Enhance candidates with suggested size/leverage
        candidates = candidates_data.get("candidates", [])
        enhanced = []
        
        for c in candidates:
            tier = c.get("tier", "unknown")
            trend_1d = c.get("trend_1d", "")
            trend_4h = c.get("trend_4h", "")
            sl_dist_pct = c.get("hard_sl_dist_pct", 0)
            
            # Suggested size % (simplified: Base 6%, Chase 8%, Continuation 3%)
            entry_type = c.get("type", "Base")
            if entry_type == "Continuation":
                suggested_size_pct = 3.0
            elif entry_type == "Chase":
                suggested_size_pct = 8.0
            else:
                suggested_size_pct = 6.0
            
            # Suggested leverage (conservative: 2-3x)
            suggested_leverage = 2.5
            
            # Estimated liquidation price (rough)
            close_1d = c.get("close_1d", 0)
            if close_1d and account.get("equity"):
                equity = account["equity"]
                size_usd = equity * (suggested_size_pct / 100.0)
                liq_estimate = close_1d * (1 - (equity - size_usd / suggested_leverage) / size_usd)
            else:
                liq_estimate = None
            
            enhanced.append({
                **c,
                "suggested_size_pct": suggested_size_pct,
                "suggested_leverage": suggested_leverage,
                "estimated_liq_price": liq_estimate,
            })
        
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

    def _ai_decision(self) -> None:
        """POST /api/ai/decision: store AI approval/veto decisions.
        
        Auth: header X-AI-Key or query param key.
        Body: {decisions: [{symbol, decision: approve|veto, size_pct, leverage, reason}, ...]}
        
        Clamps size/leverage to SoT bands server-side.
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
        
        try:
            result = store_decisions(decisions)
            self._send_json(200, result)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

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
        finally:
            _scan_lock.release()
        if ok:
            _log_desk_data()  # emit updated desk data after manual rescan
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
