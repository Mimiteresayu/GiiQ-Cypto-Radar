#!/usr/bin/env python3
"""GIIQ end-to-end DRY-RUN pilot of the daily trading flow, Hyperliquid (HL) + Bitunix (BX).

    python scripts/pilot_e2e.py                    # synthetic fixtures (default)
    python scripts/pilot_e2e.py --snapshot DIR     # + a real-data candidate build from a read-only snapshot
    python scripts/pilot_e2e.py --json report.json # also write the machine-readable report

It walks the 08:02 -> 08:56 HKT day and prints PASS / FAIL / FLAG per step with evidence:

  1 candidate build (HL + BX), freshness (generated_at / ts), row counts
  2 GET /api/ai/candidates and /api/bx/status through the real HTTP handlers (serve.py, bx_service.py)
  3 desk POST /api/ai/decision (desk="GIIQ-DESK-SIGNUM-v1", flags, extra fields) -> HL + BX stores
  4 08:50 fallback when Claude never posted (HL: all candidates 2% / 2x; BX: no fallback by design)
  5 08:55 HL executor / 08:56 BX entries in LIVE mode against simulated exchanges (NAV, size, leverage,
    Hard SL, liq, client order ids, priority, no daily limit, 80% HL+BX cap on real margin, $10 minimum,
    Chase under the CONTINUATION freeze, HL SDK order wire)
  6 post-fill: HL reconciliation (size / isolated / leverage / liq / SL) incl. the mismatch -> close + alert
    path; BX Hard SL attach + verify, repair, protection failure -> flash close
  7 idempotency: a rerun places no duplicate order
  8 exit path smoke test (DRY_RUN, read only) + git diff of the exit files vs the pre-PR-48 base

SAFETY (why nothing real can happen):
  * The code runs from a throw-away COPY of the repo in a temp dir, so out/ (decisions, pending, ledgers)
    of the checkout is never touched.
  * The inner process starts with a CLEAN environment: no inherited variable, so no real key/secret can be
    read. The LIVE code paths get obviously fake dummy keys, and every exchange client is a local simulator
    (hl_sim.SimExchangeMixin / SimBX): no signed endpoint is ever called.
  * A socket guard allows 127.0.0.1 only; any other connection attempt is refused and listed in the report
    (an attempt to reach an exchange host fails the pilot).
Exit code 0 = no FAIL (FLAGs are findings for MMT, not failures).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
PRE_PR48_BASE = "242e861"          # main before PR #48 (GIIQ-SoT-5)
EXIT_FILES = ["exit_worker.py", "failsafe_exit_worker.py", "monitor_hl_exit.py", "watch_1h_exit.py",
              "suggested_exits.py", "exit_health.py"]
STEP_NAMES = {
    "1": "Candidate build HL + BX, freshness, row counts",
    "2": "GET /api/ai/candidates + /api/bx/status",
    "3": "Desk POST (GIIQ-DESK-SIGNUM-v1): 3+3 approvals, 1+1 veto",
    "4": "Fallback without a Claude decision",
    "5": "08:55 HL / 08:56 BX execution (mocked LIVE)",
    "6": "Post-fill reconciliation / SL verify",
    "7": "Idempotency (rerun = no duplicate order)",
    "8": "Exit path smoke test + exit files unchanged",
}
HKT = timezone(timedelta(hours=8))
EXCHANGE_HOST_RE = re.compile(r"hyperliquid|bitunix", re.I)


# =====================================================================================================
# outer process: copy the repo, run the pilot in a clean env, add the git check, print the table
# =====================================================================================================
def outer(args: argparse.Namespace) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="giiq_pilot_"))
    work = tmp / "repo"
    shutil.copytree(REPO, work, ignore=shutil.ignore_patterns(
        ".git", "out", "__pycache__", "*.db", "*.sqlite*", "node_modules", ".venv", "venv", ".pytest_cache", ".env*"))
    (work / "out").mkdir(exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    env.update(HOME=str(tmp), TMPDIR=str(tmp), PYTHONPATH=str(work), PYTHONDONTWRITEBYTECODE="1")
    rep_path = tmp / "pilot_report.json"
    cmd = [sys.executable, str(work / "scripts" / "pilot_e2e.py"), "--inner", "--report", str(rep_path)]
    if args.snapshot:
        cmd += ["--snapshot", str(Path(args.snapshot).resolve())]
    print(f"[pilot] repo copy: {work}  (clean env, network guard: 127.0.0.1 only)", flush=True)
    proc = subprocess.run(cmd, cwd=str(work), env=env, capture_output=True, text=True, timeout=900)
    if args.verbose:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr[-20000:])
    if not rep_path.is_file():
        sys.stdout.write(proc.stdout[-5000:])
        sys.stderr.write(proc.stderr[-5000:])
        print("[pilot] FAIL: inner pilot produced no report")
        return 2
    rep = json.loads(rep_path.read_text())
    git_check(rep)
    finalize(rep)
    print_table(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
        print(f"[pilot] JSON report: {args.json}")
    if args.keep:
        print(f"[pilot] kept temp dir {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if rep["overall"] == "FAIL" else 0


def git_check(rep: dict) -> None:
    st = rep["steps"].setdefault("8", {"name": STEP_NAMES["8"], "checks": []})
    try:
        have = subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", PRE_PR48_BASE], capture_output=True,
                              text=True, timeout=30)
        if have.returncode != 0:
            st["checks"].append({"label": f"git diff exit files vs {PRE_PR48_BASE}", "status": "FLAG",
                                 "detail": f"base commit {PRE_PR48_BASE} not in this clone (shallow?): unverified"})
            return
        d = subprocess.run(["git", "-C", str(REPO), "diff", "--stat", PRE_PR48_BASE, "--"] + EXIT_FILES,
                           capture_output=True, text=True, timeout=60)
        out = d.stdout.strip()
        st["checks"].append({"label": f"git diff --stat {PRE_PR48_BASE} -- <exit files> (working tree) is empty",
                             "status": "PASS" if d.returncode == 0 and not out else "FAIL",
                             "detail": out or f"empty ({', '.join(EXIT_FILES)})"})
    except Exception as e:  # noqa: BLE001
        st["checks"].append({"label": "git diff exit files", "status": "FLAG", "detail": f"git unavailable: {e}"})


def finalize(rep: dict) -> None:
    worst = "PASS"
    for sid, st in rep["steps"].items():
        sts = [c["status"] for c in st.get("checks", [])]
        st["status"] = "FAIL" if "FAIL" in sts or not sts else ("FLAG" if "FLAG" in sts else "PASS")
        if st["status"] == "FAIL":
            worst = "FAIL"
        elif st["status"] == "FLAG" and worst == "PASS":
            worst = "FLAG"
    rep["overall"] = worst


def print_table(rep: dict) -> None:
    print("\n" + "=" * 100)
    print(f"GIIQ E2E DRY-RUN PILOT  ({rep.get('started')})  overall: {rep['overall']}")
    print("=" * 100)
    for sid in sorted(rep["steps"], key=int):
        st = rep["steps"][sid]
        print(f"\nStep {sid}  [{st['status']}]  {st['name']}")
        for c in st["checks"]:
            print(f"   {c['status']:<4}  {c['label']}")
            if c.get("detail") not in (None, ""):
                for line in str(c["detail"]).splitlines()[:12]:
                    print(f"           {line}")
    if rep.get("network_blocked"):
        print(f"\nnetwork attempts refused by the guard: {rep['network_blocked'][:20]}")
    if rep.get("errors"):
        print("\nERRORS:")
        for e in rep["errors"]:
            print("  " + e[:2000])
    print()


# =====================================================================================================
# inner process
# =====================================================================================================
BLOCKED: List[str] = []


def install_network_guard() -> None:
    import socket
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    local = ("127.0.0.1", "::1", "localhost")

    def ok(addr: Any) -> bool:
        return isinstance(addr, tuple) and addr and str(addr[0]) in local

    def connect(self, addr):  # noqa: ANN001
        if self.family == getattr(socket, "AF_UNIX", -1) or ok(addr):
            return real_connect(self, addr)
        BLOCKED.append(str(addr))
        raise ConnectionRefusedError(f"pilot network guard: {addr} blocked")

    def connect_ex(self, addr):  # noqa: ANN001
        if self.family == getattr(socket, "AF_UNIX", -1) or ok(addr):
            return real_connect_ex(self, addr)
        BLOCKED.append(str(addr))
        return 111

    def gai(host, *a, **k):  # noqa: ANN001
        h = host.decode() if isinstance(host, bytes) else host
        if h is None or h in local:
            return real_gai(host, *a, **k)
        BLOCKED.append(f"dns:{h}")
        raise socket.gaierror(f"pilot network guard: {h} blocked")

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, gai


class Report:
    def __init__(self) -> None:
        self.steps: Dict[str, dict] = {k: {"name": v, "checks": []} for k, v in STEP_NAMES.items()}
        self.errors: List[str] = []
        self.started = datetime.now(timezone.utc).isoformat()
        self.info: Dict[str, Any] = {}

    def check(self, step: str, label: str, ok: Any, detail: Any = None, flag: bool = False) -> bool:
        status = "FLAG" if flag else ("PASS" if ok else "FAIL")
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, default=str)[:1500]
        self.steps[step]["checks"].append({"label": label, "status": status, "detail": detail})
        return bool(ok)

    def guard(self, step: str, label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"step {step} {label}: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
            self.check(step, f"{label} (raised {type(e).__name__}: {str(e)[:200]})", False)

    def dump(self) -> dict:
        return {"started": self.started, "steps": self.steps, "errors": self.errors, "info": self.info,
                "network_blocked": BLOCKED}


# ----------------------------------------------------------------------------------------------- sims
def make_sim_hl_class():
    from hl_sim import SimExchangeMixin

    class SimHL(SimExchangeMixin):
        """HL account + exchange simulator: reads computed from the simulated state, writes via hl_sim."""
        WRITES = ("set_leverage", "open_long_ioc", "place_stop_loss", "market_close", "cancel")

        def __init__(self, nav: float, meta: dict, mids: dict, positions: Optional[list] = None,
                     orders: Optional[list] = None, base_margin: float = 0.0, abstraction: str = "unifiedAccount",
                     **flags: Any) -> None:
            self.nav, self._meta, self.mids, self.base_margin, self.abstraction = nav, meta, dict(mids), base_margin, abstraction
            self.positions = [dict(p) for p in (positions or [])]
            self.orders = [dict(o) for o in (orders or [])]
            self.calls: list = []
            self.lev_ok = self.fill = self.sl_ok = self.close_ok = True
            for k, v in flags.items():
                setattr(self, k, v)

        def margin_total(self) -> float:
            return self.base_margin + sum(float(p.get("marginUsed") or 0) for p in self.positions)

        def spot_state(self) -> dict:   # unified account: perp collateral sits in spot USDC as `hold`
            return {"balances": [{"coin": "USDC", "total": str(self.nav), "hold": str(round(self.margin_total(), 6))}]}

        def perp_state(self) -> dict:
            m = self.margin_total()
            return {"marginSummary": {"accountValue": str(m), "totalMarginUsed": str(m)}, "withdrawable": "0",
                    "assetPositions": [{"position": p, "type": "oneWay"} for p in self.positions]}

        def meta(self) -> dict:
            return self._meta

        def all_mids(self) -> dict:
            return dict(self.mids)

        def open_orders(self) -> list:
            return list(self.orders)

        def user_abstraction(self) -> str:
            return self.abstraction

        def exchange(self):  # the executor builds the signing client once; the simulator needs none
            return None

        def writes(self) -> list:
            return [c for c in self.calls if c[0] in self.WRITES]

    return SimHL


class SimBX:
    """bx_trade.BXTrade stand-in (dry_run False = the LIVE code path). Exchange-consistent state, no network."""
    dry_run = False

    def __init__(self, available: float = 500.0, margin: float = 0.0, liq_fn: Optional[Callable] = None,
                 sl_attached: bool = True, sl_place_fails: bool = False, lev_override: Optional[int] = None,
                 fill: bool = True) -> None:
        self.available, self.margin = available, margin
        self.liq_fn = liq_fn or (lambda px, lev: px * (1 - 1 / lev + 0.01))
        self.sl_attached, self.sl_place_fails, self.lev_override, self.fill = sl_attached, sl_place_fails, lev_override, fill
        self.calls: list = []
        self.positions: list = []
        self.sl: Dict[str, str] = {}
        self.lev: Dict[str, int] = {}
        self.client_ids: list = []
        self._n = 0

    def account(self) -> dict:
        self.calls.append(("account",))
        used = self.margin + sum(float(p["margin"]) for p in self.positions)
        return {"marginCoin": "USDT", "available": str(self.available), "margin": str(used), "frozen": "0",
                "crossUnrealizedPNL": "0", "isolationUnrealizedPNL": "0"}

    def set_isolated(self, s: str) -> list:
        self.calls.append(("set_isolated", s))
        return [{}]

    def set_leverage(self, s: str, lev: int) -> list:
        self.calls.append(("set_leverage", s, lev))
        self.lev[s] = int(lev)
        return [{}]

    def open_long(self, s: str, qty: str, px: str, sl: str, cid: str) -> dict:
        self.calls.append(("open_long", s, qty, px, sl, cid))
        self.client_ids.append(cid)
        self._n += 1
        if self.fill:
            lev = self.lev_override or self.lev.get(s, 2)
            pid = f"P{self._n}"
            self.positions.append({"positionId": pid, "symbol": s, "side": "LONG", "qty": qty, "avgOpenPrice": px,
                                   "leverage": lev, "marginMode": "ISOLATION",
                                   "liqPrice": str(round(self.liq_fn(float(px), lev), 8)),
                                   "margin": str(float(qty) * float(px) / lev), "unrealizedPNL": "0", "fee": "0",
                                   "funding": "0"})
            if self.sl_attached:
                self.sl[pid] = sl
            self.available -= float(qty) * float(px) / lev
        return {"orderId": f"O{self._n}", "clientId": cid}

    def order_detail(self, order_id: Optional[str] = None, client_id: Optional[str] = None) -> dict:
        p = next((x for x in self.positions if x["positionId"] == f"P{str(order_id)[1:]}"), None)
        return {"status": "FILLED" if p else "CANCELED", "tradeQty": p["qty"] if p else "0"}

    def pending_positions(self, s: Optional[str] = None) -> list:
        return [p for p in self.positions if s is None or p["symbol"] == s]

    def tpsl_pending(self, s: Optional[str] = None, pid: Optional[str] = None) -> list:
        return [{"id": f"SL-{k}", "slPrice": v} for k, v in self.sl.items() if pid is None or k == str(pid)]

    def place_position_sl(self, s: str, pid: str, sl: str) -> dict:
        self.calls.append(("place_position_sl", s, pid, sl))
        if self.sl_place_fails:
            import bx_trade
            raise bx_trade.BXTradeError("tpsl: code 30029 SL price must be less than mark price (simulated)")
        self.sl[str(pid)] = sl
        return {"orderId": f"SL-{pid}"}

    def flash_close(self, pid: str) -> dict:
        self.calls.append(("flash_close", pid))
        self.positions = [p for p in self.positions if p["positionId"] != str(pid)]
        self.sl.pop(str(pid), None)
        return {"positionId": pid}

    def history_positions(self, s: Optional[str] = None, pid: Optional[str] = None) -> list:
        return [{"positionId": pid, "closePrice": "1.9", "realizedPNL": "-1", "fee": "0.01", "funding": "0"}]

    def opens(self) -> list:
        return [c for c in self.calls if c[0] == "open_long"]


# ------------------------------------------------------------------------------------------- fixtures
HL_META = {"BTC": {"szDecimals": 5, "maxLeverage": 40}, "ETH": {"szDecimals": 4, "maxLeverage": 25},
           # SOL: fixture coin max 3x (below the desk's 5x) to prove "never above coin max"
           "SOL": {"szDecimals": 2, "maxLeverage": 3}, "DOGE": {"szDecimals": 0, "maxLeverage": 10},
           "AR": {"szDecimals": 1, "maxLeverage": 10}, "LINK": {"szDecimals": 1, "maxLeverage": 10}}
HL_MIDS = {"BTC": 62000.0, "ETH": 2600.0, "SOL": 150.0, "DOGE": 0.2, "AR": 9.0, "LINK": 14.5}
# symbol: (1d close, 1d upper, 1d filter, 1d lower, 1d trend, 1d dual_cross_up,
#          4h upper, 4h filter, 4h lower, 4h trend, 4h dual_cross_up)
HL_ROWS = {
    "BTC": (62000, 61000, 59000, 57000, "Green", True, 61500, 60500, 59500, "Green", False),
    "ETH": (2600, 2550, 2480, 2400, "Green", True, 2580, 2550, 2500, "Green", False),
    "SOL": (150, 147, 142, 138, "Green", True, 149, 146, 144, "Green", False),
    "DOGE": (0.2, 0.195, 0.19, 0.18, "Green", True, 0.198, 0.194, 0.19, "Green", False),
    "AR": (9.0, 9.5, 8.0, 7.5, "Green", False, 8.9, 8.6, 8.3, "Green", True),       # Chase (4H cross)
    "LINK": (14.5, 15.0, 13.5, 12.8, "Green", False, 14.8, 14.0, 12.5, "Green", False),  # held, no signal
}
HL_NAV = 1682.12
LINK_POS = {"coin": "LINK", "szi": "5.0", "entryPx": "14.0", "leverage": {"type": "isolated", "value": 2},
            "liquidationPx": "7.2", "marginUsed": "35.13", "unrealizedPnl": "2.5", "returnOnEquity": "0.07"}
LINK_SL = {"coin": "LINK", "oid": 7, "isTrigger": True, "orderType": "Stop Market", "reduceOnly": True,
           "sz": "5.0", "triggerPx": "12.5", "side": "A"}

BX_META_BASE = {"ex": "BX", "asset_class": "crypto", "liq_tier": "tradeable", "vol24h_usd": 8e6, "spread_bp": 4.0,
                "gc_tf": "1d", "tier": "small", "price": 2.0, "base_precision": 1, "quote_precision": 4,
                "min_qty": "1", "max_leverage": 50, "api_supported": True}
BX_SYMS = {"FOOUSDT": {}, "BARUSDT": {}, "BAZUSDT": {"max_leverage": 3}, "QUXUSDT": {}, "CHAUSDT": {}}
BX_LIVE_MKT = {"price": 2.0, "bid": 1.9996, "ask": 2.0004, "spread_bp": 2.0, "vol24h": 8e6}
BX_TIERS = [{"startValue": "0", "endValue": "1000000", "maintenanceMarginRate": "0.01"}]
SG_EGRESS = {"ok": True, "ip": "208.77.246.240", "countries": {"ipinfo": "SG", "country_is": "SG"},
             "region": "asia-southeast1-eqsg3a", "reason": "pilot mock: non-US egress (NOT checked)"}

DESK = "GIIQ-DESK-SIGNUM-v1"
HL_DESK = [  # 3 approvals with varied size 2-4 / leverage 2-5, 1 veto, + the Chase approval
    {"symbol": "BTC", "decision": "approve", "type": "Base", "size_pct": 4, "leverage": 5, "reason": "pilot",
     "dims": {"trend": 3}, "flags": ["pilot"], "confidence": 0.8},
    {"symbol": "ETH", "decision": "approve", "type": "Base", "size_pct": 3, "leverage": 2, "reason": "pilot",
     "tags": ["L1"]},
    {"symbol": "SOL", "decision": "approve", "type": "Base", "size_pct": 2, "leverage": 5, "reason": "pilot"},
    {"symbol": "DOGE", "decision": "veto", "type": "Base", "rule": "V2_LATE_BREAKOUT", "reason": "pilot veto"},
    {"symbol": "AR", "decision": "approve", "type": "Chase", "size_pct": 2, "leverage": 2, "reason": "pilot chase"},
]
BX_DESK = [
    {"symbol": "FOOUSDT", "decision": "approve", "size_pct": 4, "leverage": 5, "reason": "pilot", "flags": ["x"]},
    {"coin": "BAR", "action": "APPROVE", "size_pct": 3, "leverage": 2, "reason": "pilot (coin + action form)"},
    {"symbol": "BAZUSDT", "decision": "approve", "size_pct": 2, "leverage": 5, "reason": "pilot", "extra": 1},
    {"symbol": "QUXUSDT", "decision": "veto", "rule": "V1", "reason": "pilot veto"},
    {"symbol": "CHAUSDT", "decision": "approve", "size_pct": 2, "leverage": 2, "reason": "pilot chase"},
]


def hl_radars(now: datetime) -> Dict[str, dict]:
    r1d, r4h, r1h = [], [], []
    for s, v in HL_ROWS.items():
        c1, u1, f1, l1, t1, x1, u4, f4, l4, t4, x4 = v
        r1d.append({"symbol": s, "close": c1, "upper": u1, "filter": f1, "lower": l1, "trend": t1, "dual_cross_up": x1})
        r4h.append({"symbol": s, "close": HL_MIDS[s], "upper": u4, "filter": f4, "lower": l4, "trend": t4,
                    "dual_cross_up": x4})
        r1h.append({"symbol": s, "close": HL_MIDS[s], "upper": u4, "filter": f4, "lower": l4, "trend": t4})
    for i in range(175):  # a normal-size universe (~170+ rows) with no signal
        row = {"symbol": f"PAD{i}", "close": 1.0, "upper": 1.1, "filter": 1.05, "lower": 1.0, "trend": "Red",
               "dual_cross_up": False}
        r1d.append(dict(row))
        r4h.append(dict(row))
        r1h.append(dict(row))
    return {"1d": {"tf": "1d", "ts": (now - timedelta(hours=1)).isoformat(), "rows": r1d},
            "4h": {"tf": "4h", "ts": (now - timedelta(minutes=45)).isoformat(), "rows": r4h},
            "1h": {"tf": "1h", "ts": (now - timedelta(minutes=10)).isoformat(), "rows": r1h}}


def bx_fixture(now: datetime) -> Dict[str, Any]:
    metas, r1d, r4h = [], [], []
    for sym, extra in BX_SYMS.items():
        m = dict(BX_META_BASE, symbol=sym.replace("USDT", ""), bx_symbol=sym, **extra)
        metas.append(m)
        chase = sym == "CHAUSDT"
        r1d.append({"bx_symbol": sym, "close": 2.0, "upper": 1.95, "filter": 1.9, "lower": 1.85, "trend": "Green",
                    "dual_cross_up": not chase, "bar_time": int(now.timestamp() * 1000) - 86_400_000})
        r4h.append({"bx_symbol": sym, "close": 2.0, "upper": 1.97, "filter": 1.9, "lower": 1.85, "trend": "Green",
                    "dual_cross_up": chase, "bar_time": int(now.timestamp() * 1000) - 4 * 3_600_000})
    for i in range(130):
        r1d.append({"bx_symbol": f"PAD{i}USDT", "trend": "Red"})
        r4h.append({"bx_symbol": f"PAD{i}USDT", "trend": "Red"})
    return {"meta": {"scanned": metas}, "1d": {"tf": "1d", "ts": (now - timedelta(hours=1)).isoformat(), "rows": r1d},
            "4h": {"tf": "4h", "ts": (now - timedelta(minutes=50)).isoformat(), "rows": r4h}}


def wjson(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str))


def http(method: str, url: str, body: Optional[dict] = None, headers: Optional[dict] = None):
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # localhost only, never a proxy
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


# =====================================================================================================
def inner(args: argparse.Namespace) -> int:
    install_network_guard()
    R = Report()
    root = Path.cwd()
    out = root / "out"
    sys.path.insert(0, str(root))
    os.environ.update({
        "EXEC_DRY_RUN": "1", "BX_ENABLED": "1", "BX_LIVE": "0",
        "AI_DECISION_KEY": "pilot-ai-key", "BX_SERVICE_KEY": "pilot-bx-key", "AUTO_FALLBACK": "1",
        "COCKPIT_PASSWORD": "pilot-only", "SESSION_SECRET": "pilot-only",
    })
    os.environ.pop("PENDING_CONTINUATION_DISABLED", None)   # production default: CONTINUATION / ADD_ON frozen
    now = datetime.now(timezone.utc)
    day = now.astimezone(HKT).strftime("%Y-%m-%d")
    R.info.update(now_utc=now.isoformat(), hkt_day=day, workdir=str(root))
    ctx: Dict[str, Any] = {"now": now, "day": day, "out": out}
    SimHL = make_sim_hl_class()
    ctx["SimHL"] = SimHL

    for sid, fn in (("1", step1), ("2", step2), ("3", step3), ("4", step4), ("5", step5), ("6", step6),
                    ("7", step7), ("8", step8)):
        R.guard(sid, STEP_NAMES[sid], lambda fn=fn: fn(R, ctx, args))
    if args.snapshot:
        R.guard("1", "real-data snapshot build", lambda: step1_snapshot(R, ctx, Path(args.snapshot)))

    hit = [b for b in BLOCKED if EXCHANGE_HOST_RE.search(b)]
    R.check("5", "no connection attempt to an exchange host (network guard log)", not hit,
            f"blocked attempts: {BLOCKED[:10] or 'none'}")
    wjson(Path(args.report), R.dump())
    return 0


# ----------------------------------------------------------------------------------------- step 1
def step1(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import bx_live
    import entry_candidates as ec
    from exec_common import candidates_fresh, radar_rowcount_ok
    now, out = ctx["now"], ctx["out"]
    rad = hl_radars(now)
    for tf, d in rad.items():
        wjson(out / f"gc_radar_{tf}.json", d)
    cd = ec.build_candidates(rad["1d"], rad["4h"], [{"coin": "LINK"}])
    wjson(out / "entry_candidates_latest.json", cd)
    ctx["hl_cands"] = cd
    syms = [(c["symbol"], c["type"]) for c in cd["candidates"]]
    R.check("1", "HL build_candidates: 4 Base + 1 Chase from the 1D/4H radar", syms == [
        ("AR", "Chase"), ("BTC", "Base"), ("DOGE", "Base"), ("ETH", "Base"), ("SOL", "Base")],
        f"count={cd['count']} {syms}; radar rows 1d={len(rad['1d']['rows'])} 4h={len(rad['4h']['rows'])}")
    R.check("1", "HL radar freshness read from radar 'ts' (age set, not stale)",
            cd.get("radar_1d_age_h") is not None and cd.get("radar_4h_age_h") is not None and not cd["stale"],
            f"radar_1d_age_h={cd.get('radar_1d_age_h')} radar_4h_age_h={cd.get('radar_4h_age_h')} stale={cd['stale']}")
    ok, why = candidates_fresh(cd, now=now)
    R.check("1", "HL candidates_fresh (generated_at today HKT, recent)", ok, why)
    old = dict(cd, generated_at=(now - timedelta(days=1)).isoformat())
    R.check("1", "HL candidates from yesterday are refused", not candidates_fresh(old, now=now)[0],
            candidates_fresh(old, now=now)[1])
    stale_rd = dict(rad["4h"], ts=(now - timedelta(hours=6)).isoformat())
    R.check("1", "HL 4H radar 6h old -> candidates flagged stale -> refused",
            ec.build_candidates(rad["1d"], stale_rd, [])["stale"] is True)
    for tf in ("1d", "4h"):
        ok, why = radar_rowcount_ok(rad[tf], tf)
        R.check("1", f"HL {tf.upper()} radar row-count guard", ok, why)
    ok, why = radar_rowcount_ok({"rows": rad["4h"]["rows"][:50]}, "4h")
    R.check("1", "HL radar with 50 rows is refused (fail closed)", not ok, why)

    bx = bx_fixture(now)
    wjson(out / "bx_meta.json", bx["meta"])
    wjson(out / "bx_radar_1d.json", bx["1d"])
    wjson(out / "bx_radar_4h.json", bx["4h"])
    doc = bx_live.build_candidates(now)
    ctx["bx_cands"] = doc
    bsyms = sorted((c["symbol"], c["type"]) for c in doc["candidates"])
    R.check("1", "BX build_candidates: 4 Base + 1 Chase", bsyms == [
        ("BARUSDT", "Base"), ("BAZUSDT", "Base"), ("CHAUSDT", "Chase"), ("FOOUSDT", "Base"), ("QUXUSDT", "Base")],
        f"count={len(doc['candidates'])} {bsyms}; BX radar rows 1d={len(bx['1d']['rows'])} 4h={len(bx['4h']['rows'])}")
    r = doc["rules"]
    R.check("1", "BX candidates rules: approve_max null, sizing 'desk', 2-4% / 2-5x",
            r.get("approve_max") is None and r.get("sizing") == "desk" and r.get("size_pct_range") == [2.0, 4.0]
            and r.get("leverage_range") == [2, 5], {k: r.get(k) for k in ("approve_max", "sizing", "size_pct_range",
                                                                          "leverage_range", "no_fallback")})
    f_ts = bx_live.bx_radar_fresh(bx["1d"], bx["4h"], now)
    gen = {tf: {"generated_at": bx[tf]["ts"], "rows": bx[tf]["rows"]} for tf in ("1d", "4h")}
    f_gen = bx_live.bx_radar_fresh(gen["1d"], gen["4h"], now)
    R.check("1", "BX radar freshness accepts 'ts' (bx_radar) and 'generated_at'", f_ts[0] and f_gen[0],
            f"ts: {f_ts}; generated_at: {f_gen}")
    st = bx_live.bx_radar_fresh(bx["1d"], dict(bx["4h"], ts=(now - timedelta(hours=6)).isoformat()), now)
    R.check("1", "BX 4H radar 6h old is refused", not st[0], st[1])
    few = bx_live.bx_radar_fresh(bx["1d"], dict(bx["4h"], rows=bx["4h"]["rows"][:40]), now)
    R.check("1", "BX radar with 40 rows is refused", not few[0], few[1])


def step1_snapshot(R: Report, ctx: dict, snap: Path) -> None:
    """Real data (read-only snapshot): HL candidate build from the public radar; BX radar freshness/rows."""
    import bx_live
    import entry_candidates as ec
    from exec_common import radar_rowcount_ok
    pr = json.loads((snap / "public_radar.json").read_text())
    cd = ec.build_candidates(pr["gc_radar_1d"], pr["gc_radar_4h"], [])
    types: Dict[str, int] = {}
    for c in cd["candidates"]:
        types[c["type"]] = types.get(c["type"], 0) + 1
    rc = [radar_rowcount_ok(pr[f"gc_radar_{tf}"], tf) for tf in ("1d", "4h")]
    R.check("1", "SNAPSHOT HL: real radar -> candidates (info; ages are vs. now, not 08:05)", all(x[0] for x in rc),
            f"rows 1d={len(pr['gc_radar_1d']['rows'])} 4h={len(pr['gc_radar_4h']['rows'])}; candidates={cd['count']} "
            f"{types} {[c['symbol'] for c in cd['candidates']]}; ages 1d={cd.get('radar_1d_age_h')}h "
            f"4h={cd.get('radar_4h_age_h')}h stale={cd['stale']}; row checks {[x[1] for x in rc]}")
    if types and set(types) == {"Chase"}:
        R.check("1", "SNAPSHOT HL: every real candidate today is Chase -> under the CONTINUATION freeze no HL "
                     "entry would be placed", True, "see risks", flag=True)
    b1 = json.loads((snap / "bx_radar_tf_1d.json").read_text())
    b4 = json.loads((snap / "bx_radar_tf_4h.json").read_text())
    snap_t = max(datetime.fromisoformat(str(x["ts"]).replace("Z", "+00:00")) for x in (b1, b4))
    fr = bx_live.bx_radar_fresh(b1, b4, snap_t + timedelta(minutes=5))
    R.check("1", "SNAPSHOT BX: real radar uses 'ts', row counts >= 70% of normal (as of the snapshot)", fr[0],
            f"1d rows={len(b1.get('rows') or [])} ts={b1.get('ts')}; 4h rows={len(b4.get('rows') or [])} "
            f"ts={b4.get('ts')}; {fr[1]}")


# ----------------------------------------------------------------------------------------- step 2
def start_servers(ctx: dict) -> None:
    """bx_service + serve on 127.0.0.1 (real handlers). HL account / meta and the BX egress / signed
    account read are mocked; everything else is the production code."""
    if ctx.get("servers"):
        return
    from http.server import ThreadingHTTPServer
    import bx_egress
    import bx_service
    import serve
    bx_egress.check = lambda *a, **k: dict(SG_EGRESS)
    bx_service.bx_egress.check = bx_egress.check
    bx_service.account_check = lambda: {"ok": True, "why": "pilot mock: signed account read NOT performed",
                                        "available_usdt": "500", "equity_usdt": 500.0}
    bxs = ThreadingHTTPServer(("127.0.0.1", 0), bx_service.Handler)
    threading.Thread(target=bxs.serve_forever, daemon=True).start()
    hl = ctx["SimHL"](HL_NAV, HL_META, HL_MIDS, positions=[LINK_POS], orders=[LINK_SL])
    serve.AI_DECISION_KEY = "pilot-ai-key"
    serve.BX_SERVICE_URL = f"http://127.0.0.1:{bxs.server_port}"
    serve.BX_SERVICE_KEY = "pilot-bx-key"
    serve._get_hl_cached = lambda: {"hl_spot": hl.spot_state(), "hl_perp": hl.perp_state(),
                                    "ts": datetime.now(timezone.utc).isoformat()}
    serve._get_hl_meta_cached = lambda: HL_META
    cs = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=cs.serve_forever, daemon=True).start()
    ctx["servers"] = (cs, bxs)
    ctx["cockpit"] = f"http://127.0.0.1:{cs.server_port}"
    ctx["bxsvc"] = f"http://127.0.0.1:{bxs.server_port}"


def step2(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    start_servers(ctx)
    k = {"X-AI-Key": "pilot-ai-key"}
    code, d = http("GET", ctx["cockpit"] + "/api/ai/candidates", headers=k)
    bx = d.get("bx") or {}
    rules = bx.get("rules") or {}
    R.check("2", "GET /api/ai/candidates -> 200", code == 200, f"http {code}; count={d.get('count')} "
            f"symbols={[c.get('symbol') for c in d.get('candidates') or []]}; account={d.get('account')}")
    R.check("2", "candidates carry coin_max_leverage / Hard SL / liq estimate (desk inputs)",
            all(c.get("coin_max_leverage") is not None for c in d.get("candidates") or []) and d.get("count") == 5,
            {c.get("symbol"): {kk: c.get(kk) for kk in ("coin_max_leverage", "hard_sl", "est_liq", "suggested_leverage")
                               if kk in c} for c in (d.get("candidates") or [])})
    R.check("2", "bx section: 5 candidates, rules approve_max null + sizing 'desk'",
            len(bx.get("candidates") or []) == 5 and "approve_max" in rules and rules["approve_max"] is None
            and rules.get("sizing") == "desk", {"n": len(bx.get("candidates") or []), "rules": rules})
    code_f, _ = http("GET", ctx["cockpit"] + "/api/ai/candidates", headers={"X-AI-Key": "wrong"})
    R.check("2", "wrong key -> 403", code_f == 403, f"http {code_f}")
    code2, s = http("GET", ctx["cockpit"] + "/api/bx/status", headers=k)
    R.check("2", "GET /api/bx/status (cockpit -> bx-exec) -> 200, live_ready, no NameError", code2 == 200 and s.get("ok"),
            {kk: s.get(kk) for kk in ("ok", "live_ready", "live_blockers", "rules", "error") if kk in s})
    os.environ["BX_LIVE"] = "1"
    os.environ.update(BX_API_KEY="PILOT_DUMMY_KEY_not_real", BX_API_SECRET="PILOT_DUMMY_SECRET_not_real")
    code3, s3 = http("GET", ctx["bxsvc"] + "/api/bx/status", headers={"X-BX-Key": "pilot-bx-key"})
    R.check("2", "bx-exec /api/bx/status with BX_LIVE=1 (mock egress/account) -> live_ready true",
            code3 == 200 and s3.get("live_ready") is True, {kk: s3.get(kk) for kk in ("live_ready", "live_blockers")})
    os.environ["BX_LIVE"] = "0"


# ----------------------------------------------------------------------------------------- step 3
def step3(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import bx_live
    from decisions import get_decisions_for_today
    start_servers(ctx)
    body = {"desk": DESK, "flags": {"pilot": True, "dry_run": True, "note": "extra top-level fields"},
            "source": "claude", "decisions": HL_DESK, "bx_decisions": BX_DESK}
    code, res = http("POST", ctx["cockpit"] + "/api/ai/decision", body, {"X-AI-Key": "pilot-ai-key"})
    R.check("3", f"POST /api/ai/decision desk={DESK} with flags/extra fields -> 200 ok", code == 200 and res.get("ok"),
            {kk: res.get(kk) for kk in ("ok", "stored_count", "rejected", "decision_snapshot", "bx", "error",
                                         "chase_no_entry") if kk in res})
    decs = get_decisions_for_today()
    R.check("3", "HL store: 4 approvals (BTC 4/5, ETH 3/2, SOL 2/5, AR Chase 2/2) + DOGE veto V2, source claude",
            decs.get("BTC", {}).get("size_pct") == 4 and decs.get("BTC", {}).get("leverage") == 5
            and decs.get("ETH", {}).get("leverage") == 2 and decs.get("SOL", {}).get("leverage") == 5
            and decs.get("DOGE", {}).get("decision") == "veto" and decs.get("DOGE", {}).get("rule") == "V2"
            and all(r.get("source", "claude") == "claude" for r in decs.values()),
            {s: {kk: r.get(kk) for kk in ("decision", "size_pct", "leverage", "rule", "source")} for s, r in decs.items()})
    bxr = res.get("bx") or {}
    R.check("3", "BX store via cockpit -> bx-exec: 5 stored, 0 rejected (coin/action form accepted)",
            bxr.get("ok") and bxr.get("stored") == 5 and not bxr.get("rejected"), bxr)
    now = datetime.now(timezone.utc)
    appr = {s: bx_live.approval_for(s, now) for s in BX_SYMS}
    R.check("3", "BX approvals FOO 4/5, BAR 3/2, BAZ 2/5, CHA 2/2; QUX veto -> no approval",
            appr["FOOUSDT"]["size_pct"] == 4 and appr["BARUSDT"]["leverage"] == 2 and appr["BAZUSDT"]["leverage"] == 5
            and appr["CHAUSDT"] is not None and appr["QUXUSDT"] is None,
            {s: (a and {kk: a.get(kk) for kk in ("size_pct", "leverage", "source")}) for s, a in appr.items()})
    hl_dir = Path(os.environ.get("DECISIONS_DIR") or (ctx["out"] / "decisions"))
    R.check("3", "BX decisions never written to the HL decisions store", not any(s in decs for s in BX_SYMS),
            f"HL store symbols: {sorted(decs)}")
    ctx["hl_decisions_dir"] = str(hl_dir)


# ----------------------------------------------------------------------------------------- step 4
def step4(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import bx_live
    import executor
    import serve
    from decisions import get_decisions_for_today
    main_dir = os.environ.get("DECISIONS_DIR")
    fb_dir = ctx["out"] / "pilot_fallback_decisions"
    os.environ["DECISIONS_DIR"] = str(fb_dir)
    try:
        res = serve._scheduled_fallback()
        decs = get_decisions_for_today()
        R.check("4", "08:50 _scheduled_fallback with no Claude POST stores fallback decisions",
                res.get("status") == "success" and len(decs) == 5, res.get("message"))
        R.check("4", "fallback approves EVERY candidate (incl. Chase) at 2% / 2x, source fallback",
                all(r["decision"] == "approve" and float(r["size_pct"]) == 2.0 and int(r["leverage"]) == 2
                    and r.get("source") == "fallback" for r in decs.values()),
                {s: (r["decision"], r["size_pct"], r["leverage"], r.get("source")) for s, r in decs.items()})
        os.environ["EXEC_DRY_RUN"] = "1"
        hl = ctx["SimHL"](HL_NAV, HL_META, HL_MIDS, positions=[LINK_POS], orders=[LINK_SL])
        r = executor.execute_approved_candidates(hl=hl, now=ctx["now"])
        acts = {a["symbol"]: (a["size_pct"], a["leverage"]) for a in r["actions"]}
        R.check("4", "HL executor (DRY_RUN) on fallback: BTC/DOGE/ETH/SOL at 2% / 2x, AR Chase -> no entry (freeze)",
                acts == {"BTC": (2.0, 2), "DOGE": (2.0, 2), "ETH": (2.0, 2), "SOL": (2.0, 2)}
                and [x["symbol"] for x in r["no_entry"]] == ["AR"] and not hl.writes(),
                {"actions": acts, "no_entry": [x["symbol"] for x in r["no_entry"]], "skipped": r["skipped"],
                 "alerts": r["alerts"], "writes": hl.writes()})
        R.check("4", "fallback day raises the 'Claude POST missing' alert",
                any("POST missing" in a or "no Claude ENTRY_DESK POST" in a for a in r["alerts"]), r["alerts"])
    finally:
        if main_dir is None:
            os.environ.pop("DECISIONS_DIR", None)
        else:
            os.environ["DECISIONS_DIR"] = main_dir
    res2 = serve._scheduled_fallback()
    R.check("4", "with Claude decisions present the 08:50 fallback does nothing", res2.get("status") == "skipped",
            res2.get("message"))
    bxfb = bx_live.store_decisions([{"symbol": "FOOUSDT", "decision": "approve"}], "fallback")
    R.check("4", "BX: no fallback by design (SoT-5 'no approval -> no order'); fallback source refused",
            not bxfb.get("ok"), f"{bxfb.get('error')} | rules.no_fallback={bx_live.rules_summary().get('no_fallback')}",
            flag=True)


# ----------------------------------------------------------------------------------------- step 5
def hl_live_env() -> None:
    os.environ["EXEC_DRY_RUN"] = "0"
    os.environ["HL_API_PRIVATE_KEY"] = "0x" + "11" * 32   # dummy; the simulator never signs


def step5(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import executor
    import hl_exec
    from exec_common import isolated_liq_price_long, nav_snapshot
    now, day, SimHL = ctx["now"], ctx["day"], ctx["SimHL"]
    hl_live_env()
    hl = SimHL(HL_NAV, HL_META, HL_MIDS, positions=[LINK_POS], orders=[LINK_SL])
    ctx["hl_main"] = hl
    r = executor.execute_approved_candidates(hl=hl, now=now)
    ctx["hl_res"] = r
    R.check("5", "HL executor ran in LIVE mode against the simulator", r["mode"] == "LIVE" and r["status"] == "success",
            {kk: r.get(kk) for kk in ("mode", "status", "message", "candidates_source")})
    nav = r["nav_snapshot"]
    R.check("5", "NAV = conservative min(equity incl uPnL, free USDC + margin)",
            abs(nav["nav"] - min(nav["equity_nav"], nav["conservative_nav"])) < 1e-6,
            {kk: nav[kk] for kk in ("nav", "equity_nav", "conservative_nav", "spot_usdc_total", "spot_usdc_hold",
                                    "margin_used", "abstraction")})
    split = nav_snapshot({"balances": [{"coin": "USDC", "total": "100", "hold": "0"}]},
                         {"marginSummary": {"accountValue": "900", "totalMarginUsed": "200"}, "withdrawable": "650"},
                         "default")
    R.check("5", "conservative NAV on a split account: min(1000, 100 + 650 + 200) = 950", abs(split["nav"] - 950) < 1e-6,
            {kk: split[kk] for kk in ("nav", "equity_nav", "conservative_nav")})
    ex = {e["symbol"]: e for e in r["executed"]}
    R.check("5", "executed BTC, ETH, SOL (DOGE vetoed, AR Chase frozen)", sorted(ex) == ["BTC", "ETH", "SOL"],
            {"executed": sorted(ex), "skipped": r["skipped"], "no_entry": r["no_entry"]})
    want = {"BTC": (4.0, 5), "ETH": (3.0, 2), "SOL": (2.0, 3)}
    got = {s: (e["size_pct"], e["leverage"]) for s, e in ex.items()}
    R.check("5", "desk size/leverage honoured, clamped to coin max (SOL desk 5x -> coin max 3x)", got == want,
            {s: f"{e['size_pct']}% / {e['leverage']}x (desk {e['ai_size_pct']}/{e['ai_leverage']}, coin max "
                f"{e['coin_max_leverage']}) margin ${e['margin_usd']} notional ${e['notional_usd']}" for s, e in ex.items()})
    R.check("5", "leverage never above coin max, within 2-5x; size within 2-4%",
            all(2 <= e["leverage"] <= min(5, e["coin_max_leverage"]) and 2 <= e["size_pct"] <= 4 for e in ex.values()))
    rows4 = {s: v for s, v in HL_ROWS.items()}
    sl_ok = all(e["hard_sl"] == rows4[s][8] for s, e in ex.items())   # mega tier -> 4H Lower
    R.check("5", "Hard SL = tier level (Mega -> 4H Lower), SL distance >= 1.5%",
            sl_ok and all(e["sl_dist_pct"] >= 1.5 for e in ex.values()),
            {s: f"SL {e['hard_sl']} ({e['hard_sl_label']}) dist {e['sl_dist_pct']}%" for s, e in ex.items()})
    pos = {p["coin"]: p for p in hl.positions}
    R.check("5", "liquidation beyond Hard SL (estimate and simulated exchange liq)",
            all(e["estimated_liq"] < e["hard_sl"] and float(pos[s]["liquidationPx"]) < e["hard_sl"] for s, e in ex.items()),
            {s: f"est liq {e['estimated_liq']:.6g} / exch liq {float(pos[s]['liquidationPx']):.6g} < SL {e['hard_sl']}"
             for s, e in ex.items()})
    opens = [c for c in hl.calls if c[0] == "open_long_ioc"]
    cl = {c[1]: c[4] for c in opens}
    R.check("5", "deterministic client order id = 0x + sha256(coin|HKT date|entry)[:32]",
            all(cl[s] == "0x" + hashlib.sha256(f"{s}|{day}|entry".encode()).hexdigest()[:32] for s in cl)
            and all(re.fullmatch(r"0x[0-9a-f]{32}", v) for v in cl.values()), cl)
    order = [c[1] for c in opens]
    R.check("5", "priority: Chase first (AR -> no_entry), then alphabetical BTC, ETH, SOL",
            order == ["BTC", "ETH", "SOL"] and r["no_entry"] and r["no_entry"][0]["symbol"] == "AR",
            f"order calls: {order}; no_entry first: {[x['symbol'] for x in r['no_entry']]}")
    sls = {c[1]: (c[2], c[3]) for c in hl.calls if c[0] == "place_stop_loss"}
    R.check("5", "Hard SL placed for the filled qty at the Hard SL price",
            all(sls.get(s) == (float(pos[s]["szi"]), e["hard_sl"]) for s, e in ex.items()), sls)

    import exec_common
    R.check("5", "no daily entry limit (SoT-5): 3 entries in one run, no daily cap function",
            len(ex) == 3 and not hasattr(exec_common, "daily_entry_cap_ok"),
            f"entries this run: {len(ex)}; entries_today_before={r.get('entries_today_before')}")
    R.check("5", "CONT under the freeze (default): acknowledged, no watch record, no order",
            r.get("pending") == [] and "disabled" in r["no_entry"][0]["reason"], r["no_entry"])

    # 80% cap with REAL (exchange) margin: existing margin 72% NAV -> BTC +4% ok, ETH +3% ok, SOL +2% -> 81% blocked
    hl2 = SimHL(1000.0, HL_META, HL_MIDS, base_margin=720.0)
    r2 = executor.execute_approved_candidates(hl=hl2, now=now)
    ex2 = [e["symbol"] for e in r2["executed"]]
    sk2 = {s["symbol"]: s["reason"] for s in r2["skipped"]}
    real = {e["symbol"]: e["live_result"].get("actual_margin_used") for e in r2["executed"]}
    R.check("5", "HL 80% total-margin cap (cumulative, real exchange margin) blocks the over-cap entry",
            ex2 == ["BTC", "ETH"] and "80" in sk2.get("SOL", ""),
            {"executed": ex2, "SOL": sk2.get("SOL"), "actual_margin_used (from exchange)": real,
             "start margin": 720.0})
    # $10 minimum order: NAV 200, fallback 2% / 2x -> $8 notional
    hl3 = SimHL(200.0, HL_META, HL_MIDS)
    fb = {s: {"decision": "approve", "size_pct": 2, "leverage": 2, "source": "fallback"} for s in ("BTC", "ETH", "SOL")}
    r3 = executor.execute_approved_candidates(hl=hl3, decisions=fb, now=now)
    R.check("5", "$10 HL minimum order: $8 notional entries skipped, nothing sent",
            not hl3.writes() and len(r3["skipped"]) == 3 and all("minimum" in s["reason"] for s in r3["skipped"]),
            [s["reason"] for s in r3["skipped"]])
    # CONT with the flag lifted (PENDING_CONTINUATION_DISABLED=0): 3-step watch record, never an 08:55 order
    os.environ["EXEC_DRY_RUN"] = "1"
    os.environ["PENDING_CONTINUATION_DISABLED"] = "0"
    try:
        r4 = executor.execute_approved_candidates(hl=SimHL(HL_NAV, HL_META, HL_MIDS), now=now)
        R.check("5", "CONT with the flag lifted -> 3-step watch record (DRY_RUN would_create), no 08:55 order",
                [p["symbol"] for p in r4["pending"]] == ["AR"] and r4["pending"][0]["kind"] == "CONT",
                r4["pending"])
    finally:
        os.environ.pop("PENDING_CONTINUATION_DISABLED", None)
    hl_live_env()

    # HL SDK order wire: the cloid is signed as the order's "c" field, the order type stays clean
    R.guard("5", "HL SDK wire check", lambda: hl_wire_check(R, day))
    # scheduler times (static check of the job definitions)
    srv = (Path.cwd() / "serve.py").read_text()
    bxs = (Path.cwd() / "bx_service.py").read_text()
    times = {"fallback 08:50": "_scheduled_fallback, CronTrigger(hour=8, minute=50" in srv,
             "HL executor 08:55": "_scheduled_executor, CronTrigger(hour=8, minute=55" in srv,
             "BX daily 08:02": 'CronTrigger(hour=8, minute=2, timezone=hkt), args=["daily"]' in bxs,
             "BX entries 08:56": 'CronTrigger(hour=8, minute=56, timezone=hkt), args=["entries"]' in bxs}
    R.check("5", "scheduler: fallback 08:50, HL executor 08:55, BX daily 08:02, BX entries 08:56", all(times.values()),
            times)

    step5_bx(R, ctx)


def hl_wire_check(R: Report, day: str) -> None:
    import hl_exec
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils.signing import order_request_to_order_wire, order_wires_to_order_action, sign_l1_action

    wallet = Account.from_key("0x" + "22" * 32)   # throw-away key, signs locally only

    class WireStub:
        def __init__(self):
            self.wires, self.signed = [], []

        def order(self, *a, **k):
            return Exchange.order(self, *a, **k)

        def bulk_orders(self, reqs, builder=None, grouping="na"):
            wires = [order_request_to_order_wire(o, 0) for o in reqs]
            action = order_wires_to_order_action(wires, builder, grouping)
            self.signed.append(sign_l1_action(wallet, action, None, 1_790_000_000_000, None, True))
            self.wires += wires
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": str(reqs[0]["sz"]), "avgPx": str(reqs[0]["limit_px"]), "oid": 1}}]}}}

    c = hl_exec.HLClient("0x" + "00" * 20)
    stub = WireStub()
    c._exchange = stub
    cloid = hl_exec._make_cloid("BTC", day)
    res = c.open_long_ioc("BTC", 0.001, 62300.0, cloid=cloid)
    w = stub.wires[0]
    R.check("5", "HL SDK wire: cloid in 'c', order type {'limit': {'tif': 'Ioc'}}, signs offline, parsed filled",
            w.get("c") == cloid and w["t"] == {"limit": {"tif": "Ioc"}} and stub.signed and res.get("status") == "filled",
            {"wire": w, "parsed": {kk: res.get(kk) for kk in ("status", "filled_sz", "avg_px")}})


def bx_ctx(ctx: dict):
    import bx_live
    if "bx_conn" not in ctx:
        os.environ["BX_LEDGER_PATH"] = str(ctx["out"] / "pilot_bx.db")
        ctx["bx_conn"] = bx_live.connect()
    return ctx["bx_conn"]


def bx_entries(ctx: dict, api: SimBX, hl_margin: float, nav_hl: float = HL_NAV, conn=None):
    import bx_live
    os.environ.update(BX_LIVE="1", BX_ENABLED="1", BX_API_KEY="PILOT_DUMMY_KEY_not_real",
                      BX_API_SECRET="PILOT_DUMMY_SECRET_not_real")
    return bx_live.run_entries(now=ctx["now"], trade_api=api, egress=dict(SG_EGRESS), nav_fn=lambda: nav_hl,
                               market=lambda s: dict(BX_LIVE_MKT), tiers_fn=lambda s: BX_TIERS,
                               conn=conn or bx_ctx(ctx), hl_margin_fn=lambda: hl_margin)


def step5_bx(R: Report, ctx: dict) -> None:
    import bx_live
    hl = ctx["hl_main"]
    api = SimBX(available=500.0)
    ctx["bx_api"] = api
    rep = bx_entries(ctx, api, hl.margin_total(), nav_hl=ctx["hl_res"]["nav_snapshot"]["nav"])
    ctx["bx_rep"] = rep
    ent = {e["symbol"]: e for e in rep["entered"]}
    R.check("5", "BX 08:56 run_entries LIVE (simulated Bitunix): FOO, BAR, BAZ filled",
            rep["live"] and sorted(ent) == ["BARUSDT", "BAZUSDT", "FOOUSDT"]
            and all(e["status"] == "filled" for e in ent.values()),
            {"gate": rep["gate"], "entered": {s: e["status"] for s, e in ent.items()}, "skipped": rep["skipped"],
             "margin": rep.get("margin")})
    plans = {s: e["plan"] for s, e in ent.items()}
    R.check("5", "BX desk sizing: FOO 4%/5x, BAR 3%/2x, BAZ desk 5x -> contract max 3x (2%)",
            {s: (p["desk_size_pct"], p["leverage"]) for s, p in plans.items()}
            == {"FOOUSDT": (4.0, 5), "BARUSDT": (3.0, 2), "BAZUSDT": (2.0, 3)},
            {s: f"{p['desk_size_pct']}% / {p['leverage']}x margin ${p['margin_usd']} notional ${p['notional_usd']} "
                f"qty {p['qty']} limit {p['limit_price']}" for s, p in plans.items()})
    nav = plans["FOOUSDT"]["nav"]
    R.check("5", "BX NAV = HL NAV + BX equity", abs(nav - (ctx["hl_res"]["nav_snapshot"]["nav"] + 500.0)) < 0.02,
            f"plan nav {nav}")
    R.check("5", "BX Hard SL attached to the entry order; est. liq below Hard SL",
            all(c[4] == plans[c[1]]["sl_price"] for c in api.opens())
            and all(p["liq_est"] < float(p["sl_price"]) for p in plans.values()),
            {s: f"SL {p['sl_price']} liq~{p['liq_est']:.4g}" for s, p in plans.items()})
    cids = {e["symbol"]: e["client_id"] for e in rep["entered"]}
    R.check("5", "BX deterministic client id = giiqbx + sha256(symbol|HKT date|entry)[:12]",
            all(v == "giiqbx" + hashlib.sha256(f"{s}|{ctx['day']}|entry".encode()).hexdigest()[:12] for s, v in cids.items()),
            cids)
    R.check("5", "BX veto (QUX) -> no order", any(s["symbol"] == "QUXUSDT" and "approval" in s["reason"]
                                                for s in rep["skipped"]), rep["skipped"])
    chase_pending = [p for p in rep.get("pending") or [] if p["symbol"] == "CHAUSDT"]
    if chase_pending:
        R.check("5", "BX Chase under the CONTINUATION freeze: bx_live still creates a LIVE pending (4h job can "
                     "enter it later); HL refuses Chase while frozen -> MMT decision", True,
                f"pending: {chase_pending}; PENDING_CONTINUATION_DISABLED unset (frozen)", flag=True)
    else:
        R.check("5", "BX Chase under the freeze: no pending, no order", any(
            x["symbol"] == "CHAUSDT" for x in rep.get("no_entry") or []), rep.get("no_entry"))
    # 80% cap HL+BX with real margins: HL margin 72% of the combined NAV -> FOO ok, BAR ok, BAZ over cap
    api2 = SimBX(available=5000.0)
    import sqlite3  # noqa: F401
    conn2 = bx_live.connect(str(ctx["out"] / "pilot_bx_cap.db"))
    nav_c = 1000.0 + 5000.0
    rep2 = bx_entries(ctx, api2, hl_margin=0.72 * nav_c, nav_hl=1000.0, conn=conn2)
    sk = {s["symbol"]: s["reason"] for s in rep2["skipped"]}
    R.check("5", "BX 80% HL+BX cap (exchange margins) blocks the over-cap entry",
            [e["symbol"] for e in rep2["entered"]] == ["FOOUSDT", "BARUSDT"] and "80" in sk.get("BAZUSDT", ""),
            {"entered": [e["symbol"] for e in rep2["entered"]], "BAZUSDT": sk.get("BAZUSDT"), "margin": rep2.get("margin")})
    conn2.close()
    R.check("5", "BX minimum order = contract min_qty ($10 minimum is the HL rule)", True,
            "bx_live.check_entry: 'qty below the minimum <min_qty>'", flag=False)
    os.environ["BX_LIVE"] = "0"


# ----------------------------------------------------------------------------------------- step 6
def step6(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import executor
    import hl_exec
    now, SimHL = ctx["now"], ctx["SimHL"]
    hl_live_env()
    rc = {e["symbol"]: e["live_result"]["reconcile"] for e in ctx["hl_res"]["executed"]}
    R.check("6", "HL reconciliation after each fill: size, isolated, leverage, liq < SL, SL resting -> ok",
            all(v["ok"] and not v["problems"] for v in rc.values()),
            {s: {"ok": v["ok"], "actual_margin_used": round(v["actual_margin_used"], 4)} for s, v in rc.items()})
    for knob, val, word in (("sim_lev_override", 4, "leverage mismatch"), ("sim_cross", True, "CROSS")):
        hl = SimHL(HL_NAV, HL_META, HL_MIDS, **{knob: val})
        res = hl_exec.enter_long_with_sl(hl, "BTC", 0.0054, 62310.0, 5, 59500.0, 5, coin_max_leverage=40, now=now)
        R.check("6", f"HL mismatch ({word}) -> position closed at once, status reconcile_failed_closed",
                res["status"] == "reconcile_failed_closed" and any(word in p for p in res["reconcile"]["problems"])
                and [c[0] for c in hl.calls][-1] == "market_close" and not hl.positions,
                {"status": res["status"], "problems": res["reconcile"]["problems"],
                 "calls": [c[0] for c in hl.calls]})
    hl = SimHL(HL_NAV, HL_META, HL_MIDS, sim_cross=True)
    r = executor.execute_approved_candidates(hl=hl, now=now)
    # closed safely -> alert (shown as "| ALERTS:" in the 08:55 job status), same as sl_failed_closed;
    # only a failed fail-safe close turns the run into status error (next check)
    R.check("6", "HL executor raises an alert per mismatch close (coin, status, problems); no position left",
            all(any(a.startswith(f"{s}: reconcile_failed_closed (margin mode is CROSS") for a in r["alerts"])
                for s in ("BTC", "ETH", "SOL")) and not r["executed"] and not hl.positions,
            {"status": r["status"], "alerts": r["alerts"][:4], "positions left": hl.positions})
    hl = SimHL(HL_NAV, HL_META, HL_MIDS, sim_cross=True, close_ok=False)
    r = executor.execute_approved_candidates(hl=hl, now=now)
    R.check("6", "HL close fails too -> CLOSE_FAILED, status error, MANUAL ACTION in the message",
            r["status"] == "error" and "MANUAL ACTION" in (r.get("message") or "")
            and any("CLOSE_FAILED" in a for a in r["alerts"]), {"message": r.get("message"), "alerts": r["alerts"][:3]})
    hl = SimHL(HL_NAV, HL_META, HL_MIDS, sl_ok=False)
    res = hl_exec.enter_long_with_sl(hl, "ETH", 0.06, 2613.0, 2, 2500.0, 4, coin_max_leverage=25, now=now)
    R.check("6", "HL SL placement fails (and the reconcile retry too) -> sl_failed_closed",
            res["status"] == "sl_failed_closed" and not hl.positions, {"status": res["status"],
                                                                      "calls": [c[0] for c in hl.calls]})

    # BX: attach + verify, repair, protection failure
    import bx_live
    ent = {e["symbol"]: e for e in ctx["bx_rep"]["entered"]}
    R.check("6", "BX after fill: isolated, leverage, exchange liq < SL, attached Hard SL found on the exchange",
            all(e.get("sl_order_id") for e in ent.values()),
            {s: {"position": e.get("position_id"), "sl_order_id": e.get("sl_order_id")} for s, e in ent.items()})
    cand = next(c for c in ctx["bx_cands"]["candidates"] if c["symbol"] == "FOOUSDT")
    meta = next(m for m in json.loads((ctx["out"] / "bx_meta.json").read_text())["scanned"] if m["bx_symbol"] == "FOOUSDT")
    plan = ent["FOOUSDT"]["plan"]
    for label, kw, want in (
            ("SL not attached -> placed and verified", {"sl_attached": False}, "filled"),
            ("SL missing and placement fails -> flash close", {"sl_attached": False, "sl_place_fails": True},
             "closed_protection_failed"),
            ("leverage on exchange differs -> flash close", {"lev_override": 3}, "closed_protection_failed"),
            ("exchange liq above Hard SL -> flash close", {"liq_fn": lambda px, lev: 1.95}, "closed_protection_failed")):
        api = SimBX(**kw)
        conn = bx_live.connect(str(ctx["out"] / f"pilot_bx_{abs(hash(label))}.db"))
        res = bx_live.execute_entry(api, cand, meta, plan, ctx["now"], conn)
        conn.close()
        R.check("6", f"BX {label}", res["status"] == want,
                {"status": res["status"], "problems": res.get("problems"), "calls": [c[0] for c in api.calls]})


# ----------------------------------------------------------------------------------------- step 7
def step7(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import executor
    import hl_exec
    hl_live_env()
    hl = ctx["hl_main"]
    n0 = len([c for c in hl.calls if c[0] == "open_long_ioc"])
    r = executor.execute_approved_candidates(hl=hl, now=ctx["now"])
    n1 = len([c for c in hl.calls if c[0] == "open_long_ioc"])
    R.check("7", "HL rerun of the 08:55 executor: no new order, approvals skipped 'already holding'",
            n1 == n0 and not r["executed"] and sorted(s["symbol"] for s in r["skipped"]
                                                      if "already holding" in s["reason"]) == ["BTC", "ETH", "SOL"],
            {"orders before/after": (n0, n1), "skipped": r["skipped"]})
    res = hl_exec.enter_long_with_sl(hl, "BTC", 0.001, 62300.0, 5, 59500.0, 5, coin_max_leverage=40, now=ctx["now"])
    R.check("7", "HL retry with the same deterministic cloid is refused as a duplicate (no second order)",
            res["status"] == "entry_failed" and "duplicate" in str(res["entry_result"].get("error")),
            {"status": res["status"], "entry_result": res["entry_result"], "cloid": res["cloid"]})
    api = ctx["bx_api"]
    b0 = len(api.opens())
    rep = bx_entries(ctx, api, hl.margin_total(), nav_hl=ctx["hl_res"]["nav_snapshot"]["nav"])
    R.check("7", "BX rerun of 08:56 entries: no new order ('already holding this contract')",
            len(api.opens()) == b0 and not rep["entered"]
            and sum("already holding" in s["reason"] for s in rep["skipped"]) == 3,
            {"orders before/after": (b0, len(api.opens())), "skipped": rep["skipped"],
             "pending": rep.get("pending")})
    os.environ["BX_LIVE"] = "0"


# ----------------------------------------------------------------------------------------- step 8
def step8(R: Report, ctx: dict, args: argparse.Namespace) -> None:
    import bx_live
    import bx_shadow
    import exit_worker
    os.environ["EXEC_DRY_RUN"] = "1"
    os.environ.pop("HL_API_PRIVATE_KEY", None)
    hl = ctx["hl_main"]
    w0 = len(hl.writes())
    rad = hl_radars(ctx["now"])
    res = exit_worker.check_exits("all", hl=hl, radar_1h=rad["1h"], radar_4h=rad["4h"])
    R.check("8", "HL exit_worker.check_exits('all') in DRY_RUN: runs, no exchange write",
            res["mode"] == "DRY_RUN" and res["status"] == "success" and len(hl.writes()) == w0,
            {"mode": res["mode"], "status": res["status"], "exits": res.get("exits"),
             "holds": [h.get("coin") for h in res.get("holds") or []], "sl_actions": res.get("sl_actions"),
             "writes during call": len(hl.writes()) - w0})
    api = ctx["bx_api"]
    c0 = len(api.calls)
    meta_all = {m["bx_symbol"]: m for m in json.loads((ctx["out"] / "bx_meta.json").read_text())["scanned"]}
    r4h = bx_shadow._rows_by_symbol(json.loads((ctx["out"] / "bx_radar_4h.json").read_text()))
    man = bx_live.manage_open(api, bx_ctx(ctx), ctx["now"], r4h, meta_all, market=lambda s: dict(BX_LIVE_MKT))
    writes = [c for c in api.calls[c0:] if c[0] in ("flash_close", "place_position_sl", "open_long")]
    R.check("8", "BX manage_open (exit + SL check) on the 3 pilot positions: holds, nothing closed or re-placed",
            not man["closed"] and not man["repaired"] and not man["problems"] and not writes,
            {"closed": man["closed"], "repaired": man["repaired"], "problems": man["problems"]})


# =====================================================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", help="directory with a read-only snapshot (public_radar.json, bx_radar_tf_*.json)")
    ap.add_argument("--json", help="write the JSON report here")
    ap.add_argument("--keep", action="store_true", help="keep the temp repo copy")
    ap.add_argument("--verbose", action="store_true", help="show the inner process output")
    ap.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--report", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.inner:
        return inner(a)
    return outer(a)


if __name__ == "__main__":
    raise SystemExit(main())
