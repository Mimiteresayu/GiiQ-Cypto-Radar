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
import executor  # noqa: E402
import exit_worker  # noqa: E402
import hl_exec  # noqa: E402

NOW = datetime(2026, 9, 28, 0, 55, tzinfo=timezone.utc)  # 08:55 HKT


class FakeHL:
    """Mimics hl_exec.HLClient."""

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

    # write
    def set_leverage(self, coin, lev):
        self.calls.append(("set_leverage", coin, lev))
        return {"ok": self.lev_ok}

    def open_long_ioc(self, coin, qty, px):
        self.calls.append(("open_long_ioc", coin, qty, px))
        if not self.fill:
            return {"status": "error", "error": "Order could not immediately match", "filled_sz": 0.0}
        return {"status": "filled", "filled_sz": qty, "avg_px": px, "oid": 1}

    def place_stop_loss(self, coin, qty, trig, szd):
        self.calls.append(("place_stop_loss", coin, qty, trig))
        if not self.sl_ok:
            return {"status": "error", "error": "Invalid TP/SL price"}
        return {"status": "resting", "oid": 99, "trigger_px": trig}

    def market_close(self, coin, qty):
        self.calls.append(("market_close", coin, qty))
        if not self.close_ok:
            return {"status": "error", "error": "boom", "filled_sz": 0.0}
        return {"status": "filled", "filled_sz": qty, "avg_px": 1.0}

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return {"ok": True}

    def names(self):
        return [c[0] for c in self.calls]


def _cands(*items, generated_at=None, stale=False):
    return {"generated_at": (generated_at or (NOW - timedelta(minutes=50))).isoformat(), "stale": stale,
            "candidates": list(items)}


def _cand(sym, tier="tiny", typ="Base", filt=0.9, lower=0.8, close=1.0):
    return {"symbol": sym, "type": typ, "tier": tier, "trend_1d": "Green", "trend_4h": "Green",
            "close_1d": close, "filter_4h": filt, "lower_4h": lower}


def _radar4h(*rows):
    return {"rows": [dict(symbol=s, filter=f, lower=l, close=c) for s, f, l, c in rows]}


class EnvMixin:
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("EXEC_DRY_RUN", "HL_API_PRIVATE_KEY", "EXEC_MAX_CANDIDATE_AGE_H")}
        os.environ["EXEC_DRY_RUN"] = "1"
        os.environ.pop("HL_API_PRIVATE_KEY", None)
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
        cand = {"type": "Base"}
        self.assertEqual(executor._clamp_size_leverage(cand, {"size_pct": 6, "leverage": 5}, False, 3), (6.0, 3))

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


# ------------------------------------------------------------------ executor flow (DRY_RUN)
class TestExecutorDryRun(EnvMixin, unittest.TestCase):
    META = {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}, "BBB": {"szDecimals": 1, "maxLeverage": 3.0},
            "LRG": {"szDecimals": 2, "maxLeverage": 10.0}}

    def run_exec(self, hl, cands, decisions, r4h=None):
        return executor.execute_approved_candidates(hl=hl, candidates_data=cands, decisions=decisions,
                                                    radar_1h={}, radar_4h=r4h or {"rows": []}, now=NOW)

    def test_qty_notional_liq_and_no_orders(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA", filt=0.9)),
                            {"AAA": {"decision": "approve", "size_pct": 6, "leverage": 5}})
        self.assertEqual(res["mode"], "DRY_RUN")
        self.assertEqual(res["status"], "success")
        a = res["actions"][0]
        self.assertEqual(a["leverage"], 3)                     # clamped to coin max 3x
        self.assertEqual(a["limit_px"], 1.005)                 # live mid + 0.5%
        self.assertEqual(a["qty"], 179.0)                      # floor(60*3/1.005)
        self.assertAlmostEqual(a["notional_usd"], 179.0, 2)    # qty*mid ~= margin*lev, not margin
        self.assertGreater(a["estimated_liq"], 0)
        self.assertLess(a["estimated_liq"], a["hard_sl"])
        self.assertEqual(a["margin_mode"], "isolated")
        self.assertEqual(hl.calls, [])                         # nothing signed/sent
        self.log_entry.assert_called_once()
        self.assertTrue(self.log_entry.call_args.kwargs["dry_run"])
        self.assertEqual(self.log_entry.call_args.kwargs["entry_size"], 179.0)

    def test_cumulative_margin_cap(self):
        hl = FakeHL(equity=1000, margin_used=700, meta=self.META, mids={"AAA": 1.0, "BBB": 1.0})
        res = self.run_exec(hl, _cands(_cand("AAA"), _cand("BBB")),
                            {"AAA": {"decision": "approve", "size_pct": 6, "leverage": 2},
                             "BBB": {"decision": "approve", "size_pct": 6, "leverage": 2}})
        self.assertEqual([a["symbol"] for a in res["actions"]], ["AAA"])  # 70%+6% ok
        self.assertEqual(res["skipped"][0]["symbol"], "BBB")               # 76%+6% > 80%
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
        res = executor.execute_approved_candidates(hl=hl, candidates_data=_cands(_cand("AAA", filt=0.9)),
                                                   decisions={"AAA": {"decision": "approve", "size_pct": 6, "leverage": 2}},
                                                   radar_1h={}, radar_4h={"rows": []}, now=NOW)
        self.assertEqual(res["mode"], "LIVE")
        self.assertEqual(len(res["executed"]), 1)
        self.assertEqual(hl.names(), ["set_leverage", "open_long_ioc", "place_stop_loss"])
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 2))
        self.assertEqual(hl.calls[2], ("place_stop_loss", "AAA", 119.0, 0.9))  # SL for the filled qty at Hard SL
        self.assertFalse(self.log_entry.call_args.kwargs["dry_run"])

    def test_live_sl_failure_closes_position(self):
        hl = FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}, sl_ok=False)
        res = executor.execute_approved_candidates(hl=hl, candidates_data=_cands(_cand("AAA")),
                                                   decisions={"AAA": {"decision": "approve"}},
                                                   radar_1h={}, radar_4h={"rows": []}, now=NOW)
        self.assertEqual(hl.names(), ["set_leverage", "open_long_ioc", "place_stop_loss", "market_close"])
        self.assertEqual(res["executed"], [])
        self.assertEqual(res["alerts"], ["AAA: sl_failed_closed"])
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

    def test_refuses_wrong_api_wallet(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "11" * 32}):
            os.environ.pop("HL_API_WALLET_ADDRESS", None)
            with self.assertRaises(hl_exec.LiveModeRefused):
                hl_exec.HLClient().exchange()  # derived address != 0xb74a...

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
                                    "dual_cross_down", "tier", "mcap"})
        self.assertEqual(row["close"], 1.23457)  # rounded
        self.assertEqual(p["gc_radar_1d"]["flags"], {"dual_cross_up": ["C0"]})
        self.assertEqual(p["hl_spot"]["usdc_total"], 1711.01)
        c = p["candidates"][0]
        for k in ("symbol", "tier", "type", "sl_pct", "size_pct", "lev", "liq", "hard_sl"):
            self.assertIn(k, c)
        self.assertGreater(c["liq"], 0)
        self.assertEqual(c["lev"], 2)

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
