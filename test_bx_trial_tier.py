"""BX live pilot 'trial' tier (BX_TRIAL_*): off by default and identical to the tradeable-only pilot; when on,
watch-tier $0.3M-$2M coins become half-size candidates, at most 1 trial fill per HKT day, every cap still applies."""
import io
import itertools
import json
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bx_live as L
import bx_radar as R
import bx_universe as U
from test_bx_live import APPROVED, LIVE, T0, TIERS, FakeAPI, Tmp, cand, meta
from test_bx_radar import H4, NOW, TmpOut, FakeClient, daily_bars, pair, tick

ON = {"BX_TRIAL_TIER_ENABLED": "1"}
TRIAL_VARS = ("BX_TRIAL_TIER_ENABLED", "BX_TRIAL_MIN_VOL_USD", "BX_TRIAL_MAX_SPREAD_BP", "BX_TRIAL_SIZE_MULT",
              "BX_TRIAL_MAX_PER_DAY")
TRIAL_LIVE = {"price": 2.0, "bid": 1.9985, "ask": 2.0015, "spread_bp": 15.0, "vol24h": 6.2e5}


def trial_meta(sym="FOOUSDT", **kw):
    return meta(sym, **{"liq_tier": "watch", "vol24h_usd": 6.2e5, "spread_bp": 15.0, **kw})


def no_trial_env():
    return patch.dict(os.environ, {k: "" for k in TRIAL_VARS})


def main_pilot_eligible(meta):
    """Frozen copy of bx_live.pilot_eligible on main @ 5255a4c (before the trial tier)."""
    if meta.get("ex") != "BX":
        return False, "listed on HL (HL path only)"
    if meta.get("asset_class") != "crypto":
        return False, f"asset class {meta.get('asset_class')} (no stocks / commodities / indices)"
    if meta.get("liq_tier") != "tradeable":
        return False, f"tier {meta.get('liq_tier')} (entry tier only)"
    if (meta.get("vol24h_usd") or 0) < 2_000_000.0:
        return False, "24h volume < $2M"
    sp = U._f(meta.get("spread_bp"))
    if sp is None or sp >= 10.0:
        return False, f"spread {sp} bp not < 10"
    if meta.get("gc_tf") not in ("1d", "4h"):
        return False, f"GC timeframe {meta.get('gc_tf')} (no 1H signals)"
    if meta.get("api_supported") is False:
        return False, "API trading not supported on this contract"
    if meta.get("max_leverage") is not None and (U._f(meta.get("max_leverage")) or 0) < 3:
        return False, "max leverage < 3x"
    return True, ""


def grid():
    for ex, cls, lt, vol, sp, tf, lev in itertools.product(
            ["BX", "HL+BX"], ["crypto", "stock"], ["tradeable", "watch", "exclude", None],
            [0, 2.5e5, 3e5, 6.2e5, 1.99e6, 2e6, 8e6], [None, 4.0, 10.0, 15.0, 19.99, 20.0, 40.0],
            ["1d", "4h", "1h"], [None, 2, 50]):
        yield meta(ex=ex, asset_class=cls, liq_tier=lt, vol24h_usd=vol, spread_bp=sp, gc_tf=tf, max_leverage=lev)


def chk(c=None, m=None, live=None, nav=10_000.0, avail=1_000.0, open_live=(), today=0, trial_today=0, **kw):
    return L.check_entry(c or cand(), m or meta(), dict(live or LIVE), nav, avail, list(open_live), today, APPROVED,
                         TIERS, trial_today=trial_today, **kw)


