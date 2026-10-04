"""Read-only guarantees, secret hygiene, alert / persistence wiring and the --dry-run mode. No network: urlopen is
replaced by a fake that serves the fixture payloads and records every request."""
import ast
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ops_cron import main as ops_main
from ops_cron import persist, sources

PKG = Path(__file__).resolve().parents[1]
FIX = PKG / "tests" / "fixtures"
SECRET_KEY = "ai-key-SECRET-123"
SECRET_PW = "smtp-PASSWORD-456"
SECRET_HOOK = "https://hooks.example.invalid/T000/SECRET-HOOK-789"
SECRET_DSN = "postgresql://ops:DSN-PASS-000@db.invalid/brain"
ENV = {"COCKPIT_URL": "https://cockpit.example.invalid", "COCKPIT_AI_KEY": SECRET_KEY,
       "HL_ADDRESS": "0x0000000000000000000000000000000000000001"}
ALLOWED_GETS = ("/api/exit/health", "/api/scheduler/status", "/api/bx/status", "/api/bx/day",
                "/api/exec/run-report", "/api/exec/pending")


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeHTTP:
    """urlopen stand-in: cockpit paths and HL info types -> fixture `data`."""

    def __init__(self, inputs):
        self.inputs, self.requests = inputs, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.requests.append({"url": req.full_url, "method": req.get_method(), "headers": dict(req.header_items()),
                              "body": body})
        if req.full_url.startswith(ENV["COCKPIT_URL"]):
            path = req.full_url[len(ENV["COCKPIT_URL"]):].split("?")[0]
            name = {"/api/exit/health": "exit_health", "/api/scheduler/status": "scheduler",
                    "/api/bx/status": "bx_status", "/api/bx/day": "bx_day", "/api/exec/pending": "pending"}.get(path)
            if path == "/api/exec/run-report":
                name = "run_report_today" if "2026-10-04" in req.full_url else "run_report_yesterday"
            data = (self.inputs.get(name) or {}).get("data", {})
        else:
            t = body["type"]
            data = {"clearinghouseState": (self.inputs.get("hl_state") or {}).get("data"),
                    "frontendOpenOrders": (self.inputs.get("hl_orders") or {}).get("data"),
                    "userFillsByTime": (self.inputs.get("hl_fills") or {}).get("data"),
                    "candleSnapshot": ((self.inputs.get("candles") or {}).get((body.get("req") or {}).get("coin"))
                                       or {}).get("data", [])}[t]
        return _Resp(data)


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


