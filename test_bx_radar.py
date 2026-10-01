"""Bitunix shadow radar: catalog, enrichment, GC rows, daily run with a fake Bitunix client."""
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import bx_radar as R
import bx_universe as U
import cg_client
import scan_gc_radar as sgr

DAY = 86_400_000
H4 = 14_400_000
NOW = int(datetime(2026, 9, 29, 0, 20, tzinfo=timezone.utc).timestamp() * 1000)
TODAY0 = NOW - NOW % DAY


def daily_bars(n, start_px=1.0, drift=0.002, end_jump=None, now=NOW, tf_ms=DAY, vol=1e6, vol_last=None):
    """n closed bars ending at the last closed boundary + one forming bar (must be ignored)."""
    last_open = now - now % tf_ms - tf_ms
    bars, px = [], start_px
    for i in range(n):
        t = last_open - (n - 1 - i) * tf_ms
        wobble = 1 + 0.01 * math.sin(i / 3.0)
        o = px
        c = px * (1 + drift) * wobble / (1 + 0.01 * math.sin((i - 1) / 3.0))
        if end_jump and i == n - 1:
            c = o * end_jump
        h, l = max(o, c) * 1.005, min(o, c) * 0.995
        v = vol_last if (vol_last and i == n - 1) else vol
        bars.append([t, o, h, l, c, v])
        px = c
    bars.append([last_open + tf_ms, px, px * 1.5, px * 0.5, px * 1.4, vol])   # forming: ignored
    return bars


class FakeClient:
    KLINE_MAX = 200
    STATS = {"calls": 0, "errors": 0, "n429": 0, "ms_total": 0.0}

    def __init__(self, pairs, tickers, bars, depth_bp=4.0):
        self.pairs, self.ticks, self.bars, self.depth_bp = pairs, tickers, bars, depth_bp
        self.calls = []

    def trading_pairs(self):
        return self.pairs

    def tickers(self):
        return self.ticks

    def klines_history(self, sym, tf, n, now_ms=None):
        self.calls.append(("history", sym, tf))
        return [b for b in self.bars.get((sym, tf), [])][-n:]

    def klines(self, sym, tf, start_ms=None, end_ms=None, limit=200):
        self.calls.append(("page", sym, tf))
        return [b for b in self.bars.get((sym, tf), []) if start_ms is None or b[0] >= start_ms][:limit]

    def depth(self, sym, limit=5):
        mid = 100.0
        half = mid * self.depth_bp / 20_000
        return {"asks": [[str(mid + half), "1"]], "bids": [[str(mid - half), "1"]]}

    @staticmethod
    def spread_bp(book):
        import bx_client
        return bx_client.spread_bp(book)


def pair(sym, base, quote="USDT", status="OPEN", launch=None, delist=None):
    p = {"symbol": sym, "base": base, "quote": quote, "symbolStatus": status,
         "launchTime": str(launch or (NOW - 500 * DAY)), "maxLeverage": 50}
    if delist:
        p["delistTime"] = str(delist)
    return p


def tick(sym, px, vol):
    return {"symbol": sym, "lastPrice": str(px), "markPrice": str(px), "quoteVol": str(vol),
            "baseVol": str(vol / px)}