# ---------------------------------------------------------------------------------------------
class TestDefaultsUnchanged(unittest.TestCase):
    def test_config_defaults(self):
        cfg = U.trial_config({})
        self.assertEqual({k: cfg[k] for k in U.TRIAL_DEFAULTS}, U.TRIAL_DEFAULTS)
        self.assertFalse(cfg["enabled"])
        self.assertEqual((cfg["min_vol_usd"], cfg["max_spread_bp"], cfg["size_mult"], cfg["max_per_day"]),
                         (300_000.0, 20.0, 0.5, 1))
        self.assertEqual(cfg["invalid"], [])

    def test_eligibility_identical_to_main(self):
        with no_trial_env():
            for m in grid():
                self.assertEqual(L.pilot_eligible(m), main_pilot_eligible(m), m)
        # other trial variables set but the switch off -> still identical
        with patch.dict(os.environ, {"BX_TRIAL_TIER_ENABLED": "0", "BX_TRIAL_MIN_VOL_USD": "300000",
                                     "BX_TRIAL_MAX_SPREAD_BP": "30", "BX_TRIAL_SIZE_MULT": "1"}):
            for m in grid():
                self.assertEqual(L.pilot_eligible(m), main_pilot_eligible(m), m)

    def test_sizing_identical_to_main(self):
        with no_trial_env():
            r = chk()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["plan"], {"qty": "149.9", "limit_price": "2.0104", "sl_price": "1.8600",
                                         "margin_usd": 100.0, "notional_usd": 299.86, "leverage": 3, "mmr": 0.01,
                                         "liq_est": 1.3603706667, "sl_dist_pct": 7.0, "nav": 10000.0, "note": ""})
            self.assertEqual(r["reason"], "ok")
            big = chk(nav=2_000_000.0, avail=1e9)
            self.assertEqual(big["plan"]["note"], "downsized to 0.5% of 24h vol ($40,000)")
            self.assertAlmostEqual(big["plan"]["margin_usd"], 40_000 / 3, places=3)
            # a trial-looking coin is refused with main's exact reason
            self.assertEqual(chk(m=trial_meta(), live=TRIAL_LIVE)["reason"], "tier watch (entry tier only)")
            self.assertEqual(chk(live=dict(LIVE, vol24h=1.9e6))["reason"], "live 24h vol 1900000.0 < $2M")
            self.assertEqual(chk(live=dict(LIVE, spread_bp=10.5))["reason"], "live spread 10.5 bp not < 10")

    def test_spread_fetch_and_exit_unchanged(self):
        with no_trial_env():
            self.assertEqual(U.spread_fetch_min_vol(), U.VOL_TRADEABLE)
            t = {"entry_time": T0.isoformat(), "kind": "Base"}
            self.assertEqual(L.exit_reason(t, None, None, {"vol24h": 9e5, "spread_bp": 5}, T0 + timedelta(hours=1)),
                             "liquidity_exit")

    def test_status_rules_unchanged(self):
        with no_trial_env(), patch("bx_trade.keys_present", return_value=False):
            import sqlite3
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE shadow_trades (bx_symbol, kind, entry_time, entry_px, hard_sl, qty, "
                         "position_id, exit_time, exit_reason, pnl_usd, mode, status)")
            self.assertNotIn("trial", L.status(conn)["rules"])


class TestTrialConfigFallback(unittest.TestCase):
    def test_valid_overrides(self):
        cfg = U.trial_config({"BX_TRIAL_TIER_ENABLED": "true", "BX_TRIAL_MIN_VOL_USD": "500000",
                              "BX_TRIAL_MAX_SPREAD_BP": "30", "BX_TRIAL_SIZE_MULT": "0.25",
                              "BX_TRIAL_MAX_PER_DAY": "2"})
        self.assertEqual((cfg["enabled"], cfg["min_vol_usd"], cfg["max_spread_bp"], cfg["size_mult"],
                          cfg["max_per_day"], cfg["invalid"]), (True, 500_000.0, 30.0, 0.25, 2, []))
        self.assertEqual(U.trial_config({"BX_TRIAL_SIZE_MULT": "0"})["size_mult"], 0.0)
        self.assertEqual(U.trial_config({"BX_TRIAL_SIZE_MULT": "1"})["size_mult"], 1.0)

    def test_invalid_values_fall_back(self):
        bad = {"BX_TRIAL_MIN_VOL_USD": ["abc", "100000", "2000000", "5e6", "nan", "-1"],
               "BX_TRIAL_MAX_SPREAD_BP": ["0", "-5", "31", "nan", "x"],
               "BX_TRIAL_SIZE_MULT": ["1.5", "-0.1", "2", "half", "nan", "inf"],
               "BX_TRIAL_MAX_PER_DAY": ["-1", "1.5", "x", "nan"]}
        for name, values in bad.items():
            for v in values:
                cfg = U.trial_config({"BX_TRIAL_TIER_ENABLED": "1", name: v})
                self.assertEqual({k: cfg[k] for k in U.TRIAL_DEFAULTS},
                                 {**U.TRIAL_DEFAULTS, "enabled": True}, (name, v))
                self.assertEqual(cfg["invalid"], [name], (name, v))

    def test_enable_switch_only_on_truthy(self):
        for v in ("", "0", "no", "off", "2", "enabled"):
            self.assertFalse(U.trial_config({"BX_TRIAL_TIER_ENABLED": v})["enabled"], v)
        for v in ("1", "true", "YES", " on "):
            self.assertTrue(U.trial_config({"BX_TRIAL_TIER_ENABLED": v})["enabled"], v)


