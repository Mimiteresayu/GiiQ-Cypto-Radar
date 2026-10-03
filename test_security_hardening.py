"""Security hardening step 1: scheduler/status auth, public radar trimming, X-AI-Key header on
keyed endpoints, key redaction in request logs. Real HTTP against serve.Handler; no network, no trading."""
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import serve

AI_KEY = "ai-test-key"
ENTRY_KEY = "entry-test-key"
PASSWORD = "pw-test"

SECRET_FIELDS = ("gc_params", "universe_requested", "universe_source", "universe_floors", "narrative_map",
                 "narrative_forced", "narrative_not_on_hl", "narrative_retry_after_ms", "cemetery_forced")

KEYED_GETS = ("/api/ai/candidates", "/api/ai/narrative", "/api/exec/preflight", "/api/exit/health",
              "/api/ai/dimensions", "/api/dimensions/report", "/api/whales", "/api/exec/pending",
              "/api/bx/radar", "/api/bx/shadow", "/api/bx/review", "/api/bx/status", "/api/bx/day")


def _radar(tf):
    return {"tf": tf, "ts": "2026-10-03T00:00:00+00:00",
            "gc_params": {"source": "hlc3", "poles": 4, "period": 144, "mult": 1.414},
            "universe_source": "hl", "universe_requested": ["BTC", "ETH", "SOL"],
            "universe_floors": {"min_day_ntl_vlm": 1}, "narrative_map": {"FOO": "FOO"},
            "narrative_forced": ["FOO"], "narrative_not_on_hl": [], "narrative_retry_after_ms": {},
            "cemetery_forced": [], "breadth": {"n": 2}, "flags": {"dual_cross_up": ["BTC"]},
            "rows": [{"symbol": "BTC", "trend": "Green"}, {"symbol": "ETH", "trend": "Red"}]}


class _Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        for tf in ("1h", "4h", "1d"):
            with open(os.path.join(out, f"gc_radar_{tf}.json"), "w") as f:
                json.dump(_radar(tf), f)
        with open(os.path.join(out, "entry_candidates_latest.json"), "w") as f:
            json.dump({"generated_at": "x", "candidates": []}, f)
        self.stack = ExitStack()
        for name, val in (("OUT_DIR", out), ("PASSWORD", PASSWORD), ("AI_DECISION_KEY", AI_KEY),
                          ("ENTRY_READ_KEY", ENTRY_KEY), ("SCHEDULER_ENABLED", True),
                          ("BX_SERVICE_URL", "http://bx.invalid"),
                          ("_bx_service", lambda path, body=None, timeout=15.0: (200, {"ok": True, "stub": path})),
                          ("_get_scheduler_status", lambda: {"executor": {"status": "success", "mode": "LIVE"}}),
                          ("_next_runs", lambda: {"executor": "08:55"}),
                          ("_get_hl_cached", lambda: {}), ("_get_hl_meta_cached", lambda: {}),
                          ("_compute_unified_equity", lambda d: {}),
                          ("_exit_health", lambda *a, **k: {"ok": True}),
                          ("_read_exec_preflight", lambda: {}), ("_pending_view", lambda: [])):
            self.stack.enter_context(patch.object(serve, name, val))
        self.stack.enter_context(patch.dict(os.environ, {"DIM_LEDGER_PATH": os.path.join(self.tmp, "l.db")}))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.stack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}{path}",
                                     headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestSchedulerStatusAuth(_Server):
    def _full(self, code, body):
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertTrue(d["enabled"])
        self.assertIn("jobs", d)
        self.assertIn("next_runs_hkt", d)

    def test_unauthenticated_401_without_details(self):
        for path, headers in (("/api/scheduler/status", {}),
                              ("/api/scheduler/status?key=wrong", {}),
                              ("/api/scheduler/status", {"X-AI-Key": "wrong"}),
                              ("/api/scheduler/status", {"Cookie": "otr_session=forged"})):
            code, body = self.get(path, headers)
            self.assertEqual(code, 401, (path, headers))
            self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_cookie(self):
        self._full(*self.get("/api/scheduler/status",
                             {"Cookie": f"{serve.COOKIE_NAME}={serve._token_for(PASSWORD)}"}))

    def test_cockpit_bearer(self):
        self._full(*self.get("/api/scheduler/status", {"Authorization": f"Bearer {PASSWORD}"}))

    def test_header_key(self):
        self._full(*self.get("/api/scheduler/status", {"X-AI-Key": AI_KEY}))
        self._full(*self.get("/api/scheduler/status", {"X-AI-Key": ENTRY_KEY}))

    def test_query_key_still_works(self):
        self._full(*self.get(f"/api/scheduler/status?key={AI_KEY}"))
        self._full(*self.get(f"/api/scheduler/status?key={ENTRY_KEY}"))


