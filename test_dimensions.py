#!/usr/bin/env python3
"""Tests for GIIQ dimensions (shadow scoring), whales, measurement ledger, decision schema."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import dim_ledger as L  # noqa: E402
import dimensions as D  # noqa: E402
import whales as W  # noqa: E402

DAY = 86_400_000


def bars_from_closes(closes, start_ms=1_700_000_000_000 - (1_700_000_000_000 % DAY)):
    return [[start_ms + i * DAY, c, c * 1.02, c * 0.98, c, 1.0] for i, c in enumerate(closes)]


class TestScores(unittest.TestCase):
    def test_trend_base_strong_and_weak(self):
        c = {"type": "Base", "trend_1d": "Green", "trend_4h": "Green", "close_1d": 101.5, "upper_1d": 100}
        d = D.score_trend(c, {"trend": "Green"})
        self.assertEqual(d["score"], 2)  # align 3 -> +1, breakout 1.5% -> +1
        c2 = {"type": "Chase", "is_chase": True, "trend_1d": "Green", "trend_4h": "Green",
              "close_4h": 0.51082, "upper_4h": 0.51053}  # TIA 28 Sep: 0.06% breakout
        d2 = D.score_trend(c2, {"trend": "Red"})
        self.assertEqual(d2["raw"]["signal_tf"], "4h")
        self.assertEqual(d2["score"], -1)  # align 2 -> 0, weak breakout -> -1

    def test_extension(self):
        self.assertEqual(D.score_extension({"close_1d": 102, "upper_1d": 100})["score"], 1)
        self.assertEqual(D.score_extension({"close_1d": 115, "upper_1d": 100})["score"], -1)
        self.assertEqual(D.score_extension({"close_1d": 130, "upper_1d": 100})["score"], -2)
        self.assertIsNone(D.score_extension({"close_1d": None, "upper_1d": 100})["score"])

    def test_rel_strength_uses_closed_bars_only(self):
        now = 1_700_000_000_000 - (1_700_000_000_000 % DAY) + 9 * DAY + 3600_000
        coin = bars_from_closes([100] * 8 + [120, 999])  # last bar still forming -> ignored
        btc = bars_from_closes([100] * 9 + [50])
        d = D.score_rel_strength(coin, btc, now)
        self.assertEqual(d["raw"]["ret7_pct"], 20.0)
        self.assertEqual(d["score"], 2)

    def test_crowding_funding_and_oi(self):
        d = D.score_crowding({"funding": 0.0001, "oi": 110}, {"oi": 100})  # 87.6%/yr
        self.assertEqual(d["score"], -2)
        self.assertEqual(d["raw"]["oi_chg_24h_pct"], 10.0)
        self.assertEqual(D.score_crowding({"funding": -0.00001}, None)["score"], 1)
        self.assertIsNone(D.score_crowding(None, None)["score"])

    def test_liquidity_and_narrative(self):
        self.assertEqual(D.score_liquidity({"day_ntl_vlm": 60e6}, {})["score"], 2)
        self.assertEqual(D.score_liquidity(None, {"dayNtlVlm": 100_000})["score"], -2)
        self.assertEqual(D.score_narrative({"symbol": "CAT", "cat_tags": "C"}, {})["score"], 0)
        self.assertEqual(D.score_narrative({"symbol": "X", "cat_tags": "N V"}, {})["score"], 1)

    def test_btc_regime(self):
        r1d = {"rows": [{"symbol": "BTC", "close": 100, "filter": 90}]}
        r4h = {"rows": [{"symbol": "BTC", "close": 100, "filter": 110}]}
        self.assertEqual(D.btc_regime(r1d, r4h)["score"], 0)
        self.assertEqual(D.btc_regime({"rows": [{"symbol": "BTC", "close": 80, "filter": 90}]}, r4h)["score"], -2)

    def test_concentration(self):
        sec = {"A": "ai", "B": "ai", "C": "ai", "D": "meme"}
        self.assertEqual(D.score_concentration("ai", ["B", "C", "D"], sec, "A")["score"], -2)
        self.assertEqual(D.score_concentration("meme", ["B"], sec, "D")["score"], 0)

    def test_smart_money(self):
        agg = {"long_ntl": 900_000, "short_ntl": 100_000, "n_long": 4, "n_short": 1}
        d = D.score_smart_money(agg, {"long_ntl": 500_000, "short_ntl": 500_000})
        self.assertEqual(d["score"], 2)
        self.assertEqual(d["raw"]["net_change"], 0.8)
        self.assertEqual(D.score_smart_money({"long_ntl": 1, "n_long": 1}, None)["score"], 0)  # too few wallets
        self.assertIsNone(D.score_smart_money(None, None)["score"])

    def test_compute_all_and_compact(self):
        now = 1_700_000_000_000 - (1_700_000_000_000 % DAY) + 9 * DAY + 3600_000
        cands = [{"symbol": "MON", "type": "Base", "is_base": True, "trend_1d": "Green", "trend_4h": "Green",
                  "close_1d": 0.0131, "upper_1d": 0.0129, "tier": "small", "cat_tags": "N"}]
        res = D.compute_all(
            cands, radar_1d={"rows": [{"symbol": "BTC", "close": 1, "filter": 0.9},
                                      {"symbol": "MON", "category": "L1"}]},
            radar_4h={"rows": []}, radar_1h={"rows": [{"symbol": "MON", "trend": "Green"}]},
            bars_1d={"MON": bars_from_closes([1] * 10), "BTC": bars_from_closes([1] * 10)},
            asset_ctxs={"MON": {"funding": 0.0000125, "day_ntl_vlm": 20e6}}, prev_ctxs={},
            narrative_wl={"items": [{"ticker": "MON", "sector": "L1"}]},
            perp_state={"assetPositions": [{"position": {"coin": "SUI", "szi": "10"}}]},
            whale_coins={}, prev_whale_coins=None, now_ms=now)
        self.assertEqual(res["n"], 1)
        c = res["candidates"][0]
        self.assertEqual(set(c["dims"]), set(D.QUANT_DIMS))
        self.assertEqual(c["total"], sum(v["score"] for v in c["dims"].values() if v["score"] is not None))
        comp = D.compact({**res, "signal_date": "2026-09-28"})
        self.assertIn("MON", comp["scores"])
        self.assertIn("smart_money", comp["scores"]["MON"])

    def test_parse_asset_ctxs(self):
        raw = [{"universe": [{"name": "BTC"}, {"name": "ETH"}]},
               [{"funding": "0.0000125", "openInterest": "100", "dayNtlVlm": "5", "markPx": "1", "prevDayPx": "1"},
                {"funding": "x"}]]
        out = D.parse_asset_ctxs(raw)
        self.assertAlmostEqual(out["BTC"]["funding"], 0.0000125)
        self.assertIsNone(out["ETH"]["funding"])
        self.assertEqual(D.parse_asset_ctxs(None), {})


LB = [
    {"ethAddress": "0x" + "a" * 40, "accountValue": "2000000", "displayName": "good",
     "windowPerformances": [["month", {"pnl": "300000", "roi": "0.15", "vlm": "10000000"}],
                            ["allTime", {"pnl": "900000", "roi": "1.2", "vlm": "1"}]]},
    {"ethAddress": "0x" + "b" * 40, "accountValue": "5000000",  # market maker: huge turnover
     "windowPerformances": [["month", {"pnl": "900000", "roi": "0.2", "vlm": "9000000000"}],
                            ["allTime", {"pnl": "1", "roi": "1", "vlm": "1"}]]},
    {"ethAddress": "0x" + "c" * 40, "accountValue": "100", "windowPerformances": []},  # too small
    {"ethAddress": "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303", "accountValue": "1e9",
     "windowPerformances": [["month", {"pnl": "1", "roi": "1", "vlm": "1"}], ["allTime", {"pnl": "1"}]]},
]


class TestWhales(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_select_smart_money_filters(self):
        picked = W.select_smart_money(LB)
        self.assertEqual([p["address"] for p in picked], ["0x" + "a" * 40])

    def test_snapshot_aggregates_and_manual_first(self):
        W.save_manual_watchlist(self.tmp, [{"address": "0x" + "d" * 40, "label": "fomo guy", "source": "fomo"},
                                           {"address": "not-an-address"}])
        states = {
            "0x" + "a" * 40: {"assetPositions": [{"position": {"coin": "BTC", "szi": "1", "positionValue": "100000"}},
                                                 {"position": {"coin": "ETH", "szi": "-2", "positionValue": "8000"}}]},
            "0x" + "d" * 40: {"assetPositions": [{"position": {"coin": "BTC", "szi": "0.5", "positionValue": "50000"}}]},
        }
        snap = W.build_snapshot(lambda body: states[body["user"]], self.tmp,
                                datetime(2026, 9, 28, 1, tzinfo=timezone.utc), leaderboard_fetch=lambda: LB, sleep_s=0)
        self.assertEqual(snap["n_wallets"], 2)
        self.assertEqual(snap["n_manual"], 1)
        self.assertEqual(snap["coins"]["BTC"], {"long_ntl": 150000, "short_ntl": 0, "n_long": 2, "n_short": 0})
        self.assertEqual(snap["coins"]["ETH"]["n_short"], 1)
        self.assertEqual(snap["wallets"][0]["source"], "fomo")
        # leaderboard cached for the day: a failing fetch must not matter now
        snap2 = W.build_snapshot(lambda body: states[body["user"]], self.tmp,
                                 datetime(2026, 9, 28, 2, tzinfo=timezone.utc),
                                 leaderboard_fetch=lambda: 1 / 0, sleep_s=0)
        self.assertEqual(snap2["errors"], [])
        self.assertIsNone(W.load_previous(self.tmp, "20260928"))
        self.assertIsNotNone(W.load_previous(self.tmp, "20260929"))

    def test_wallet_error_does_not_break(self):
        def boom(body):
            raise RuntimeError("429")
        snap = W.build_snapshot(boom, self.tmp, leaderboard_fetch=lambda: LB, sleep_s=0)
        self.assertEqual(snap["n_wallets"], 0)
        self.assertTrue(any("429" in e for e in snap["errors"]))


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = L.connect(os.path.join(self.tmp, "l.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compute_outcome(self):
        ref = 1_700_000_000_000 - (1_700_000_000_000 % DAY)
        bars = [[ref + i * DAY, 100, 100 * (1 + 0.01 * i), 100 - i, 100 + i, 1] for i in range(0, 10)]
        now = ref + 10 * DAY
        o = L.compute_outcome(bars, ref, 100.0, None, now)
        self.assertEqual(o["ret_1d"], 1.0)
        self.assertEqual(o["ret_7d"], 7.0)
        self.assertEqual(o["complete"], 1)
        self.assertEqual(o["ret_7d_sl"], 7.0)
        o2 = L.compute_outcome(bars, ref, 100.0, 97.0, now)  # low on day 3 = 97 -> SL hit
        self.assertEqual(o2["sl_hit"], 1)
        self.assertEqual(o2["sl_hit_day"], 3)
        self.assertEqual(o2["ret_7d_sl"], -3.0)
        o3 = L.compute_outcome(bars, ref, 100.0, 50.0, ref + 3 * DAY)  # only 2 closed bars after
        self.assertEqual(o3["bars_after"], 2)
        self.assertIsNone(o3["sl_hit"])
        self.assertEqual(o3["complete"], 0)

    def test_end_to_end_report(self):
        import random
        rnd = random.Random(7)
        ref = 1_700_000_000_000 - (1_700_000_000_000 % DAY)
        bars = {}
        for day in range(40):
            date = f"2026-08-{(day % 28) + 1:02d}-{day}"
            sym = f"C{day}"
            good = day % 2 == 0
            drift = 0.02 if good else -0.02
            closes = [100 * (1 + drift * i + rnd.uniform(-0.002, 0.002)) for i in range(10)]
            bars[sym] = [[ref + i * DAY, c, c * 1.001, c * 0.999, c, 1] for i, c in enumerate(closes)]
            cand = {"symbol": sym, "type": "Base", "close_1d": closes[0], "hard_sl": None}
            dims = {"dim_version": "t", "candidates": [{"symbol": sym, "dims": {
                "useful": {"score": 1 if good else -1, "raw": {}},
                "noise": {"score": rnd.choice([-1, 0, 1]), "raw": {}}}}]}
            L.record_signals(self.conn, date, [cand], dims, {sym: ref})
            L.record_decisions(self.conn, date, [{"symbol": sym, "decision": "approve" if good else "veto",
                                                  "dims": {"macro": 1 if good else -1, "bad": "x"}}])
        st = L.fill_outcomes(self.conn, bars, ref + 11 * DAY)
        self.assertEqual(st["completed"], 40)
        rep = L.report(self.conn, min_n=30)
        dims = {d["dim"]: d for d in rep["dimensions"]}
        self.assertEqual(dims["railway:useful"]["verdict"], "helpful")
        self.assertGreater(dims["railway:useful"]["veto_lift"], 0)
        self.assertEqual(dims["claude:macro"]["verdict"], "helpful")
        self.assertNotIn("claude:bad", dims)
        self.assertEqual(rep["claude"]["verdict"], "helpful")
        self.assertGreater(rep["claude"]["approve_edge_vs_all"], 0)
        small = L.report(self.conn, min_n=100)
        self.assertTrue(small["dimensions"][0]["verdict"].startswith("insufficient"))

    def test_spearman(self):
        self.assertEqual(L.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertEqual(L.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(L.spearman([1, 1, 1], [1, 2, 3]))


class TestDecisionSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DECISIONS_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("DECISIONS_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompt_shape_coin_action_is_accepted(self):
        from decisions import store_decisions, get_approved_symbols
        res = store_decisions([
            {"coin": "mon", "action": "APPROVE", "type": "BASE", "size_pct": 3, "leverage": 4, "reason": "ok",
             "dims": {"macro": 1}},
            {"symbol": "TIA", "decision": "veto", "reason": "weak breakout"},
            {"coin": "YGG", "action": "MAYBE"},
        ])
        self.assertTrue(res["ok"])
        self.assertEqual(res["stored_count"], 2)
        self.assertEqual(res["rejected_count"], 1)
        self.assertEqual(get_approved_symbols(), ["MON"])

    def test_all_invalid_is_not_ok(self):
        from decisions import store_decisions
        res = store_decisions([{"ticker": "MON", "verdict": "yes"}])
        self.assertFalse(res["ok"])
        self.assertIn("error", res)


class TestDimsJobAndServe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"DIM_LEDGER_PATH": os.path.join(self.tmp, "l.db"),
                                           "DECISIONS_DIR": os.path.join(self.tmp, "dec")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_inputs(self, now):
        def w(name, obj):
            with open(os.path.join(self.tmp, name), "w") as f:
                json.dump(obj, f)
        w("entry_candidates_latest.json", {"generated_at": now.isoformat(), "candidates": [
            {"symbol": "MON", "type": "Base", "is_base": True, "trend_1d": "Green", "trend_4h": "Green",
             "close_1d": 0.0131, "upper_1d": 0.0129, "tier": "small", "lower_4h": 0.012, "filter_4h": 0.0125}]})
        w("gc_radar_1d.json", {"rows": [{"symbol": "BTC", "close": 1, "filter": 0.9},
                                        {"symbol": "MON", "bar_time": 1_700_000_000_000}]})
        w("gc_radar_4h.json", {"rows": []})
        w("gc_radar_1h.json", {"rows": []})
        with gzip.open(os.path.join(self.tmp, "candles_1d.json.gz"), "wt") as f:
            json.dump({"bars": {"MON": bars_from_closes([1] * 10)}}, f)

    def test_snapshot_writes_file_and_ledger(self):
        import dims_job
        now = datetime.now(timezone.utc)
        self._write_inputs(now)

        def fake_post(body):
            if body["type"] == "metaAndAssetCtxs":
                return [{"universe": [{"name": "MON"}]}, [{"funding": "0.00001", "dayNtlVlm": "3000000"}]]
            return {"assetPositions": []}
        with patch.object(dims_job, "OUT_DIR", self.tmp), patch.object(dims_job, "_hl_post", lambda: fake_post), \
                patch.object(W, "fetch_leaderboard", lambda: LB):
            res = dims_job.snapshot(no_whales=True)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["ledger_rows"], 1)
        with open(os.path.join(self.tmp, "dimensions_latest.json")) as f:
            d = json.load(f)
        mon = d["candidates"][0]
        self.assertEqual(mon["dims"]["liquidity"]["score"], 0)
        self.assertIsNone(mon["dims"]["smart_money"]["score"])  # whales skipped
        conn = L.connect()
        rows = L.signal_rows(conn, d["signal_date"])
        conn.close()
        self.assertEqual(rows[0]["ref_bar_time"], 1_700_000_000_000)
        self.assertEqual(rows[0]["hard_sl"], 0.0125)  # small tier -> 4H Filter

    def test_ai_decision_endpoint_accepts_prompt_shape_and_records_ledger(self):
        import serve
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with patch.object(serve, "AI_DECISION_KEY", "k"):
                def post(body):
                    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ai/decision",
                                                 data=json.dumps(body).encode(), method="POST",
                                                 headers={"Content-Type": "application/json", "X-AI-Key": "k"})
                    try:
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.status, json.loads(r.read())
                    except urllib.error.HTTPError as e:
                        return e.code, json.loads(e.read())
                code, res = post({"date": "x", "decisions": [
                    {"coin": "MON", "action": "APPROVE", "type": "BASE", "size_pct": 3, "reason": "r",
                     "dims": {"macro": 1, "news": 0}}]})
                self.assertEqual(code, 200, res)
                self.assertEqual(res["stored_count"], 1)
                self.assertEqual(res["ledger_recorded"], 1)
                self.assertNotIn("stored", res)
                code2, res2 = post({"decisions": [{"ticker": "MON"}]})
                self.assertEqual(code2, 422)
                self.assertFalse(res2["ok"])
        finally:
            srv.shutdown()
            srv.server_close()
        conn = L.connect()
        n = conn.execute("SELECT COUNT(*) c FROM dim_scores WHERE source='claude'").fetchone()["c"]
        conn.close()
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()


class TestVetoRulesAndBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"DIM_LEDGER_PATH": os.path.join(self.tmp, "l.db"),
                                           "DECISIONS_DIR": os.path.join(self.tmp, "dec")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rule_is_normalized_stored_and_reported(self):
        from decisions import normalize
        self.assertEqual(normalize({"symbol": "TIA", "decision": "veto", "rule": "V1_WEAK_4H_BREAKOUT"})["rule"],
                         "V1_WEAK_4H_BREAKOUT")
        conn = L.connect()
        ref = 1_700_000_000_000 - (1_700_000_000_000 % DAY)
        bars = {}
        for k in range(12):
            sym, good = f"S{k}", k % 2 == 0
            closes = [100 * (1 + (0.02 if good else -0.02) * i) for i in range(10)]
            bars[sym] = [[ref + i * DAY, c, c, c, c, 1] for i, c in enumerate(closes)]
            L.record_signals(conn, f"2026-09-{k + 1:02d}", [{"symbol": sym, "type": "Chase", "close_1d": 100.0}],
                             {"candidates": []}, {sym: ref})
            L.record_decisions(conn, f"2026-09-{k + 1:02d}", [{"symbol": sym, "decision": "approve" if good else "veto",
                                                               "rule": None if good else "V1_WEAK_4H_BREAKOUT"}])
        L.fill_outcomes(conn, bars, ref + 11 * DAY)
        rep = L.report(conn, min_n=5)
        conn.close()
        vr = {r["rule"]: r for r in rep["veto_rules"]}
        self.assertEqual(vr["V1_WEAK_4H_BREAKOUT"]["blocked"], 6)
        self.assertGreater(vr["V1_WEAK_4H_BREAKOUT"]["avoided_vs_approved"], 0)

    def test_old_ledger_gets_rule_column(self):
        import sqlite3
        p = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(p)
        c.executescript(L.SCHEMA.replace("    rule TEXT,                            -- veto rule id (V1_WEAK_4H_BREAKOUT ...) or NULL\n", ""))
        c.close()
        conn = L.connect(p)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        conn.close()
        self.assertIn("rule", cols)

    def test_backfill_replays_history_into_separate_db(self):
        import dims_job
        import math
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        day0 = now_ms - now_ms % DAY - 280 * DAY
        H4 = 14_400_000

        import random

        def walk(n, seed, drift=0.004, vol=0.05):
            r, c = random.Random(seed), [10.0]
            for _ in range(n - 1):
                c.append(c[-1] * (1 + r.gauss(drift, vol)))
            return c

        def mk(closes, step, start):
            return [[start + i * step, c, c * 1.02, c * 0.98, c, 1000.0] for i, c in enumerate(closes)]
        b1d = {"BTC": mk([100 + i * 0.1 for i in range(280)], DAY, day0),
               "AAA": mk(walk(280, 2), DAY, day0), "BBB": mk(walk(280, 0), DAY, day0)}
        start4 = now_ms - now_ms % H4 - 450 * H4
        b4h = {"BTC": mk([100 + i * 0.02 for i in range(450)], H4, start4),
               "AAA": mk(walk(450, 3, 0.001, 0.02), H4, start4)}
        for tf, b in (("1d", b1d), ("4h", b4h)):
            with gzip.open(os.path.join(self.tmp, f"candles_{tf}.json.gz"), "wt") as f:
                json.dump({"bars": b}, f)
        with patch.object(dims_job, "OUT_DIR", self.tmp):
            res = dims_job.backfill(120)
        self.assertEqual(res["status"], "success", res)
        self.assertGreater(res["signals"], 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "giiq_ledger_backfill.db")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "dimensions_report_backfill.json")))
        conn = L.connect()  # live ledger untouched
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"], 0)
        conn.close()
        bconn = L.connect(L.backfill_db_path())
        dims = {r["dim"] for r in bconn.execute("SELECT DISTINCT dim FROM dim_scores")}
        bconn.close()
        self.assertEqual(dims, {"trend", "extension", "rel_strength", "liquidity", "btc_regime"})


class TestAiShadowJobEndpoint(unittest.TestCase):
    def test_only_shadow_jobs(self):
        import serve
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started = []
        try:
            with patch.object(serve, "AI_DECISION_KEY", "k"), \
                    patch.object(serve, "_start_manual_job", lambda job: (started.append(job) or True, "started")):
                def post(job, key="k"):
                    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ai/jobs/run",
                                                 data=json.dumps({"job": job}).encode(), method="POST",
                                                 headers={"X-AI-Key": key, "Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.status
                    except urllib.error.HTTPError as e:
                        return e.code
                self.assertEqual(post("dims_backfill"), 202)
                self.assertEqual(post("executor"), 400)
                self.assertEqual(post("dims", key="bad"), 403)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(started, ["dims_backfill"])