class TestTrialEligibility(unittest.TestCase):
    def setUp(self):
        self.cfg = U.trial_config(ON)

    def tier(self, **kw):
        return L.pilot_tier(trial_meta(**kw), self.cfg)

    def test_watch_coin_in_band_is_trial(self):
        self.assertEqual(self.tier(), ("trial", ""))
        self.assertEqual(self.tier(vol24h_usd=3e5), ("trial", ""))
        self.assertEqual(self.tier(vol24h_usd=1.99e6, spread_bp=19.9), ("trial", ""))
        self.assertEqual(L.pilot_tier(meta(), self.cfg), ("tradeable", ""))     # tradeable stays tradeable

    def test_outside_band_or_rules_refused(self):
        self.assertIn("< $0.3M", self.tier(vol24h_usd=2.5e5)[1])
        self.assertIn(">= $2M", self.tier(vol24h_usd=2.5e6)[1])                 # $2M+ wide spread: not trial
        self.assertIn("spread None", self.tier(spread_bp=None)[1])              # spread must be measured
        self.assertIn("not < 20", self.tier(spread_bp=20.0)[1])                 # strictly below
        self.assertEqual(self.tier(liq_tier="exclude")[1], "tier exclude (entry tier only)")
        self.assertEqual(self.tier(ex="HL+BX")[1], "listed on HL (HL path only)")   # BX-only rule unchanged
        self.assertIn("asset class", self.tier(asset_class="stock")[1])
        self.assertIn("no 1H", self.tier(gc_tf="1h")[1])
        self.assertIn("API trading", self.tier(api_supported=False)[1])
        self.assertIn("max leverage", self.tier(max_leverage=2)[1])
        for m in grid():                                                         # only watch rows can change
            got = L.pilot_tier(m, self.cfg)[0]
            if m["liq_tier"] != "watch":
                self.assertEqual(got is not None, main_pilot_eligible(m)[0], m)
            elif got:
                self.assertEqual(got, "trial")
                self.assertTrue(3e5 <= m["vol24h_usd"] < 2e6 and m["spread_bp"] < 20 and m["ex"] == "BX", m)

    def test_custom_band(self):
        cfg = U.trial_config({**ON, "BX_TRIAL_MIN_VOL_USD": "500000", "BX_TRIAL_MAX_SPREAD_BP": "12"})
        self.assertIsNone(L.pilot_tier(trial_meta(vol24h_usd=4e5), cfg)[0])
        self.assertIsNone(L.pilot_tier(trial_meta(spread_bp=12.5), cfg)[0])
        self.assertEqual(L.pilot_tier(trial_meta(spread_bp=11.0), cfg)[0], "trial")

    def test_spread_fetched_down_to_the_trial_floor(self):
        self.assertEqual(U.spread_fetch_min_vol(self.cfg), 300_000.0)
        self.assertEqual(U.spread_fetch_min_vol(U.trial_config({**ON, "BX_TRIAL_MIN_VOL_USD": "450000"})), 450_000.0)


