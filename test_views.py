"""Unit tests for the cockpit / market view builders (pure functions, no network)."""
import os
import unittest

import account_view as AV
import market_view as MV

H = 3_600_000


def fake_gc(h, lo, c, period):
    """Simple stand-in for compute_gc: filter = SMA(period), upper = filter * 1.02."""
    out = []
    for i in range(len(c)):
        w = c[max(0, i - period + 1): i + 1]
        f = sum(w) / len(w)
        out.append({"filter": f, "upper": f * 1.02, "lower": f * 0.98})
    return out


def bars(prices, start=0, step=H):
    return [[start + i * step, p, p * 1.01, p * 0.99, p, 100.0] for i, p in enumerate(prices)]


class TestAccountView(unittest.TestCase):
    def test_portfolio_pnl_and_curve(self):
        raw = [["day", {"accountValueHistory": [[1, "100"], [2, "103"]], "pnlHistory": [[1, "0"], [2, "3"]], "vlm": "10"}],
               ["week", {"accountValueHistory": [[1, "90"], [2, "103"]], "pnlHistory": [[1, "5"], [2, "18"]], "vlm": "50"}]]
        pf = AV.parse_portfolio(raw)
        w = AV.pnl_windows(pf)
        self.assertEqual(w["day"], 3.0)
        self.assertEqual(w["week"], 13.0)
        self.assertIsNone(w["month"])
        self.assertEqual(pf["week"]["av"][-1], [2, 103.0])

    def test_portfolio_bad_input(self):
        self.assertEqual(AV.parse_portfolio(None), {})
        self.assertEqual(AV.parse_portfolio({"x": 1}), {})

    def test_fills_summary(self):
        now = 10 * 86_400_000
        fills = [{"time": now - 1000, "coin": "MON", "dir": "Open Long", "px": "0.03", "sz": "1000", "closedPnl": "0", "fee": "0.01"},
                 {"time": now - 2 * 86_400_000, "coin": "ETH", "dir": "Close Long", "px": "2000", "sz": "0.1", "closedPnl": "-5", "fee": "0.1"},
                 {"time": now - 9 * 86_400_000, "coin": "BTC", "side": "A", "px": "1", "sz": "1", "closedPnl": "50", "fee": "0"}]
        s = AV.summarize_fills(fills, now)
        self.assertEqual(s["recent"][0]["coin"], "MON")
        self.assertEqual(s["recent"][-1]["dir"], "Sell")
        self.assertEqual(s["d7"]["n"], 2)
        self.assertEqual(s["d7"]["closed_pnl"], -5.0)
        self.assertEqual(s["d7"]["volume"], 230.0)

    def test_funding(self):
        rows = [{"delta": {"type": "funding", "coin": "MON", "usdc": "-0.5"}},
                {"delta": {"type": "funding", "coin": "MON", "usdc": "-0.25"}},
                {"delta": {"type": "deposit", "usdc": "100"}}]
        f = AV.summarize_funding(rows)
        self.assertEqual(f["total"], -0.75)
        self.assertEqual(f["by_coin"], {"MON": -0.75})

    def _perp(self):
        return {"assetPositions": [{"position": {"coin": "MON", "szi": "7232", "entryPx": "0.028193",
                                                 "positionValue": "210.0", "unrealizedPnl": "6.1",
                                                 "liquidationPx": "0.02", "marginUsed": "52",
                                                 "leverage": {"type": "isolated", "value": 4}}},
                                   {"position": {"coin": "ETH", "szi": "0.1", "entryPx": "2000", "positionValue": "210",
                                                 "leverage": {"type": "cross", "value": 3}}}]}

    def test_orders_classification_and_risk(self):
        pos = AV.positions(self._perp())
        self.assertEqual(len(pos), 2)
        mon = pos[0]
        self.assertAlmostEqual(mon["mark"], 210 / 7232)
        orders = [{"coin": "MON", "side": "A", "isTrigger": True, "reduceOnly": True, "sz": "7232",
                   "triggerPx": "0.025807", "orderType": "Stop Market"},
                  {"coin": "SOL", "side": "A", "isTrigger": True, "reduceOnly": True, "sz": "1", "triggerPx": "100"},
                  {"coin": "HYPE", "side": "B", "isTrigger": False, "sz": "2", "limitPx": "30", "orderType": "Limit"}]
        o = AV.classify_orders(orders, pos)
        self.assertEqual(len(o["sl"]), 2)
        self.assertEqual(len(o["other"]), 1)
        self.assertEqual(o["uncovered"], ["ETH"])
        self.assertTrue(o["sl"][1]["orphan"])
        self.assertEqual(o["sl"][0]["covers_pct"], 100.0)
        self.assertLess(o["sl"][0]["dist_pct"], 0)
        r = AV.risk_to_sl(pos, o["sl"])
        self.assertEqual(len(r["per_coin"]), 1)
        self.assertAlmostEqual(r["vs_entry"], round((0.025807 - 0.028193) * 7232, 2), places=2)
        self.assertLess(r["from_mark"], 0)


