#!/usr/bin/env python3
"""AIQ-0022 (MMT decision 2026-10-03 13:35-13:45 HKT): total isolated margin cap 70% NAV with the
80% utilisation cap kept as the outer hard cap; Tiny tier max 3x leverage and 2% NAV margin per trade.
HL fully mocked; no network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exec_common as ec  # noqa: E402
from test_live_execution import EnvMixin, FakeHL, NOW, _cand, _cands  # noqa: E402
import executor  # noqa: E402
import test_pending_entries as TPE  # noqa: E402

META5 = {"AAA": {"szDecimals": 0, "maxLeverage": 5.0}, "BBB": {"szDecimals": 0, "maxLeverage": 5.0}}


class TestCaps(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(ec.MAX_TOTAL_MARGIN_NAV_PCT, 80.0)   # GIIQ-SoT-5: one 80% NAV cap (was 70%)
        self.assertEqual(ec.MAX_MARGIN_UTILIZATION_PCT, 80.0)   # outer hard cap unchanged
        self.assertEqual(ec.MAX_COIN_NOTIONAL_NAV_PCT, 20.0)    # per-coin cap unchanged
        self.assertEqual(ec.TINY_MAX_LEV, 3)
        self.assertEqual(ec.TINY_MAX_MARGIN_PCT, 2.0)

    def test_total_margin_80_boundary(self):
        self.assertTrue(ec.total_margin_nav_ok(760, 40, 1000)[0])    # exactly 80% (GIIQ-SoT-5)
        self.assertFalse(ec.total_margin_nav_ok(761, 40, 1000)[0])   # 80.1%

    def test_outer_80_cap(self):
        self.assertTrue(ec.margin_cap_ok(760, 40, 1000)[0])          # exactly 80%
        self.assertFalse(ec.margin_cap_ok(761, 40, 1000)[0])


class TestTinySizing(unittest.TestCase):
    def _sz(self, tier, **kw):
        return ec.size_by_margin(1000, 100.0, 90.0, 5.0, ai_size_pct=4, ai_leverage=5, tier=tier, **kw)

    def test_tiny_clamps_to_3x_and_2pct(self):
        sz = self._sz("tiny")
        self.assertTrue(sz["ok"], sz)
        self.assertEqual(sz["leverage"], 3)
        self.assertEqual(sz["margin_pct"], 2.0)
        self.assertAlmostEqual(sz["notional_usd"], 60.0)

    def test_unknown_or_empty_tier_is_tiny(self):
        for t in ("unknown", "", "TINY"):
            sz = self._sz(t)
            self.assertEqual((sz["leverage"], sz["margin_pct"]), (3, 2.0), t)

    def test_other_tiers_unchanged(self):
        for t in ("mega", "large", "small"):
            sz = self._sz(t)
            self.assertEqual((sz["leverage"], sz["margin_pct"]), (5, 4.0), t)

    def test_tier_none_keeps_old_behaviour(self):
        sz = self._sz(None)
        self.assertEqual((sz["leverage"], sz["margin_pct"]), (5, 4.0))

    def test_tiny_addon_fixed_leverage(self):
        self.assertFalse(self._sz("tiny", fixed_leverage=5)["ok"])
        ok = self._sz("tiny", fixed_leverage=3)
        self.assertTrue(ok["ok"])
        self.assertEqual((ok["leverage"], ok["margin_pct"]), (3, 2.0))


class TestExecutorCaps(EnvMixin, unittest.TestCase):
    def run_exec(self, hl, cands, decisions):
        return executor.execute_approved_candidates(hl=hl, candidates_data=cands, decisions=decisions,
                                                    radar_1h={}, radar_4h={"rows": []}, radar_1d={"rows": []},
                                                    now=NOW)

    def test_80pct_cap_blocks_entry(self):
        # GIIQ-SoT-5: 78% used + ~4% small-tier margin > 80% NAV
        hl = FakeHL(equity=1000, margin_used=780, meta=META5, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", tier="small", filt=0.97, lower=0.95)),
                            {"AAA": {"decision": "approve", "size_pct": 4, "leverage": 3}})
        self.assertEqual(res["actions"], [])
        self.assertIn("> 80", res["skipped"][0]["reason"])

    def test_entry_under_70pct_allowed(self):
        # 40% used would have been blocked by the old 30% cap; now allowed
        hl = FakeHL(equity=1000, margin_used=400, meta=META5, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", tier="small", filt=0.97, lower=0.95)),
                            {"AAA": {"decision": "approve", "size_pct": 4, "leverage": 3}})
        self.assertEqual([a["symbol"] for a in res["actions"]], ["AAA"])

    def test_80pct_outer_cap_still_enforced(self):
        # Even if the NAV cap were loosened, the 80% utilisation cap still blocks.
        hl = FakeHL(equity=1000, margin_used=780, meta=META5, mids={"AAA": 1.0})
        with patch.object(ec, "MAX_TOTAL_MARGIN_NAV_PCT", 100.0):
            res = self.run_exec(hl, _cands(_cand("AAA", tier="small", filt=0.97, lower=0.95)),
                                {"AAA": {"decision": "approve", "size_pct": 4, "leverage": 3}})
        self.assertEqual(res["actions"], [])
        self.assertIn("Margin utilization", res["skipped"][0]["reason"])
        self.assertIn("80", res["skipped"][0]["reason"])

    def test_tiny_entry_clamped(self):
        hl = FakeHL(equity=1000, meta=META5, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", tier="tiny", filt=0.90, lower=0.85)),
                            {"AAA": {"decision": "approve", "size_pct": 4, "leverage": 5}})
        a = res["actions"][0]
        self.assertEqual(a["leverage"], 3)
        self.assertEqual(a["size_pct"], 2.0)

    def test_small_entry_not_clamped(self):
        hl = FakeHL(equity=1000, meta=META5, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", tier="small", filt=0.90, lower=0.85)),
                            {"AAA": {"decision": "approve", "size_pct": 4, "leverage": 5}})
        a = res["actions"][0]
        self.assertEqual(a["leverage"], 5)
        self.assertEqual(a["size_pct"], 4.0)


class TestPendingTiny(unittest.TestCase):
    """Borrows the pending-worker fixtures from test_pending_entries.TestWorker (not its tests).
    A Tiny CONT that fires on the 3-step rule is still clamped to 2% / 3x."""
    setUp = TPE.TestWorker.setUp
    run_w = TPE.TestWorker.run_w
    tearDown = TPE.TestWorker.tearDown

    def test_tiny_continuation_clamped(self):
        hl = TPE.FakeHL(mids={"AAA": 1.06})
        ents = [TPE.new_rec(TPE.pe.CONT, breakout=True, retrace=True, size=6, lev=5, tier="tiny")]
        res = self.run_w(hl, ents)
        self.assertEqual(res["status"], "success", res)
        f = res["filled"][0]
        self.assertEqual(f["size_pct"], 2.0)
        self.assertEqual(f["leverage"], 3)


if __name__ == "__main__":
    unittest.main()