class TmpOut(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (R.OUT_DIR, cg_client.OUT_DIR)
        R.OUT_DIR = cg_client.OUT_DIR = self.tmp

    def tearDown(self):
        R.OUT_DIR, cg_client.OUT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCatalog(TmpOut):
    def setUp(self):
        super().setUp()
        self.pairs = [pair("BTCUSDT", "BTC"), pair("BTCUSDC", "BTC", "USDC"), pair("BTCUSD", "BTC", "USD"),
                      pair("1000PEPEUSDT", "PEPE"), pair("FOOUSDT", "FOO"), pair("XAUUSDT", "XAU"),
                      pair("ZZZUSDT", "ZZZ"), pair("BARUSDT", "BAR", delist=NOW + 3 * DAY),
                      pair("NEWUSDT", "NEW", launch=NOW - 4 * DAY)]
        self.ticks = [tick("BTCUSDT", 83000, 2e9), tick("1000PEPEUSDT", 0.012, 5e7), tick("FOOUSDT", 2.0, 3e6),
                      tick("XAUUSDT", 3300, 8e6), tick("ZZZUSDT", 1.0, 9e6), tick("BARUSDT", 1.0, 5e6),
                      tick("NEWUSDT", 0.5, 4e6)]
        self.mids = {"BTC": 83010.0, "kPEPE": 0.01201, "ETH": 2660.0}
        self.cg = [{"id": "foo-token", "symbol": "foo", "current_price": 2.01, "market_cap": 4e8, "ath": 10.0},
                   {"id": "new-token", "symbol": "new", "current_price": 0.5, "market_cap": 1e7, "ath": 0.6}]

    def build(self, first_seen=None):
        return R.build_catalog(self.pairs, self.ticks, self.mids, self.cg, NOW, overrides={},
                               seed_class={"XAU": "commodity"}, narrative={"FOO"},
                               first_seen_fn=first_seen or (lambda cid: NOW - 6 * DAY))

    def test_rows_and_classes(self):
        cat = self.build()
        rows = {r["symbol"]: r for r in cat["rows"]}
        self.assertEqual(rows["BTC"]["ex"], "HL+BX")
        self.assertEqual(rows["BTC"]["bx_symbol"], "BTCUSDT")               # USDT preferred over USDC, USD skipped
        self.assertEqual(sum(1 for r in cat["rows"] if r["symbol"] == "BTC"), 1)
        self.assertEqual((rows["1000PEPE"]["hl_name"], rows["1000PEPE"]["ex"]), ("kPEPE", "HL+BX"))
        self.assertEqual((rows["FOO"]["ex"], rows["FOO"]["asset_class"], rows["FOO"]["cg_id"]), ("BX", "crypto", "foo-token"))
        self.assertTrue(rows["FOO"]["narrative"])
        self.assertEqual(rows["XAU"]["asset_class"], "commodity")
        self.assertEqual(rows["ZZZ"]["asset_class"], "unknown")
        self.assertIn("ZZZ", {x["symbol"] for x in cat["review"]})
        self.assertEqual((rows["NEW"]["new_contract"], rows["NEW"]["asset_age"]), (True, "new_token"))

    def test_scan_set(self):
        cat = self.build()
        got = {r["symbol"] for r in R.scan_set(cat["rows"])}
        self.assertEqual(got, {"FOO", "XAU", "NEW"})   # no HL overlap, no unknown, no delisting

    def test_enrich_cemetery_and_ignition(self):
        cat = self.build()
        foo = next(r for r in cat["rows"] if r["symbol"] == "FOO")
        bars = daily_bars(30, vol=1e6)
        e = R.enrich_row(foo, bars, 4.0, NOW)
        self.assertEqual(e["ign_x"], 3.0)
        self.assertTrue(e["ign"])
        self.assertTrue(e["cemetery"])                   # CoinGecko ATH 10 vs price 2 -> 80% down
        self.assertEqual(e["ath_src"], "cg")
        self.assertEqual(e["cat_tags"], "N C V")
        self.assertEqual(e["liq_tier"], "tradeable")
        self.assertEqual(e["tier"], "small")             # $400M mcap
        self.assertEqual(e["max_notional_usd"], 15000.0)

    def test_partial_ath_needs_280_bars(self):
        meta = {"symbol": "X", "price": 1.0, "vol24h_usd": 5e5, "symbol_status": "OPEN", "asset_class": "crypto",
                "asset_age": "old", "ath_usd": None, "ath_src": None}
        short = daily_bars(50, start_px=10, drift=-0.03)
        self.assertFalse(R.enrich_row(meta, short, None, NOW)["cemetery"])
        long_ = daily_bars(290, start_px=10, drift=-0.01)
        e = R.enrich_row(dict(meta, price=long_[-2][4]), long_, None, NOW)
        self.assertEqual(e["ath_src"], "partial")
        self.assertTrue(e["cemetery"])


class TestGcRow(unittest.TestCase):
    def test_matches_locked_math_on_closed_bars(self):
        bars = daily_bars(200, drift=0.0, end_jump=1.3)
        r = R.gc_row("FOO", bars, "1d", NOW, sgr.compute_gc)
        closed = bars[:-1]
        gc = sgr.compute_gc([b[2] for b in closed], [b[3] for b in closed], [b[4] for b in closed], period=144)
        c, pc = closed[-1][4], closed[-2][4]
        self.assertAlmostEqual(r["upper"], gc[-1]["upper"], places=8)
        self.assertEqual(r["dual_cross_up"], c > gc[-1]["upper"] and pc <= gc[-2]["upper"])
        self.assertTrue(r["dual_cross_up"])
        self.assertEqual(r["bar_time"], closed[-1][0])          # forming bar ignored
        self.assertEqual(r["bars"], 200)

    def test_too_short_history(self):
        self.assertIsNone(R.gc_row("NEW", daily_bars(100), "1d", NOW, sgr.compute_gc))
        self.assertIsNotNone(R.gc_row("NEW", daily_bars(100, tf_ms=H4), "4h", NOW, sgr.compute_gc))


class TestRunDaily(TmpOut):
    def test_end_to_end_with_fake_client(self):
        pairs = [pair("FOOUSDT", "FOO"), pair("XAUUSDT", "XAU"), pair("ZZZUSDT", "ZZZ"), pair("BTCUSDT", "BTC")]
        ticks = [tick("FOOUSDT", 2.0, 3e6), tick("XAUUSDT", 3300, 8e6), tick("ZZZUSDT", 1.0, 9e6),
                 tick("BTCUSDT", 83000, 2e9)]
        bars = {("FOOUSDT", "1d"): daily_bars(200, drift=0.0, end_jump=1.3), ("FOOUSDT", "4h"): daily_bars(200, tf_ms=H4),
                ("XAUUSDT", "1d"): daily_bars(200, start_px=3000), ("XAUUSDT", "4h"): daily_bars(200, 3000, tf_ms=H4)}
        client = FakeClient(pairs, ticks, bars)

        class CG:
            @staticmethod
            def markets():
                return {"coins": [{"id": "foo", "symbol": "foo", "current_price": 2.0, "market_cap": 3e9, "ath": 2.5}]}

            @staticmethod
            def first_seen_ms(cid):
                return None
        orig = R._data_file
        R._data_file = lambda name: {"XAU": "commodity"} if name == "bx_asset_class.json" else {}
        try:
            res = R.run_daily(now_ms=NOW, client=client, hl_mids_fn=lambda: {"BTC": 83000.0}, cg=CG)
        finally:
            R._data_file = orig
        self.assertEqual(res["status"], "success", res)
        r1d = json.loads((self.tmp / "bx_radar_1d.json").read_text())
        tradfi = json.loads((self.tmp / "bx_tradfi_radar.json").read_text())
        meta = json.loads((self.tmp / "bx_meta.json").read_text())
        self.assertEqual([r["symbol"] for r in r1d["rows"]], ["FOO"])        # crypto only, no unknown / HL overlap
        self.assertTrue(r1d["rows"][0]["dual_cross_up"])
        self.assertEqual(r1d["rows"][0]["gc_tf"], "1d")
        self.assertEqual(r1d["rows"][0]["tier"], "large")
        self.assertTrue(r1d["display_only"])
        self.assertEqual([r["symbol"] for r in tradfi["rows"]], ["XAU"])
        self.assertIn("ZZZ", [x["symbol"] for x in meta["review"]])
        self.assertEqual(meta["counts"]["ex_HL+BX"], 1)
        # second run: warm cache -> one page per symbol, no full history
        client.calls.clear()
        R._data_file = lambda name: {}
        try:
            R.run_daily(now_ms=NOW + 3_600_000, client=client, hl_mids_fn=lambda: {"BTC": 83000.0}, cg=CG)
        finally:
            R._data_file = orig
        self.assertFalse([c for c in client.calls if c[0] == "history" and c[1] == "FOOUSDT"])

    def test_catalog_failure_is_reported_not_raised(self):
        class Down(FakeClient):
            def trading_pairs(self):
                raise RuntimeError("timeout")
        res = R.run_daily(now_ms=NOW, client=Down([], [], {}), hl_mids_fn=lambda: {}, cg=cg_client)
        self.assertEqual(res["status"], "error")
        self.assertIn("unreachable", res["message"])


if __name__ == "__main__":
    unittest.main()
