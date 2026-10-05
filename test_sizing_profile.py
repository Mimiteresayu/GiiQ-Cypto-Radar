#!/usr/bin/env python3
"""DRAFT sizing profile (SIZING_PROFILE=lev1x_4pct, SOT2_MIN_LEV env): paper-only, default = SoT-4.
HL fully mocked; no network."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exec_common as ec  # noqa: E402
import executor  # noqa: E402
import sizing_golden_cases as G  # noqa: E402
import test_pending_entries as TPE  # noqa: E402
from test_live_execution import EnvMixin, FakeHL, NOW, _cand, _cands  # noqa: E402

GOLDEN = ROOT / "test_fixtures" / "sizing_sot4_golden.json"
ENV_KEYS = ("SIZING_PROFILE", "SIZING_ALLOW_LIVE", "SOT2_MIN_LEV")
PAPER = {"EXEC_DRY_RUN": "1"}
LIVE = {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "11" * 32}
LEV1X = {"profile": "lev1x_4pct", "min_lev": 1, "max_lev": 1, "overrides_allowed": True, "ignored": []}


class ProfileEnv:
    """Clears the sizing env vars around each test."""

    def setUp(self):
        super().setUp()
        self._sz_env = {k: os.environ.pop(k, None) for k in ENV_KEYS}

    def tearDown(self):
        for k, v in self._sz_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()


def _dump(x):
    return json.dumps(x, sort_keys=True, indent=0)


class TestDefaultIsSoT4(ProfileEnv, unittest.TestCase):
    def test_golden_byte_identical(self):
        golden = GOLDEN.read_text(encoding="utf-8")
        for mode in ({}, PAPER, LIVE):
            with patch.dict(os.environ, mode):
                self.assertEqual(_dump(G.run_all()), golden, mode)

    def test_live_ignores_overrides_without_flag(self):
        golden = GOLDEN.read_text(encoding="utf-8")
        for extra in ({"SIZING_PROFILE": "lev1x_4pct"}, {"SOT2_MIN_LEV": "1"},
                      {"SIZING_PROFILE": "lev1x_4pct", "SOT2_MIN_LEV": "1", "SIZING_ALLOW_LIVE": "0"}):
            with patch.dict(os.environ, {**LIVE, **extra}):
                got = json.loads(_dump(G.run_all()))
            want = json.loads(golden)
            for g, w in zip(got, want):
                ign = [n for n in g["out"]["notes"] if "ignored in LIVE" in n]
                self.assertTrue(ign, extra)
                g["out"]["notes"] = [n for n in g["out"]["notes"] if n not in ign]
                self.assertEqual(g, w)

    def test_constants_unchanged(self):
        self.assertEqual((ec.SOT2_MIN_LEV, ec.SOT2_MAX_LEV), (3, 5))
        self.assertEqual((ec.SOT2_MIN_MARGIN_PCT, ec.SOT2_MAX_MARGIN_PCT), (2.0, 4.0))
        self.assertEqual((ec.TINY_MAX_LEV, ec.TINY_MAX_MARGIN_PCT), (3, 2.0))
        self.assertEqual(ec.MAX_TOTAL_MARGIN_NAV_PCT, 70.0)
        self.assertEqual(ec.MAX_MARGIN_UTILIZATION_PCT, 80.0)
        self.assertEqual(ec.MAX_COIN_NOTIONAL_NAV_PCT, 20.0)
        self.assertEqual(ec.MAX_NEW_ENTRIES_PER_DAY, 3)
        self.assertEqual(ec.MIN_SL_DIST_PCT, 1.5)
        self.assertEqual(ec.SOT_ID, "GIIQ-SoT-4")


class TestSizingParams(unittest.TestCase):
    def p(self, env, live):
        return ec.sizing_params(env=env, live=live)

    def test_default(self):
        for live in (False, True):
            self.assertEqual(self.p({}, live), {"profile": "sot4", "min_lev": 3, "max_lev": 5,
                                                "overrides_allowed": not live, "ignored": []})

    def test_paper_profile(self):
        self.assertEqual(self.p({"SIZING_PROFILE": "lev1x_4pct"}, False), LEV1X)

    def test_live_needs_explicit_flag(self):
        p = self.p({"SIZING_PROFILE": "lev1x_4pct"}, True)
        self.assertEqual((p["profile"], p["min_lev"], p["max_lev"]), ("sot4", 3, 5))
        self.assertIn("ignored in LIVE", p["ignored"][0])
        for flag in ("0", "", "no", "maybe"):
            self.assertEqual(self.p({"SIZING_PROFILE": "lev1x_4pct", "SIZING_ALLOW_LIVE": flag}, True)["max_lev"], 5)
        p = self.p({"SIZING_PROFILE": "lev1x_4pct", "SIZING_ALLOW_LIVE": "1"}, True)
        self.assertEqual((p["profile"], p["min_lev"], p["max_lev"]), ("lev1x_4pct", 1, 1))

    def test_min_lev_env(self):
        for v in (1, 2, 3, 4, 5):
            self.assertEqual(self.p({"SOT2_MIN_LEV": str(v)}, False)["min_lev"], v)
        self.assertEqual(self.p({"SOT2_MIN_LEV": "3"}, True), self.p({}, True))
        live = self.p({"SOT2_MIN_LEV": "1"}, True)
        self.assertEqual(live["min_lev"], 3)
        self.assertIn("ignored in LIVE", live["ignored"][0])
        self.assertEqual(self.p({"SOT2_MIN_LEV": "1", "SIZING_ALLOW_LIVE": "1"}, True)["min_lev"], 1)

    def test_min_lev_invalid_keeps_default(self):
        for raw in ("0", "-1", "6", "2.5", "x"):
            p = self.p({"SOT2_MIN_LEV": raw}, False)
            self.assertEqual(p["min_lev"], 3, raw)
            self.assertIn("invalid", p["ignored"][0])

    def test_min_lev_never_above_profile_max(self):
        self.assertEqual(self.p({"SIZING_PROFILE": "lev1x_4pct", "SOT2_MIN_LEV": "3"}, False)["min_lev"], 1)

    def test_unknown_profile_falls_back(self):
        p = self.p({"SIZING_PROFILE": "yolo10x"}, False)
        self.assertEqual((p["profile"], p["min_lev"], p["max_lev"]), ("sot4", 3, 5))
        self.assertIn("unknown", p["ignored"][0])


class TestLev1xSizing(unittest.TestCase):
    def sz(self, *a, **kw):
        return ec.size_by_margin(*a, params=LEV1X, **kw)

    def test_1x_4pct(self):
        r = self.sz(1000, 100.0, 90.0, 10, ai_size_pct=4, ai_leverage=5, tier="large")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["leverage"], r["margin_pct"]), (1, 4.0))
        self.assertAlmostEqual(r["notional_usd"], 40.0)
        self.assertEqual(r["liq"], 0.0)  # 1x isolated long cannot be liquidated above 0
        self.assertIn("sizing profile lev1x_4pct 1-1x (not SoT, paper)", r["notes"])

    def test_missing_ai_size_is_4pct(self):
        self.assertEqual(self.sz(1000, 100.0, 90.0, 10)["margin_pct"], 4.0)

    def test_ai_size_still_a_maximum_and_band_unchanged(self):
        self.assertEqual(self.sz(1000, 100.0, 90.0, 10, ai_size_pct=3)["margin_pct"], 3.0)
        self.assertEqual(self.sz(1000, 100.0, 90.0, 10, ai_size_pct=9)["margin_pct"], 4.0)
        self.assertEqual(self.sz(1000, 100.0, 90.0, 10, ai_size_pct=1)["margin_pct"], 2.0)
        self.assertFalse(self.sz(1000, 100.0, 90.0, 10, max_margin_pct=1.5)["ok"])

    def test_tiny_cap_still_applies(self):
        r = self.sz(1000, 100.0, 90.0, 10, ai_size_pct=4, tier="tiny")
        self.assertEqual((r["leverage"], r["margin_pct"]), (1, 2.0))
        self.assertAlmostEqual(r["notional_usd"], 20.0)

    def test_deep_sl_ok_at_1x(self):
        # SoT-4 skips (3x liq 70.18 >= SL 70); 1x liq = 0 so the Hard SL always fires first
        self.assertFalse(ec.size_by_margin(1000, 100.0, 70.0, 10)["ok"])
        self.assertEqual(self.sz(1000, 100.0, 70.0, 10)["leverage"], 1)

    def test_invalid_inputs_still_rejected(self):
        self.assertFalse(self.sz(0, 100.0, 90.0, 10)["ok"])
        self.assertFalse(self.sz(1000, 100.0, 101.0, 10)["ok"])

    def test_addon_above_band_refused(self):
        r = self.sz(1000, 100.0, 90.0, 10, fixed_leverage=3, max_margin_pct=4)
        self.assertFalse(r["ok"])
        self.assertIn("existing leverage 3x > 1x", r["reason"])
        self.assertEqual(self.sz(1000, 100.0, 90.0, 10, fixed_leverage=1, max_margin_pct=4)["leverage"], 1)

    def test_notional_never_above_sot4(self):
        for nav, (e, sl), cm, a_sz, a_lev, tier in [(c["nav"], (c["entry_px"], c["hard_sl"]), c["coin_max_leverage"],
                                                     c.get("ai_size_pct"), c.get("ai_leverage"), c.get("tier"))
                                                    for c in G.CASES if "fixed_leverage" not in c]:
            new = self.sz(nav, e, sl, cm, a_sz, a_lev, tier=tier)
            old = ec.size_by_margin(nav, e, sl, cm, a_sz, a_lev, tier=tier, params=ec.sizing_params(env={}))
            if not new["ok"]:
                continue
            self.assertLessEqual(new["notional_usd"], nav * ec.SOT2_MAX_MARGIN_PCT / 100.0 + 1e-9)
            self.assertLessEqual(new["notional_usd"], nav * ec.MAX_COIN_NOTIONAL_NAV_PCT / 100.0)
            if old["ok"]:
                self.assertLessEqual(new["notional_usd"], old["notional_usd"] + 1e-9)
                self.assertEqual(new["margin_pct"], old["margin_pct"])

    def test_min_lev_1_band_steps_down(self):
        p = ec.sizing_params(env={"SOT2_MIN_LEV": "1"}, live=False)
        r = ec.size_by_margin(1000, 100.0, 70.0, 10, params=p)  # 3x liq 70.18 not below 70 -> 2x
        self.assertEqual(r["leverage"], 2)
        self.assertTrue(ec.size_by_margin(1000, 100.0, 98.0, 1, params=p)["ok"])  # coin max 1x now fits
        self.assertFalse(ec.size_by_margin(1000, 100.0, 98.0, 1)["ok"])


class TestWorkedExample(unittest.TestCase):
    """$1,000 NAV, entry 100, Hard SL 90 (10%), coin maxLeverage 10, AI 4% / 5x."""

    def test_1000_nav(self):
        sot4 = ec.size_by_margin(1000, 100.0, 90.0, 10, 4, 5, tier="large", params=ec.sizing_params(env={}))
        lev1 = ec.size_by_margin(1000, 100.0, 90.0, 10, 4, 5, tier="large", params=LEV1X)
        self.assertEqual((sot4["leverage"], sot4["margin_pct"], sot4["notional_usd"]), (5, 4.0, 200.0))
        self.assertEqual((lev1["leverage"], lev1["margin_pct"], lev1["notional_usd"]), (1, 4.0, 40.0))
        self.assertAlmostEqual(sot4["notional_usd"] * 0.10, 20.0)   # loss at Hard SL = 2% NAV
        self.assertAlmostEqual(lev1["notional_usd"] * 0.10, 4.0)    # 0.4% NAV
        self.assertTrue(ec.coin_notional_ok(0, sot4["notional_usd"], 1000)[0])  # 20% NAV = at the cap
        tiny4 = ec.size_by_margin(1000, 100.0, 90.0, 10, 4, 5, tier="tiny", params=ec.sizing_params(env={}))
        tiny1 = ec.size_by_margin(1000, 100.0, 90.0, 10, 4, 5, tier="tiny", params=LEV1X)
        self.assertEqual((tiny4["leverage"], tiny4["notional_usd"]), (3, 60.0))
        self.assertEqual((tiny1["leverage"], tiny1["notional_usd"]), (1, 20.0))


META5 = {"AAA": {"szDecimals": 0, "maxLeverage": 5.0}, "BBB": {"szDecimals": 0, "maxLeverage": 5.0}}


class TestExecutorProfile(ProfileEnv, EnvMixin, unittest.TestCase):
    def run_exec(self, hl, cands, decisions):
        return executor.execute_approved_candidates(hl=hl, candidates_data=cands, decisions=decisions,
                                                    radar_1h={}, radar_4h={"rows": []}, radar_1d={"rows": []},
                                                    now=NOW)

    def _one(self, hl=None, tier="small"):
        hl = hl or FakeHL(equity=1000, meta=META5, mids={"AAA": 1.0})
        return self.run_exec(hl, _cands(_cand("AAA", tier=tier, filt=0.90, lower=0.85)),
                             {"AAA": {"decision": "approve", "size_pct": 4, "leverage": 5}})

    def test_paper_1x(self):
        os.environ["SIZING_PROFILE"] = "lev1x_4pct"
        a = self._one()["actions"][0]
        self.assertEqual((a["leverage"], a["size_pct"]), (1, 4.0))
        self.assertLessEqual(a["notional_usd"], 40.5)
        self.assertTrue(any("lev1x_4pct" in n for n in a["sizing_notes"]))

    def test_paper_default_unchanged(self):
        a = self._one()["actions"][0]
        self.assertEqual((a["leverage"], a["size_pct"]), (5, 4.0))

    def test_paper_1x_caps_still_enforced(self):
        os.environ["SIZING_PROFILE"] = "lev1x_4pct"
        hl = FakeHL(equity=1000, margin_used=680, meta=META5, mids={"AAA": 1.0})
        res = self._one(hl)
        self.assertEqual(res["actions"], [])
        self.assertIn("> 70% cap", res["skipped"][0]["reason"])
        hl = FakeHL(equity=1000, margin_used=780, meta=META5, mids={"AAA": 1.0})
        with patch.object(ec, "MAX_TOTAL_MARGIN_NAV_PCT", 100.0):
            res = self._one(hl)
        self.assertIn("Margin utilization", res["skipped"][0]["reason"])

    def test_live_ignores_profile(self):
        os.environ.update(LIVE)
        os.environ["SIZING_PROFILE"] = "lev1x_4pct"
        hl = FakeHL(equity=1000, meta=META5, mids={"AAA": 1.0})
        res = self._one(hl)
        self.assertEqual(res["mode"], "LIVE")
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 5))
        self.assertEqual(res["executed"][0]["leverage"], 5)

    def test_live_with_explicit_flag(self):
        os.environ.update(LIVE)
        os.environ.update({"SIZING_PROFILE": "lev1x_4pct", "SIZING_ALLOW_LIVE": "1"})
        hl = FakeHL(equity=1000, meta=META5, mids={"AAA": 1.0})
        self._one(hl)
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 1))


class TestPendingProfile(ProfileEnv, unittest.TestCase):
    """Borrows the pending-worker fixtures from test_pending_entries.TestWorker (not its tests)."""
    radars = TPE.TestWorker.radars
    entry = TPE.TestWorker.entry
    run_w = TPE.TestWorker.run_w

    def setUp(self):
        super().setUp()
        TPE.TestWorker.setUp(self)

    def tearDown(self):
        TPE.TestWorker.tearDown(self)
        super().tearDown()

    def fill(self, **env):
        with patch.dict(os.environ, env):
            hl = TPE.FakeHL(mids={"AAA": 0.9})
            res = self.run_w(hl, self.entry(size=6, lev=5, tier="small"), TPE.bar(0.88, 0.93))
        f = res["filled"][0]
        return f["size_pct"], f["leverage"], hl.calls

    def test_continuation_1x_paper(self):
        size, lev, calls = self.fill(EXEC_DRY_RUN="1", SIZING_PROFILE="lev1x_4pct")
        self.assertEqual((size, lev), (4.0, 1))
        self.assertFalse([c for c in calls if c[0] in ("set_leverage", "open_long_ioc")])

    def test_continuation_live_ignores_profile(self):
        self.assertEqual(self.fill(SIZING_PROFILE="lev1x_4pct")[:2], self.fill()[:2])
        self.assertEqual(self.fill()[:2], (4.0, 5))
        self.assertEqual(self.fill(SIZING_PROFILE="lev1x_4pct", SIZING_ALLOW_LIVE="1")[:2], (4.0, 1))


if __name__ == "__main__":
    unittest.main()
