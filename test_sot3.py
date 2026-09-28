#!/usr/bin/env python3
"""GIIQ-SoT-3: exit health checks, fallback decisions, Claude POST streak, daily entry counter."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exit_health as EH  # noqa: E402

NOW = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)  # 14:00 HKT
H = 3_600_000


def _perp(*positions):
    return {"marginSummary": {"totalMarginUsed": "0"},
            "assetPositions": [{"position": p} for p in positions]}


def _pos(coin, szi="10", lev=4, typ="isolated", margin="30"):
    return {"coin": coin, "szi": szi, "leverage": {"type": typ, "value": lev}, "marginUsed": margin}


def _radar(rows, age_h=0.5):
    return {"ts": (NOW - timedelta(hours=age_h)).isoformat(), "rows": rows}


def _jobs():
    t = (NOW - timedelta(minutes=10)).isoformat()
    return {"1h_scan_exits": {"last_run": t, "status": "success"},
            "4h_scan_exits": {"last_run": t, "status": "success"}}


class TestExitHealth(unittest.TestCase):
    def run_check(self, perp, orders, r1h=None, r4h=None, jobs=None, pending=None, nav=1000.0):
        return EH.check(perp=perp, open_orders=orders, nav=nav, radar_1h=r1h or _radar([]),
                        radar_4h=r4h or _radar([]), tier_for=lambda c: "small" if c == "CAKE" else "large",
                        job_status=_jobs() if jobs is None else jobs, pending=pending or [], now=NOW)

    def test_clean_is_ok_summary(self):
        r = self.run_check(_perp(_pos("MON")), [{"coin": "MON", "isTrigger": True, "reduceOnly": True}])
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["summary"].startswith("OK · 1 倉"))

    def test_no_sl_orphan_and_leverage(self):
        r = self.run_check(_perp(_pos("MON", lev=2, typ="cross")), [{"coin": "TIA", "isTrigger": True, "reduceOnly": True}])
        codes = sorted(p["code"] for p in r["problems"])
        self.assertEqual(codes, ["LEVERAGE_OFF", "LEVERAGE_OFF", "NO_SL", "ORPHAN_SL"])
        self.assertFalse(r["ok"])

    def test_exit_not_done_small_tier_after_grace(self):
        now_ms = int(NOW.timestamp() * 1000)
        bar_open = now_ms - now_ms % H - 2 * H          # closed 1h+ ago
        r1h = _radar([{"symbol": "CAKE", "close": 2.70, "lower": 2.75, "bar_time": bar_open}])
        r = self.run_check(_perp(_pos("CAKE")), [{"coin": "CAKE", "isTrigger": True}], r1h=r1h)
        self.assertEqual([p["code"] for p in r["problems"]], ["EXIT_NOT_DONE"])
        recent = _radar([{"symbol": "CAKE", "close": 2.70, "lower": 2.75, "bar_time": now_ms - now_ms % H - H}])
        r2 = EH.check(perp=_perp(_pos("CAKE")), open_orders=[{"coin": "CAKE", "isTrigger": True}], nav=1000,
                      radar_1h=recent, radar_4h=_radar([]), tier_for=lambda c: "small", job_status=_jobs(),
                      pending=[], now=datetime.fromtimestamp((now_ms - now_ms % H + 10 * 60_000) / 1000, timezone.utc))
        self.assertTrue(r2["ok"], r2)  # inside the 25-min grace after the bar close

    def test_margin_jobs_radar_pending(self):
        jobs = {"4h_scan_exits": {"last_run": (NOW - timedelta(hours=6)).isoformat(), "status": "success"},
                "executor": {"last_run": (NOW - timedelta(hours=5)).isoformat(), "status": "error", "error": "boom"},
                "manual_executor": {"last_run": NOW.isoformat(), "status": "error"}}
        pend = [{"status": "pending", "symbol": "SUI", "kind": "ADD_ON",
                 "created_at": (NOW - timedelta(days=8)).isoformat()}]
        r = self.run_check(_perp(_pos("MON", margin="900")), [{"coin": "MON", "reduceOnly": True}],
                           r1h=_radar([], age_h=3), jobs=jobs, pending=pend)
        codes = sorted(p["code"] for p in r["problems"])
        self.assertEqual(codes, ["JOB_FAILED", "JOB_MISSED", "JOB_MISSED", "MARGIN_HIGH", "PENDING_STALE", "RADAR_STALE"])

    def test_hl_fetch_error(self):
        r = self.run_check({"error": "timeout"}, [])
        self.assertEqual(r["problems"][0]["code"], "HL_FETCH")


class TestFallbackAndStreak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"DECISIONS_DIR": self.tmp})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fallback_ignored_when_claude_posted(self):
        from decisions import store_decisions, get_decisions_for_today
        now = datetime.now(timezone.utc)
        self.assertTrue(store_decisions([{"coin": "MON", "action": "VETO"}], source="claude", now=now)["ok"])
        r = store_decisions([{"coin": "MON", "action": "APPROVE"}], source="fallback", now=now)
        self.assertFalse(r["ok"])
        self.assertIn("fallback ignored", r["error"])
        self.assertEqual(get_decisions_for_today()["MON"]["decision"], "veto")

    def test_fallback_stored_when_no_claude(self):
        from decisions import store_decisions, get_decisions_for_today
        r = store_decisions([{"coin": "MON", "action": "APPROVE"}], source="fallback")
        self.assertTrue(r["ok"])
        self.assertEqual(get_decisions_for_today()["MON"]["source"], "fallback")

    def test_days_without_claude(self):
        from decisions import days_without_claude, store_decisions
        self.assertEqual(days_without_claude(NOW), 7)
        store_decisions([{"coin": "MON", "action": "VETO"}], source="claude", now=NOW - timedelta(days=2))
        store_decisions([{"coin": "MON", "action": "APPROVE"}], source="fallback", now=NOW - timedelta(days=1))
        self.assertEqual(days_without_claude(NOW), 2)
        # legacy records without "source" count as Claude
        f = Path(self.tmp) / f"decisions_{(NOW + timedelta(hours=8)).strftime('%Y%m%d')}.json"
        f.write_text(json.dumps({"decisions": {"X": {"decision": "veto"}}, "history": []}))
        self.assertEqual(days_without_claude(NOW), 0)

    def test_late_flag(self):
        from decisions import store_decisions
        late = datetime(2026, 9, 28, 0, 52, tzinfo=timezone.utc)  # 08:52 HKT
        self.assertIn("late", store_decisions([{"coin": "MON", "action": "VETO"}], now=late))
        early = datetime(2026, 9, 28, 0, 20, tzinfo=timezone.utc)  # 08:20 HKT
        self.assertNotIn("late", store_decisions([{"coin": "MON", "action": "VETO"}], now=early))


class TestExecutorFallback(unittest.TestCase):
    def test_fallback_chase_skipped_and_base_at_2pct(self):
        import test_live_execution as T
        t = T.TestExecutorDryRun("test_cumulative_margin_cap")
        t.setUp()
        try:
            hl = T.FakeHL(equity=1000, meta=t.META, mids={"AAA": 1.0, "BBB": 1.0})
            chase = dict(T._cand("BBB", filt=0.97, lower=0.95), type="Chase", is_base=False, is_chase=True)
            res = t.run_exec(hl, T._cands(T._cand("AAA", filt=0.97, lower=0.95), chase),
                             {"AAA": {"decision": "approve", "size_pct": 4, "source": "fallback"},
                              "BBB": {"decision": "approve", "size_pct": 4, "source": "fallback"}})
        finally:
            t.tearDown()
        self.assertEqual([a["symbol"] for a in res["actions"]], ["AAA"])
        self.assertEqual(res["actions"][0]["size_pct"], 2.0)
        self.assertEqual(res["actions"][0]["decision_source"], "fallback")
        self.assertTrue(any("fallback decision: Base only" in s["reason"] for s in res["skipped"]))


class TestEntryCounter(unittest.TestCase):
    def test_count_entries_today(self):
        tmp = tempfile.mkdtemp()
        try:
            with patch.dict(os.environ, {"TRADE_LOG_PATH": os.path.join(tmp, "t.json")}):
                import trade_log
                trade_log.log_entry(trade_id="a", symbol="A", dry_run=False)
                trade_log.log_entry(trade_id="b", symbol="B", dry_run=True)
                self.assertEqual(trade_log.count_entries_today(dry_run=False), 1)
                self.assertEqual(trade_log.count_entries_today(datetime.now(timezone.utc) + timedelta(days=2)), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class TestRailwayFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"DECISIONS_DIR": self.tmp, "DIM_LEDGER_PATH": os.path.join(self.tmp, "l.db")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cd(self, **kw):
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "stale": False, "candidates": [
            {"symbol": "MON", "type": "Base", "is_base": True}, {"symbol": "TIA", "type": "Chase", "is_chase": True}], **kw}

    def test_build(self):
        import serve
        fb, why = serve.build_fallback_decisions(self._cd())
        self.assertEqual(why, "")
        self.assertEqual({d["symbol"]: (d["decision"], d["size_pct"]) for d in fb},
                         {"MON": ("approve", 2.0), "TIA": ("veto", 0)})
        self.assertEqual(serve.build_fallback_decisions(self._cd(stale=True))[1], "candidates stale")
        old = self._cd(generated_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        self.assertEqual(serve.build_fallback_decisions(old)[1], "candidates not generated today")

    def test_job_stores_only_without_claude(self):
        import serve
        from decisions import get_decisions_for_today, store_decisions
        with patch.object(serve, "_load_candidates_file", lambda: self._cd()), patch.object(serve, "AUTO_FALLBACK", True):
            prev = serve._scheduled_fallback(manual=True)
            self.assertEqual(prev["status"], "preview")
            self.assertEqual(get_decisions_for_today(), {})
            res = serve._scheduled_fallback()
            self.assertEqual(res["status"], "success", res)
            d = get_decisions_for_today()
            self.assertEqual((d["MON"]["source"], d["MON"]["decision"], d["TIA"]["decision"]), ("fallback", "approve", "veto"))
            self.assertEqual(serve._scheduled_fallback()["status"], "skipped")
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        store_decisions([{"coin": "MON", "action": "VETO"}], source="claude")
        with patch.object(serve, "_load_candidates_file", lambda: self._cd()):
            self.assertIn("Claude decisions present", serve._scheduled_fallback()["message"])

    def test_auto_fallback_off_does_not_store(self):
        import serve
        from decisions import get_decisions_for_today
        with patch.object(serve, "_load_candidates_file", lambda: self._cd()), patch.object(serve, "AUTO_FALLBACK", False):
            self.assertEqual(serve._scheduled_fallback()["status"], "preview")
        self.assertEqual(get_decisions_for_today(), {})
