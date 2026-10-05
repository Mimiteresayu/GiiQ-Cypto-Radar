"""W40 item 2: entry re-runs (keyed /api/exec/run, password /api/jobs/run executor) are blocked from
08:55 HKT (EXEC_RERUN_CUTOFF_HKT) until midnight; the scheduled 08:55 run is never blocked.
No network, no trading: the executor worker is mocked."""
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import exec_common as ec
import serve

HKT = ec.HKT
AI_KEY = "ai-test-key"
WORKER_OUT = json.dumps({"mode": "DRY_RUN", "status": "fail_closed", "message": "No approved candidates"})


def hkt(h, m, s=0, day=5):
    return datetime(2026, 10, day, h, m, s, tzinfo=HKT)


class TestHelper(unittest.TestCase):
    def test_before_cutoff_allowed(self):
        for t in (hkt(0, 0), hkt(8, 0), hkt(8, 54), hkt(8, 54, 59)):
            self.assertFalse(ec.entry_rerun_blocked(t)[0], t)

    def test_at_and_after_cutoff_blocked(self):
        for t in (hkt(8, 55), hkt(8, 55, 1), hkt(9, 4), hkt(9, 30), hkt(23, 59, 59)):
            blocked, why = ec.entry_rerun_blocked(t)
            self.assertTrue(blocked, t)
            self.assertIn("08:55", why)

    def test_date_boundary_reallowed_until_cutoff(self):
        self.assertTrue(ec.entry_rerun_blocked(hkt(23, 59, day=5))[0])
        self.assertFalse(ec.entry_rerun_blocked(hkt(0, 0, day=6))[0])
        self.assertFalse(ec.entry_rerun_blocked(hkt(8, 54, day=6))[0])
        self.assertTrue(ec.entry_rerun_blocked(hkt(8, 55, day=6))[0])

    def test_utc_and_naive_inputs_use_hkt_wall_clock(self):
        from datetime import timezone
        self.assertTrue(ec.entry_rerun_blocked(datetime(2026, 10, 5, 0, 55, tzinfo=timezone.utc))[0])    # 08:55 HKT
        self.assertFalse(ec.entry_rerun_blocked(datetime(2026, 10, 5, 0, 54, tzinfo=timezone.utc))[0])
        self.assertTrue(ec.entry_rerun_blocked(datetime(2026, 10, 5, 8, 55))[0])                        # naive = HKT

    def test_cutoff_env(self):
        self.assertEqual(ec.exec_rerun_cutoff_hkt({}), "08:55")
        self.assertEqual(ec.exec_rerun_cutoff_hkt({"EXEC_RERUN_CUTOFF_HKT": "9:05"}), "09:05")
        for bad in ("", "nope", "25:00", "08:61", "08"):
            self.assertEqual(ec.exec_rerun_cutoff_hkt({"EXEC_RERUN_CUTOFF_HKT": bad}), "08:55", bad)
        self.assertFalse(ec.entry_rerun_blocked(hkt(9, 0), "09:05")[0])
        self.assertTrue(ec.entry_rerun_blocked(hkt(9, 5), "09:05")[0])
        self.assertTrue(ec.entry_rerun_blocked(hkt(8, 55), "garbage")[0])                              # default


class _Serve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [patch.object(serve, "SCHEDULER_STATUS_PATH", os.path.join(self.tmp, "s.json")),
                        patch.object(serve, "OUT_DIR", self.tmp),
                        patch.object(serve, "RUN_REPORT_DIR", os.path.join(self.tmp, "run_reports")),
                        patch.dict(os.environ, {"EXEC_RERUN_CUTOFF_HKT": ""})]
        for p in self.patches:
            p.start()
        self.worker = patch.object(serve, "_run_worker", return_value=(0, WORKER_OUT, "")).start()

    def tearDown(self):
        patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_at(self, now, **kw):
        err = io.StringIO()
        with patch.object(serve, "_hkt_now", return_value=now), patch("sys.stderr", err):
            res = serve._scheduled_executor(**kw)
        return res, err.getvalue()

    def report_runs(self, day="2026-10-05"):
        try:
            with open(os.path.join(self.tmp, "run_reports", f"{day}.json")) as f:
                return json.load(f)["runs"]
        except FileNotFoundError:
            return []


