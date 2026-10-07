"""MMT 2026-10-07: flat Base size by tier (tiny 2% / small 3% / large 4% / mega 5% of NAV), desk size ignored."""
import unittest

import exec_common as ec


def size(tier, ai=None, lev=None, **kw):
    # entry 1.0, Hard SL 0.9: with maxLeverage 10 the 5x liq (~0.842) is still below it
    return ec.size_by_margin(1000.0, 1.0, 0.9, 10, ai, lev, tier=tier, flat_tier_size=True, **kw)


class TestFlatTierSize(unittest.TestCase):
    def test_tier_sizes(self):
        for tier, pct in (("tiny", 2.0), ("small", 3.0), ("large", 4.0), ("mega", 5.0), ("", 2.0), ("weird", 2.0)):
            r = size(tier)
            self.assertTrue(r["ok"], (tier, r))
            self.assertEqual(r["margin_pct"], pct, tier)

    def test_desk_size_is_ignored(self):
        self.assertEqual(size("large", ai=2)["margin_pct"], 4.0)       # a low desk size does not shrink it
        self.assertEqual(size("small", ai=4)["margin_pct"], 3.0)       # nor does a high one raise it

    def test_mega_leverage_keeps_coin_notional_within_20pct(self):
        r = size("mega", lev=5)
        self.assertEqual(r["margin_pct"], 5.0)
        self.assertLessEqual(r["leverage"], 4)                         # 5% x 5x = 25% NAV would break the 20% cap
        self.assertLessEqual(r["margin_pct"] * r["leverage"], ec.MAX_COIN_NOTIONAL_NAV_PCT + 1e-9)

    def test_large_can_still_use_5x(self):
        r = size("large", lev=5)
        self.assertEqual((r["margin_pct"], r["leverage"]), (4.0, 5))   # 4% x 5x = 20% exactly

    def test_tiny_leverage_cap_unchanged(self):
        self.assertLessEqual(size("tiny", lev=5)["leverage"], ec.TINY_MAX_LEV)

    def test_off_by_default(self):                                     # other callers keep the desk-size behaviour
        r = ec.size_by_margin(1000.0, 1.0, 0.8, 10, 3, None, tier="mega")
        self.assertLessEqual(r["margin_pct"], 4.0)


if __name__ == "__main__":
    unittest.main()