class TestMarketView(unittest.TestCase):
    def test_series_closed_only_and_breadth(self):
        now = 200 * H
        up = bars([100 + i for i in range(201)])          # last bar starts at `now` → still forming
        s = MV.series(up, 20, now, H, fake_gc)
        self.assertEqual(s["t"][-1], 199 * H)
        self.assertTrue(s["green"][-1])
        sers = {f"C{i}": s for i in range(12)}
        h = MV.breadth_history(sers, 50)
        self.assertEqual(len(h), 50)
        self.assertEqual(h[-1]["green_pct"], 100.0)
        # fewer than 10 coins → no breadth point
        self.assertEqual(MV.breadth_history({"A": s}, 5), [])

    def test_fresh_events(self):
        now = 60 * H
        p = [100.0] * 55 + [110.0] * 5                   # jump through Upper on bar 55
        s = MV.series(bars(p), 20, now, H, fake_gc)
        ev = MV.fresh_events({"X": s}, "1h", 6)
        self.assertEqual([e["kind"] for e in ev], ["cross_up"])
        self.assertEqual(ev[0]["t"], 55 * H)

    def test_sentiment_labels(self):
        mk = lambda g: {"green_pct": g}
        hist = [{"green_pct": 70}] * 30
        self.assertEqual(MV.sentiment({"1h": mk(60), "4h": mk(70), "1d": mk(70)}, hist)["label"], "Risk-on")
        self.assertEqual(MV.sentiment({"1h": mk(20), "4h": mk(40), "1d": mk(70)}, hist)["label"], "Pullback in an uptrend")
        self.assertEqual(MV.sentiment({"1h": mk(20), "4h": mk(30), "1d": mk(30)}, hist)["label"], "Risk-off")
        self.assertEqual(MV.sentiment({"1h": mk(70), "4h": mk(45), "1d": mk(40)}, hist)["label"], "Bounce in a downtrend")
        self.assertEqual(MV.sentiment({"1h": None, "4h": mk(45), "1d": mk(40)}, hist)["label"], "No data")
        drop = [{"green_pct": 70}] * 25 + [{"green_pct": 50}] * 5 + [{"green_pct": 30}]
        s = MV.sentiment({"1h": mk(30), "4h": mk(50), "1d": mk(50)}, drop)
        self.assertEqual(s["d6h"], -40.0)
        self.assertIn("-40 pts", s["why"])

    def test_change_24h(self):
        b = bars([100.0] * 10 + [110.0] * 25)
        self.assertEqual(MV.change_24h(b, None), 0.0)
        b2 = bars([100.0] * 11 + [110.0] * 24)
        self.assertEqual(MV.change_24h(b2, None), 10.0)
        self.assertEqual(MV.change_24h(None, {"close": 100, "live": {"close": 95}}), -5.0)

    def test_build_end_to_end(self):
        now = 300 * H
        syms = [f"C{i}" for i in range(12)] + ["BTC"]
        bt = {tf: {s: bars([100 + (j if k % 2 else -j) * 0.1 for j in range(300)], step=MV.TF_BAR_MS[tf] if tf == "1h" else H)
                   for k, s in enumerate(syms)} for tf in ("1h", "4h", "1d")}
        radars = {tf: {"ts": "x", "rows": [{"symbol": s, "trend": "Green" if k % 2 else "Red", "close": 100, "filter": 99,
                                            "tier": "mega" if s == "BTC" else "small", "above_upper": k % 3 == 0}
                                           for k, s in enumerate(syms)]} for tf in ("1h", "4h", "1d")}
        out = MV.build(radars, bt, {"C1": "AI", "C2": "AI"}, {"1h": 20, "4h": 20, "1d": 20}, now, fake_gc)
        self.assertEqual(len(out["tiles"]), 13)
        self.assertEqual(out["regime"][0]["symbol"], "BTC")
        self.assertEqual(out["sectors"][0]["sector"], "AI")
        self.assertEqual(out["sectors"][0]["n"], 2)
        self.assertIn(out["sentiment"]["label"], ("Mixed", "Risk-off", "Risk-on", "Pullback in an uptrend", "Bounce in a downtrend"))
        self.assertTrue(out["breadth"]["history"]["1h"])


class TestViewEndpoints(unittest.TestCase):
    """The two cockpit-only endpoints must sit behind the cookie login."""

    def test_routes_are_auth_gated(self):
        with open(os.path.join(os.path.dirname(__file__), "serve.py"), encoding="utf-8") as fh:
            src = fh.read()
        i_route = src.index('if path in ("/api/account-ui", "/api/market-ui")')
        i_auth = src.rfind("_need_auth", 0, i_route)
        self.assertGreater(i_auth, 0)
        self.assertLess(i_route - i_auth, 4000)


if __name__ == "__main__":
    unittest.main()