class TestExecutorPaths(_Serve):
    def test_manual_before_cutoff_runs(self):
        res, _ = self.run_at(hkt(8, 54), manual=True)
        self.assertEqual(self.worker.call_count, 1)
        self.assertTrue(self.worker.call_args.kwargs["force_dry_run"])
        self.assertEqual(res["status"], "fail_closed")

    def test_manual_and_keyed_blocked_at_or_after_cutoff(self):
        for t in (hkt(8, 55), hkt(9, 30), hkt(23, 59)):
            for kw, job in (({"manual": True}, "manual_executor"), ({"live_api": True}, "api_exec_run")):
                res, err = self.run_at(t, **kw)
                self.assertEqual(res["status"], "blocked_after_cutoff", (t, kw))
                self.assertEqual((res["cutoff"], res["now_hkt"]), ("08:55", t.strftime("%H:%M")))
                self.assertIn(f"[EXEC_GUARD] blocked {job} at {t:%H:%M} HKT (cutoff 08:55)", err)
                st = serve._get_scheduler_status()[job]
                self.assertEqual(st["status"], "skipped")
                self.assertIn("08:55", st["message"])
        self.worker.assert_not_called()

    def test_block_writes_run_report_entry(self):
        with patch.object(serve, "_record_run_report", wraps=serve._record_run_report) as rec:
            self.run_at(hkt(9, 30), live_api=True)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], "api_exec_run")
        self.assertEqual(rec.call_args.args[1]["status"], "blocked_after_cutoff")
        runs = [r for r in self.report_runs() if r.get("job") == "api_exec_run"]
        self.assertTrue(runs, "run report entry missing")
        r = runs[-1]
        self.assertEqual((r["status"], r["cutoff"]), ("blocked_after_cutoff", "08:55"))
        self.assertEqual(r["mode"], "DRY_RUN")                                 # no key in the test env

    def test_scheduled_run_never_blocked(self):
        for t in (hkt(8, 55), hkt(9, 4), hkt(23, 0)):
            res, err = self.run_at(t)
            self.assertEqual(res["status"], "fail_closed", t)
            self.assertNotIn("[EXEC_GUARD]", err)
        self.assertEqual(self.worker.call_count, 3)
        self.assertFalse(any(c.kwargs["force_dry_run"] for c in self.worker.call_args_list))

    def test_next_day_reallowed_until_cutoff(self):
        self.run_at(hkt(23, 59, day=5), manual=True)
        self.worker.assert_not_called()
        self.run_at(hkt(0, 0, day=6), manual=True)
        self.run_at(hkt(8, 54, day=6), live_api=True)
        self.assertEqual(self.worker.call_count, 2)

    def test_cutoff_env_override(self):
        with patch.dict(os.environ, {"EXEC_RERUN_CUTOFF_HKT": "09:30"}):
            self.run_at(hkt(9, 0), live_api=True)
            self.assertEqual(self.worker.call_count, 1)
            res, err = self.run_at(hkt(9, 30), live_api=True)
        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(res["cutoff"], "09:30")
        self.assertIn("(cutoff 09:30)", err)

    def test_manual_job_thread_blocked(self):
        done = threading.Event()
        orig = serve._scheduled_executor

        def wrapped(**kw):
            try:
                return orig(**kw)
            finally:
                done.set()
        with patch.object(serve, "_hkt_now", return_value=hkt(10, 0)), patch.dict(serve.MANUAL_JOBS, {"executor": wrapped}):
            ok, _ = serve._start_manual_job("executor")
            self.assertTrue(ok)
            self.assertTrue(done.wait(10))
        self.worker.assert_not_called()
        self.assertEqual(serve._get_scheduler_status()["manual_executor"]["status"], "skipped")


class TestExecRunHttp(_Serve):
    def setUp(self):
        super().setUp()
        patch.object(serve, "AI_DECISION_KEY", AI_KEY).start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, now, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}/api/exec/run", data=b"{}",
                                     method="POST", headers={"Content-Type": "application/json", **(headers or {})})
        with patch.object(serve, "_hkt_now", return_value=now), patch("sys.stderr", io.StringIO()):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

    def test_409_shape_after_cutoff(self):
        code, body = self.post(hkt(9, 30), {"X-AI-Key": AI_KEY})
        self.assertEqual(code, 409)
        self.assertEqual({k: body[k] for k in ("ok", "status", "cutoff", "now_hkt")},
                         {"ok": False, "status": "blocked_after_cutoff", "cutoff": "08:55", "now_hkt": "09:30"})
        self.worker.assert_not_called()

    def test_before_cutoff_runs(self):
        code, body = self.post(hkt(8, 30), {"X-AI-Key": AI_KEY})
        self.assertEqual(code, 200)
        self.assertEqual(body["result"]["status"], "fail_closed")
        self.assertEqual(self.worker.call_count, 1)

    def test_key_still_required(self):
        self.assertEqual(self.post(hkt(9, 30))[0], 403)
        self.worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
