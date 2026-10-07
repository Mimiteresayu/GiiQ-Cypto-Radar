"""Bitunix shadow book: simulated entries / exits / caps / pending Chase / comparison. No orders anywhere."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bx_radar
import bx_shadow as S
import dim_ledger

T0 = datetime(2026, 9, 29, 0, 20, tzinfo=timezone.utc)
DAY = 86_400_000
H1 = 3_600_000
H4 = 14_400_000


def ms(dt):
    return int(dt.timestamp() * 1000)


def meta(sym="FOO", **kw):
    m = {"symbol": sym, "bx_symbol": f"{sym}USDT", "ex": "BX", "asset_class": "crypto", "liq_tier": "tradeable",
         "gc_tf": "1d", "price": 2.0, "tier": "small", "spread_bp": 4.0, "vol24h_usd": 3e6,
         "max_notional_usd": 15000.0, "asset_age": "old"}
    m.update(kw)
    return m


def row(sym="FOO", **kw):
    r = {"symbol": sym, "bx_symbol": f"{sym}USDT", "close": 2.0, "low": 1.95, "high": 2.05, "filter": 1.8,
         "upper": 1.9, "lower": 1.7, "trend": "Green", "dual_cross_up": False, "bar_time": ms(T0) - DAY}
    r.update(kw)
    return r


class ShadowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (bx_radar.OUT_DIR, S.OUT_DIR)
        bx_radar.OUT_DIR = S.OUT_DIR = self.tmp
        os.environ["BX_LEDGER_PATH"] = str(self.tmp / "bx.db")
        self.conn = S.connect()

    def tearDown(self):
        self.conn.close()
        bx_radar.OUT_DIR, S.OUT_DIR = self._orig
        os.environ.pop("BX_LEDGER_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, metas, r1d=(), r4h=(), r1h=()):
        (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": metas}))
        for tf, rows in (("1d", r1d), ("4h", r4h), ("1h", r1h)):
            (self.tmp / f"bx_radar_{tf}.json").write_text(json.dumps({"rows": list(rows)}))

    def trades(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM shadow_trades ORDER BY id")]


class TestSignals(unittest.TestCase):
    def test_classify(self):
        base = S.classify_signal(meta(), row(dual_cross_up=True), row(), None)
        self.assertEqual((base["type"], base["gc_tf"]), ("Base", "1d"))
        chase = S.classify_signal(meta(), row(), row(dual_cross_up=True), None)
        self.assertEqual(chase["type"], "Chase")
        self.assertIsNone(S.classify_signal(meta(), row(), row(dual_cross_up=True, trend="Red"), None))
        nt4 = S.classify_signal(meta(gc_tf="4h"), None, row(dual_cross_up=True), None)
        self.assertEqual((nt4["type"], nt4["gc_tf"]), ("NewToken", "4h"))
        nt1 = S.classify_signal(meta(gc_tf="1h"), None, None, row(dual_cross_up=True))
        self.assertEqual(nt1["gc_tf"], "1h")
        for m in (meta(liq_tier="exclude"), meta(ex="HL+BX"), meta(asset_class="stock")):
            self.assertIsNone(S.classify_signal(m, row(dual_cross_up=True), row(), None))

    def test_returns_and_slippage(self):
        self.assertEqual(S.trade_return(100, 110), round(10 - 0.12, 4))
        self.assertAlmostEqual(S.fill_px(100, 12, "buy"), 100.12)
        self.assertAlmostEqual(S.fill_px(100, 12, "sell"), 99.88)


class TestEntries(ShadowCase):
    def test_base_opens_with_slippage_and_is_counted(self):
        self.write([meta()], r1d=[row(dual_cross_up=True)], r4h=[row()])
        rep = S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)
        self.assertEqual(len(rep["opened"]), 1)
        t = self.trades()[0]
        self.assertAlmostEqual(t["entry_px"], 2.0 * 1.0005)          # max(5 bp, 4/2) = 5 bp
        self.assertEqual((t["size_pct_nav"], t["leverage"], t["hard_sl"]), (2.0, 3.0, 1.8))   # small: 4H Filter
        self.assertEqual(t["exit_rule"], "1h_close_below_lower")
        m = self.conn.execute("SELECT counted, gc_tf FROM bx_signal_meta").fetchone()
        self.assertEqual(tuple(m), (1, "1d"))
        s = self.conn.execute("SELECT strategy, symbol FROM signals").fetchone()
        self.assertEqual(tuple(s), ("giiq-gc-bx-shadow", "FOOUSDT"))

    def test_half_spread_slippage_on_thin_book(self):
        self.write([meta(spread_bp=30.0, liq_tier="watch")], r1d=[row(dual_cross_up=True)], r4h=[row()])
        S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)
        t = self.trades()[0]
        self.assertAlmostEqual(t["slip_bp"], 15.0)
        self.assertEqual(t["counted"], 0)                              # watch tier never counts

    def test_downsized_to_half_percent_of_volume(self):
        self.write([meta()], r1d=[row(dual_cross_up=True)], r4h=[row()])
        S.run("daily", now=T0, nav_usd=1_000_000, conn=self.conn)
        self.assertAlmostEqual(self.trades()[0]["size_pct_nav"], 0.5)  # $15k / ($1M x 3)

    def test_three_fills_per_day_cap(self):
        ms_ = [meta(s) for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]
        self.write(ms_, r1d=[row(s, dual_cross_up=True) for s in ("AAA", "BBB", "CCC", "DDD", "EEE")],
                   r4h=[row(s) for s in ("AAA", "BBB", "CCC", "DDD", "EEE")])
        rep = S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)
        self.assertEqual(len(rep["opened"]), 3)
        self.assertEqual(sum(1 for s in rep["skipped"] if "3 new fills" in s["reason"]), 2)

    def test_no_hard_sl_no_entry(self):
        self.write([meta()], r1d=[row(dual_cross_up=True)], r4h=[])
        rep = S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["opened"], [])
        self.assertIn("no valid Hard SL", rep["skipped"][0]["reason"])

    def test_one_h_new_token_is_watch_only(self):
        self.write([meta(gc_tf="1h", asset_age="new_token")], r1h=[row(dual_cross_up=True, lower=1.9)])
        rep = S.run("1h", now=T0, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["signals"][0]["counted"], False)
        t = self.trades()[0]
        self.assertEqual((t["size_pct_nav"], t["counted"], t["hard_sl"]), (1.0, 0, 1.9))


class TestExits(ShadowCase):
    def open_base(self, **mkw):
        self.write([meta(**mkw)], r1d=[row(dual_cross_up=True)], r4h=[row()])
        S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)

    def test_small_tier_1h_lower_exit(self):
        self.open_base()
        later = T0 + timedelta(hours=5)
        self.write([meta()], r1d=[row()], r4h=[row()],
                   r1h=[row(close=1.85, low=1.84, lower=1.86, bar_time=ms(later) - H1)])
        rep = S.run("1h", now=later, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["closed"][0]["reason"], "tier_exit_1h_lower")
        t = self.trades()[0]
        self.assertAlmostEqual(t["exit_px"], 1.85 * (1 - 0.0005))
        self.assertAlmostEqual(t["ret_pct"], round((t["exit_px"] / t["entry_px"] - 1) * 100 - 0.12, 4))

    def test_hard_sl_first(self):
        self.open_base()
        later = T0 + timedelta(hours=9)
        self.write([meta()], r4h=[row(low=1.75, close=1.9, bar_time=ms(later) - H4)])
        rep = S.run("4h", now=later, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["closed"][0]["reason"], "hard_sl")
        self.assertAlmostEqual(self.trades()[0]["exit_px"], 1.8 * (1 - 0.0005))

    def test_large_tier_uses_4h_filter(self):
        self.open_base(tier="large")
        t = self.trades()[0]
        self.assertEqual((t["exit_rule"], t["hard_sl"]), ("4h_close_below_filter", 1.7))   # 4H Lower
        later = T0 + timedelta(hours=9)
        self.write([meta(tier="large")], r4h=[row(close=1.79, low=1.78, bar_time=ms(later) - H4)])
        self.assertEqual(S.run("4h", now=later, conn=self.conn)["closed"][0]["reason"], "tier_exit_4h_filter")

    def test_new_token_time_stop_and_liquidity_exit(self):
        self.write([meta(gc_tf="4h", asset_age="new_token")], r4h=[row(dual_cross_up=True)])
        S.run("4h", now=T0, nav_usd=10_000, conn=self.conn)
        self.assertEqual(self.trades()[0]["size_pct_nav"], 1.0)
        later = T0 + timedelta(days=5, hours=1)
        self.write([meta(gc_tf="4h", asset_age="new_token")], r4h=[row(bar_time=ms(later) - H4)])
        self.assertEqual(S.run("4h", now=later, conn=self.conn)["closed"][0]["reason"], "time_stop_5d")
        # liquidity exit on a second token
        self.write([meta("NEW", gc_tf="4h", asset_age="new_token")], r4h=[row("NEW", dual_cross_up=True)])
        S.run("4h", now=later, nav_usd=10_000, conn=self.conn)
        self.write([meta("NEW", gc_tf="4h", asset_age="new_token", vol24h_usd=1.5e5)],
                   r4h=[row("NEW", bar_time=ms(later))])
        rep = S.run("4h", now=later + timedelta(hours=4), conn=self.conn)
        self.assertEqual(rep["closed"][0]["reason"], "liquidity_exit")


class TestChasePending(ShadowCase):
    def test_continuation_fills_on_n_plus_1(self):
        # day 0: Chase signal -> simulated CONTINUATION pending (no immediate entry)
        self.write([meta()], r1d=[row()], r4h=[row(dual_cross_up=True)])
        rep = S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["opened"], [])
        pend = S.load_pending()
        self.assertEqual((pend[0]["kind"], pend[0]["status"]), ("CONTINUATION", "pending"))
        # day 1: bar N (low <= Filter, close > Lower)
        d1 = T0 + timedelta(days=1)
        self.write([meta()], r1d=[row(low=1.79, close=1.85, bar_time=ms(d1) - DAY)], r4h=[row()])
        S.run("daily", now=d1, nav_usd=10_000, conn=self.conn)
        self.assertIsNotNone(S.load_pending()[0].get("setup"))
        # day 2: bar N+1 closes above Lower and above N close -> fill
        d2 = T0 + timedelta(days=2)
        self.write([meta(price=1.95)], r1d=[row(low=1.84, close=1.93, bar_time=ms(d2) - DAY)], r4h=[row()])
        rep = S.run("daily", now=d2, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["opened"][0]["kind"], "Chase")
        self.assertEqual(S.load_pending()[0]["status"], "filled")
        self.assertEqual(self.trades()[0]["counted"], 1)

    def test_pending_file_is_separate_from_hl(self):
        self.assertEqual(S.pending_path().name, "bx_shadow_pending.json")


class TestCompare(ShadowCase):
    def test_hl_ledger_read_only_and_insufficient(self):
        hl_path = str(self.tmp / "giiq_ledger.db")
        hl = dim_ledger.connect(hl_path)
        hl.execute("INSERT INTO signals(signal_date, symbol, type, ref_close, ref_bar_time) VALUES ('2026-09-01','AAA','Base',1,1)")
        hl.execute("INSERT INTO outcomes(signal_id, ret_7d_sl, complete) VALUES (1, 4.0, 1)")
        hl.execute("INSERT INTO decisions(signal_id, source, action) VALUES (1, 'claude', 'approve')")
        hl.commit()
        hl.close()
        before = hashlib.sha256(Path(hl_path).read_bytes()).hexdigest()
        self.write([meta()], r1d=[row(dual_cross_up=True)], r4h=[row()])
        S.run("daily", now=T0, nav_usd=10_000, conn=self.conn)
        c = S.compare(self.conn, hl_path=hl_path)
        self.assertEqual(hashlib.sha256(Path(hl_path).read_bytes()).hexdigest(), before)
        self.assertTrue(c["verdict"].startswith("insufficient"))
        self.assertEqual(c["hl_rule_only"]["n"], 1)
        self.assertEqual(c["hl_claude_approved"]["n"], 1)
        self.assertEqual(c["signals"], {"total": 1, "counted": 1, "watch": 0})
        self.assertEqual(c["hl_ledger"], "read-only")


if __name__ == "__main__":
    unittest.main()
