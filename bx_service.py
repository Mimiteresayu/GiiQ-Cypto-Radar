#!/usr/bin/env python3
"""bx-exec — the Singapore Railway service for Bitunix (radar + shadow book + live pilot).

MMT-approved 2026-09-30. Runs ONLY in a non-US Railway region (asia-southeast1-eqsg3a). It never loads the HL
private key and never talks to the HL order path; it reads HL NAV from the public HL info API.

Startup: logs one [BX_EGRESS] line (egress IP, country from two geo sources, Railway region). A US or unknown
egress blocks every live order (bx_live.live_gate); a US egress with BX_LIVE=1 also logs [BX_ALERT].

Schedule (HKT, APScheduler, one lock, each step a subprocess with a hard timeout):
  08:02         bx_radar daily -> bx_shadow daily -> bx_live candidates  (ready before the 08:10 ENTRY_DESK)
  08:56         bx_live entries        (approved candidates only; no approval -> no order)
  every 4h :05  bx_radar 4h -> bx_shadow 4h -> bx_live 4h   (exits, SL repair, Chase pending fills, breaker)
  hourly :09    bx_radar 1h -> bx_shadow 1h -> bx_live 1h   (exits, SL repair, breaker)
Switches: BX_ENABLED=0 stops everything (no scans, no orders; exchange SL orders stay in place).
          BX_LIVE=0 (default) -> shadow only: no new live orders; exits / SL repair of open live positions continue.

HTTP (PORT): GET /health (open, no secrets)  ·  X-BX-Key = BX_SERVICE_KEY for everything else:
  GET  /api/bx/status /api/bx/candidates /api/bx-ui /api/bx/radar?tf= /api/bx/review /api/bx/shadow
  POST /api/bx/decision   {decisions:[{symbol|coin, decision|action, type, rule, reason}], source:"claude"}
  POST /api/bx/breaker/reset  (header X-BX-Admin-Key = BX_ADMIN_KEY; MMT only)
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bx_egress  # noqa: E402
import bx_live  # noqa: E402

OUT_DIR = Path(os.environ.get("BX_OUT_DIR") or (ROOT / "out"))
STEP_TIMEOUT_S = int(os.environ.get("BX_TIMEOUT_S", "480"))
_lock = threading.Lock()
_jobs: Dict[str, dict] = {}


def _key() -> str:
    return (os.environ.get("BX_SERVICE_KEY") or "").strip()


def _step(script: str, args: List[str]) -> Dict[str, Any]:
    env = dict(os.environ)
    env.pop("HL_API_PRIVATE_KEY", None)          # never present here; stripped anyway
    proc = subprocess.run([sys.executable, str(ROOT / script), *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=STEP_TIMEOUT_S, env=env)
    for line in (proc.stderr or "").strip().splitlines()[-40:]:
        bx_live._log(f"  [{script}] {line}")
    try:
        res = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        lines = (proc.stdout or "").strip().splitlines()
        try:
            res = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            res = {"status": "error", "message": (proc.stdout or "")[-300:]}
    res["_rc"] = proc.returncode
    return res


def run_job(job: str) -> Dict[str, Any]:
    """One scheduled job = its steps in order, under one lock. Never raises."""
    if not bx_live.env_on("BX_ENABLED", "1"):
        _jobs[job] = {"at": datetime.now(timezone.utc).isoformat(), "status": "skipped", "why": "BX_ENABLED=0"}
        return _jobs[job]
    if not _lock.acquire(timeout=600):
        _jobs[job] = {"at": datetime.now(timezone.utc).isoformat(), "status": "skipped", "why": "previous job still running"}
        return _jobs[job]
    res: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "steps": {}}
    try:
        nav = None
        if job in ("daily", "4h", "1h"):
            res["steps"]["radar"] = r = _step("bx_radar.py", [job])
            if r.get("_rc") == 0 and r.get("status") != "skipped":
                nav = bx_live.hl_nav()
                res["steps"]["shadow"] = _step("bx_shadow.py", [job] + (["--nav", str(nav)] if nav else []))
        if job == "daily":
            res["steps"]["candidates"] = _step("bx_live.py", ["candidates"])
        elif job == "entries":
            res["steps"]["live"] = _step("bx_live.py", ["entries"])
        elif job in ("4h", "1h"):
            res["steps"]["live"] = _step("bx_live.py", [job])
        bad = [k for k, v in res["steps"].items() if v.get("_rc") != 0]
        res["status"] = "success" if not bad else "error"
        if bad:
            res["error_steps"] = bad
        live = res["steps"].get("live") or {}
        if live.get("problems"):
            _write_json("bx_live_problems.json", {"at": res["at"], "job": job, "problems": live["problems"]})
        elif job in ("4h", "1h") and "live" in res["steps"]:
            _write_json("bx_live_problems.json", {"at": res["at"], "job": job, "problems": []})
        bx_live._log(f"[BX_DATA] {job} {res['status']} " + json.dumps(
            {k: {kk: v.get(kk) for kk in ("status", "n", "n_scanned", "live", "gate", "entered", "closed", "breaker", "note")
                 if v.get(kk) not in (None, [], {})} for k, v in res["steps"].items()}, default=str)[:900])
    except Exception as e:  # noqa: BLE001
        res.update(status="error", message=str(e)[:300])
    finally:
        _lock.release()
    _jobs[job] = {k: res.get(k) for k in ("at", "status", "error_steps", "message")}
    return res


def _write_json(name: str, obj: Any) -> None:
    p = OUT_DIR / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _read_json(name: str) -> dict:
    try:
        return json.loads((OUT_DIR / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def account_check() -> Dict[str, Any]:
    """Signed READ-ONLY check (no order): proves key + signature + IP whitelist work. No secrets returned."""
    import bx_trade
    if not bx_trade.keys_present():
        return {"ok": False, "why": "no key set"}
    try:
        api = bx_trade.BXTrade()
        acct = api.account()
        return {"ok": True, "available_usdt": acct.get("available"), "position_mode": api.position_mode(),
                "equity_usdt": bx_live.bx_equity(acct)}
    except Exception as e:  # noqa: BLE001  (messages are already redacted by bx_trade)
        return {"ok": False, "why": str(e)[:200], "code": getattr(e, "code", None)}


def status_payload() -> Dict[str, Any]:
    eg = bx_egress.check()
    st = bx_live.status()
    import bx_trade
    gate_ok, gate_why = bx_live.live_gate(eg, bx_trade.keys_present(), bx_live.breaker_state())
    acct = account_check() if eg.get("ok") else {"ok": False, "why": "not tried: egress not verified"}
    if bx_trade.keys_present() and not acct.get("ok"):
        gate_ok = False
        gate_why = gate_why + [f"signed account read failed: {acct.get('why')}"]
    return {"ok": True, "region": os.environ.get("RAILWAY_REPLICA_REGION"), "egress": eg,
            "live_ready": gate_ok, "live_blockers": gate_why, "account_check": acct, "jobs": dict(_jobs),
            "problems": (_read_json("bx_live_problems.json").get("problems") or []), **st}


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "bx-exec"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep query strings (keys) out of logs
        sys.stderr.write(f"[HTTP] {self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _keyed(self) -> bool:
        want = _key()
        got = self.headers.get("X-BX-Key") or ""
        if not want:
            self._send(404, {"ok": False, "error": "BX_SERVICE_KEY not set"})
            return False
        if not got or not hmac.compare_digest(got, want):
            self._send(403, {"ok": False, "error": "forbidden"})
            return False
        return True

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path in ("/health", "/healthz"):
            eg = bx_egress.check()
            self._send(200, {"ok": True, "service": "bx-exec", "region": os.environ.get("RAILWAY_REPLICA_REGION"),
                             "egress_ok": eg.get("ok"), "bx_enabled": bx_live.env_on("BX_ENABLED", "1"),
                             "bx_live": bx_live.env_on("BX_LIVE", "0"),
                             "breaker_tripped": bool(bx_live.breaker_state().get("tripped"))})
            return
        if not self._keyed():
            return
        try:
            if u.path == "/api/bx/status":
                self._send(200, status_payload())
            elif u.path == "/api/bx/candidates":
                d = _read_json("bx_candidates_latest.json")
                self._send(200 if d else 404, d or {"ok": False, "error": "no BX candidates yet (08:02 HKT)"})
            elif u.path == "/api/bx-ui":
                import bx_view
                self._send(200, bx_view.build(OUT_DIR))
            elif u.path == "/api/bx/radar":
                tf = (parse_qs(u.query).get("tf") or ["1d"])[0]
                name = "bx_tradfi_radar.json" if tf == "tradfi" else f"bx_radar_{tf if tf in ('1d', '4h', '1h') else '1d'}.json"
                d = _read_json(name)
                self._send(200 if d else 404, d or {"ok": False, "error": "BX radar not built yet"})
            elif u.path == "/api/bx/review":
                m = _read_json("bx_meta.json")
                self._send(200, {"ok": bool(m), "ts": m.get("ts"), "review": m.get("review") or []})
            elif u.path == "/api/bx/shadow":
                import bx_shadow
                self._send(200, {"ok": True, "compare": bx_shadow.compare(bx_live.connect()),
                                 "probe_runs": (_read_json("bx_probe_log.json").get("runs") or [])[-40:]})
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}") if length else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if u.path == "/api/bx/breaker/reset":
            ok, msg = bx_live.reset_breaker(self.headers.get("X-BX-Admin-Key") or "")
            self._send(200 if ok else 403, {"ok": ok, "message": msg})
            return
        if not self._keyed():
            return
        if u.path == "/api/bx/decision":
            decs = body.get("decisions")
            if not isinstance(decs, list):
                self._send(400, {"ok": False, "error": "need {decisions: [...]}"})
                return
            res = bx_live.store_decisions(decs, str(body.get("source") or "claude"))
            self._send(200 if res.get("ok") else 422, res)
            return
        self._send(404, {"ok": False, "error": "not found"})


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    hkt = pytz.timezone("Asia/Hong_Kong")
    s = BackgroundScheduler(timezone=hkt)
    common = dict(max_instances=1, coalesce=True, misfire_grace_time=600)
    s.add_job(run_job, CronTrigger(hour=8, minute=2, timezone=hkt), args=["daily"], id="bx_daily", **common)
    s.add_job(run_job, CronTrigger(hour=8, minute=56, timezone=hkt), args=["entries"], id="bx_entries", **common)
    s.add_job(run_job, CronTrigger(hour="0,4,8,12,16,20", minute=5, timezone=hkt), args=["4h"], id="bx_4h", **common)
    s.add_job(run_job, CronTrigger(minute=9, timezone=hkt), args=["1h"], id="bx_1h", **common)
    s.start()
    return s


def main() -> int:
    eg = bx_egress.check(force=True)
    bx_live._log(bx_egress.log_line(eg))
    if not eg.get("ok") and bx_live.env_on("BX_LIVE", "0"):
        bx_live._log(f"[BX_ALERT] BX_LIVE=1 but egress not verified non-US ({eg.get('reason')}): live orders blocked")
    if bx_live.env_on("BX_ENABLED", "1"):
        start_scheduler()
        bx_live._log("[BX] scheduler: 08:02 daily+candidates · 08:56 entries · 4h :05 · hourly :09 (HKT)")
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