def run_main(argv, env, fake=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if fake is not None:
            with patch("urllib.request.urlopen", fake):
                rc = ops_main.main(argv, env)
        else:
            rc = ops_main.main(argv, env)
    return rc, out.getvalue(), err.getvalue()


class TestReadOnly(unittest.TestCase):
    def test_only_stdlib_and_postgres_driver_imports(self):
        allowed = set(sys.stdlib_module_names) | {"psycopg", "psycopg2"}
        for f in PKG.glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [node.module.split(".")[0]]
                else:
                    continue
                for m in mods:
                    self.assertIn(m, allowed, f"{f.name} imports {m}: ops_cron must not import repo / exchange code")

    def _requests(self, check, fx_name):
        fx = fixture(fx_name)
        fake = FakeHTTP(fx["inputs"])
        rc, _, _ = run_main([check, "--dry-run", "--now", fx["now"]], ENV, fake)
        self.assertEqual(rc, 0)
        return fake.requests

    def test_cockpit_get_only_with_header_key_and_hl_read_types_only(self):
        reqs = self._requests("exit-monitor", "exit_ok.json") + self._requests("daily-audit", "audit_ok.json")
        cockpit = [r for r in reqs if r["url"].startswith(ENV["COCKPIT_URL"])]
        hl = [r for r in reqs if not r["url"].startswith(ENV["COCKPIT_URL"])]
        self.assertTrue(cockpit and hl)
        for r in cockpit:
            self.assertEqual(r["method"], "GET", r["url"])
            self.assertTrue(r["url"][len(ENV["COCKPIT_URL"]):].startswith(ALLOWED_GETS), r["url"])
            self.assertEqual(r["headers"].get("X-ai-key"), SECRET_KEY)
            self.assertNotIn(SECRET_KEY, r["url"])
        for r in hl:
            self.assertEqual(r["url"], "https://api.hyperliquid.xyz/info")
            self.assertIn(r["body"]["type"], sources.HL_READ_TYPES)
        self.assertIn("candleSnapshot", {r["body"]["type"] for r in hl})

    def test_hl_client_refuses_non_read_types(self):
        src = sources.Sources("", "", "0x1", "https://api.hyperliquid.xyz/info")
        for t in ("order", "cancel", "updateLeverage", "usdSend", None):
            with self.assertRaises(ValueError):
                src.hl({"type": t})

    def test_missing_config_is_reported_not_raised(self):
        r = sources.Sources("", "", "", "https://x.invalid/info").cockpit("/api/exit/health")
        self.assertEqual((r["ok"], r["error"]), (False, "COCKPIT_URL / COCKPIT_AI_KEY not set"))


class TestRunModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def problem_fixture(self):
        fx = fixture("exit_ok.json")
        fx["inputs"]["hl_orders"]["data"] = []
        p = Path(self.tmp.name) / "exit_problem.json"
        p.write_text(json.dumps(fx), encoding="utf-8")
        return str(p)

    def test_dry_run_prints_report_and_sends_nothing(self):
        with patch("ops_cron.alerts.send", side_effect=AssertionError("alert sent")), \
                patch("ops_cron.persist.insert", side_effect=AssertionError("db write")):
            rc, out, _ = run_main(["exit-monitor", "--dry-run", "--fixture", self.problem_fixture()],
                                  {**ENV, "ALERT_WEBHOOK_URL": SECRET_HOOK, "BRAIN_DSN": SECRET_DSN})
        self.assertEqual(rc, 0)
        self.assertIn("## Exit monitor", out)
        self.assertIn("`NO_SL` SOL", out)
        self.assertIn('"status": "problem"', out)

    def test_ok_run_one_json_line_no_alert(self):
        with patch("ops_cron.alerts.send", side_effect=AssertionError("alert sent")):
            rc, out, _ = run_main(["exit-monitor", "--fixture", str(FIX / "exit_ok.json")], ENV)
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        rep = json.loads(lines[0])
        self.assertEqual((rep["status"], rep["alerted"], rep["persisted"]), ("ok", False, "skipped (BRAIN_DSN not set)"))

    def test_problem_run_alerts_webhook_and_email_without_leaking_secrets(self):
        sent = {}
        env = {**ENV, "ALERT_WEBHOOK_URL": SECRET_HOOK, "SMTP_HOST": "smtp.invalid", "SMTP_USER": "ops",
               "SMTP_PASSWORD": SECRET_PW, "ALERT_EMAIL_TO": "owner@example.invalid"}
        with patch("ops_cron.alerts._webhook", side_effect=lambda url, text, t: sent.setdefault("hook", text) and "sent"), \
                patch("ops_cron.alerts._email", side_effect=lambda e, s, text, t: sent.setdefault("mail", s) and "sent"):
            rc, out, err = run_main(["exit-monitor", "--fixture", self.problem_fixture()], env)
        self.assertEqual(rc, 0)
        rep = json.loads(out.strip())
        self.assertEqual(rep["status"], "problem")
        self.assertTrue(rep["alerted"])
        self.assertEqual(rep["alert"], {"log": "sent", "webhook": "sent", "email": "sent"})
        self.assertIn("NO_SL", sent["hook"])
        self.assertTrue(sent["mail"].startswith("[ops] PROBLEM exit monitor"))
        self.assertIn("[OPS_ALERT]", err)
        for s in (SECRET_KEY, SECRET_PW, SECRET_HOOK):
            self.assertNotIn(s, out + err)

    def test_no_sink_configured_logs_structured_alert(self):
        rc, out, err = run_main(["exit-monitor", "--fixture", self.problem_fixture()], ENV)
        rep = json.loads(out.strip())
        self.assertEqual(rep["alert"], {"log": "sent"})
        self.assertFalse(rep["alerted"])
        line = next(l for l in err.splitlines() if l.startswith("[OPS_ALERT] {"))
        self.assertIn("NO_SL", json.loads(line[len("[OPS_ALERT] "):])["text"])

    def test_failed_sink_is_reported(self):
        with patch("ops_cron.alerts._webhook", side_effect=OSError("boom")):
            rc, out, _ = run_main(["exit-monitor", "--fixture", self.problem_fixture()],
                                  {**ENV, "ALERT_WEBHOOK_URL": SECRET_HOOK})
        self.assertEqual(json.loads(out.strip())["alert"]["webhook"], "error: OSError")

    def test_daily_audit_ok_summary_opt_in(self):
        with patch("ops_cron.alerts.send", return_value={"log": "sent"}) as send:
            run_main(["daily-audit", "--fixture", str(FIX / "audit_ok.json")], ENV)
            send.assert_not_called()
            run_main(["daily-audit", "--fixture", str(FIX / "audit_ok.json")], {**ENV, "OPS_AUDIT_SEND_OK": "1"})
            send.assert_called_once()

    def test_brain_insert_only(self):
        executed = []

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                executed.append((sql, params))

        class Conn:
            def cursor(self):
                return Cur()

            def commit(self):
                pass

            def close(self):
                pass

        with patch("ops_cron.persist._connect", return_value=Conn()):
            rc, out, _ = run_main(["daily-audit", "--fixture", str(FIX / "audit_ok.json")], {**ENV, "BRAIN_DSN": SECRET_DSN})
        rep = json.loads(out.strip())
        self.assertEqual(rep["persisted"], "ok")
        (sql, params), = executed
        self.assertTrue(sql.startswith("INSERT INTO raw.ops_check_run "))
        self.assertEqual(params[1:4], ("daily_audit", rep["run_at"], "ok"))
        self.assertEqual(json.loads(params[7])["run_id"], rep["run_id"])
        self.assertNotIn(SECRET_DSN, out)

    def test_brain_error_never_leaks_dsn(self):
        with patch("ops_cron.persist._connect", side_effect=RuntimeError(f"cannot connect to {SECRET_DSN}")):
            res = persist.insert({"run_id": "r", "check": "c", "run_at": "t", "status": "ok"}, SECRET_DSN)
        self.assertTrue(res.startswith("error: RuntimeError"))
        self.assertNotIn(SECRET_DSN, res)

    def test_crash_exits_1_and_alerts(self):
        with patch("ops_cron.rules.exit_monitor", side_effect=KeyError("boom")), \
                patch("ops_cron.alerts.send", return_value={"log": "sent"}) as send:
            rc, out, _ = run_main(["exit-monitor", "--fixture", str(FIX / "exit_ok.json")], ENV)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.strip())["problems"][0]["code"], "OPS_CRASH")
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
