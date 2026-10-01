"""Bitunix live pilot — fail-closed tests (MMT 2026-09-30). No network: every exchange call goes to FakeAPI."""
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bx_egress
import bx_live as L
import bx_radar
import bx_shadow
import bx_trade

T0 = datetime(2026, 9, 30, 0, 56, tzinfo=timezone.utc)    # 08:56 HKT
SG = {"ok": True, "ip": "136.110.48.50", "countries": {"ipinfo": "SG", "country_is": "SG"},
      "region": "asia-southeast1-eqsg3a", "reason": "non-US egress verified"}
LIVE_ENV = {"BX_ENABLED": "1", "BX_LIVE": "1", "BX_API_KEY": "KEY_abcdef123", "BX_API_SECRET": "SECRET_zyx987"}


def meta(sym="FOOUSDT", **kw):
    m = {"symbol": sym.replace("USDT", ""), "bx_symbol": sym, "ex": "BX", "asset_class": "crypto",
         "liq_tier": "tradeable", "vol24h_usd": 8e6, "spread_bp": 4.0, "gc_tf": "1d", "tier": "small", "price": 2.0,
         "base_precision": 1, "quote_precision": 4, "min_qty": "1", "max_leverage": 50, "api_supported": True}
    m.update(kw)
    return m


def cand(sym="FOOUSDT", **kw):
    c = {"symbol": sym, "coin": sym.replace("USDT", ""), "type": "Base", "gc_tf": "1d", "tier": "small",
         "close": 2.0, "hard_sl": 1.86, "hard_sl_rule": "4h_filter"}
    c.update(kw)
    return c


LIVE = {"price": 2.0, "bid": 1.9996, "ask": 2.0004, "spread_bp": 2.0, "vol24h": 8e6}
TIERS = [{"startValue": "0", "endValue": "1000000", "maintenanceMarginRate": "0.01"}]
APPROVED = {"decision": "approve", "source": "claude"}