class TestTrialSizing(unittest.TestCase):
    def test_half_size(self):
        with patch.dict(os.environ, ON):
            r = chk(m=trial_meta(), live=TRIAL_LIVE)
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(r["plan"]["margin_usd"], 50.0)                    # 0.5 x 1% of 10k NAV
        self.assertEqual((r["plan"]["liq_tier"], r["plan"]["size_mult"]), ("trial", 0.5))
        self.assertEqual(r["plan"]["note"], "trial tier x0.5")
        self.assertLessEqual(float(r["plan"]["qty"]) * 2.0015, 150.0 + 1e-6)

    def test_never_larger_than_the_tradeable_size(self):
        with patch.dict(os.environ, {**ON, "BX_TRIAL_SIZE_MULT": "1"}):
            self.assertAlmostEqual(chk(m=trial_meta(), live=TRIAL_LIVE)["plan"]["margin_usd"], 100.0)
        with patch.dict(os.environ, {**ON, "BX_TRIAL_SIZE_MULT": "3"}):          # invalid -> 0.5
            self.assertAlmostEqual(chk(m=trial_meta(), live=TRIAL_LIVE)["plan"]["margin_usd"], 50.0)
        with patch.dict(os.environ, ON):                                         # a row asking for more is ignored
            r = chk(c=cand(liq_tier="trial", size_mult=2.0), m=trial_meta(), live=TRIAL_LIVE)
            self.assertAlmostEqual(r["plan"]["margin_usd"], 50.0)
            r = chk(c=cand(liq_tier="trial", size_mult=0.2), m=trial_meta(), live=TRIAL_LIVE)
            self.assertAlmostEqual(r["plan"]["margin_usd"], 20.0)
        with patch.dict(os.environ, {**ON, "BX_TRIAL_SIZE_MULT": "0"}):
            self.assertIn("size_mult is 0", chk(m=trial_meta(), live=TRIAL_LIVE)["reason"])

    def test_volume_cap_on_top_of_the_half_size(self):
        with patch.dict(os.environ, ON):
            r = chk(m=trial_meta(), live=TRIAL_LIVE, nav=1_000_000.0, avail=1e9)  # 0.5 x 1% x 3 = $15k > $3.1k
        self.assertTrue(r["ok"], r)
        self.assertLessEqual(float(r["plan"]["qty"]) * 2.0015, 0.005 * 6.2e5 + 1e-6)
        self.assertEqual(r["plan"]["note"], "trial tier x0.5; downsized to 0.5% of 24h vol ($3,100)")

    def test_caps_still_enforced(self):
        with patch.dict(os.environ, ON):
            m, lv = trial_meta(), TRIAL_LIVE
            self.assertIn("trial-tier BX entry already today (max 1)", chk(m=m, live=lv, trial_today=1)["reason"])
            self.assertIn("max 2", chk(m=m, live=lv, open_live=[{"bx_symbol": "A"}, {"bx_symbol": "B"}])["reason"])
            self.assertIn("already holding", chk(m=m, live=lv, open_live=[{"bx_symbol": "FOOUSDT"}])["reason"])
            self.assertIn("new BX entry already today", chk(m=m, live=lv, today=1)["reason"])
            self.assertIn("1.5%", chk(c=cand(hard_sl=1.98), m=m, live=lv)["reason"])
            self.assertIn("liq", chk(c=cand(hard_sl=1.30), m=m, live=lv)["reason"])
            self.assertIn("available", chk(m=m, live=lv, avail=10.0)["reason"])
            self.assertIn("no ENTRY_DESK approval",
                          L.check_entry(cand(), m, dict(lv), 10_000.0, 1e3, [], 0, None, TIERS)["reason"])
            self.assertIn("live 24h vol", chk(m=m, live=dict(lv, vol24h=2.9e5))["reason"])
            self.assertIn("live spread 20.0 bp not < 20", chk(m=m, live=dict(lv, spread_bp=20.0))["reason"])
            # tradeable rows keep the $2M / 10 bp live re-check and full size
            self.assertIn("< $2M", chk(live=dict(LIVE, vol24h=1.9e6))["reason"])
            self.assertAlmostEqual(chk()["plan"]["margin_usd"], 100.0)
            self.assertNotIn("liq_tier", chk()["plan"])
        with patch.dict(os.environ, {**ON, "BX_TRIAL_MAX_PER_DAY": "0"}):
            self.assertIn("max 0", chk(m=trial_meta(), live=TRIAL_LIVE)["reason"])

    def test_trial_row_refused_once_switched_off(self):
        with patch.dict(os.environ, {"BX_TRIAL_TIER_ENABLED": "0"}):
            r = chk(c=cand(liq_tier="trial", size_mult=0.5), m=trial_meta(), live=TRIAL_LIVE)
        self.assertEqual(r["reason"], "tier watch (entry tier only)")


