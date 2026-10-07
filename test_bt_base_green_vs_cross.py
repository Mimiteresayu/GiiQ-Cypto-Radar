"""The A/B backtest helpers run on synthetic bars (no network) and A + R == B."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import bt_base_green_vs_cross as BT  # noqa: E402


def synth(seed, n=900):
    r, px, bars = random.Random(seed), 100.0, []
    for i in range(n):
        o = px
        px *= 1 + r.gauss(0.0006, 0.03)
        hi, lo = max(o, px) * 1.01, min(o, px) * 0.99
        bars.append({"t": i * 86_400_000, "open": o, "high": hi, "low": lo, "close": px})
    return bars


class TestBt(unittest.TestCase):
    def test_partition_and_report(self):
        trades = []
        for s in range(6):
            trades += BT.trades_for(synth(s))
        self.assertGreater(len(trades), 5)
        rep = BT.report(trades)
        for m in ("filter", "lower"):
            n = rep[m]
            self.assertEqual(n["A_cross_green"]["n"] + n["R_cross_red"]["n"], n["B_cross_only"]["n"])
        self.assertEqual(BT.trades_for(synth(1)[:50]), [])          # too short -> no signals

    def test_stats(self):
        s = BT.stats([0.1, -0.05, 0.2])
        self.assertAlmostEqual(s["win_rate"], 2 / 3)
        self.assertIsNone(BT.stats([0.1, 0.1])["sharpe"])           # zero variance -> undefined


if __name__ == "__main__":
    unittest.main()