class FakeAPI:
    """Stands in for bx_trade.BXTrade (dry_run False). Records every call."""
    dry_run = False

    def __init__(self, fill=True, sl_attached=True, sl_place_fails=False, liq=1.35, positions=None,
                 account_fails=False, unrealized=0.0):
        self.calls, self.fill, self.sl_attached, self.sl_place_fails = [], fill, sl_attached, sl_place_fails
        self.liq, self.account_fails, self.unrealized = liq, account_fails, unrealized
        self.positions = positions if positions is not None else []
        self.history = []

    def account(self):
        self.calls.append(("account",))
        if self.account_fails:
            raise bx_trade.BXTradeError("account: code 10004 The current ip is not in the apikey ip whitelist", 10004)
        return {"available": "1000", "margin": "0", "frozen": "0", "isolationUnrealizedPNL": "0", "crossUnrealizedPNL": "0"}

    def set_isolated(self, s):
        self.calls.append(("set_isolated", s))

    def set_leverage(self, s, lev):
        self.calls.append(("set_leverage", s, lev))

    def open_long(self, s, qty, px, sl, cid):
        self.calls.append(("open_long", s, qty, px, sl))
        if self.fill:
            self.positions.append({"positionId": "P1", "symbol": s, "side": "LONG", "qty": qty, "avgOpenPrice": "2.0005",
                                   "leverage": 3, "marginMode": "ISOLATION", "liqPrice": str(self.liq),
                                   "unrealizedPNL": str(self.unrealized), "fee": "0", "funding": "0"})
        return {"orderId": "O1"}

    def order_detail(self, order_id=None, client_id=None):
        return {"status": "FILLED" if self.fill else "CANCELED", "tradeQty": "149.9" if self.fill else "0"}

    def pending_positions(self, s=None):
        return [p for p in self.positions if s is None or p["symbol"] == s]

    def tpsl_pending(self, s=None, pid=None):
        return [{"id": "SL1", "slPrice": "1.86"}] if self.sl_attached else []

    def place_position_sl(self, s, pid, sl):
        self.calls.append(("place_sl", s, pid, sl))
        if self.sl_place_fails:
            raise bx_trade.BXTradeError("tpsl: code 30029 SL price must be less than mark price")
        self.sl_attached = True
        return {"orderId": "SL2"}

    def flash_close(self, pid):
        self.calls.append(("flash_close", pid))
        self.positions = [p for p in self.positions if p["positionId"] != pid]
        self.history.append({"positionId": pid, "closePrice": "1.9", "realizedPNL": "-15", "fee": "0.4", "funding": "0"})
        return {"positionId": pid}

    def history_positions(self, s=None, pid=None):
        return self.history or [{"positionId": pid, "closePrice": "1.86", "realizedPNL": "-21", "fee": "0.4",
                                 "funding": "-0.1"}]

    def orders(self):
        return [c for c in self.calls if c[0] == "open_long"]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (L.OUT_DIR, bx_radar.OUT_DIR, bx_shadow.OUT_DIR)
        L.OUT_DIR = bx_radar.OUT_DIR = bx_shadow.OUT_DIR = self.tmp
        os.environ["BX_LEDGER_PATH"] = str(self.tmp / "bx.db")
        self.env = patch.dict(os.environ, LIVE_ENV)
        self.env.start()
        self.conn = L.connect()

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        L.OUT_DIR, bx_radar.OUT_DIR, bx_shadow.OUT_DIR = self._orig
        os.environ.pop("BX_LEDGER_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, cands, metas, decisions=None, now=T0, r4h=()):
        day = L.hkt_date(now)
        (self.tmp / "bx_candidates_latest.json").write_text(json.dumps({"date": day, "candidates": cands}))
        (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": metas}))
        (self.tmp / "bx_radar_4h.json").write_text(json.dumps({"rows": list(r4h)}))
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": []}))
        if decisions is not None:
            L.store_decisions(decisions, "claude", now=now - timedelta(minutes=30))

    def entries(self, api, egress=SG, nav=10_000.0, now=T0):
        return L.run_entries(now=now, trade_api=api, egress=egress, nav_fn=lambda: nav,
                             market=lambda s: dict(LIVE), tiers_fn=lambda s: TIERS, conn=self.conn)


# ---------------------------------------------------------------------------------------------
class TestEgressGate(unittest.TestCase):
    def ans(self, c1, c2, ip1="1.2.3.4", ip2="1.2.3.4"):
        return {"ipinfo": {"ip": ip1, "country": c1}, "country_is": {"ip": ip2, "country": c2}}

    def test_us_ip_refused(self):
        r = bx_egress.evaluate(self.ans("US", "US"), "asia-southeast1-eqsg3a")
        self.assertFalse(r["ok"])
        self.assertIn("US", r["reason"])
        self.assertFalse(bx_egress.evaluate(self.ans("PR", "PR"), None)["ok"])       # US territory

    def test_unknown_or_disagreeing_refused(self):
        self.assertFalse(bx_egress.evaluate({"ipinfo": {"ip": "1.2.3.4", "country": "SG"}}, None)["ok"])
        self.assertFalse(bx_egress.evaluate(self.ans("SG", None), None)["ok"])
        self.assertFalse(bx_egress.evaluate(self.ans("SG", "US"), None)["ok"])
        self.assertFalse(bx_egress.evaluate(self.ans("SG", "SG", "1.1.1.1", "2.2.2.2"), None)["ok"])

    def test_us_region_and_wrong_static_ip_refused(self):
        self.assertFalse(bx_egress.evaluate(self.ans("SG", "SG"), "us-west2")["ok"])
        r = bx_egress.evaluate(self.ans("SG", "SG"), "asia-southeast1-eqsg3a", expected_ip="9.9.9.9")
        self.assertFalse(r["ok"])
        self.assertIn("whitelisted", r["reason"])

    def test_singapore_ok(self):
        r = bx_egress.evaluate(self.ans("SG", "SG", "136.110.48.50", "136.110.48.50"), "asia-southeast1-eqsg3a",
                               expected_ip="136.110.48.50")
        self.assertTrue(r["ok"])

    def test_check_fails_closed_when_a_source_is_down(self):
        def opener(req, timeout=None):
            if "ipinfo" in req.full_url:
                raise OSError("down")
            return io.BytesIO(json.dumps({"ip": "1.2.3.4", "country": "SG"}).encode())
        r = bx_egress.check(force=True, opener=opener)
        self.assertFalse(r["ok"])
        self.assertIn("source_errors", r)


class TestLiveGate(unittest.TestCase):
    def test_all_prerequisites(self):
        ok, why = L.live_gate(SG, True, {"tripped": False}, env={"BX_ENABLED": "1", "BX_LIVE": "1"})
        self.assertTrue(ok, why)

    def test_each_missing_item_blocks(self):
        base = {"BX_ENABLED": "1", "BX_LIVE": "1"}
        self.assertFalse(L.live_gate(SG, True, {}, env={**base, "BX_LIVE": "0"})[0])
        self.assertFalse(L.live_gate(SG, True, {}, env={"BX_ENABLED": "1"})[0])            # BX_LIVE defaults to 0
        self.assertFalse(L.live_gate(SG, True, {}, env={**base, "BX_ENABLED": "0"})[0])
        self.assertFalse(L.live_gate(dict(SG, ok=False, reason="egress IP is in the US"), True, {}, env=base)[0])
        self.assertFalse(L.live_gate(None, True, {}, env=base)[0])
        self.assertFalse(L.live_gate(SG, False, {}, env=base)[0])                          # missing key
        ok, why = L.live_gate(SG, True, {"tripped": True, "at": "x"}, env=base)
        self.assertFalse(ok)
        self.assertTrue(any("breaker" in w for w in why))


class TestEntriesFailClosed(Tmp):
    def test_us_ip_sends_nothing(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        api = FakeAPI()
        rep = self.entries(api, egress=dict(SG, ok=False, reason="egress IP is in the US"))
        self.assertFalse(rep["live"])
        self.assertEqual(api.calls, [])

    def test_missing_key_sends_nothing(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        api = FakeAPI()
        with patch.dict(os.environ, {"BX_API_KEY": "", "BX_API_SECRET": ""}):
            rep = self.entries(api)
            with self.assertRaises(bx_trade.BXTradeError):
                bx_trade.BXTrade()
        self.assertFalse(rep["live"])
        self.assertEqual(api.calls, [])

    def test_no_approval_no_order(self):
        self.seed([cand()], [meta()], decisions=[])
        api = FakeAPI()
        rep = self.entries(api)
        self.assertEqual(api.orders(), [])
        self.assertIn("no ENTRY_DESK approval", rep["skipped"][0]["reason"])

    def test_veto_no_order(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "veto", "rule": "V1_WEAK_4H_BREAKOUT"}])
        api = FakeAPI()
        self.entries(api)
        self.assertEqual(api.orders(), [])

    def test_whitelist_error_refuses(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        api = FakeAPI(account_fails=True)
        rep = self.entries(api)
        self.assertFalse(rep["live"])
        self.assertIn("10004", rep["gate"][0])
        self.assertEqual(api.orders(), [])

    def test_nav_unavailable_refuses(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        api = FakeAPI()
        rep = self.entries(api, nav=None)
        self.assertEqual(api.orders(), [])
        self.assertIn("NAV", rep["skipped"][0]["reason"])

    def test_approved_entry_with_sl_and_live_marking(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        api = FakeAPI()
        rep = self.entries(api)
        self.assertEqual(rep["entered"][0]["status"], "filled", rep)
        o = api.orders()[0]
        self.assertEqual(o[4], "1.8600")                       # Hard SL attached to the entry order
        self.assertIn(("set_isolated", "FOOUSDT"), api.calls)
        self.assertIn(("set_leverage", "FOOUSDT", 3), api.calls)
        t = L.open_live_trades(self.conn)[0]
        self.assertEqual((t["mode"], t["position_id"], t["size_pct_nav"]), ("live", "P1", 1.0))

    def test_one_new_entry_per_day(self):
        self.seed([cand(), cand("BARUSDT")], [meta(), meta("BARUSDT")],
                  [{"symbol": "FOOUSDT", "decision": "approve"}, {"symbol": "BARUSDT", "decision": "approve"}])
        api = FakeAPI()
        rep = self.entries(api)
        self.assertEqual(len(api.orders()), 1)
        self.assertIn("new BX entry already today", rep["skipped"][-1]["reason"])

    def test_fallback_decisions_rejected(self):
        self.seed([cand()], [meta()])
        res = L.store_decisions([{"symbol": "FOOUSDT", "decision": "approve"}], "fallback", now=T0)
        self.assertFalse(res["ok"])
        self.assertIsNone(L.approval_for("FOOUSDT", T0))
        res = L.store_decisions([{"symbol": "ZZZUSDT", "decision": "approve"}], "claude", now=T0)
        self.assertEqual(res["rejected"][0]["why"], "not in today's BX candidate list")
        res = L.store_decisions([{"coin": "FOO", "action": "APPROVE"}], "claude", now=T0)   # desk prompt shape
        self.assertEqual(res["stored"], 1)
        self.assertTrue(L.approval_for("FOOUSDT", T0))


class TestProtection(Tmp):
    def go(self, api):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        return self.entries(api)["entered"][0]

    def test_missing_sl_is_placed(self):
        api = FakeAPI(sl_attached=False)
        res = self.go(api)
        self.assertEqual(res["status"], "filled")
        self.assertTrue(any(c[0] == "place_sl" for c in api.calls))

    def test_sl_cannot_be_placed_position_closed(self):
        api = FakeAPI(sl_attached=False, sl_place_fails=True)
        res = self.go(api)
        self.assertEqual(res["status"], "closed_protection_failed")
        self.assertIn(("flash_close", "P1"), api.calls)
        self.assertEqual(L.open_live_trades(self.conn), [])

    def test_exchange_liq_above_sl_position_closed(self):
        api = FakeAPI(liq=1.9)
        res = self.go(api)
        self.assertEqual(res["status"], "closed_protection_failed")
        self.assertIn(("flash_close", "P1"), api.calls)

    def test_not_filled_records_nothing(self):
        api = FakeAPI(fill=False)
        self.assertEqual(self.go(api)["status"], "not_filled")
        self.assertEqual(L.open_live_trades(self.conn), [])


class TestPilotRules(unittest.TestCase):
    def chk(self, c=None, m=None, live=None, nav=10_000.0, avail=1_000.0, open_live=(), today=0, appr=APPROVED,
            tiers=TIERS):
        return L.check_entry(c or cand(), m or meta(), live or dict(LIVE), nav, avail, list(open_live), today, appr, tiers)

    def test_ok_plan(self):
        r = self.chk()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["plan"]["leverage"], 3)
        self.assertAlmostEqual(r["plan"]["margin_usd"], 100.0)   # 1% of 10k NAV
        self.assertLess(r["plan"]["liq_est"], 1.86)

    def test_tier_and_universe_limits(self):
        self.assertFalse(self.chk(m=meta(liq_tier="watch"))["ok"])
        self.assertFalse(self.chk(m=meta(gc_tf="1h"))["ok"])
        self.assertFalse(self.chk(m=meta(asset_class="stock"))["ok"])
        self.assertFalse(self.chk(m=meta(asset_class="commodity"))["ok"])
        self.assertFalse(self.chk(m=meta(ex="HL+BX"))["ok"])
        self.assertFalse(self.chk(m=meta(spread_bp=10.0))["ok"])                     # must be < 10 bp
        self.assertFalse(self.chk(live=dict(LIVE, spread_bp=10.5))["ok"])            # live re-check
        self.assertFalse(self.chk(live=dict(LIVE, vol24h=1.9e6))["ok"])
        self.assertFalse(self.chk(m=meta(vol24h_usd=1.5e6))["ok"])

    def test_position_caps(self):
        two = [{"bx_symbol": "A"}, {"bx_symbol": "B"}]
        self.assertIn("max 2", self.chk(open_live=two)["reason"])
        self.assertIn("already holding", self.chk(open_live=[{"bx_symbol": "FOOUSDT"}])["reason"])
        self.assertIn("max 1", self.chk(today=1)["reason"])

    def test_sl_distance_and_liquidation(self):
        self.assertIn("1.5%", self.chk(c=cand(hard_sl=1.98))["reason"])
        # SL below the 3x liquidation price (~1.36) -> liq not beyond the SL -> skip
        self.assertIn("liq", self.chk(c=cand(hard_sl=1.30))["reason"])
        self.assertIn("liq", self.chk(tiers=[{"startValue": "0", "endValue": "1e9", "maintenanceMarginRate": "0.30"}])["reason"])

    def test_downsized_to_half_percent_of_volume(self):
        r = self.chk(nav=2_000_000.0, avail=1e9)            # 1% x 3 = $60k notional > $40k (0.5% of $8M)
        self.assertTrue(r["ok"])
        self.assertLessEqual(float(r["plan"]["qty"]) * 2.0004, 0.005 * 8e6 + 1e-6)
        self.assertIn("0.5% of 24h vol", r["plan"]["note"])

    def test_margin_above_bx_balance_refused(self):
        self.assertIn("available", self.chk(avail=50.0)["reason"])


class TestBreaker(Tmp):
    def test_trips_at_minus_3pct(self):
        self.assertFalse(L.breaker_check(-299.0, 0, 10_000)["trip"])
        self.assertTrue(L.breaker_check(-200.0, -100.0, 10_000)["trip"])

    def test_manage_trips_breaker_blocks_entries_and_reports(self):
        L.baseline_nav(10_000.0)
        api = FakeAPI(unrealized=-320.0)
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        self.entries(api)                                     # opens P1
        api.positions[0]["unrealizedPNL"] = "-320"
        with patch.object(L, "railway_set_live_off", return_value="set to 0"):
            rep = L.run_manage("1h", now=T0 + timedelta(hours=1), trade_api=api, egress=SG, nav_fn=lambda: 9_600.0,
                               market=lambda s: dict(LIVE), tiers_fn=lambda s: TIERS, conn=self.conn)
        self.assertTrue(rep["breaker"]["trip"], rep)
        st = L.breaker_state()
        self.assertTrue(st["tripped"])
        self.assertEqual(st["railway_var"], "set to 0")
        ok, why = L.live_gate(SG, True, st, env={"BX_ENABLED": "1", "BX_LIVE": "1"})
        self.assertFalse(ok)
        # next day: approved candidate, breaker tripped -> no order
        api2 = FakeAPI()
        self.seed([cand("BARUSDT")], [meta("BARUSDT")], [{"symbol": "BARUSDT", "decision": "approve"}],
                  now=T0 + timedelta(days=1))
        self.entries(api2, now=T0 + timedelta(days=1))
        self.assertEqual(api2.orders(), [])
        # cockpit exit health shows it as a problem (EXIT_DESK emails MMT)
        import exit_health
        h = exit_health.check(perp={"assetPositions": []}, open_orders=[], nav=1.0, radar_1h={}, radar_4h={},
                              tier_for=lambda s: "small", job_status={}, pending=[], now=T0,
                              bx_status={"ok": True, "breaker": st, "problems": [], "bx_live": True, "egress": SG})
        self.assertIn("BX_BREAKER", [p["code"] for p in h["problems"]])

    def test_reset_needs_admin_key(self):
        L.trip_breaker({"pnl_usd": -301}, set_var=lambda: "x")
        self.assertFalse(L.reset_breaker("anything")[0])                      # BX_ADMIN_KEY not set
        with patch.dict(os.environ, {"BX_ADMIN_KEY": "admin-secret"}):
            self.assertFalse(L.reset_breaker("wrong")[0])
            self.assertTrue(L.reset_breaker("admin-secret")[0])
        self.assertFalse(L.breaker_state()["tripped"])


class TestExits(Tmp):
    def open_one(self, **kw):
        self.seed([cand(**kw)], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        api = FakeAPI()
        self.entries(api)
        return api

    def manage(self, api, now, r4h=(), live=None, met=None):
        (self.tmp / "bx_radar_4h.json").write_text(json.dumps({"rows": list(r4h)}))
        if met is not None:
            (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": met}))
        return L.run_manage("4h", now=now, trade_api=api, egress=SG, nav_fn=lambda: 10_000.0,
                            market=lambda s: dict(live or LIVE), tiers_fn=lambda s: TIERS, conn=self.conn)

    def test_4h_filter_exit(self):
        api = self.open_one()
        later = T0 + timedelta(hours=8)
        rep = self.manage(api, later, r4h=[{"bx_symbol": "FOOUSDT", "close": 1.9, "filter": 1.95,
                                             "bar_time": int(later.timestamp() * 1000) - 14_400_000}])
        self.assertEqual(rep["closed"][0]["reason"], "exit_4h_close_below_filter")
        self.assertIn(("flash_close", "P1"), api.calls)
        t = self.conn.execute("SELECT status, pnl_usd FROM shadow_trades WHERE mode='live'").fetchone()
        self.assertEqual(t[0], "closed")
        self.assertAlmostEqual(t[1], -15.4)

    def test_time_cap_and_liquidity_exit(self):
        api = self.open_one()
        rep = self.manage(api, T0 + timedelta(days=7, minutes=5))
        self.assertEqual(rep["closed"][0]["reason"], "time_cap_7d")
        api = FakeAPI()
        self.seed([cand("BARUSDT")], [meta("BARUSDT")], [{"symbol": "BARUSDT", "decision": "approve"}],
                  now=T0 + timedelta(days=8))
        self.entries(api, now=T0 + timedelta(days=8))
        rep = self.manage(api, T0 + timedelta(days=8, hours=4), live=dict(LIVE, spread_bp=35.0))
        self.assertEqual(rep["closed"][0]["reason"], "liquidity_exit")

    def test_exchange_sl_hit_is_recorded(self):
        api = self.open_one()
        api.positions.clear()
        rep = self.manage(api, T0 + timedelta(hours=3))
        self.assertIn("exchange_close", rep["closed"][0]["reason"])

    def test_exits_run_while_bx_live_off(self):
        api = self.open_one()
        with patch.dict(os.environ, {"BX_LIVE": "0"}):
            rep = self.manage(api, T0 + timedelta(days=7, minutes=5))
        self.assertEqual(rep["closed"][0]["reason"], "time_cap_7d")


class TestSigningAndSecrets(unittest.TestCase):
    def test_sign_formula(self):
        body = '{"uid":"2899","arr":[{"id":1,"name":"maple"},{"id":2,"name":"lily"}]}'
        d = hashlib.sha256(("123456" + "20241120123045" + "yourApiKey" + "id1uid200" + body).encode()).hexdigest()
        want = hashlib.sha256((d + "yourSecretKey").encode()).hexdigest()
        got = bx_trade.sign("yourApiKey", "yourSecretKey", "123456", "20241120123045", {"uid": 200, "id": 1}, body)
        self.assertEqual(got, want)
        self.assertEqual(bx_trade.query_string_for_sign({"uid": 200, "id": 1}), "id1uid200")
        self.assertEqual(bx_trade.compact_body({"a": 1, "b": "x y"}), '{"a":1,"b":"x y"}')

    def test_wire_body_is_the_signed_body(self):
        sent = {}

        def opener(req, timeout=None):
            sent["body"], sent["headers"] = req.data.decode(), dict(req.header_items())
            return io.BytesIO(b'{"code":0,"data":{"orderId":"1"}}')
        api = bx_trade.BXTrade(opener=opener, api_key="K" * 12, secret="S" * 12, clock=lambda: 1.0)
        api.open_long("FOOUSDT", "1", "2", "1.8", "cid")
        h = {k.lower(): v for k, v in sent["headers"].items()}
        want = bx_trade.sign("K" * 12, "S" * 12, h["nonce"], h["timestamp"], None, sent["body"])
        self.assertEqual(h["sign"], want)
        self.assertNotIn(" ", sent["body"])

    def test_key_never_in_errors_repr_or_dry_run(self):
        with patch.dict(os.environ, {"BX_API_KEY": "KEY_abcdef123", "BX_API_SECRET": "SECRET_zyx987"}):
            def opener(req, timeout=None):
                raise OSError("boom KEY_abcdef123 SECRET_zyx987")
            api = bx_trade.BXTrade(opener=opener)
            with self.assertRaises(bx_trade.BXTradeError) as cm:
                api.account()
            self.assertNotIn("KEY_abcdef123", str(cm.exception))
            self.assertNotIn("SECRET_zyx987", str(cm.exception))
            self.assertNotIn("KEY_abcdef123", repr(api))
            d = bx_trade.BXTrade(dry_run=True)
            d.account()
            self.assertNotIn("KEY_abcdef123", json.dumps(d.recorded))
            self.assertNotIn("SECRET_zyx987", json.dumps(d.recorded))

    def test_no_withdraw_or_transfer_endpoints(self):
        import ast
        tree = ast.parse(Path(bx_trade.__file__).read_text(encoding="utf-8"))
        doc = tree.body[0].value.value          # the raw module docstring (it describes what is NOT allowed)
        strings = [n.value.lower() for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and n.value != doc]
        names = [n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)] + \
                [n.name.lower() for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        for bad in ("withdraw", "transfer", "sub_account", "subaccount", "/asset/", "/spot/"):
            self.assertFalse([x for x in strings + names if bad in x], bad)
        for method, path in bx_trade.PATHS.values():
            self.assertTrue(path.startswith(("/api/v1/futures/account", "/api/v1/futures/position",
                                             "/api/v1/futures/trade", "/api/v1/futures/tpsl")), path)


class TestDryRun(unittest.TestCase):
    def test_exact_payloads(self):
        r = L.dry_run()
        self.assertTrue(r["ok"], r)
        reqs = r["requests"]
        self.assertEqual([x["url"].split("/api/v1/futures/")[1] for x in reqs],
                         ["account/change_margin_mode", "account/change_leverage", "trade/place_order",
                          "tpsl/position/place_order", "trade/flash_close_position"])
        order = json.loads(reqs[2]["body"])
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["orderType"], "LIMIT")
        self.assertEqual(order["effect"], "IOC")
        self.assertEqual(order["slPrice"], r["plan"]["sl_price"])
        self.assertEqual(order["slStopType"], "MARK_PRICE")
        self.assertEqual(json.loads(reqs[1]["body"])["leverage"], 3)
        self.assertEqual(json.loads(reqs[0]["body"])["marginMode"], "ISOLATION")
        self.assertTrue(all(x["headers"]["api-key"] == "***" for x in reqs))


class TestCockpitRoutesBxDecisionsAway(unittest.TestCase):
    def test_bx_decisions_never_reach_hl_store(self):
        import serve
        from http.server import ThreadingHTTPServer
        tmp = tempfile.mkdtemp()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        forwarded = {}

        def fake_service(path, body=None, timeout=15.0):
            forwarded["path"], forwarded["body"] = path, body
            return 200, {"ok": True, "stored": 1}
        try:
            with patch.object(serve, "AI_DECISION_KEY", "k"), patch.object(serve, "BX_SERVICE_URL", "http://bx"), \
                    patch.object(serve, "_bx_service", side_effect=fake_service), \
                    patch.dict(os.environ, {"DECISIONS_DIR": tmp}):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ai/decision",
                                             data=json.dumps({"bx_decisions": [{"coin": "FOO", "action": "APPROVE"}],
                                                              "source": "claude"}).encode(),
                                             method="POST", headers={"Content-Type": "application/json", "X-AI-Key": "k"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    res = json.loads(r.read())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertTrue(res["ok"], res)
        self.assertEqual(forwarded["path"], "/api/bx/decision")
        self.assertEqual(os.listdir(tmp), [])                     # nothing written to the HL decisions store
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
