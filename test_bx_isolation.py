"""Bitunix shadow — isolation guarantees (Harbor's key requirement, 2026-09-29).

The HL order path must never read Bitunix data:
  1. no order-path module imports bx_* / cg_* (static AST check, including lazy imports inside functions);
  2. no order-path module names a bx_* file;
  3. entry candidates are identical with and without BX files on disk;
  4. the [DESK_DATA] payload builder (what ENTRY_DESK reads) does not reference BX;
  5. the BX worker runs with HL_API_PRIVATE_KEY stripped and uses its own lock;
  6. BX modules contain no order / signing / private-endpoint code.
"""
import ast
import inspect
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORDER_PATH = ["executor.py", "pending_worker.py", "exit_worker.py", "failsafe_exit_worker.py",
              "entry_candidates.py", "hl_exec.py", "exec_common.py", "pending_entries.py", "decisions.py",
              "exec_preflight.py", "align_hard_sl.py"]
BX_MODULES = ["bx_client.py", "bx_universe.py", "bx_radar.py", "bx_shadow.py", "cg_client.py", "bx_view.py",
              "bx_egress.py"]          # read-only modules: no order code (orders live only in bx_trade.py)


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__" and node.args:
            if isinstance(node.args[0], ast.Constant):
                names.add(str(node.args[0].value).split(".")[0])
    return names


class TestOrderPathNeverReadsBX(unittest.TestCase):
    def test_no_bx_imports_on_order_path(self):
        for f in ORDER_PATH:
            p = ROOT / f
            if not p.exists():
                continue
            bad = {n for n in _imports(p) if n.startswith(("bx_", "cg_")) or n in ("bx_client", "cg_client")}
            self.assertEqual(bad, set(), f"{f} imports BX modules: {bad}")

    def test_no_bx_file_names_on_order_path(self):
        for f in ORDER_PATH:
            p = ROOT / f
            if not p.exists():
                continue
            src = p.read_text(encoding="utf-8").lower()
            for token in ("bx_radar", "bx_meta", "bx_candles", "bx_shadow", "bx_tradfi", "bitunix", "coingecko",
                          "bx_live", "bx_trade", "bx_decisions", "bx_candidates", "bx_breaker"):
                self.assertNotIn(token, src, f"{f} mentions {token}")

    def test_candidates_identical_with_bx_files_present(self):
        import entry_candidates as ec
        now_ms = 1_790_000_000_000
        r1d = {"ts": "2026-09-29T00:05:00+00:00", "rows": [
            {"symbol": "AAA", "close": 11, "upper": 10, "filter": 9, "lower": 8, "trend": "Green",
             "dual_cross_up": True, "bar_time": now_ms, "tier": "small"},
            {"symbol": "BBB", "close": 5, "upper": 6, "filter": 4, "lower": 3, "trend": "Green",
             "dual_cross_up": False, "bar_time": now_ms, "tier": "small"}]}
        r4h = {"ts": "2026-09-29T00:05:00+00:00", "rows": [
            {"symbol": "AAA", "close": 11, "upper": 10.5, "filter": 10, "lower": 9, "trend": "Green",
             "dual_cross_up": False, "bar_time": now_ms},
            {"symbol": "BBB", "close": 5.2, "upper": 5.1, "filter": 4.8, "lower": 4.5, "trend": "Green",
             "dual_cross_up": True, "bar_time": now_ms}]}
        base = ec.build_candidates(r1d, r4h, [])
        tmp = tempfile.mkdtemp()
        out = ROOT / "out"
        created = []
        try:
            out.mkdir(exist_ok=True)
            for name in ("bx_radar_1d.json", "bx_radar_4h.json", "bx_meta.json"):
                p = out / name
                if not p.exists():
                    p.write_text(json.dumps({"rows": [{"symbol": "AAA", "dual_cross_up": True, "trend": "Green",
                                                       "close": 999, "upper": 1}]}))
                    created.append(p)
            again = ec.build_candidates(r1d, r4h, [])
        finally:
            for p in created:
                p.unlink()
            shutil.rmtree(tmp, ignore_errors=True)
        strip = lambda d: [{k: v for k, v in c.items() if k != "generated_at"} for c in d["candidates"]]
        self.assertEqual(strip(base), strip(again))

    def test_desk_data_payload_has_no_bx(self):
        import serve
        src = inspect.getsource(serve._build_desk_data_payload).lower()
        for token in ("bx_", "bitunix"):
            self.assertNotIn(token, src)
        src_c = inspect.getsource(serve._generate_entry_candidates).lower() if hasattr(serve, "_generate_entry_candidates") else ""
        self.assertNotIn("bx_", src_c)


