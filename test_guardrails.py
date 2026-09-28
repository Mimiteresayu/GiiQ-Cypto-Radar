#!/usr/bin/env python3
"""GIIQ-SoT-1 guardrails: radar row-count, price sanity, 1% NAV minimum order, NAV snapshot,
exits -> re-fetch -> entries order, run report, SoT id. HL fully mocked; no network, no orders."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exec_common as ec  # noqa: E402
import executor  # noqa: E402
import pending_worker  # noqa: E402
from test_live_execution import NOW, EnvMixin, FakeHL, _cand, _cands  # noqa: E402

# Real HL numbers of the main (unified) wallet on 2026-09-28 12:3x HKT
SPOT_UNIFIED = {"balances": [{"coin": "USDC", "total": "1691.795646", "hold": "96.44505"}]}
PERP_UNIFIED = {"marginSummary": {"accountValue": "96.44505", "totalMarginUsed": "96.44505"}, "assetPositions": []}


def _radar(n, requested=None):
    r = {"rows": [{"symbol": f"C{i}", "close": 1.0} for i in range(n)]}
    if requested is not None:
        r["universe_requested"] = [f"C{i}" for i in range(requested)]
    return r


class TestSotId(unittest.TestCase):
    def test_sot_id(self):
        self.assertEqual(ec.SOT_ID, "GIIQ-SoT-1")
        doc = (ROOT / "docs" / "SOT_CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("GIIQ-SoT-1", doc)
        self.assertTrue((ROOT / "docs" / "reference" / "signum_workflow_reference.md").is_file())


class TestRowCount(unittest.TestCase):
    def setUp(self):
        self._p = patch.dict(os.environ, {"EXEC_RADAR_MIN_ROWS": "120"})
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_normal_counts_pass(self):
        for n in (173, 176, 177):
            self.assertTrue(ec.radar_rowcount_ok(_radar(n, n), "1d")[0])

    def test_below_absolute_floor_fails(self):
        ok, why = ec.radar_rowcount_ok(_radar(119), "4h")
        self.assertFalse(ok)
        self.assertIn("119 rows < minimum 120", why)

    def test_below_universe_fraction_fails(self):
        ok, why = ec.radar_rowcount_ok(_radar(140, 175), "1d")  # 80% of requested
        self.assertFalse(ok)
        self.assertIn("85%", why)
        self.assertTrue(ec.radar_rowcount_ok(_radar(149, 175), "1d")[0])  # 85.1%

    def test_missing_fails(self):
        self.assertFalse(ec.radar_rowcount_ok({}, "1d")[0])
        self.assertFalse(ec.radar_rowcount_ok(None, "1d")[0])

    def test_default_threshold_constant(self):
        self.assertEqual(ec.RADAR_MIN_ROWS, 120)
        self.assertEqual(ec.RADAR_MIN_UNIVERSE_FRAC, 0.85)


class TestPriceAndMinOrder(unittest.TestCase):
    def test_price_sanity(self):
        self.assertTrue(ec.price_sane(1.49, 1.0)[0])
        self.assertFalse(ec.price_sane(1.51, 1.0)[0])
        self.assertFalse(ec.price_sane(0.49, 1.0)[0])
        self.assertFalse(ec.price_sane(None, 1.0)[0])
        self.assertFalse(ec.price_sane(1.0, None)[0])

    def test_radar_ref_price_prefers_4h(self):
        self.assertEqual(ec.radar_ref_price({"close": 2.0}, {"close": 1.0}), 2.0)
        self.assertEqual(ec.radar_ref_price(None, {"close": 1.0}), 1.0)
        self.assertIsNone(ec.radar_ref_price(None, None))

    def test_min_order(self):
        self.assertEqual(ec.min_order_usd(500), 10.0)       # HL minimum dominates
        self.assertAlmostEqual(ec.min_order_usd(1691.8), 16.918)  # 1% NAV
        self.assertEqual(ec.min_order_usd(0), 10.0)


class TestNav(unittest.TestCase):
    def test_unified_no_double_count(self):
        n = ec.nav_snapshot(SPOT_UNIFIED, PERP_UNIFIED, "unifiedAccount")
        self.assertAlmostEqual(n["nav"], 1691.795646)
        self.assertIn("unified", n["source"])

    def test_standard_adds_perp(self):
        n = ec.nav_snapshot({"balances": [{"coin": "USDC", "total": "100"}]},
                            {"marginSummary": {"accountValue": "900"}}, "default")
        self.assertAlmostEqual(n["nav"], 1000.0)

    def test_unknown_is_max(self):
        self.assertAlmostEqual(ec.nav_snapshot(SPOT_UNIFIED, PERP_UNIFIED, None)["nav"], 1691.795646)
        self.assertAlmostEqual(ec.nav_snapshot({"balances": []}, {"marginSummary": {"accountValue": "50"}})["nav"], 50.0)


class TestRunReport(unittest.TestCase):
    def test_classification(self):
        res = {"mode": "LIVE", "status": "success", "timestamp": "t",
               "executed": [{"symbol": "AAA", "entry_type": "Base", "qty": 10, "limit_px": 1.0, "size_pct": 4.0,
                             "leverage": 2, "live_result": {"filled_sz": 10, "avg_px": 1.01}}],
               "skipped": [{"symbol": "BBB", "reason": "below 1D Upper at entry"},
                           {"symbol": "CCC", "reason": "live entry no_fill", "live_result": {"status": "no_fill"}}],
               "actions": [], "alerts": []}
        rep = ec.build_run_report("executor", res, {"nav": 1690.0, "source": "x"},
                                  {"AAA": {"size_pct": 6, "leverage": 3}})
        self.assertEqual(rep["sot"], "GIIQ-SoT-1")
        self.assertEqual([x["symbol"] for x in rep["executed"]], ["AAA"])
        self.assertEqual(rep["executed"][0]["px"], 1.01)
        self.assertEqual([x["symbol"] for x in rep["skipped"]], ["BBB"])
        self.assertEqual([x["symbol"] for x in rep["failed"]], ["CCC"])
        self.assertIn("size 6% -> 4%", rep["downsized"][0]["reason"])
        self.assertIn("leverage 3x -> 2x", rep["downsized"][0]["reason"])
        self.assertEqual(rep["nav"], 1690.0)


class TestExecutorGuardrails(EnvMixin, unittest.TestCase):
    META = {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}}

    def _run(self, hl, cands, r4h=None, r1d=None):
        return executor.execute_approved_candidates(hl=hl, candidates_data=cands,
                                                    decisions={"AAA": {"decision": "approve", "size_pct": 10}},
                                                    radar_1h={}, radar_4h=r4h or {"rows": []},
                                                    radar_1d=r1d or {"rows": []}, now=NOW)

    def test_rowcount_fail_closed_before_hl(self):
        hl = FakeHL(meta=self.META, mids={"AAA": 1.0})
        with patch.dict(os.environ, {"EXEC_RADAR_MIN_ROWS": "120"}):
            res = self._run(hl, _cands(_cand("AAA", upper_1d=0.99)), r4h=_radar(170), r1d=_radar(50))
        self.assertEqual(res["status"], "fail_closed")
        self.assertIn("1D radar has 50 rows", res["message"])
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["sot"], "GIIQ-SoT-1")
        self.assertEqual(res["run_report"]["skipped"][0]["symbol"], "AAA")

    def test_price_sanity_skip(self):
        hl = FakeHL(meta=self.META, mids={"AAA": 2.0})  # candidate/radar close 1.0 -> +100%
        res = self._run(hl, _cands(_cand("AAA", upper_1d=0.99)))
        self.assertEqual(hl.calls, [])
        self.assertIn("price sanity", res["skipped"][0]["reason"])
        self.assertIn("price sanity", res["run_report"]["skipped"][0]["reason"])

    def test_min_order_one_pct_nav(self):
        hl = FakeHL(meta=self.META, mids={"AAA": 1.0})
        with patch.object(executor, "min_order_usd", side_effect=lambda nav: nav * 0.5):
            res = self._run(hl, _cands(_cand("AAA", upper_1d=0.99)))
        self.assertEqual(hl.calls, [])
        self.assertIn("< minimum $500.00", res["skipped"][0]["reason"])

    def test_nav_snapshot_and_downsized_report(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self._run(hl, _cands(_cand("AAA", upper_1d=0.99)))  # DRY_RUN
        self.assertEqual(res["nav_snapshot"]["nav"], 1000.0)
        rep = res["run_report"]
        self.assertEqual(rep["executed"][0]["symbol"], "AAA")
        self.assertTrue(rep["executed"][0]["dry_run"])
        self.assertIn("size 10% -> 8%", rep["downsized"][0]["reason"])  # Primary band 4-8%


class TestPendingGuardrails(EnvMixin, unittest.TestCase):
    def test_rowcount_fail_closed_no_state_change(self):
        entries = [{"id": "AAA_CONTINUATION_20260928", "symbol": "AAA", "kind": "CONTINUATION", "status": "pending"}]
        before = [dict(e) for e in entries]
        hl = FakeHL(mids={"AAA": 1.0})
        with patch.dict(os.environ, {"EXEC_RADAR_MIN_ROWS": "120"}):
            res = pending_worker.run_pending(hl=hl, radar_1d=_radar(10), radar_4h=_radar(170), now=NOW,
                                             entries=entries, log_entry_fn=lambda **k: None,
                                             bar_fn=lambda *a: self.fail("bar fetched"))
        self.assertEqual(res["status"], "fail_closed")
        self.assertEqual(entries, before)
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["run_report"]["sot"], "GIIQ-SoT-1")

    def test_sequence_recorded(self):
        res = pending_worker.run_pending(hl=FakeHL(), radar_1d={"rows": []}, radar_4h={"rows": []}, now=NOW,
                                         entries=[], after_exits="2026-09-28T04:10:30+00:00")
        self.assertIn("exits (done 2026-09-28T04:10:30+00:00)", res["sequence"])


class TestServeOrderAndReport(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        self._p = patch.object(serve, "RUN_REPORT_DIR", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_4h_job_skips_entries_when_exits_fail(self):
        s = self.serve
        with patch.object(s, "_scan_and_exits", return_value={"exits_ok": False, "why": "exit_worker rc=1"}), \
             patch.object(s, "_scheduled_pending") as pend, patch.object(s, "_update_job_status") as st:
            s._scheduled_4h_scan_exits()
        pend.assert_not_called()
        self.assertEqual(st.call_args.args[:2], ("pending_entries", "skipped"))

    def test_4h_job_runs_entries_after_exits(self):
        s = self.serve
        with patch.object(s, "_scan_and_exits", return_value={"exits_ok": True, "at": "T"}), \
             patch.object(s, "_scheduled_pending") as pend:
            s._scheduled_4h_scan_exits()
        pend.assert_called_once_with(manual=False, after_exits="T")

    def test_daily_report_merges_runs(self):
        s = self.serve
        now = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
        s._record_run_report("executor", {"run_report": {"sot": "GIIQ-SoT-1", "run": "executor", "mode": "LIVE",
                                                         "status": "success", "ts": now.isoformat(),
                                                         "executed": [{"symbol": "AAA"}], "skipped": [],
                                                         "downsized": [], "failed": [{"symbol": "BBB", "reason": "no_fill"}]}},
                             now=now)
        s._record_run_report("pending_entries", {"mode": "LIVE", "status": "error", "message": "boom"}, now=now)
        rr = s._today_run_report(now)
        self.assertEqual(rr["sot"], "GIIQ-SoT-1")
        self.assertEqual(rr["date"], "2026-09-28")
        self.assertEqual(len(rr["runs"]), 2)
        self.assertEqual(rr["executed"][0]["symbol"], "AAA")
        self.assertEqual(rr["failed"][0]["run_job"], "executor")

    def test_desk_payload_has_sot_and_report(self):
        s = self.serve
        with patch.object(s, "_get_hl_cached", return_value={"fetch_error": True, "hl_perp": {}, "hl_spot": {}}):
            p = s._build_desk_data_payload("test", kind="live")
        self.assertEqual(p["sot"], "GIIQ-SoT-1")
        self.assertIn("run_report", p)
        self.assertIn("exec_mode", p)
        self.assertEqual(s._exec_mode()["sot"], "GIIQ-SoT-1")


if __name__ == "__main__":
    unittest.main()
