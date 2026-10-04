"""ops_cron entrypoint (Railway cron service). READ-ONLY: GETs cockpit endpoints with the X-AI-Key read key and
reads the public Hyperliquid info API; never places / modifies / cancels orders or changes env / feature flags.

  python -m ops_cron exit-monitor [--dry-run] [--fixture FILE] [--now ISO]
  python -m ops_cron daily-audit  [--dry-run] [--fixture FILE] [--now ISO]

Normal run: stdout = exactly one JSON line (the report); alerts only on `problem` (plus the 20:xx HKT daily OK
summary for the exit monitor); INSERT into raw.ops_check_run when BRAIN_DSN is set.
--dry-run: same data reads, prints the markdown + pretty JSON report, sends nothing and writes nothing.
--fixture: read inputs from a JSON file {"now": ISO, "inputs": {...}} instead of the network (offline).
Exit code 0 for ok and problem; 1 only if the script itself crashed (an alert is attempted).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from . import alerts, persist, rules, sources

CHECKS = ("exit-monitor", "daily-audit")


def _env_bool(env: Mapping[str, str], name: str, default: Optional[bool]) -> Optional[bool]:
    v = (env.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def config(env: Mapping[str, str]) -> Dict[str, Any]:
    return {
        "bx_enabled": _env_bool(env, "OPS_BX_ENABLED", True),
        "lookback_min": int(env.get("OPS_LOOKBACK_MIN") or 65),
        "daily_summary_hour_hkt": int(env.get("OPS_DAILY_SUMMARY_HOUR_HKT") or 20),
        "expect_bx_live": _env_bool(env, "OPS_EXPECT_BX_LIVE", None),
        "slippage_max_bp": float(env.get("OPS_SLIPPAGE_MAX_BP") or 60),
        "bx_country": (env.get("OPS_BX_EXPECTED_COUNTRY") or "SG").strip().upper(),
        "bx_region_prefix": (env.get("OPS_BX_EXPECTED_REGION_PREFIX") or "asia-southeast1").strip(),
        "window_h": int(env.get("OPS_AUDIT_WINDOW_H") or 24),
        "audit_send_ok": _env_bool(env, "OPS_AUDIT_SEND_OK", False),
        "timeout_s": float(env.get("OPS_HTTP_TIMEOUT_S") or 20),
    }


def _parse_now(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def build_report(check: str, env: Mapping[str, str], fixture: Optional[str] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    cfg = config(env)
    if fixture:
        with open(fixture, encoding="utf-8") as f:
            fx = json.load(f)
        inputs = fx["inputs"]
        now = now or _parse_now(fx.get("now")) or sources.utcnow()
    else:
        now = now or sources.utcnow()
        src = sources.Sources(env.get("COCKPIT_URL") or "", env.get("COCKPIT_AI_KEY") or "",
                              env.get("HL_ADDRESS") or "", env.get("HL_INFO_URL") or "https://api.hyperliquid.xyz/info",
                              cfg["timeout_s"])
        if check == "exit-monitor":
            inputs = sources.exit_monitor_inputs(src, now, cfg["lookback_min"], cfg["bx_enabled"])
        else:
            inputs = sources.daily_audit_inputs(src, now, cfg["bx_enabled"], cfg["window_h"])
    rep = rules.exit_monitor(inputs, now, cfg) if check == "exit-monitor" else rules.daily_audit(inputs, now, cfg)
    if check == "daily-audit" and rep["notify"] is None and cfg["audit_send_ok"]:
        rep["notify"] = "daily_ok"
    rep["run_id"] = str(uuid.uuid4())
    return rep


def _subject(rep: Dict[str, Any]) -> str:
    name = "exit monitor" if rep["check"] == "exit_monitor" else "daily audit"
    tag = "PROBLEM" if rep["status"] == "problem" else "OK"
    return f"[ops] {tag} {name} {rep['run_at_hkt']} HKT: {rep['summary']}"[:200]


def run(check: str, env: Mapping[str, str], dry_run: bool = False, fixture: Optional[str] = None,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    rep = build_report(check, env, fixture, now)
    md = rules.to_markdown(rep)
    rep["dry_run"] = dry_run
    if dry_run:
        rep["alerted"] = False
        sys.stdout.write(md + "\n```json\n" + json.dumps(rep, indent=1, default=str, ensure_ascii=False) + "\n```\n")
        return rep
    rep["alerted"] = False
    if rep.get("notify"):
        rep["alert"] = alerts.send(_subject(rep), md, env, timeout=config(env)["timeout_s"])
        rep["alerted"] = any(v == "sent" for k, v in rep["alert"].items() if k != "log")
    sys.stderr.write(md)
    dsn = (env.get("BRAIN_DSN") or "").strip()
    rep["persisted"] = persist.insert(rep, dsn) if dsn else "skipped (BRAIN_DSN not set)"
    if dsn and rep["persisted"] != "ok":
        sys.stderr.write(f"[OPS] Brain insert failed: {rep['persisted']}\n")
    persist.emit(rep)
    return rep


def main(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ops_cron", description=__doc__.split("\n")[0])
    ap.add_argument("check", choices=CHECKS)
    ap.add_argument("--dry-run", action="store_true", help="print the report; send no alert, write nothing")
    ap.add_argument("--fixture", help="read inputs from a fixture JSON file instead of the network")
    ap.add_argument("--now", help="override the check time (ISO 8601)")
    a = ap.parse_args(argv)
    env = env if env is not None else os.environ
    try:
        run(a.check, env, dry_run=a.dry_run, fixture=a.fixture, now=_parse_now(a.now))
        return 0
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=5)
        sys.stderr.write(f"[OPS] crashed: {tb}\n")
        crash = {"check": a.check.replace("-", "_"), "run_at": datetime.now(timezone.utc).isoformat(),
                 "status": "problem", "summary": f"ops_cron crashed: {type(e).__name__}: {str(e)[:160]}",
                 "problems": [{"code": "OPS_CRASH", "coin": None, "msg": str(e)[:300], "source": "ops_cron"}],
                 "run_id": str(uuid.uuid4()), "dry_run": a.dry_run}
        if not a.dry_run:
            crash["alert"] = alerts.send(f"[ops] PROBLEM {a.check}: {crash['summary']}"[:200], tb, env)
            persist.emit(crash)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
