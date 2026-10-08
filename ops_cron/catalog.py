"""Dispatch for the jobs that are not the original exit-monitor / daily-audit rule set."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Mapping

from . import bo_report, desk_missing, harbor, river_jobs, sources

JOBS = ("desk-missing", "harbor-pnl", "bo-report", "c48-scoreboard", "desk-veto", "trade-journal")


def make_sources(env: Mapping[str, str], testnet: bool = False) -> sources.Sources:
    timeout = float(env.get("OPS_HTTP_TIMEOUT_S") or 20)
    if testnet:
        addr = sources.normalize_hl_address(env.get("TESTNET_WALLET_ADDRESS") or "")
        url = env.get("HL_TESTNET_INFO_URL") or "https://api.hyperliquid-testnet.xyz/info"
    else:
        addr = sources.normalize_hl_address(env.get("HL_ADDRESS") or "")
        url = env.get("HL_INFO_URL") or "https://api.hyperliquid.xyz/info"
    return sources.Sources(
        env.get("COCKPIT_URL") or "", env.get("COCKPIT_AI_KEY") or "", addr, url, timeout,
        bx_base=env.get("BX_PUBLIC_URL") or "https://fapi.bitunix.com")


def fetch(job: str, env: Mapping[str, str], now: datetime) -> Dict[str, Any]:
    if job == "c48-scoreboard":
        return river_jobs.fetch_scoreboard(make_sources(env, testnet=True), now, dict(env))
    src = make_sources(env, testnet=False)
    if job == "desk-missing":
        return desk_missing.fetch(src, now, dict(env))
    if job == "harbor-pnl":
        return harbor.fetch(src, now, dict(env))
    if job == "bo-report":
        return bo_report.fetch(src, now, dict(env))
    if job == "desk-veto":
        return river_jobs.fetch_veto(src, now, dict(env))
    if job == "trade-journal":
        return river_jobs.fetch_journal(src, now, dict(env))
    raise ValueError(job)


def build(job: str, inputs: Dict[str, Any], now: datetime, env: Mapping[str, str]) -> Dict[str, Any]:
    e = dict(env)
    if job == "desk-missing":
        return desk_missing.build(inputs, now, e)
    if job == "harbor-pnl":
        return harbor.build(inputs, now, e)
    if job == "bo-report":
        return bo_report.build(inputs, now, e)
    if job == "c48-scoreboard":
        return river_jobs.build_scoreboard(inputs, now, e)
    if job == "desk-veto":
        return river_jobs.build_veto(inputs, now, e)
    if job == "trade-journal":
        return river_jobs.build_journal(inputs, now, e)
    raise ValueError(job)


def load_fixture(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
