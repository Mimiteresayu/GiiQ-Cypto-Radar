#!/usr/bin/env python3
"""Egress gate for the Bitunix live pilot (MMT-approved 2026-09-30, prerequisite 1).

Bitunix restricts US users, so the BX execution service must run in a non-US Railway region and must refuse
to trade from a US IP. check() asks two independent geo services for the egress IP's country, and also looks
at the Railway region. The result is fail-closed:

  ok=True only when BOTH geo sources answer, BOTH say a non-US country, they agree on the IP when both
  report one, and the Railway region (if set) is not a us-* region. Anything else (US, unknown, a source
  down, a disagreement) -> ok=False with the reason. If BX_EXPECTED_EGRESS_IP is set (the static outbound
  IP(s) that the Bitunix key is whitelisted to; comma-separated for Railway HA static IPs), every IP the geo
  sources report must be one of them. With HA static IPs the two sources may see two different IPs of the
  set; that is allowed only when both are in the whitelisted set.

The service logs one [BX_EGRESS] line at startup and re-checks before every live run (cached CACHE_S).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

SOURCES = (("ipinfo", "https://ipinfo.io/json"), ("country_is", "https://api.country.is/"))
BLOCKED_COUNTRIES = {"US", "UM", "PR", "VI", "GU", "AS", "MP"}   # US and its territories
CACHE_S = 900
TIMEOUT_S = 8.0

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def _fetch(url: str, opener=None) -> dict:
    op = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "own-trend-radar-bx/1"})
    with op(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_expected(expected_ip: Optional[str]) -> set:
    """'1.2.3.4, 1.2.3.5' -> {'1.2.3.4', '1.2.3.5'}; empty/None -> empty set (no IP pinning)."""
    return {p.strip() for p in str(expected_ip or "").split(",") if p.strip()}


def evaluate(answers: Dict[str, dict], region: Optional[str], expected_ip: Optional[str] = None) -> Dict[str, Any]:
    """Pure decision from the two geo answers + Railway region. -> {ok, ip, countries, region, reason}."""
    countries = {k: str((v or {}).get("country") or "").upper() or None for k, v in answers.items()}
    ips = {k: (v or {}).get("ip") for k, v in answers.items()}
    ip_vals = {i for i in ips.values() if i}
    expected = parse_expected(expected_ip)
    ip = next(iter(ip_vals)) if len(ip_vals) == 1 else (",".join(sorted(ip_vals)) or None)
    out = {"ok": False, "ip": ip, "countries": countries, "region": region, "reason": ""}
    if len(answers) < len(SOURCES) or any(c is None for c in countries.values()):
        out["reason"] = "egress country unknown (a geo source failed)"
    elif expected and not ip_vals:
        out["reason"] = "egress IP unknown (no geo source reported it)"
    elif expected and not ip_vals <= expected:
        out["reason"] = f"egress IP {sorted(ip_vals - expected)} is not in the whitelisted static IPs {sorted(expected)}"
    elif len(ip_vals) > 1 and not expected:
        out["reason"] = f"geo sources disagree on the egress IP {sorted(ip_vals)}"
    elif any(c in BLOCKED_COUNTRIES for c in countries.values()):
        out["reason"] = f"egress IP is in the US ({countries})"
    elif len(set(countries.values())) > 1:
        out["reason"] = f"geo sources disagree on the country {countries}"
    elif region and str(region).lower().startswith("us-"):
        out["reason"] = f"Railway region {region} is in the US"
    else:
        out["ok"] = True
        out["reason"] = "non-US egress verified"
    return out


def check(force: bool = False, opener=None, now: Callable[[], float] = time.time) -> Dict[str, Any]:
    with _lock:
        if not force and _cache.get("res") and now() - _cache.get("t", 0) < CACHE_S:
            return _cache["res"]
    answers: Dict[str, dict] = {}
    errors = {}
    for name, url in SOURCES:
        try:
            answers[name] = _fetch(url, opener)
        except Exception as e:  # noqa: BLE001 - any failure is "unknown" -> fail closed
            errors[name] = f"{type(e).__name__}: {str(e)[:80]}"
    res = evaluate({k: v for k, v in answers.items()}, os.environ.get("RAILWAY_REPLICA_REGION"),
                   os.environ.get("BX_EXPECTED_EGRESS_IP") or None)
    if errors:
        res["source_errors"] = errors
        if res["ok"]:  # cannot happen (missing source -> unknown), kept as a belt-and-braces guard
            res["ok"], res["reason"] = False, "geo source error"
    res["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))
    with _lock:
        _cache.update(res=res, t=now())
    return res


def log_line(res: Dict[str, Any]) -> str:
    return (f"[BX_EGRESS] ok={res.get('ok')} ip={res.get('ip')} countries={res.get('countries')} "
            f"region={res.get('region')} reason={res.get('reason')}")
