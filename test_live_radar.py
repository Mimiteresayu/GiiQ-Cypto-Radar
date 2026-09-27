"""LIVE radar (10-min, display only) + DESK_DATA new fields + candidates sync."""
import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import live_radar
import scan_gc_radar as sgr

H = 3600_000


def _bars(n, start, step, base=1.0):
    out = []
    for i in range(n):
        c = base + 0.001 * i
        out.append([start + i * step, c, c * 1.01, c * 0.99, c, 10.0])
    return out


class TestApplyMid(unittest.TestCase):
    def test_updates_forming_bar(self):
        now = 10 * H + 600_000
        bars = [[9 * H, 1, 1.2, 0.9, 1.1, 5], [10 * H, 1.1, 1.15, 1.05, 1.12, 1]]
        nb = live_radar.apply_mid(bars, 1.3, now, H)
        self.assertEqual(len(nb), 2)
        self.assertEqual(nb[-1][4], 1.3)
        self.assertEqual(nb[-1][2], 1.3)  # high extended
        self.assertEqual(bars[-1][4], 1.12)  # input untouched

    def test_new_bar_after_boundary(self):
        now = 11 * H + 60_000
        bars = [[9 * H, 1, 1.2, 0.9, 1.1, 5], [10 * H, 1.1, 1.15, 1.05, 1.12, 1]]
        nb = live_radar.apply_mid(bars, 1.0, now, H)
        self.assertEqual(nb[-1], [11 * H, 1.0, 1.0, 1.0, 1.0, 0.0])


class TestRefreshTf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.now = 1000 * H + 1_200_000  # 20 min into a 1H bar
        start = (1000 - 299) * H
        bars = _bars(300, start, H)  # last bar = forming
        self.closed_row = {"symbol": "AAA", "close": 1.298, "filter": 1.2, "upper": 1.3, "lower": 1.1,
                           "trend": "Green", "dual_cross_up": True, "dual_cross_down_filter": False,
                           "bar_time": 999 * H}
        json.dump({"tf": "1h", "ts": "2026-01-01T00:07:00+00:00", "rows": [dict(self.closed_row)]},
                  open(os.path.join(self.tmp, "gc_radar_1h.json"), "w"))
        with gzip.open(os.path.join(self.tmp, "candles_1h.json.gz"), "wt") as f:
            json.dump({"tf": "1h", "bars": {"AAA": bars}}, f)

    def test_live_written_closed_untouched(self):
        r = live_radar.refresh_tf("1h", {"AAA": 5.0}, now_ms=self.now, out_dir=self.tmp)
        self.assertTrue(r["ok"])
        self.assertEqual(r["n_live"], 1)
        d = json.load(open(os.path.join(self.tmp, "gc_radar_1h.json")))
        row = d["rows"][0]
        for k, v in self.closed_row.items():  # closed SoT unchanged
            self.assertEqual(row[k], v)
        self.assertEqual(d["ts"], "2026-01-01T00:07:00+00:00")
        lv = row["live"]
        self.assertEqual(lv["close"], 5.0)
        self.assertTrue(lv["forming"])
        self.assertTrue(lv["above_upper"])
        self.assertEqual(lv["bar_time"], 1000 * H)
        self.assertEqual(d["next_close_ms"], 1001 * H)
        self.assertEqual(d["closed_bar_open_ms"], 999 * H)
        self.assertIn("AAA", d["live_flags"]["above_upper"])
        self.assertEqual(d["live_breadth"]["n"], 1)
        with gzip.open(os.path.join(self.tmp, "candles_1h.json.gz"), "rt") as f:
            self.assertEqual(json.load(f)["bars"]["AAA"][-1][4], 5.0)

    def test_missing_cache(self):
        os.remove(os.path.join(self.tmp, "candles_1h.json.gz"))
        r = live_radar.refresh_tf("1h", {"AAA": 5.0}, now_ms=self.now, out_dir=self.tmp)
        self.assertFalse(r["ok"])

    def test_scanner_live_values_match_live_radar_math(self):
        bars = [{"t": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]}
                for b in _bars(300, (1000 - 299) * H, H)]
        gc = sgr.compute_gc([b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars], period=48)
        lv = sgr.live_values(bars, gc, len(gc) - 1, self.now, H)
        self.assertEqual(lv["bar_time"], 1000 * H)
        self.assertTrue(lv["forming"])
        t = sgr.radar_timing("1h", [{"bar_time": 999 * H}], self.now)
        self.assertEqual(t["closed_bar_close_ms"], 1000 * H)


