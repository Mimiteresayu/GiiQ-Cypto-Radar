"""Bitunix universe rules (pure) — symbol match, class, age, tiers, ignition, slippage, GC TF."""
import unittest
from datetime import datetime, timezone

import bx_client
import bx_universe as U

DAY = U.DAY_MS
NOW = int(datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


class TestSymbolMatch(unittest.TestCase):
    MIDS = {"kPEPE": 0.0120, "kBONK": 0.0250, "SPX": 1.10, "KAITO": 1.50, "BTC": 83000.0, "KAS": 0.09}

    def test_1000_prefix_maps_to_k(self):
        m = U.match_hl("1000PEPE", 0.01205, self.MIDS)
        self.assertEqual((m["hl_name"], m["match_rule"], m["px_scale"]), ("kPEPE", "1000k", 1.0))
        self.assertEqual(U.match_hl("1000BONK", 0.0249, self.MIDS)["hl_name"], "kBONK")

    def test_exact_and_k_names_not_stripped(self):
        self.assertEqual(U.match_hl("KAITO", 1.49, self.MIDS)["hl_name"], "KAITO")
        self.assertEqual(U.match_hl("KAS", 0.0901, self.MIDS)["hl_name"], "KAS")
        self.assertIsNone(U.match_hl("AITO", 1.49, self.MIDS)["hl_name"])   # the old K-strip bug
        self.assertEqual(U.hl_multiplier("KAITO"), ("KAITO", 1))
        self.assertEqual(U.hl_multiplier("kPEPE"), ("PEPE", 1000))

    def test_override_and_alias(self):
        self.assertEqual(U.match_hl("SPX6900", 1.1, self.MIDS)["hl_name"], "SPX")          # alias
        m = U.match_hl("FOO", 5.0, self.MIDS, overrides={"FOO": "BTC"})
        self.assertEqual((m["hl_name"], m["match_rule"]), ("BTC", "override"))

    def test_same_ticker_different_asset_rejected(self):
        m = U.match_hl("BTC", 12.0, self.MIDS)      # a different "BTC" priced at 12
        self.assertIsNone(m["hl_name"])
        self.assertEqual(m["match_rule"], "price_mismatch")

    def test_multiplier_parse(self):
        self.assertEqual(U.split_multiplier("1000PEPE"), ("PEPE", 1000))
        self.assertEqual(U.split_multiplier("1MBABYDOGE"), ("BABYDOGE", 1_000_000))
        self.assertEqual(U.split_multiplier("1INCH"), ("1INCH", 1))
        self.assertEqual(U.ex_label(True, True), "HL+BX")
        self.assertEqual(U.ex_label(False, True), "BX")


class TestAssetClass(unittest.TestCase):
    SEED = {"XAU": "commodity", "CL": "commodity", "MSTR": "stock"}

    def test_seed_wins_then_crypto_then_unknown(self):
        self.assertEqual(U.asset_class("XAU", self.SEED, False, False), "commodity")
        self.assertEqual(U.asset_class("MSTR", self.SEED, False, True), "stock")
        self.assertEqual(U.asset_class("PEPE", self.SEED, True, False), "crypto")
        self.assertEqual(U.asset_class("NEWCOIN", self.SEED, False, True), "crypto")
        self.assertEqual(U.asset_class("005930", self.SEED, False, False), "unknown")

    def test_session_gap(self):
        sat = int(datetime(2026, 9, 26, tzinfo=timezone.utc).timestamp() * 1000)
        mon = int(datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp() * 1000)
        sun = int(datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertTrue(U.session_gap("stock", sat, "1d"))
        self.assertTrue(U.session_gap("stock", sun, "1d"))
        self.assertFalse(U.session_gap("stock", mon, "1d"))
        self.assertTrue(U.session_gap("commodity", sat, "1d"))
        self.assertFalse(U.session_gap("commodity", sun, "1d"))     # Globex reopens Sunday evening
        self.assertFalse(U.session_gap("crypto", sat, "1d"))
        mon_14 = mon + 14 * 3_600_000
        mon_02 = mon + 2 * 3_600_000
        self.assertFalse(U.session_gap("stock", mon_14, "1h"))
        self.assertTrue(U.session_gap("stock", mon_02, "1h"))


class TestListingAge(unittest.TestCase):
    def test_old_contract(self):
        a = U.listing_age(NOW - 400 * DAY, None, NOW, "crypto")
        self.assertEqual((a["contract_age_days"], a["new_contract"], a["asset_age"]), (400, False, "old"))

    def test_new_contract_old_asset(self):
        a = U.listing_age(NOW - 5 * DAY, None, NOW, "crypto", [NOW - 300 * DAY])
        self.assertEqual((a["new_contract"], a["asset_age"]), (True, "old_asset_new_contract"))

    def test_new_token_and_unknown(self):
        self.assertEqual(U.listing_age(NOW - 5 * DAY, None, NOW, "crypto", [NOW - 8 * DAY])["asset_age"], "new_token")
        self.assertEqual(U.listing_age(NOW - 5 * DAY, None, NOW, "crypto", [None])["asset_age"], "unknown")
        self.assertTrue(U.is_new_token("unknown"))

    def test_tradfi_always_old_asset(self):
        a = U.listing_age(NOW - 3 * DAY, None, NOW, "stock")
        self.assertEqual(a["asset_age"], "old_asset_new_contract")


class TestLiquidityAndSignals(unittest.TestCase):
    def test_tiers(self):
        t = lambda **k: U.liq_tier(k.get("vol"), k.get("st", "OPEN"), k.get("dl"), k.get("cls", "crypto"),
                                   k.get("spr"), k.get("ign"), k.get("cem", False), k.get("new", False))
        self.assertEqual(t(vol=5e6, spr=4), "tradeable")
        self.assertEqual(t(vol=5e6, spr=12), "watch")
        self.assertEqual(t(vol=5e6, spr=None), "watch")                 # unknown spread never tradeable
        self.assertEqual(t(vol=1e6), "watch")
        self.assertEqual(t(vol=1e5), "exclude")
        self.assertEqual(t(vol=1e5, ign=4.0, cem=True), "watch")        # ignition-flagged cemetery
        self.assertEqual(t(vol=1e5, ign=4.0, new=True), "watch")
        self.assertEqual(t(vol=1e5, ign=4.0), "exclude")                # plain coin: ignition alone is not enough
        self.assertEqual(t(vol=5e6, spr=4, st="CANCEL_ONLY"), "exclude")
        self.assertEqual(t(vol=5e6, spr=4, dl=NOW + DAY), "exclude")
        self.assertEqual(t(vol=5e6, spr=4, cls="unknown"), "exclude")

    def test_ignition(self):
        self.assertEqual(U.ignition(3e6, [1e6] * 7), 3.0)
        self.assertIsNone(U.ignition(3e6, [1e6, 1e6]))
        self.assertEqual(U.ignition(1e6, [1e6] * 20), 1.0)               # only the last 7 bars count

    def test_slippage_max_of_5bp_and_half_spread(self):
        self.assertEqual(U.shadow_slippage_bp(4), 5.0)
        self.assertEqual(U.shadow_slippage_bp(24), 12.0)
        self.assertEqual(U.shadow_slippage_bp(None), 5.0)

    def test_gc_tf_and_counting(self):
        self.assertEqual(U.gc_tf_for({"1d": 170, "4h": 200, "1h": 200}), "1d")
        self.assertEqual(U.gc_tf_for({"1d": 40, "4h": 95, "1h": 200}), "4h")
        self.assertEqual(U.gc_tf_for({"1d": 3, "4h": 20, "1h": 70}), "1h")
        self.assertIsNone(U.gc_tf_for({"1d": 1, "4h": 5, "1h": 30}))
        self.assertTrue(U.counts_in_test("1d", "tradeable"))
        self.assertTrue(U.counts_in_test("4h", "tradeable"))
        self.assertFalse(U.counts_in_test("1h", "tradeable"))           # Harbor: 1H is watch-only
        self.assertFalse(U.counts_in_test("1d", "watch"))

    def test_gc_periods_match_hl_scan(self):
        import scan_gc_radar as sgr
        for tf in ("1d", "4h", "1h"):
            self.assertEqual(U.GC_PERIOD[tf], sgr.gc_period_for_tf(tf))
        self.assertEqual(U.min_bars("1d"), sgr.gc_period_for_tf("1d") + 20)


class TestCoinGeckoMatch(unittest.TestCase):
    def test_price_and_multiplier(self):
        idx = {"pepe": [{"id": "pepe", "current_price": 0.000012, "market_cap": 5e9},
                        {"id": "fake-pepe", "current_price": 0.5, "market_cap": 1e6}]}
        self.assertEqual(U.cg_match("1000PEPE", 0.01201, idx)["id"], "pepe")
        self.assertIsNone(U.cg_match("1000PEPE", 0.05, idx))


class TestClientHelpers(unittest.TestCase):
    def test_usd_volume_picks_consistent_field(self):
        self.assertEqual(bx_client.usd_volume("2309261747.2", "27728.38", "83038"), 2309261747.2)  # live shape
        self.assertEqual(bx_client.usd_volume("1", "60000", "60000"), 60000.0)                   # docs typo shape

    def test_bar_parse_string_time_and_order(self):
        raw = [{"open": "2", "high": "3", "low": "1", "close": "2.5", "quoteVol": "250", "baseVol": "100",
                "time": "1790553600000"},
               {"open": "1", "high": "2", "low": "1", "close": "2", "quoteVol": "200", "baseVol": "100",
                "time": "1790467200000"}]
        bars = sorted(filter(None, (bx_client._bar(k) for k in raw)), key=lambda b: b[0])
        self.assertEqual(bars[0][0], 1790467200000)
        self.assertEqual(bars[1][5], 250.0)

    def test_spread(self):
        self.assertEqual(bx_client.spread_bp({"asks": [["100.1", "1"]], "bids": [["99.9", "1"]]}), 20.0)
        self.assertIsNone(bx_client.spread_bp({"asks": [], "bids": [["1", "1"]]}))


if __name__ == "__main__":
    unittest.main()