class TestLivePilotBoundary(unittest.TestCase):
    """Live Bitunix orders exist only in the Singapore bx-exec service (bx_service -> bx_live -> bx_trade)."""

    def test_cockpit_never_imports_trade_code(self):
        names = _imports(ROOT / "serve.py")
        self.assertFalse(names & {"bx_trade", "bx_live", "bx_service", "bx_egress"}, names)

    def test_only_bx_live_imports_bx_trade(self):
        users = {p.name for p in ROOT.glob("*.py")
                 if not p.name.startswith("test_") and "bx_trade" in _imports(p) and p.name != "bx_trade.py"}
        self.assertEqual(users - {"bx_live.py", "bx_service.py"}, set(), users)

    def test_service_never_touches_hl_private_key(self):
        src = (ROOT / "bx_service.py").read_text(encoding="utf-8")
        self.assertIn('env.pop("HL_API_PRIVATE_KEY", None)', src)
        for f in ("bx_live.py", "bx_trade.py", "bx_service.py"):
            names = _imports(ROOT / f)
            self.assertFalse(names & {"hl_exec", "hyperliquid", "eth_account", "executor", "pending_worker"},
                             f"{f}: {names}")


class TestBXWorkerSandbox(unittest.TestCase):
    def test_bx_job_strips_hl_key_and_uses_own_lock(self):
        import serve
        src = inspect.getsource(serve._scheduled_bx)
        self.assertIn("force_dry_run=True", src)
        self.assertIn("_bx_lock", src)
        self.assertNotIn("_scan_lock", src)
        self.assertNotIn("_exec_lock", src)

    def test_force_dry_run_strips_private_key(self):
        import serve
        captured = {}

        class P:
            returncode, stdout, stderr = 0, "{}", ""

        def fake_run(cmd, **kw):
            captured.update(kw["env"])
            return P()
        orig = serve.subprocess.run
        serve.subprocess.run = fake_run
        os.environ["HL_API_PRIVATE_KEY"] = "0xdeadbeef"
        try:
            serve._run_worker("bx_radar.py", ["daily"], timeout=5, force_dry_run=True)
        finally:
            serve.subprocess.run = orig
            os.environ.pop("HL_API_PRIVATE_KEY", None)
        self.assertNotIn("HL_API_PRIVATE_KEY", captured)
        self.assertEqual(captured.get("EXEC_DRY_RUN"), "1")

    def test_bx_modules_have_no_order_code(self):
        for f in BX_MODULES:
            src = (ROOT / f).read_text(encoding="utf-8")
            names = _imports(ROOT / f)
            self.assertFalse(names & {"hl_exec", "hyperliquid", "eth_account", "executor", "pending_worker"},
                             f"{f} imports order code: {names}")
            for token in ("/api/v1/futures/trade", "place_order", "signature", "secretKey"):
                self.assertNotIn(token, src, f"{f} contains {token}")
            if f != "cg_client.py":  # CoinGecko's public Demo key header is fine; Bitunix private auth is not
                for token in ("api-key", "nonce", "BITUNIX_API", "BX_API_KEY"):
                    self.assertNotIn(token, src, f"{f} contains {token}")

    def test_bx_shadow_never_touches_hl_pending_or_live_ledger(self):
        import bx_shadow
        self.assertNotEqual(bx_shadow.pending_path().name, "pending_entries.json")
        self.assertIn("bx_shadow_ledger.db", bx_shadow.db_path())
        self.assertIn("mode=ro", inspect.getsource(bx_shadow._hl_rows_readonly))


if __name__ == "__main__":
    unittest.main()