class TestDeskDataLiveFields(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        patch.object(serve, "OUT_DIR", self.tmp).start()
        self.now = datetime.now(timezone.utc)
        for tf in ("1d", "4h", "1h"):
            rows = [{"symbol": s, "trend": "Green", "close": 1.0, "filter": 0.9, "upper": 1.1, "lower": 0.8,
                     "dual_cross_up": s == "AAA", "dual_cross_down_filter": False, "tier": "tiny", "bar_time": 1,
                     "category": "Narrative", "categories": ["Narrative", "Cemetery"],
                     "live": {"close": 1.05, "filter": 0.91, "upper": 1.12, "lower": 0.81, "trend": "Green",
                              "above_upper": False, "cross_up": False}}
                    for s in ("AAA", "BTC", "ZZZ", "NARR")]
            json.dump({"ts": self.now.isoformat(), "live_ts": self.now.isoformat(), "closed_bar_open_ms": 1,
                       "rows": rows, "flags": {"dual_cross_up": ["AAA"]}},
                      open(os.path.join(self.tmp, f"gc_radar_{tf}.json"), "w"))
        json.dump({"generated_at": self.now.isoformat(), "stale": False, "count": 1,
                   "candidates": [{"symbol": "AAA", "type": "Base", "is_base": True, "is_chase": False,
                                   "tier": "tiny", "close_1d": 1.0, "upper_1d": 1.1, "filter_4h": 0.9,
                                   "lower_4h": 0.8, "entry_ref": 1.1}]},
                  open(os.path.join(self.tmp, "entry_candidates_latest.json"), "w"))
        json.dump({"updated": "2026-09-27", "items": [{"ticker": "NARR", "sector": "ai", "narrative": "x" * 200},
                                                     {"ticker": "OFFHL", "venue": "bitunix"}]},
                  open(os.path.join(self.tmp, "narrative_watchlist.json"), "w"))
        hl = {"hl_perp": {"marginSummary": {}, "withdrawable": "0", "assetPositions": []},
              "hl_spot": {"balances": [{"coin": "USDC", "total": "100", "hold": "0"}]},
              "hl_open_orders": [{"coin": "AAA", "side": "A", "sz": "10", "limitPx": "0.8", "orderType": "Stop Market",
                                  "isTrigger": True, "triggerPx": "0.8", "triggerCondition": "Price below 0.8",
                                  "reduceOnly": True, "isPositionTpsl": True, "oid": 1, "timestamp": 1}]}
        patch.object(serve, "_get_hl_cached", return_value=hl).start()
        patch.object(serve, "_get_hl_meta_cached", return_value={}).start()

    def tearDown(self):
        patch.stopall()

    def test_full_payload(self):
        p = self.serve._build_desk_data_payload("x", now=self.now, kind="full")
        self.assertEqual(p["kind"], "full")
        for tf in ("1d", "4h", "1h"):
            t = p["timing"][tf]
            for k in ("closed_scan_ts", "last_closed_bar_open", "last_closed_bar_close", "live_ts",
                      "forming_bar_open", "next_close", "next_closed_scan", "next_live_update"):
                self.assertIn(k, t)
            self.assertTrue(t["next_close"].endswith("+08:00"))
            self.assertEqual(p[f"gc_radar_{tf}"]["n"], 4)
        row = p["gc_radar_1h"]["rows"][0]
        self.assertEqual(row["close"], 1.0)
        self.assertEqual(row["live_close"], 1.05)
        self.assertEqual([c["symbol"] for c in p["entry_tab"]["base"]], ["AAA"])
        self.assertEqual(p["entry_tab"]["chase"], [])
        self.assertEqual(p["candidates"][0]["symbol"], "AAA")
        o = p["hl_open_orders"][0]
        self.assertTrue(o["isTrigger"] and o["reduceOnly"])
        self.assertEqual(o["triggerPx"], "0.8")
        self.assertEqual(p["narrative"]["items"][0]["ticker"], "NARR")
        self.assertTrue(p["narrative"]["items"][0]["on_hl"])
        self.assertLessEqual(len(p["narrative"]["items"][0]["narrative"]), 80)
        self.assertEqual(p["narrative"]["items"][0]["venue"], "HL")
        self.assertEqual(p["narrative"]["items"][1]["venue"], "bitunix")  # non-HL ticker kept
        self.assertFalse(p["narrative"]["items"][1]["on_hl"])
        self.assertEqual(row["cat"], "Narrative + Cemetery")

    def test_live_payload_is_compact_focus(self):
        p = self.serve._build_desk_data_payload("live_radar", now=self.now, kind="live")
        syms = {r["symbol"] for r in p["gc_radar_4h"]["rows"]}
        self.assertEqual(syms, {"AAA", "BTC", "NARR"})  # candidate, BTC, narrative on HL; ZZZ dropped
        self.assertTrue(p["gc_radar_4h"]["focus_only"])
        self.assertEqual(p["gc_radar_4h"]["n_total"], 4)
        self.assertNotIn("items", p["narrative"])

    def test_log_upgrades_to_full_hourly(self):
        out = []
        with patch("sys.stdout.write", side_effect=out.append), patch("sys.stdout.flush"):
            self.serve._last_full_desk["t"] = 0.0
            self.serve._log_desk_data("live_radar", kind="live")
            self.serve._log_desk_data("live_radar", kind="live")
        kinds = [json.loads(l.split("] ", 1)[1])["kind"] for l in out if l.startswith("[DESK_DATA")]
        self.assertEqual(kinds, ["full", "live"])


class TestCandidatesFlags(unittest.TestCase):
    def test_is_base_is_chase(self):
        from entry_candidates import build_candidates
        now = datetime.now(timezone.utc).isoformat()
        r1d = {"ts": now, "rows": [{"symbol": "A", "trend": "Green", "dual_cross_up": True, "upper": 2, "close": 2.1},
                                   {"symbol": "B", "trend": "Green", "dual_cross_up": False, "upper": 3, "close": 2.9}]}
        r4h = {"ts": now, "rows": [{"symbol": "B", "trend": "Green", "dual_cross_up": True, "upper": 2.8, "filter": 2.5}]}
        res = build_candidates(r1d, r4h)
        by = {c["symbol"]: c for c in res["candidates"]}
        self.assertTrue(by["A"]["is_base"] and not by["A"]["is_chase"])
        self.assertTrue(by["B"]["is_chase"] and not by["B"]["is_base"])
        self.assertEqual(by["B"]["entry_ref"], 2.8)
        self.assertEqual(by["A"]["entry_ref"], 2)


class TestSchedulerLiveJob(unittest.TestCase):
    def test_live_job_registered_and_manual(self):
        import serve
        self.assertIn("live", serve.MANUAL_JOBS)
        self.assertEqual(serve.LIVE_RADAR_MINUTES, "3-59/10")
        nxt = serve._next_live_update(datetime(2026, 9, 27, 17, 4, tzinfo=timezone.utc))
        self.assertEqual(nxt, "2026-09-28T01:13:00+08:00")

    def test_live_job_runs_refresh_and_candidates(self):
        import serve
        with patch.object(serve, "_candles_cache_missing", return_value=[]), \
             patch.object(serve.live_radar, "refresh_live", return_value={"ok": True, "n_mids": 5, "tfs": {}, "elapsed_s": 0.1}) as rl, \
             patch.object(serve, "_generate_entry_candidates", return_value=3) as gen, \
             patch.object(serve, "_update_job_status") as st, \
             patch.object(serve, "_log_desk_data") as log, \
             patch.object(serve, "_run_scan") as scan:
            serve._scheduled_live_radar()
        rl.assert_called_once()
        gen.assert_called_once()
        scan.assert_not_called()  # no boot scan when caches exist; never exits/orders
        self.assertEqual(st.call_args[0][1], "success")
        self.assertEqual(log.call_args.kwargs.get("kind"), "live")


if __name__ == "__main__":
    unittest.main()


class TestNarrativeEndpoint(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        patch.object(serve, "OUT_DIR", self.tmp).start()
        json.dump({"sot": "x", "items": [{"ticker": "OLD", "sector": "ai", "first_seen": "2026-09-01", "notes": "n"}]},
                  open(os.path.join(self.tmp, "narrative_watchlist.json"), "w"))

    def tearDown(self):
        patch.stopall()

    def _wl(self):
        return json.load(open(os.path.join(self.tmp, "narrative_watchlist.json")))

    def test_merge_upsert_and_remove(self):
        r = self.serve.update_narrative_watchlist(
            {"items": [{"ticker": "$new", "sector": "rwa", "venue": "HL", "narrative": "n" * 400},
                       {"ticker": "OLD", "venue": "bitunix"}], "remove": []})
        self.assertEqual(r["count"], 2)
        wl = self._wl()
        by = {i["ticker"]: i for i in wl["items"]}
        self.assertEqual(by["OLD"]["first_seen"], "2026-09-01")  # kept
        self.assertEqual(by["OLD"]["sector"], "ai")
        self.assertEqual(by["OLD"]["venue"], "bitunix")
        self.assertEqual(len(by["NEW"]["narrative"]), 300)
        self.assertEqual(wl["managed_by"], "api")
        self.assertEqual(wl["sot"], "x")
        self.serve.update_narrative_watchlist({"remove": ["old"]})
        self.assertEqual([i["ticker"] for i in self._wl()["items"]], ["NEW"])

    def test_replace_and_validation(self):
        self.serve.update_narrative_watchlist({"mode": "replace", "items": [{"ticker": "AAA"}]})
        self.assertEqual([i["ticker"] for i in self._wl()["items"]], ["AAA"])
        for bad in ({"mode": "x"}, {"items": [{"sector": "no ticker"}]}, {"items": "AAA"}):
            with self.assertRaises(ValueError):
                self.serve.update_narrative_watchlist(bad)

    def test_scanner_uses_only_api_list(self):
        self.serve.update_narrative_watchlist({"mode": "replace", "items": [{"ticker": "ZZTOP"}]})
        with patch.object(sgr, "ROOT", self.tmp):
            os.makedirs(os.path.join(self.tmp, "out"), exist_ok=True)
            os.replace(os.path.join(self.tmp, "narrative_watchlist.json"),
                       os.path.join(self.tmp, "out", "narrative_watchlist.json"))
            os.makedirs(os.path.join(self.tmp, "narrative"), exist_ok=True)
            json.dump({"items": [{"ticker": "BAKED"}]}, open(os.path.join(self.tmp, "narrative", "watchlist.json"), "w"))
            self.assertEqual(sgr.load_narrative_tickers(), {"ZZTOP"})
