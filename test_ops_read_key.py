"""OPS_READ_KEY: a GET-only monitor key. Accepted on read routes, refused on every write / decision route,
grants nothing when unset, and never appears in the request log. Real HTTP against serve.Handler; no trading."""
import io
import json
import os
import urllib.error
import urllib.request
from unittest.mock import patch

import serve
from test_security_hardening import AI_KEY, ENTRY_KEY, _Server

OPS_KEY = "ops-read-test-key"

READ_GETS = ("/api/exit/health", "/api/scheduler/status", "/api/exec/pending", "/api/ai/dimensions",
             "/api/bx/radar", "/api/bx/shadow", "/api/bx/review", "/api/bx/status", "/api/bx/day")
WRITE_ROUTES = (("POST", "/api/ai/decision"), ("POST", "/api/exec/run"), ("GET", "/api/exec/preflight"),
                ("POST", "/api/exec/preflight"), ("GET", "/api/ai/narrative"), ("POST", "/api/ai/narrative"),
                ("GET", "/api/ai/candidates"), ("GET", "/api/entry-candidates"), ("POST", "/api/ai/jobs/run"),
                ("POST", "/api/whales/watchlist"))


def _boom(*a, **k):
    raise AssertionError("write path reached with OPS_READ_KEY")


class _OpsServer(_Server):
    ops_key = OPS_KEY

    def setUp(self):
        super().setUp()
        with open(os.path.join(serve.OUT_DIR, "dimensions_latest.json"), "w") as f:
            json.dump({"ok": True}, f)
        for name, val in (("OPS_READ_KEY", self.ops_key), ("_scheduled_executor", _boom),
                          ("_scheduled_preflight", _boom), ("update_narrative_watchlist", _boom),
                          ("store_decisions", _boom), ("_start_manual_job", _boom)):
            self.stack.enter_context(patch.object(serve, name, val))

    def call(self, method, path, headers=None):
        h = dict(headers or {})
        if method == "POST":
            h.setdefault("Content-Type", "application/json")
        return self.get(path, h, method=method)


class TestOpsKeyReadRoutes(_OpsServer):
    def test_header_grants_read_routes(self):
        for path in READ_GETS:
            code, _ = self.call("GET", path, {"X-AI-Key": OPS_KEY})
            self.assertNotIn(code, (401, 403, 404), path)

    def test_query_key_still_works_on_read_routes(self):
        for path in READ_GETS:
            code, _ = self.call("GET", f"{path}?key={OPS_KEY}")
            self.assertNotIn(code, (401, 403, 404), path)

    def test_ai_key_still_works_on_read_routes(self):
        for path in READ_GETS:
            self.assertNotIn(self.call("GET", path, {"X-AI-Key": AI_KEY})[0], (401, 403, 404), path)

    def test_wrong_key_refused(self):
        for path in READ_GETS:
            self.assertIn(self.call("GET", path, {"X-AI-Key": OPS_KEY + "x"})[0], (401, 403), path)

    def test_ops_key_works_when_ai_key_unset(self):
        with patch.object(serve, "AI_DECISION_KEY", ""), patch.object(serve, "ENTRY_READ_KEY", ""):
            self.assertEqual(self.call("GET", "/api/exit/health", {"X-AI-Key": OPS_KEY})[0], 200)
            self.assertEqual(self.call("GET", "/api/scheduler/status", {"X-AI-Key": OPS_KEY})[0], 200)
            self.assertEqual(self.call("POST", "/api/ai/decision", {"X-AI-Key": OPS_KEY})[0], 404)


class TestOpsKeyNeverWrites(_OpsServer):
    def test_write_and_decision_routes_refuse_ops_key(self):
        for method, path in WRITE_ROUTES:
            for headers, p in (({"X-AI-Key": OPS_KEY}, path), ({}, f"{path}?key={OPS_KEY}")):
                code, _ = self.call(method, p, headers)
                self.assertEqual(code, 403, (method, p))

    def test_ai_key_still_reaches_write_routes(self):
        with patch.object(serve, "_scheduled_executor", lambda **k: {"status": "success"}):
            self.assertEqual(self.call("POST", "/api/exec/run", {"X-AI-Key": AI_KEY})[0], 200)


class TestOpsKeyUnsetGrantsNothing(_OpsServer):
    ops_key = ""

    def test_empty_and_random_keys_refused(self):
        for path in READ_GETS:
            for headers in ({"X-AI-Key": ""}, {"X-AI-Key": OPS_KEY}, {}):
                self.assertIn(self.call("GET", path, headers)[0], (401, 403), (path, headers))

    def test_other_keys_unchanged(self):
        self.assertEqual(self.call("GET", "/api/scheduler/status", {"X-AI-Key": ENTRY_KEY})[0], 200)
        self.assertEqual(self.call("GET", "/api/exit/health", {"X-AI-Key": AI_KEY})[0], 200)


class TestOpsKeyNotLogged(_OpsServer):
    def test_query_key_redacted_in_log(self):
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            self.call("GET", f"/api/exit/health?key={OPS_KEY}")
            self.call("GET", f"/api/scheduler/status?key={OPS_KEY}")
            self.call("POST", f"/api/exec/run?key={OPS_KEY}")
        log = buf.getvalue()
        self.assertIn("key=***", log)
        self.assertNotIn(OPS_KEY, log)


class TestCockpitLogsBx422(_Server):
    def test_bx_422_body_logged_without_key(self):
        bx = {"ok": False, "stored": 0, "rejected": [{"symbol": "ZZZUSDT", "why": "not in today's BX candidate list"}]}
        buf = io.StringIO()
        with patch.object(serve, "_bx_service", lambda path, body=None, timeout=15.0: (422, dict(bx))), \
                patch.dict(os.environ, {"DECISIONS_DIR": os.path.join(self.tmp, "dec")}), patch("sys.stderr", buf):
            req_body = json.dumps({"bx_decisions": [{"symbol": "ZZZUSDT", "decision": "approve"}]}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}/api/ai/decision?key={AI_KEY}",
                                         data=req_body, method="POST", headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        self.assertEqual(code, 422)
        log = buf.getvalue()
        self.assertIn("[AI_DECISION] bx-exec 422", log)
        self.assertIn("not in today's BX candidate list", log)
        self.assertNotIn(AI_KEY, log)