class TestPublicRadar(_Server):
    def test_no_strategy_params_or_watchlist(self):
        code, body = self.get("/api/public/radar")
        self.assertEqual(code, 200)
        d = json.loads(body)
        for tf in ("1h", "4h", "1d"):
            r = d[f"gc_radar_{tf}"]
            for k in SECRET_FIELDS:
                self.assertNotIn(k, r, (tf, k))
            self.assertEqual([x["symbol"] for x in r["rows"]], ["BTC", "ETH"])
            self.assertEqual(r["breadth"], {"n": 2})
        self.assertNotIn(b"gc_params", body)
        self.assertNotIn(b"universe_requested", body)
        self.assertNotIn(b"hlc3", body)

    def test_radar_file_on_disk_unchanged(self):
        self.get("/api/public/radar")
        with open(os.path.join(serve.OUT_DIR, "gc_radar_1d.json")) as f:
            self.assertEqual(json.load(f)["universe_requested"], ["BTC", "ETH", "SOL"])


class TestHeaderAuthOnKeyedEndpoints(_Server):
    def test_keyed_gets(self):
        for path in KEYED_GETS:
            self.assertEqual(self.get(path)[0], 403, path)
            self.assertEqual(self.get(path, {"X-AI-Key": "wrong"})[0], 403, path)
            code_h, body_h = self.get(path, {"X-AI-Key": AI_KEY})
            code_q, body_q = self.get(f"{path}?key={AI_KEY}")
            self.assertNotIn(code_h, (401, 403), path)
            self.assertEqual(code_h, code_q, path)

    def test_ai_candidates_header_same_as_query(self):
        code_h, body_h = self.get("/api/ai/candidates", {"X-AI-Key": AI_KEY})
        code_q, body_q = self.get(f"/api/ai/candidates?key={AI_KEY}")
        self.assertEqual((code_h, code_q), (200, 200))
        self.assertEqual(json.loads(body_h)["candidates"], json.loads(body_q)["candidates"])

    def test_entry_candidates_header(self):
        self.assertEqual(self.get("/api/entry-candidates")[0], 403)
        self.assertEqual(self.get("/api/entry-candidates", {"X-AI-Key": AI_KEY})[0], 403)
        self.assertEqual(self.get("/api/entry-candidates", {"X-AI-Key": ENTRY_KEY})[0], 200)
        self.assertEqual(self.get(f"/api/entry-candidates?key={ENTRY_KEY}")[0], 200)


class TestLogRedaction(_Server):
    def test_redact_helper(self):
        r = serve.redact_secrets
        self.assertEqual(r('"GET /api/x?key=abc123&tf=1d HTTP/1.1" 200 -'), '"GET /api/x?key=***&tf=1d HTTP/1.1" 200 -')
        self.assertEqual(r("/a?tf=1d&token=t0k&password=p%40ss"), "/a?tf=1d&token=***&password=***")
        self.assertEqual(r("/a?api_key=zz"), "/a?api_key=***")
        self.assertEqual(r("/api/whales?tf=1d"), "/api/whales?tf=1d")

    def test_access_and_error_logs_redacted(self):
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            self.get(f"/api/scheduler/status?key={AI_KEY}")
            self.get(f"/api/ai/candidates?key={AI_KEY}&token=tok123")
            self.get(f"/api/nope?key={AI_KEY}", method="POST")   # send_error -> log_error path
            self.get(f"/out/missing.json?key={AI_KEY}")         # static 404 (log_error)
        log = buf.getvalue()
        self.assertIn("key=***", log)
        self.assertIn("/api/scheduler/status", log)
        self.assertNotIn(AI_KEY, log)
        self.assertNotIn("tok123", log)


if __name__ == "__main__":
    unittest.main()
