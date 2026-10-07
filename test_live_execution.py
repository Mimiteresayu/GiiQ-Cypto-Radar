#!/usr/bin/env python3
"""Tests for executor/exit_worker live-readiness (HL fully mocked; no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exec_common as ec  # noqa: E402
from hl_sim import SimExchangeMixin  # noqa: E402
import executor  # noqa: E402
import exit_worker  # noqa: E402
import hl_exec  # noqa: E402

NOW = datetime(2026, 9, 28, 0, 55, tzinfo=timezone.utc)  # 08:55 HKT


class FakeHL(SimExchangeMixin):
    """Mimics hl_exec.HLClient. Writes go through hl_sim (positions / SL orders appear like on the
    exchange, so the GIIQ-SoT-5 post-fill reconciliation sees a real state)."""

    def __init__(self, equity=1000.0, margin_used=0.0, positions=None, meta=None, mids=None, orders=None,
                 lev_ok=True, fill=True, sl_ok=True, close_ok=True):
        self.equity, self.margin_used = equity, margin_used
        self.positions = positions or []
        self._meta = meta or {}
        self.mids = mids or {}
        self.orders = orders or []
        self.lev_ok, self.fill, self.sl_ok, self.close_ok = lev_ok, fill, sl_ok, close_ok
        self.calls = []

    # read
    def spot_state(self):
        return {"balances": [{"coin": "USDC", "total": str(self.equity), "hold": "0"}]}

    def perp_state(self):
        return {"marginSummary": {"totalMarginUsed": str(self.margin_used)},
                "assetPositions": [{"position": p} for p in self.positions]}

    def meta(self):
        return self._meta

    def all_mids(self):
        return self.mids

    def open_orders(self):
        return self.orders

    def names(self):
        return [c[0] for c in self.calls]


def _cands(*items, generated_at=None, stale=False):
    return {"generated_at": (generated_at or (NOW - timedelta(minutes=50))).isoformat(), "stale": stale,
            "candidates": list(items)}


def _cand(sym, tier="tiny", typ="Base", filt=0.9, lower=0.8, close=1.0, upper_1d=None, upper_4h=None):
    return {"symbol": sym, "type": typ, "tier": tier, "trend_1d": "Green", "trend_4h": "Green",
            "close_1d": close, "filter_4h": filt, "lower_4h": lower,
            "upper_1d": close * 0.95 if upper_1d is None else upper_1d,
            "upper_4h": close * 0.97 if upper_4h is None else upper_4h}


def _radar4h(*rows):
    return {"rows": [dict(symbol=s, filter=f, lower=l, close=c) for s, f, l, c in rows]}


class EnvMixin:
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("EXEC_DRY_RUN", "HL_API_PRIVATE_KEY", "EXEC_MAX_CANDIDATE_AGE_H",
                                                    "PENDING_PATH")}
        os.environ["EXEC_DRY_RUN"] = "1"
        os.environ.pop("HL_API_PRIVATE_KEY", None)
        self._pend_dir = tempfile.mkdtemp()
        os.environ["PENDING_PATH"] = os.path.join(self._pend_dir, "pending_entries.json")
        self.log_entry = patch.object(executor, "log_entry").start()

    def tearDown(self):
        patch.stopall()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------ pure rules
class TestLiqFormula(unittest.TestCase):
    def test_isolated_long_hl_formula(self):
        # lev 2, maxLev 3 -> l=1/6 -> liq = 100*(1-(0.5-1/6)/(5/6)) = 60
        self.assertAlmostEqual(ec.isolated_liq_price_long(100, 2, 3), 60.0, places=6)
        # at max leverage 3 -> 20% away
        self.assertAlmostEqual(ec.isolated_liq_price_long(100, 3, 3), 80.0, places=6)

    def test_never_negative_and_independent_of_equity(self):
        # old formula gave -265.66 for COMP (close 23.933, 8% of $1711 at 2.5x)
        liq = ec.isolated_liq_price_long(23.933, 2, 5)
        self.assertGreater(liq, 0)
        self.assertLess(liq, 23.933)
        self.assertGreaterEqual(ec.isolated_liq_price_long(1.0, 1, None), 0.0)

    def test_unknown_max_leverage_is_conservative(self):
        known = ec.isolated_liq_price_long(100, 2, 50)
        unknown = ec.isolated_liq_price_long(100, 2, None)
        self.assertGreater(unknown, known)  # higher liq = stricter SoT check

    def test_liq_beyond_sl(self):
        self.assertTrue(ec.liq_beyond_sl_long(60, 90))
        self.assertFalse(ec.liq_beyond_sl_long(91, 90))
        self.assertFalse(ec.liq_beyond_sl_long(None, 90))

    def test_serve_enhance_candidates_positive_liq(self):
        import serve
        out = serve._enhance_candidates([_cand("COMP", tier="small", close=23.933, filt=23.15)],
                                        {"equity": 1711.0}, {"COMP": {"maxLeverage": 5}})
        c = out[0]
        self.assertGreater(c["estimated_liq_price"], 0)
        self.assertTrue(c["liq_beyond_sl"])
        self.assertEqual(c["hard_sl_label"], "4H Filter")
        self.assertIsInstance(c["suggested_leverage"], int)


class TestSizingRules(unittest.TestCase):
    def test_qty_is_notional_over_price_floored(self):
        self.assertEqual(ec.order_qty(171.1, 0.35109, 0), 487.0)
        self.assertEqual(ec.order_qty(205.32, 0.2099, 2), 978.18)
        self.assertEqual(ec.order_qty(100, 0, 2), 0.0)

    def test_per_coin_max_leverage(self):
        self.assertEqual(ec.clamp_leverage(5, 3), 3)
        self.assertEqual(ec.clamp_leverage(10, 50), 5)
        self.assertEqual(ec.clamp_leverage(2.5, None), 2)
        self.assertEqual(ec.clamp_leverage(0.2, 3), 1)
        # GIIQ-SoT-5 removed the dead executor._clamp_size_leverage; the live sizing is size_by_margin:
        # leverage never above the coin max, 2x floor, AI size clamped to the 2-4% band
        sz = ec.size_by_margin(1000.0, 1.0, 0.9, 3, ai_size_pct=6, ai_leverage=5)
        self.assertEqual((sz["leverage"], sz["margin_pct"]), (3, 4.0))

    def test_margin_cap(self):
        self.assertTrue(ec.margin_cap_ok(700, 100, 1000)[0])
        self.assertFalse(ec.margin_cap_ok(750, 60, 1000)[0])

    def test_round_price(self):
        self.assertEqual(ec.round_price(0.2099123, 2), 0.2099)
        self.assertEqual(ec.round_price(84753.57, 5), 84754.0)

    def test_live_mode_gate(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": ""}):
            self.assertFalse(ec.is_live_mode())
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1", "HL_API_PRIVATE_KEY": "0xabc"}):
            self.assertFalse(ec.is_live_mode())
        with patch.dict(os.environ, {"HL_API_PRIVATE_KEY": "0xabc"}):
            os.environ.pop("EXEC_DRY_RUN", None)
            self.assertFalse(ec.is_live_mode())  # default = dry
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0xabc"}):
            self.assertTrue(ec.is_live_mode())


class TestFreshness(unittest.TestCase):
    def test_fresh(self):
        self.assertEqual(ec.candidates_fresh(_cands(), now=NOW, max_age_h=3), (True, "ok"))

    def test_yesterday_rejected(self):
        ok, why = ec.candidates_fresh(_cands(generated_at=NOW - timedelta(hours=10)), now=NOW, max_age_h=24)
        self.assertFalse(ok)
        self.assertIn("not today", why)

    def test_too_old_and_stale_and_missing(self):
        self.assertFalse(ec.candidates_fresh(_cands(generated_at=NOW - timedelta(minutes=50)), now=NOW, max_age_h=0.5)[0])
        self.assertFalse(ec.candidates_fresh(_cands(stale=True), now=NOW, max_age_h=3)[0])
        self.assertFalse(ec.candidates_fresh({}, now=NOW)[0])


class TestHardSLTier(unittest.TestCase):
    def test_mega_large_use_4h_lower(self):
        row = {"filter": 100.0, "lower": 90.0}
        self.assertEqual(ec.hard_sl_for_tier("mega", row), (90.0, "4H Lower"))
        self.assertEqual(ec.hard_sl_for_tier("large", row), (90.0, "4H Lower"))
        self.assertEqual(ec.hard_sl_for_tier("small", row), (100.0, "4H Filter"))
        self.assertEqual(ec.hard_sl_for_tier("tiny", row), (100.0, "4H Filter"))

    def test_existing_mega_position_liq_between_lower_and_filter_is_unsafe(self):
        # BTC is mega: Hard SL = 4H Lower 90. liq 95 is ABOVE the SL -> unsafe (old code compared vs Filter 100 -> "safe")
        pos = [{"coin": "BTC", "side": "LONG", "liquidation_px": 95.0}]
        ok, unsafe = executor._check_all_positions_liq_safe(pos, {}, _radar4h(("BTC", 100.0, 90.0, 101.0)))
        self.assertFalse(ok)
        self.assertEqual(unsafe, ["BTC"])
        pos[0]["liquidation_px"] = 85.0
        self.assertTrue(executor._check_all_positions_liq_safe(pos, {}, _radar4h(("BTC", 100.0, 90.0, 101.0)))[0])


class TestHardSLConsistency(unittest.TestCase):
    """mcap_tiers.HARD_SL_BY_TIER and failsafe must match exec_common.hard_sl_for_tier."""

    def test_constants_match_exec_common(self):
        from mcap_tiers import HARD_SL_BY_TIER
        row = {"filter": 100.0, "lower": 90.0}
        for tier, rule in HARD_SL_BY_TIER.items():
            lvl, _ = ec.hard_sl_for_tier(tier, row)
            self.assertEqual(lvl, row["lower"] if rule == "4h_lower" else row["filter"], tier)
        self.assertEqual(HARD_SL_BY_TIER["mega"], "4h_lower")
        self.assertEqual(HARD_SL_BY_TIER["small"], "4h_filter")

    def test_failsafe_uses_tier_hard_sl(self):
        import failsafe_exit_worker as fs
        g4 = {"ok": True, "close": 101.0, "prev_close": 101.0, "filter": 100.0, "prev_filter": 99.0,
              "lower": 90.0, "prev_lower": 89.0, "upper": 110.0}
        g1 = dict(g4, lower=95.0, prev_lower=94.0)
        with patch.object(fs, "_gc_closed", side_effect=lambda c, tf: g4 if tf == "4h" else g1):
            self.assertEqual(fs._compute_long_signal("BTC", "mega", 100, 1)["hard_sl_px"], 90.0)
            self.assertEqual(fs._compute_long_signal("ETH", "large", 100, 1)["hard_sl_px"], 90.0)
            self.assertEqual(fs._compute_long_signal("X", "small", 100, 1)["hard_sl_px"], 100.0)
            self.assertEqual(fs._compute_long_signal("Y", "tiny", 100, 1)["hard_sl_px"], 100.0)


# ------------------------------------------------------------------ executor flow (DRY_RUN)
class TestExecutorDryRun(EnvMixin, unittest.TestCase):
    META = {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}, "BBB": {"szDecimals": 1, "maxLeverage": 3.0},
            "LRG": {"szDecimals": 2, "maxLeverage": 10.0}}

    def run_exec(self, hl, cands, decisions, r4h=None, r1d=None):
        return executor.execute_approved_candidates(hl=hl, candidates_data=cands, decisions=decisions,
                                                    radar_1h={}, radar_4h=r4h or {"rows": []}, now=NOW,
                                                    radar_1d=r1d or {"rows": []})

    # ---- Base at-entry 1D Upper guard
    def test_guard_base_above_1d_upper_passes(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", typ="Base", filt=0.97, lower=0.95, upper_1d=0.99, upper_4h=1.5)),
                            {"AAA": {"decision": "approve"}})
        self.assertEqual(len(res["actions"]), 1)          # Base ignores the 4H Upper
        self.assertEqual(res["actions"][0]["entry_upper_label"], "1D Upper")

    def test_guard_base_uses_1d_upper(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        r1d = {"rows": [{"symbol": "AAA", "upper": 1.0}]}  # mid == Upper -> not above -> skip
        res = self.run_exec(hl, _cands(_cand("AAA", typ="Base", filt=0.97, lower=0.95, upper_4h=2.0)), {"AAA": {"decision": "approve"}}, r1d=r1d)
        self.assertEqual(res["actions"], [])
        self.assertIn("below 1D Upper at entry", res["skipped"][0]["reason"])
        self.assertTrue(any("below 1D Upper at entry" in a for a in res["alerts"]))

    def test_guard_base_prefers_latest_radar_1d_upper(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        r1d = {"rows": [{"symbol": "AAA", "upper": 1.01}]}  # snapshot 0.95 would pass; newer radar -> skip
        res = self.run_exec(hl, _cands(_cand("AAA", typ="Base")), {"AAA": {"decision": "approve"}}, r1d=r1d)
        self.assertEqual(res["actions"], [])

    def test_guard_missing_upper_fails_closed(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        c = _cand("AAA", typ="Base", filt=0.97, lower=0.95)
        c["upper_1d"] = None
        res = self.run_exec(hl, _cands(c), {"AAA": {"decision": "approve"}})
        self.assertEqual(res["actions"], [])
        self.assertIn("no 1D Upper", res["skipped"][0]["reason"])

    def test_guard_uses_fresh_mid_at_order_time(self):
        class MovingHL(FakeHL):
            n = 0
            def all_mids(self):
                self.n += 1
                return {"AAA": 1.0} if self.n == 1 else {"AAA": 0.94}  # drops below 1D Upper 0.95
        hl = MovingHL(equity=1000, meta=self.META)
        res = self.run_exec(hl, _cands(_cand("AAA", typ="Base")), {"AAA": {"decision": "approve"}})
        self.assertEqual(res["actions"], [])
        self.assertIn("below 1D Upper at entry", res["skipped"][0]["reason"])

    def test_base_and_chase_row_is_treated_as_base(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        c = _cand("AAA", typ="Chase", filt=0.97, lower=0.95, upper_4h=1.5)
        c["is_base"] = True
        res = self.run_exec(hl, _cands(c), {"AAA": {"decision": "approve"}})
        self.assertEqual(len(res["actions"]), 1)
        self.assertEqual(res["pending"], [])

    # ---- Chase -> pending (never an 08:55 order)
    def test_chase_dry_run_reports_pending_but_stores_nothing(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", typ="Chase")), {"AAA": {"decision": "approve", "size_pct": 3}})
        self.assertEqual(res["actions"], [])
        self.assertEqual(res["pending"][0]["kind"], "CONT")
        self.assertTrue(res["pending"][0]["would_create"])
        self.assertFalse(os.path.exists(os.environ["PENDING_PATH"]))
        self.assertEqual(hl.calls, [])

    def test_qty_notional_liq_and_no_orders(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", tier="small", filt=0.97, lower=0.95)),
                            {"AAA": {"decision": "approve", "size_pct": 6, "leverage": 5}})
        self.assertEqual(res["mode"], "DRY_RUN")
        self.assertEqual(res["status"], "success")
        a = res["actions"][0]
        self.assertEqual(a["leverage"], 3)                     # SoT-2 3-5x, clamped to coin max 3x
        self.assertEqual(a["limit_px"], 1.005)                 # live mid + 0.5%
        self.assertEqual(a["size_pct"], 4.0)                   # SoT-2 hard cap 4% margin (AI 6% = max)
        self.assertEqual(a["qty"], 119.0)                      # floor(40*3/1.005)
        self.assertAlmostEqual(a["notional_usd"], 119.0, 2)    # qty*mid ~= margin*lev, not margin
        self.assertAlmostEqual(a["risk_margin_pct"], 3.967, 3)  # risk = isolated margin (119/3/1000)
        self.assertGreater(a["estimated_liq"], 0)
        self.assertLess(a["estimated_liq"], a["hard_sl"])
        self.assertEqual(a["margin_mode"], "isolated")
        self.assertEqual(hl.calls, [])                         # nothing signed/sent
        self.log_entry.assert_called_once()
        self.assertTrue(self.log_entry.call_args.kwargs["dry_run"])
        self.assertEqual(self.log_entry.call_args.kwargs["entry_size"], 119.0)

    def test_cumulative_margin_cap(self):
        # GIIQ-SoT-5: one total margin cap, 80% NAV (cumulative across the run; was 70% + 80% utilisation)
        hl = FakeHL(equity=1000, margin_used=740, meta=self.META, mids={"AAA": 1.0, "BBB": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", tier="small", filt=0.97, lower=0.95), _cand("BBB", tier="small", filt=0.97, lower=0.95)),
                            {"AAA": {"decision": "approve", "size_pct": 6, "leverage": 2},
                             "BBB": {"decision": "approve", "size_pct": 6, "leverage": 2}})
        self.assertEqual([a["symbol"] for a in res["actions"]], ["AAA"])  # 74%+~4% ok
        self.assertEqual(res["skipped"][0]["symbol"], "BBB")               # ~78%+~4% > 80%
        self.assertIn("cumulative", res["skipped"][0]["reason"])

    def test_stale_candidates_fail_closed(self):
        hl = FakeHL(meta=self.META, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA"), generated_at=NOW - timedelta(days=1)),
                            {"AAA": {"decision": "approve"}})
        self.assertEqual(res["status"], "fail_closed")
        self.assertIn("not fresh", res["message"])

    def test_no_approvals_fail_closed(self):
        res = self.run_exec(FakeHL(), _cands(_cand("AAA")), {"AAA": {"decision": "veto"}})
        self.assertEqual(res["status"], "fail_closed")

    def test_large_tier_hard_sl_is_4h_lower(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"LRG": 100.0})
        res = self.run_exec(hl, _cands(_cand("LRG", tier="large", filt=97.0, lower=94.0, close=100.0)),
                            {"LRG": {"decision": "approve", "size_pct": 6, "leverage": 2}},
                            r4h=_radar4h(("LRG", 97.0, 94.0, 100.0)))
        a = res["actions"][0]
        self.assertEqual(a["hard_sl"], 94.0)
        self.assertEqual(a["hard_sl_label"], "4H Lower")

    def test_sl_distance_from_live_mid(self):
        hl = FakeHL(meta=self.META, mids={"AAA": 0.91})  # 1.1% above the 0.9 SL
        res = self.run_exec(hl, _cands(_cand("AAA", filt=0.9)), {"AAA": {"decision": "approve"}})
        self.assertEqual(res["actions"], [])
        self.assertIn("SL distance", res["skipped"][0]["reason"])

    def test_already_held_skipped(self):
        hl = FakeHL(meta=self.META, mids={"AAA": 1.0},
                    positions=[{"coin": "AAA", "szi": "10", "entryPx": "1", "liquidationPx": "0.5"}])
        res = self.run_exec(hl, _cands(_cand("AAA")), {"AAA": {"decision": "approve"}},
                            r4h=_radar4h(("AAA", 0.9, 0.8, 1.0)))
        self.assertIn("already holding", res["skipped"][0]["reason"])


# ------------------------------------------------------------------ executor flow (LIVE, mocked)
class TestExecutorLive(EnvMixin, unittest.TestCase):
    META = {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}}

    def setUp(self):
        super().setUp()
        os.environ["EXEC_DRY_RUN"] = "0"
        os.environ["HL_API_PRIVATE_KEY"] = "0x" + "11" * 32

    def test_live_entry_then_sl(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = executor.execute_approved_candidates(hl=hl, candidates_data=_cands(_cand("AAA", tier="small", filt=0.97, lower=0.95)),
                                                   decisions={"AAA": {"decision": "approve", "size_pct": 6, "leverage": 2}},
                                                   radar_1h={}, radar_4h={"rows": []}, now=NOW)
        self.assertEqual(res["mode"], "LIVE")
        self.assertEqual(len(res["executed"]), 1)
        self.assertEqual(hl.names(), ["set_leverage", "open_long_ioc", "place_stop_loss"])
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 2))  # SoT-5: AI 2x is allowed (floor 2x)
        self.assertEqual(hl.calls[1][4], hl_exec._make_cloid("AAA", "2026-09-28"))   # deterministic cloid
        self.assertEqual(hl.calls[2], ("place_stop_loss", "AAA", 79.0, 0.97))  # SL for the filled qty at Hard SL
        self.assertTrue(res["executed"][0]["live_result"]["reconcile"]["ok"])  # SoT-5 post-fill reconciliation
        self.assertFalse(self.log_entry.call_args.kwargs["dry_run"])

    def test_live_guard_skip_places_nothing(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = executor.execute_approved_candidates(hl=hl, candidates_data=_cands(_cand("AAA", typ="Base", filt=0.97, lower=0.95, upper_1d=1.05)),
                                                   decisions={"AAA": {"decision": "approve"}},
                                                   radar_1h={}, radar_4h={"rows": []}, radar_1d={"rows": []}, now=NOW)
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["executed"], [])
        self.assertIn("below 1D Upper at entry", res["skipped"][0]["reason"])
        self.log_entry.assert_not_called()

    def _live_chase(self, hl, decisions=None):
        r1d = {"rows": [{"symbol": "AAA", "lower": 0.7, "filter": 0.8, "upper": 0.9, "close": 1.0, "trend": "Green"}]}
        r4h = {"rows": [{"symbol": "AAA", "lower": 0.85, "filter": 0.92, "upper": 0.97, "close": 1.0, "trend": "Green"}]}
        return executor.execute_approved_candidates(
            hl=hl, candidates_data=_cands(_cand("AAA", typ="Chase")),
            decisions=decisions or {"AAA": {"decision": "approve", "size_pct": 3, "leverage": 2}},
            radar_1h={}, radar_4h=r4h, radar_1d=r1d, now=NOW)

    def test_live_chase_no_position_creates_continuation_pending(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self._live_chase(hl)
        self.assertEqual(hl.calls, [])                       # no order at 08:55
        p = res["pending"][0]
        # 3-step watch record: no zone, the 4H Filter / Lower are reported for the retrace step
        self.assertEqual((p["kind"], p["4h_lower"], p["4h_filter"]), ("CONT", 0.85, 0.92))
        self.assertTrue(p["created"])
        import pending_entries as pe
        stored = pe.load_pending()
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0]["size_pct"], stored[0]["leverage"]), (3, 2))
        # idempotent re-run (/api/exec/run): no duplicate
        res2 = self._live_chase(FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}))
        self.assertFalse(res2["pending"][0]["created"])
        self.assertEqual(len(pe.load_pending()), 1)
        self.assertEqual(res2["pending_active"][0]["symbol"], "AAA")

    def test_live_chase_on_held_long_creates_add_on_pending(self):
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.3"}
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}, positions=[pos])
        res = self._live_chase(hl)
        self.assertEqual(hl.calls, [])
        p = res["pending"][0]
        self.assertEqual((p["kind"], p["4h_lower"], p["4h_filter"]), ("ADD_ON", 0.85, 0.92))
        self.assertEqual(res["skipped"], [])                 # not "already holding"

    def test_live_sl_failure_closes_position(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}, sl_ok=False)
        res = executor.execute_approved_candidates(hl=hl, candidates_data=_cands(_cand("AAA", filt=0.97, lower=0.95)),
                                                   decisions={"AAA": {"decision": "approve"}},
                                                   radar_1h={}, radar_4h={"rows": []}, now=NOW)
        # SoT-5: the reconciliation retries the SL once before the fail-safe close
        self.assertEqual(hl.names(), ["set_leverage", "open_long_ioc", "place_stop_loss", "place_stop_loss",
                                      "market_close"])
        self.assertEqual(res["executed"], [])
        self.assertEqual(len(res["alerts"]), 1)
        self.assertTrue(res["alerts"][0].startswith("AAA: sl_failed_closed"), res["alerts"])
        self.assertEqual(res["status"], "success")
        self.log_entry.assert_not_called()


class TestEnterLongWithSL(unittest.TestCase):
    def test_leverage_failure_places_nothing(self):
        hl = FakeHL(lev_ok=False)
        r = hl_exec.enter_long_with_sl(hl, "AAA", 10, 1.0, 2, 0.9, 0)
        self.assertEqual(r["status"], "leverage_failed")
        self.assertEqual(hl.names(), ["set_leverage"])

    def test_no_fill_places_no_sl(self):
        hl = FakeHL(fill=False)
        r = hl_exec.enter_long_with_sl(hl, "AAA", 10, 1.0, 2, 0.9, 0)
        self.assertEqual(r["status"], "entry_failed")
        self.assertNotIn("place_stop_loss", hl.names())

    def test_sl_and_close_both_fail_is_critical(self):
        hl = FakeHL(sl_ok=False, close_ok=False)
        r = hl_exec.enter_long_with_sl(hl, "AAA", 10, 1.0, 2, 0.9, 0)
        self.assertEqual(r["status"], "sl_failed_CLOSE_FAILED")

    def test_sl_exception_triggers_close(self):
        hl = FakeHL()
        hl.place_stop_loss = MagicMock(side_effect=RuntimeError("network"))
        r = hl_exec.enter_long_with_sl(hl, "AAA", 10, 1.0, 2, 0.9, 0)
        self.assertEqual(r["status"], "sl_failed_closed")


class TestHLClientSigned(unittest.TestCase):
    def test_refuses_when_dry(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1", "HL_API_PRIVATE_KEY": "0x" + "11" * 32}):
            with self.assertRaises(hl_exec.LiveModeRefused):
                hl_exec.HLClient().exchange()

    def test_refuses_key_not_approved_agent(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "11" * 32}):
            c = hl_exec.HLClient("0x" + "cd" * 20)
            with patch.object(c, "info", return_value=[{"name": "other", "address": "0x" + "ab" * 20, "validUntil": 9e15}]):
                with self.assertRaises(hl_exec.LiveModeRefused):
                    c.exchange()

    def test_refuses_when_agent_query_fails(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "11" * 32}):
            c = hl_exec.HLClient("0x" + "cd" * 20)
            with patch.object(c, "info", side_effect=RuntimeError("HL down")):
                with self.assertRaises(hl_exec.LiveModeRefused):
                    c.exchange()

    def test_agent_status_expired_and_valid(self):
        from eth_account import Account
        addr = Account.from_key("0x" + "11" * 32).address
        c = hl_exec.HLClient("0x" + "cd" * 20)
        with patch.object(c, "info", return_value=[{"name": "k", "address": addr.lower(), "validUntil": 1000}]):
            st = c.agent_status(addr, now_ms=2000)
            self.assertFalse(st["ok"])
            self.assertIn("EXPIRED", st["reason"])
            st = c.agent_status(addr, now_ms=500)
            self.assertTrue(st["ok"])

    def test_accepts_approved_agent_ignoring_stale_hint(self):
        from eth_account import Account
        addr = Account.from_key("0x" + "11" * 32).address
        env = {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "11" * 32, "HL_API_WALLET_ADDRESS": "0x" + "b7" * 20}
        with patch.dict(os.environ, env):
            c = hl_exec.HLClient("0x" + "cd" * 20)
            with patch.object(c, "info", return_value=[{"name": "Railway Key", "address": addr.lower(), "validUntil": 9e15}]), \
                 patch("hyperliquid.exchange.Exchange") as ex_cls:
                c.exchange()
                kwargs = ex_cls.call_args.kwargs
                self.assertEqual(kwargs["account_address"], "0x" + "cd" * 20)  # signs as agent FOR main account
                self.assertEqual(kwargs["wallet"].address, addr)

    def test_probe_signing(self):
        c = hl_exec.HLClient("0xmain")
        ex = MagicMock()
        c._exchange = ex
        ex.cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed"}]}}}
        self.assertTrue(c.probe_signing()["ok"])
        ex.cancel.return_value = {"status": "err", "response": "User or API Wallet 0x.. does not exist."}
        self.assertFalse(c.probe_signing()["ok"])

    def test_executor_errors_loudly_when_signing_refused(self):
        class RefusingHL(FakeHL):
            def exchange(self):
                raise hl_exec.LiveModeRefused("not an approved agent")
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "11" * 32}):
            hl = RefusingHL(meta={"AAA": {"szDecimals": 0, "maxLeverage": 5.0}}, mids={"AAA": 1.0})
            cands = {"generated_at": NOW.isoformat(), "candidates": [{"symbol": "AAA", "type": "Base", "tier": "tiny"}]}
            with patch.object(executor, "log_entry"):
                res = executor.execute_approved_candidates(
                    hl=hl, candidates_data=cands, decisions={"AAA": {"decision": "approve", "size_pct": 4, "leverage": 2}},
                    radar_1h={"rows": []}, radar_4h={"rows": [{"symbol": "AAA", "filter": 0.9, "lower": 0.8}]}, now=NOW)
            self.assertEqual(res["status"], "error")
            self.assertIn("signing client refused", res["message"])
            self.assertEqual(hl.calls, [])

    def test_order_payloads(self):
        c = hl_exec.HLClient("0xmain")
        ex = MagicMock()
        ok_rest = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 7}}]}}}
        ok_fill = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "12", "avgPx": "1.01", "oid": 8}}]}}}
        ex.order.side_effect = [ok_fill, ok_rest]
        ex.update_leverage.return_value = {"status": "ok", "response": {"type": "default"}}
        c._exchange = ex
        self.assertTrue(c.set_leverage("AAA", 3)["ok"])
        ex.update_leverage.assert_called_with(3, "AAA", is_cross=False)  # isolated
        r = c.open_long_ioc("AAA", 12, 1.02)
        self.assertEqual((r["status"], r["filled_sz"], r["avg_px"]), ("filled", 12.0, 1.01))
        self.assertEqual(ex.order.call_args_list[0].args[4], {"limit": {"tif": "Ioc"}})
        sl = c.place_stop_loss("AAA", 12, 0.91234, 0)
        self.assertEqual(sl["status"], "resting")
        args, kwargs = ex.order.call_args_list[1].args, ex.order.call_args_list[1].kwargs
        self.assertEqual(args[:3], ("AAA", False, 12))
        self.assertEqual(args[4], {"trigger": {"triggerPx": 0.91234, "isMarket": True, "tpsl": "sl"}})
        self.assertTrue(kwargs["reduce_only"])

    def test_parse_error_response(self):
        self.assertEqual(hl_exec.parse_order_response({"status": "err", "response": "bad"})["status"], "error")
        r = hl_exec.parse_order_response({"status": "ok", "response": {"data": {"statuses": [{"error": "min notional"}]}}})
        self.assertEqual((r["status"], r["error"]), ("error", "min notional"))


# ------------------------------------------------------------------ exit worker
class TestExitWorker(unittest.TestCase):
    META = {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}}
    POS = {"coin": "AAA", "szi": "100", "entryPx": "1.0", "positionValue": "100", "unrealizedPnl": "-5", "liquidationPx": "0.6"}

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("EXEC_DRY_RUN", "HL_API_PRIVATE_KEY")}
        self.tier = patch.object(exit_worker, "tier_for", return_value="tiny").start()
        self.get_open = patch.object(exit_worker, "get_open_trades", return_value=[]).start()
        self.log_exit = patch.object(exit_worker, "log_exit").start()

    def tearDown(self):
        patch.stopall()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def live(self):
        os.environ["EXEC_DRY_RUN"] = "0"
        os.environ["HL_API_PRIVATE_KEY"] = "0x" + "11" * 32

    def dry(self):
        os.environ["EXEC_DRY_RUN"] = "1"
        os.environ.pop("HL_API_PRIVATE_KEY", None)

    def test_live_primary_exit_closes_then_cancels_sl(self):
        self.live()
        orders = [{"coin": "AAA", "isTrigger": True, "triggerPx": "0.9", "sz": "100", "oid": 5}]
        hl = FakeHL(positions=[self.POS], meta=self.META, orders=orders)
        r1h = {"rows": [{"symbol": "AAA", "close": 0.95, "lower": 0.97}]}
        res = exit_worker.check_exits("hourly", hl=hl, radar_1h=r1h, radar_4h=_radar4h(("AAA", 0.9, 0.8, 0.95)))
        self.assertEqual(hl.names(), ["market_close", "cancel"])
        self.assertEqual(hl.calls[1], ("cancel", "AAA", 5))
        self.assertEqual(res["exits"][0]["coin"], "AAA")
        self.get_open.assert_called_with(dry_run=False)  # live ignores dry-run trade records

    def test_live_close_failure_keeps_sl(self):
        self.live()
        orders = [{"coin": "AAA", "isTrigger": True, "triggerPx": "0.9", "sz": "100", "oid": 5}]
        hl = FakeHL(positions=[self.POS], meta=self.META, orders=orders, close_ok=False)
        res = exit_worker.check_exits("hourly", hl=hl, radar_1h={"rows": [{"symbol": "AAA", "close": 0.95, "lower": 0.97}]},
                                      radar_4h=_radar4h(("AAA", 0.9, 0.8, 0.95)))
        self.assertEqual(hl.names(), ["market_close"])
        self.assertEqual(res["status"], "error")

    def test_live_hold_aligns_sl_place_before_cancel(self):
        self.live()
        orders = [{"coin": "AAA", "isTrigger": True, "triggerPx": "0.85", "sz": "100", "oid": 5}]
        hl = FakeHL(positions=[self.POS], meta=self.META, orders=orders)
        res = exit_worker.check_exits("hourly", hl=hl, radar_1h={"rows": [{"symbol": "AAA", "close": 1.0, "lower": 0.97}]},
                                      radar_4h=_radar4h(("AAA", 0.9, 0.8, 1.0)))
        self.assertEqual(hl.names(), ["place_stop_loss", "cancel"])  # new SL first, then old cancelled
        self.assertEqual(hl.calls[0], ("place_stop_loss", "AAA", 100.0, 0.9))
        self.assertEqual(res["sl_actions"][0]["action"], "update_sl")

    def test_aligned_sl_no_action(self):
        self.live()
        orders = [{"coin": "AAA", "isTrigger": True, "triggerPx": "0.9", "sz": "100", "oid": 5}]
        hl = FakeHL(positions=[self.POS], meta=self.META, orders=orders)
        res = exit_worker.check_exits("hourly", hl=hl, radar_1h={"rows": [{"symbol": "AAA", "close": 1.0, "lower": 0.97}]},
                                      radar_4h=_radar4h(("AAA", 0.9, 0.8, 1.0)))
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["sl_actions"], [])

    def test_sl_never_below_liq(self):
        self.live()
        pos = dict(self.POS, liquidationPx="0.95")
        hl = FakeHL(positions=[pos], meta=self.META)
        res = exit_worker.check_exits("hourly", hl=hl, radar_1h={"rows": []}, radar_4h=_radar4h(("AAA", 0.9, 0.8, 1.0)))
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["sl_actions"][0]["action"], "sl_blocked_liq")

    def test_mega_hard_sl_is_4h_lower_in_exit_worker(self):
        self.live()
        self.tier.return_value = "mega"
        hl = FakeHL(positions=[self.POS], meta=self.META)
        exit_worker.check_exits("4h", hl=hl, radar_1h={}, radar_4h=_radar4h(("AAA", 0.9, 0.8, 1.0)))
        self.assertEqual(hl.calls, [("place_stop_loss", "AAA", 100.0, 0.8)])

    def test_dry_run_sends_nothing_and_uses_dry_trades(self):
        self.dry()
        hl = FakeHL(positions=[self.POS], meta=self.META)
        res = exit_worker.check_exits("hourly", hl=hl, radar_1h={"rows": [{"symbol": "AAA", "close": 0.95, "lower": 0.97}]},
                                      radar_4h=_radar4h(("AAA", 0.9, 0.8, 0.95)))
        self.assertEqual(hl.calls, [])
        self.assertEqual(len(res["actions"]), 1)
        self.get_open.assert_called_with(dry_run=True)

    def test_flat_book(self):
        self.dry()
        res = exit_worker.check_exits("4h", hl=FakeHL(), radar_1h={}, radar_4h={})
        self.assertEqual(res["status"], "success")


class TestTradeLogDryRunFilter(unittest.TestCase):
    def test_filter(self):
        import trade_log
        with tempfile.TemporaryDirectory() as d:
            with patch.object(trade_log, "TRADE_LOG_PATH", os.path.join(d, "t.json")):
                trade_log.log_entry(trade_id="a", symbol="AAA", dry_run=True)
                trade_log.log_entry(trade_id="b", symbol="AAA", dry_run=False)
                self.assertEqual([t["trade_id"] for t in trade_log.get_open_trades(dry_run=False)], ["b"])
                self.assertEqual([t["trade_id"] for t in trade_log.get_open_trades(dry_run=True)], ["a"])
                self.assertEqual(len(trade_log.get_open_trades()), 2)


# ------------------------------------------------------------------ serve: scheduler status + DESK_DATA
class TestSchedulerStatus(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        patch.object(serve, "SCHEDULER_STATUS_PATH", os.path.join(self.tmp, "s.json")).start()
        patch.object(serve, "OUT_DIR", self.tmp).start()

    def tearDown(self):
        patch.stopall()

    def test_fail_closed_is_not_error_and_persisted(self):
        out = json.dumps({"mode": "DRY_RUN", "status": "fail_closed", "message": "No approved candidates"})
        with patch.object(self.serve, "_run_worker", return_value=(0, out, "")):
            self.serve._scheduled_executor()
        st = self.serve._get_scheduler_status()["executor"]
        self.assertEqual(st["status"], "fail_closed")
        self.assertEqual(json.load(open(os.path.join(self.tmp, "s.json")))["executor"]["status"], "fail_closed")

    def test_exit_worker_failure_is_error(self):
        with patch.object(self.serve, "_run_scan", return_value=(True, "scanned 1h")), \
             patch.object(self.serve, "_run_worker", return_value=(1, "", "Traceback boom")), \
             patch.object(self.serve, "_log_desk_data"):
            self.serve._scheduled_1h_scan_exits()
        st = self.serve._get_scheduler_status()["1h_scan_exits"]
        self.assertEqual(st["status"], "error")
        self.assertIn("boom", st["error"])

    def test_manual_executor_forced_dry_run(self):
        seen = {}

        def fake_run(script, args, timeout, force_dry_run=False):
            seen["force"] = force_dry_run
            return 0, json.dumps({"status": "fail_closed"}), ""
        with patch.object(self.serve, "_run_worker", side_effect=fake_run):
            self.serve._scheduled_executor(manual=True)
        self.assertTrue(seen["force"])
        self.assertIn("manual_executor", self.serve._get_scheduler_status())


class TestDeskDataLog(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        patch.object(serve, "OUT_DIR", self.tmp).start()
        now = datetime.now(timezone.utc)
        self.now = now
        for tf, n in (("1d", 180), ("4h", 180), ("1h", 180)):
            rows = [{"symbol": f"C{i}", "trend": "Green", "close": 1.23456789, "filter": 1.1, "upper": 1.3, "lower": 0.9,
                     "dual_cross_up": i == 0, "dual_cross_down_filter": False, "tier": "tiny", "mcap_usd": 5e7,
                     "rvol": 3.3, "ath": 9.9, "categories": ["x"]} for i in range(n)]
            json.dump({"ts": now.isoformat(), "rows": rows, "breadth": {"green_pct": 90.0},
                       "flags": {"dual_cross_up": ["C0"], "above_upper": ["C0", "C1"]}},
                      open(os.path.join(self.tmp, f"gc_radar_{tf}.json"), "w"))
        json.dump({"generated_at": now.isoformat(), "stale": False,
                   "candidates": [_cand("C0", close=1.0, filt=0.9)]},
                  open(os.path.join(self.tmp, "entry_candidates_latest.json"), "w"))
        hl = {"hl_perp": {"marginSummary": {"accountValue": "0", "totalMarginUsed": "0", "totalNtlPos": "0"},
                          "withdrawable": "0", "assetPositions": []},
              "hl_spot": {"balances": [{"coin": "USDC", "total": "1711.009279", "hold": "0"}]}}
        patch.object(serve, "_get_hl_cached", return_value=hl).start()
        patch.object(serve, "_get_hl_meta_cached", return_value={"C0": {"maxLeverage": 3}}).start()

    def tearDown(self):
        patch.stopall()

    def test_payload_keys_and_trim(self):
        p = self.serve._build_desk_data_payload("1h_scan_exits", now=self.now)
        for k in ("ts", "candidates", "gc_radar_1d", "gc_radar_4h", "gc_radar_1h", "hl_perp", "hl_spot"):
            self.assertIn(k, p)
        row = p["gc_radar_1h"]["rows"][0]
        self.assertEqual(set(row), {"symbol", "trend", "close", "filter", "upper", "lower", "dual_cross_up",
                                    "dual_cross_down", "tier", "cat", "mcap", "live_close", "live_filter", "live_upper",
                                    "live_lower", "live_trend", "live_above_upper", "live_cross_up"})
        self.assertEqual(row["close"], 1.23457)  # rounded
        self.assertEqual(p["gc_radar_1d"]["flags"], {"dual_cross_up": ["C0"]})
        self.assertEqual(p["hl_spot"]["usdc_total"], 1711.01)
        c = p["candidates"][0]
        for k in ("symbol", "tier", "type", "sl_pct", "size_pct", "lev", "liq", "hard_sl"):
            self.assertIn(k, c)
        self.assertGreater(c["liq"], 0)
        self.assertEqual(c["lev"], 3)  # GIIQ-SoT-2: 3-5x capped by coin maxLeverage 3
        self.assertTrue(c["sot2"]["ok"])
        self.assertEqual(c["size_pct"], 2.0)  # risk = isolated margin; Tiny tier 2% NAV cap (AIQ-0022)

    def test_single_line_when_small(self):
        lines = self.serve._desk_data_lines({"ts": "t", "candidates": []}, max_bytes=60000)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[DESK_DATA] {"))
        self.assertNotIn("\n", lines[0])
        json.loads(lines[0][len("[DESK_DATA] "):])

    def test_chunked_when_large_and_reassembles(self):
        p = self.serve._build_desk_data_payload("1d", now=self.now)
        lines = self.serve._desk_data_lines(p, max_bytes=8000)
        self.assertGreater(len(lines), 1)
        n = len(lines)
        merged = {}
        for i, line in enumerate(lines, 1):
            prefix = f"[DESK_DATA {i}/{n}] "
            self.assertTrue(line.startswith(prefix))
            self.assertLessEqual(len(line.encode()), 8000 + 50)
            chunk = json.loads(line[len(prefix):])
            self.assertEqual(chunk["ts"], p["ts"])
            self.assertEqual((chunk["part"], chunk["parts"]), (i, n))
            for k, v in chunk.items():
                if k.startswith("gc_radar_") and k in merged:
                    merged[k]["rows"].extend(v["rows"])
                elif k not in ("part", "parts"):
                    merged[k] = v
        for tf in ("1d", "4h", "1h"):
            self.assertEqual([r["symbol"] for r in merged[f"gc_radar_{tf}"]["rows"]],
                             [r["symbol"] for r in p[f"gc_radar_{tf}"]["rows"]])
        self.assertEqual(merged["candidates"], p["candidates"])
        self.assertEqual(merged["hl_spot"], p["hl_spot"])

    def test_log_writes_stdout_lines(self):
        buf = []
        with patch.object(self.serve.sys, "stdout") as out:
            out.write.side_effect = buf.append
            self.serve._log_desk_data("1h_scan_exits")
        self.assertTrue(buf and all(b.startswith("[DESK_DATA") for b in buf))


if __name__ == "__main__":
    unittest.main()