class TestTrialCandidates(Tmp):
    def write_radar(self, metas):
        rows1 = [{"bx_symbol": m["bx_symbol"], "dual_cross_up": True, "trend": "Green", "close": 2.0, "upper": 1.9,
                  "filter": 1.8, "lower": 1.7} for m in metas]
        rows4 = [{"bx_symbol": m["bx_symbol"], "dual_cross_up": True, "trend": "Green", "close": 2.0, "upper": 1.95,
                  "filter": 1.86, "lower": 1.8} for m in metas]
        (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": metas}))
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": rows1}))
        (self.tmp / "bx_radar_4h.json").write_text(json.dumps({"rows": rows4}))

    def metas(self):
        return [meta("FOOUSDT"), trial_meta("PORTALUSDT"), trial_meta("TAKEUSDT", vol24h_usd=3.6e5, spread_bp=25.0),
                trial_meta("CVXUSDT", vol24h_usd=1.2e5, liq_tier="exclude"), trial_meta("HLUSDT", ex="HL+BX")]

    def test_off_no_trial_rows_or_fields(self):
        self.write_radar(self.metas())
        with no_trial_env():
            doc = L.build_candidates(T0)
        self.assertEqual([c["symbol"] for c in doc["candidates"]], ["FOOUSDT"])
        self.assertNotIn("liq_tier", doc["candidates"][0])
        self.assertNotIn("size_mult", doc["candidates"][0])
        self.assertNotIn("trial", doc)
        self.assertEqual({s["symbol"]: s["reason"] for s in doc["not_eligible"]}["PORTALUSDT"],
                         "tier watch (entry tier only)")

    def test_on_trial_rows_carry_tier_and_size_mult(self):
        self.write_radar(self.metas())
        err = io.StringIO()
        with patch.dict(os.environ, ON), redirect_stderr(err):
            doc = L.build_candidates(T0)
        rows = {c["symbol"]: c for c in doc["candidates"]}
        self.assertEqual(set(rows), {"FOOUSDT", "PORTALUSDT"})
        self.assertEqual((rows["PORTALUSDT"]["liq_tier"], rows["PORTALUSDT"]["size_mult"]), ("trial", 0.5))
        self.assertEqual(rows["PORTALUSDT"]["tier"], "small")                   # market-cap tier untouched
        self.assertTrue(rows["PORTALUSDT"]["size_rule"].startswith("TRIAL: 0.5 x"))
        self.assertEqual((rows["FOOUSDT"]["liq_tier"], rows["FOOUSDT"]["size_mult"]), ("tradeable", 1.0))
        self.assertEqual(doc["trial"]["n"], 1)
        self.assertTrue(doc["trial"]["enabled"])
        skipped = {s["symbol"]: s["reason"] for s in doc["not_eligible"]}
        self.assertEqual(skipped["TAKEUSDT"], "tier watch (entry tier only; trial: spread 25.0 bp not < 20)")
        self.assertNotIn("HLUSDT", skipped)                                      # HL-listed: never a BX signal
        self.assertNotIn("CVXUSDT", skipped)                                     # exclude tier: no signal
        self.assertIn("[BX_TRIAL] candidate PORTALUSDT", err.getvalue())


class TestTrialEntries(Tmp):
    def market(self, s):
        return dict(TRIAL_LIVE) if s != "FOOUSDT" else dict(LIVE)

    def go(self, api, now=T0):
        return L.run_entries(now=now, trade_api=api, egress=dict(ok=True), nav_fn=lambda: 10_000.0,
                             market=self.market, tiers_fn=lambda s: TIERS, conn=self.conn)

    def tcand(self, sym):
        return cand(sym, liq_tier="trial", size_mult=0.5)

    def test_trial_entry_half_size_logged_and_marked(self):
        with patch.dict(os.environ, ON):
            self.seed([self.tcand("PORTALUSDT")], [trial_meta("PORTALUSDT")],
                      [{"symbol": "PORTALUSDT", "decision": "approve"}])
            err = io.StringIO()
            with redirect_stderr(err):
                rep = self.go(FakeAPI())
        self.assertEqual(rep["entered"][0]["status"], "filled", rep)
        self.assertEqual(rep["entered"][0]["liq_tier"], "trial")
        self.assertAlmostEqual(rep["entered"][0]["plan"]["margin_usd"], 55.0)    # 0.5 x 1% x (10k HL + 1k BX)
        t = L.open_live_trades(self.conn)[0]
        self.assertEqual((t["liq_tier"], t["size_pct_nav"]), ("trial", 0.5))
        self.assertIn("trial tier x0.5", t["note"])
        self.assertIn("[BX_TRIAL] ENTRY PORTALUSDT size_mult=0.5", err.getvalue())
        self.assertEqual(L.live_trial_entries_today(self.conn, T0), 1)
        self.assertEqual(L.live_trial_entries_today(self.conn, T0 + timedelta(days=1)), 0)

    def test_tradeable_entry_has_no_trial_mark(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        with no_trial_env():
            self.go(FakeAPI())
        t = L.open_live_trades(self.conn)[0]
        self.assertIsNone(t["liq_tier"])
        self.assertEqual(L.live_trial_entries_today(self.conn, T0), 0)

    def test_one_trial_fill_per_day_even_if_the_global_cap_were_higher(self):
        cands = [self.tcand("PORTALUSDT"), self.tcand("TAKEUSDT"), cand()]
        metas = [trial_meta("PORTALUSDT"), trial_meta("TAKEUSDT"), meta()]
        dec = [{"symbol": s, "decision": "approve"} for s in ("PORTALUSDT", "TAKEUSDT", "FOOUSDT")]
        with patch.dict(os.environ, ON), patch.object(L, "MAX_NEW_PER_DAY", 3), patch.object(L, "MAX_OPEN", 5):
            self.seed(cands, metas, dec)
            api = FakeAPI()
            api.open_long = self._open_long_unique(api)
            rep = self.go(api)
        reasons = {s["symbol"]: s["reason"] for s in rep["skipped"]}
        self.assertEqual([e["symbol"] for e in rep["entered"]], ["PORTALUSDT", "FOOUSDT"], rep)
        self.assertEqual(reasons["TAKEUSDT"], "1 trial-tier BX entry already today (max 1)")

    def test_global_daily_cap_still_one(self):
        with patch.dict(os.environ, ON):
            self.seed([cand(), self.tcand("PORTALUSDT")], [meta(), trial_meta("PORTALUSDT")],
                      [{"symbol": "FOOUSDT", "decision": "approve"}, {"symbol": "PORTALUSDT", "decision": "approve"}])
            rep = self.go(FakeAPI())
        self.assertEqual(len(rep["entered"]), 1)
        self.assertIn("new BX entry already today", rep["skipped"][-1]["reason"])

    @staticmethod
    def _open_long_unique(api):
        n = {"i": 0}

        def open_long(s, qty, px, sl, cid):
            api.calls.append(("open_long", s, qty, px, sl))
            n["i"] += 1
            api.positions.append({"positionId": f"P{n['i']}", "symbol": s, "side": "LONG", "qty": qty,
                                  "avgOpenPrice": "2.0005", "leverage": 3, "marginMode": "ISOLATION",
                                  "liqPrice": "1.35", "unrealizedPNL": "0", "fee": "0", "funding": "0"})
            return {"orderId": f"O{n['i']}"}
        return open_long


class TestTrialExits(unittest.TestCase):
    def test_liquidity_exit_at_the_trial_floor_for_trial_trades_only(self):
        now = T0 + timedelta(hours=1)
        trial_t = {"entry_time": T0.isoformat(), "kind": "Base", "liq_tier": "trial"}
        plain = {"entry_time": T0.isoformat(), "kind": "Base"}
        with no_trial_env():
            self.assertIsNone(L.exit_reason(trial_t, None, None, {"vol24h": 6.2e5, "spread_bp": 15}, now))
            self.assertEqual(L.exit_reason(trial_t, None, None, {"vol24h": 2.9e5, "spread_bp": 15}, now),
                             "liquidity_exit")
            self.assertEqual(L.exit_reason(trial_t, None, None, {"vol24h": 6.2e5, "spread_bp": 31}, now),
                             "liquidity_exit")
            self.assertEqual(L.exit_reason(plain, None, None, {"vol24h": 6.2e5, "spread_bp": 15}, now),
                             "liquidity_exit")
            self.assertEqual(L.exit_reason(trial_t, None, None, {"vol24h": 6.2e5}, T0 + timedelta(days=7, minutes=1)),
                             "time_cap_7d")


class TestRadarMeasuresTrialSpreads(TmpOut):
    def run_daily(self, env):
        pairs = [pair("FOOUSDT", "FOO"), pair("BIGUSDT", "BIG"), pair("TINYUSDT", "TINY")]
        ticks = [tick("FOOUSDT", 2.0, 6e5), tick("BIGUSDT", 2.0, 3e6), tick("TINYUSDT", 2.0, 2e5)]
        bars = {(s, tf): daily_bars(200, tf_ms=(H4 if tf == "4h" else 86_400_000))
                for s in ("FOOUSDT", "BIGUSDT", "TINYUSDT") for tf in ("1d", "4h")}
        depth_calls = []

        class Client(FakeClient):
            def depth(self, sym, limit=5):
                depth_calls.append(sym)
                return super().depth(sym, limit)

        class CG:
            @staticmethod
            def markets():
                return {"coins": [{"id": x.lower(), "symbol": x.lower(), "current_price": 2.0, "market_cap": 3e8,
                                   "ath": 2.5} for x in ("FOO", "BIG", "TINY")]}

            @staticmethod
            def first_seen_ms(cid):
                return None
        orig = R._data_file
        R._data_file = lambda name: {}
        try:
            with patch.dict(os.environ, env):
                R.run_daily(now_ms=NOW, client=Client(pairs, ticks, bars), hl_mids_fn=lambda: {}, cg=CG)
        finally:
            R._data_file = orig
        scanned = {m["bx_symbol"]: m for m in json.loads((self.tmp / "bx_meta.json").read_text())["scanned"]}
        return depth_calls, scanned

    def test_off_spreads_only_from_2m(self):
        calls, scanned = self.run_daily({k: "" for k in TRIAL_VARS})
        self.assertEqual(calls, ["BIGUSDT"])
        self.assertIsNone(scanned["FOOUSDT"]["spread_bp"])

    def test_on_spreads_down_to_the_trial_floor(self):
        calls, scanned = self.run_daily(ON)
        self.assertEqual(sorted(calls), ["BIGUSDT", "FOOUSDT"])                 # $0.2M still not measured
        self.assertEqual(scanned["FOOUSDT"]["spread_bp"], 4.0)
        self.assertEqual(scanned["FOOUSDT"]["liq_tier"], "watch")                # universe tiers unchanged


if __name__ == "__main__":
    unittest.main()
