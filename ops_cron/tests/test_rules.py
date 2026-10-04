"""ok/problem rules for the exit monitor and the daily audit, on fixture payloads shaped like the real endpoints
(cockpit /api/exit/health, /api/scheduler/status, /api/exec/run-report, /api/exec/pending, /api/bx/status,
/api/bx/day and the public HL info API). No network."""
import copy
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ops_cron import rules

FIX = Path(__file__).resolve().parent / "fixtures"
CFG = {"bx_enabled": True, "lookback_min": 65, "daily_summary_hour_hkt": 20, "expect_bx_live": None,
       "slippage_max_bp": 60.0, "bx_country": "SG", "bx_region_prefix": "asia-southeast1", "window_h": 24}


def load(name):
    fx = json.loads((FIX / name).read_text(encoding="utf-8"))
    return copy.deepcopy(fx["inputs"]), datetime.fromisoformat(fx["now"])


def codes(rep):
    return sorted((p["code"], p["coin"]) for p in rep["problems"])


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class TestExitMonitor(unittest.TestCase):
    def setUp(self):
        self.inp, self.now = load("exit_ok.json")
        self.bx = self.inp["bx_status"]["data"]

    def run_(self, **cfg):
        return rules.exit_monitor(self.inp, self.now, {**CFG, **cfg})

    def test_ok_fixture(self):
        r = self.run_()
        self.assertEqual(r["status"], "ok", r["problems"])
        self.assertIsNone(r["notify"])
        self.assertEqual(r["hl"]["n_positions"], 1)
        self.assertEqual(r["hl"]["entries_today"], 1)
        self.assertEqual([c["coin"] for c in r["hl"]["closes_since_last_run"]], ["TIA"])
        self.assertEqual([c["bx_symbol"] for c in r["bx"]["closes_since_last_run"]], ["BARUSDT"])
        self.assertTrue(r["summary"].startswith("OK · HL 1 倉 · BX 1 倉 · BX_LIVE=1"))

    def test_daily_ok_summary_only_in_the_summary_hour(self):
        self.assertEqual(self.run_(daily_summary_hour_hkt=12)["notify"], "daily_ok")
        self.assertIsNone(self.run_(daily_summary_hour_hkt=20)["notify"])

    def test_problem_notifies_even_in_summary_hour(self):
        self.inp["hl_orders"]["data"] = []
        self.assertEqual(self.run_(daily_summary_hour_hkt=12)["notify"], "problem")

    def test_position_without_stop(self):
        self.inp["hl_orders"]["data"] = [dict(self.inp["hl_orders"]["data"][0], coin="BTC")]
        r = self.run_()
        self.assertEqual(r["status"], "problem")
        self.assertIn(("NO_SL", "SOL"), codes(r))
        self.assertIn("1 problem(s): NO_SL/SOL", r["summary"])

    def test_orders_unavailable_is_data_problem_not_no_sl(self):
        self.inp["hl_orders"] = {"ok": False, "error": "TimeoutError: timed out", "data": None}
        self.assertEqual(codes(self.run_()), [("DATA_UNAVAILABLE", "hl_orders")])

    def test_endpoint_error_or_timeout(self):
        self.inp["exit_health"] = {"ok": False, "error": "HTTP 403: forbidden", "http": 403, "data": None}
        self.inp["bx_status"] = {"ok": False, "error": "HTTP 502: bx-exec unreachable", "http": 502, "data": None}
        r = self.run_()
        self.assertEqual(codes(r), [("DATA_UNAVAILABLE", "bx_status"), ("DATA_UNAVAILABLE", "exit_health")])
        self.assertEqual(r["sources"]["exit_health"], "error: HTTP 403: forbidden")

    def test_job_stale_beyond_interval_plus_grace(self):
        jobs = self.inp["scheduler"]["data"]["jobs"]
        jobs["1h_scan_exits"]["last_run"] = (self.now - timedelta(minutes=76)).isoformat()
        del jobs["executor"]
        r = self.run_()
        self.assertEqual(codes(r), [("JOB_STALE", "1h_scan_exits"), ("JOB_STALE", "executor")])

    def test_job_within_grace_is_ok(self):
        self.inp["scheduler"]["data"]["jobs"]["1h_scan_exits"]["last_run"] = (self.now - timedelta(minutes=74)).isoformat()
        self.assertEqual(self.run_()["status"], "ok")

    def test_scheduler_disabled(self):
        self.inp["scheduler"]["data"] = {"enabled": False, "message": "Scheduler is disabled (SCHEDULER_ENABLED=0)"}
        self.assertEqual(codes(self.run_()), [("SCHEDULER_DISABLED", None)])

    def test_exit_health_passthrough_and_superseded_job_missed(self):
        self.inp["exit_health"]["data"].update(ok=False, problems=[
            {"code": "EXIT_NOT_DONE", "coin": "SOL", "msg": "small: 1H close < 1H Lower ..."},
            {"code": "JOB_MISSED", "coin": None, "msg": "1h_scan_exits last ran ..."},
            {"code": "MARGIN_HIGH", "coin": None, "msg": "margin used 85% of NAV > 80%"}])
        self.assertEqual(codes(self.run_()), [("EXIT_NOT_DONE", "SOL"), ("MARGIN_HIGH", None)])

    def test_hl_entry_cap(self):
        t = ms("2026-10-04T01:00:00+00:00")
        self.inp["hl_fills"]["data"] += [{"coin": c, "px": "1", "sz": "1", "time": t + i, "startPosition": "0",
                                          "dir": "Open Long", "closedPnl": "0", "oid": 900 + i, "fee": "0"}
                                         for i, c in enumerate(("A", "B", "C"))]
        self.assertIn(("HL_ENTRY_CAP", None), codes(self.run_()))

    def test_partial_fills_of_one_order_count_once(self):
        t = ms("2026-10-04T01:00:00+00:00")
        self.inp["hl_fills"]["data"] += [{"coin": "A", "px": "1", "sz": "1", "time": t + i, "startPosition": str(i),
                                          "dir": "Open Long", "closedPnl": "0", "oid": 900, "fee": "0"} for i in range(5)]
        r = self.run_()
        self.assertEqual(r["hl"]["entries_today"], 2)
        self.assertEqual(r["status"], "ok")

    def test_bx_breaker_tripped(self):
        self.bx["breaker"] = {"tripped": True, "at": "2026-10-04T03:05:00+00:00", "pnl_usd": -151.0, "pct_nav": -3.02}
        self.inp["exit_health"]["data"]["problems"] = [{"code": "BX_BREAKER", "coin": None, "msg": "tripped"}]
        r = self.run_()
        self.assertEqual(codes(r), [("BX_BREAKER", None)])   # deduplicated across sources

    def test_bx_realised_loss_beyond_limit_without_trip(self):
        self.bx["realized_pnl_usd"] = -150.01   # -3% of 5000 = -150
        self.assertEqual(codes(self.run_()), [("BX_BREAKER_NOT_TRIPPED", None)])
        self.bx["realized_pnl_usd"] = -149.0
        self.assertEqual(self.run_()["status"], "ok")

    def test_bx_position_count_and_daily_entry_cap(self):
        today = "2026-10-04T00:56:00+00:00"
        self.bx["open"] += [{"bx_symbol": "AUSDT", "entry_time": today, "entry_px": 1, "hard_sl": 0.9},
                            {"bx_symbol": "BUSDT", "entry_time": today, "entry_px": 1, "hard_sl": 0.9}]
        self.assertEqual(codes(self.run_()), [("BX_ENTRY_CAP", None), ("BX_MAX_OPEN", None)])

    def test_bx_limits_come_from_the_status_rules(self):
        self.bx["rules"]["max_open"] = 0
        self.assertIn(("BX_MAX_OPEN", None), codes(self.run_()))

    def test_bx_position_without_sl(self):
        self.bx["open"][0]["hard_sl"] = None
        self.assertEqual(codes(self.run_()), [("BX_NO_SL", "FOOUSDT")])

    def test_bx_exec_reported_problems_pass_through(self):
        self.bx["problems"] = [{"code": "BX_MANAGE_ERROR", "coin": "FOOUSDT", "msg": "timeout"}]
        self.assertEqual(codes(self.run_()), [("BX_MANAGE_ERROR", "FOOUSDT")])

    def test_bx_live_but_blocked(self):
        self.bx.update(live_ready=False, live_blockers=["egress not verified non-US: country unknown"],
                       egress={"ok": False, "reason": "country unknown"})
        self.assertEqual(codes(self.run_()), [("BX_EGRESS", None), ("BX_LIVE_BLOCKED", None)])

    def test_bx_live_off_is_reported_not_a_problem(self):
        self.bx.update(bx_live=False, live_ready=False, live_blockers=["BX_LIVE=0 (shadow only)"])
        r = self.run_()
        self.assertEqual(r["status"], "ok")
        self.assertIn("BX_LIVE=0", r["summary"])

    def test_bx_job_error_and_stale(self):
        self.bx["jobs"]["4h"] = {"at": "2026-10-04T04:05:00+00:00", "status": "error", "error_steps": ["live"]}
        self.bx["jobs"]["1h"]["at"] = (self.now - timedelta(minutes=80)).isoformat()
        self.assertEqual(codes(self.run_()), [("BX_JOB_ERROR", "4h"), ("BX_JOB_STALE", "1h")])

    def test_bx_job_unknown_after_restart_is_info(self):
        self.bx["jobs"] = {}
        r = self.run_()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(sorted(i["coin"] for i in r["info"] if i["code"] == "BX_JOB_UNKNOWN"),
                         ["1h", "4h", "daily", "entries"])

    def test_bx_unexpected_shape(self):
        self.inp["bx_status"]["data"] = {"ok": True, "compare": {}}   # cockpit without BX_SERVICE_URL
        self.assertEqual(codes(self.run_()), [("DATA_UNAVAILABLE", "bx_status")])

    def test_bx_disabled(self):
        del self.inp["bx_status"]
        r = self.run_(bx_enabled=False)
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("bx_status", r["sources"])

    def test_markdown(self):
        self.inp["hl_orders"]["data"] = []
        md = rules.to_markdown(self.run_())
        self.assertIn("**PROBLEM**", md)
        self.assertIn("`NO_SL` SOL", md)


class TestDailyAudit(unittest.TestCase):
    def setUp(self):
        self.inp, self.now = load("audit_ok.json")
        self.bx = self.inp["bx_status"]["data"]
        self.today = self.inp["run_report_today"]["data"]

    def run_(self, **cfg):
        return rules.daily_audit(self.inp, self.now, {**CFG, **cfg})

    def test_ok_fixture(self):
        r = self.run_()
        self.assertEqual(r["status"], "ok", r["problems"])
        self.assertIsNone(r["notify"])
        self.assertTrue(r["bx"]["singapore_egress_ok"])
        self.assertEqual((r["hl"]["n_open_orders"], r["hl"]["n_close_orders"]), (2, 1))
        self.assertAlmostEqual(r["hl"]["realized_pnl_24h_usd"], 14.58)
        self.assertAlmostEqual(r["bx"]["realized_pnl_24h_usd"], -3.2)
        fills = {f["symbol"]: f for f in r["trade_review"]["fills"]}
        self.assertEqual(sorted(fills), ["BAZUSDT", "ETH", "SOL"])
        self.assertEqual(fills["SOL"]["slippage_bp"], 20.0)
        self.assertTrue(fills["ETH"]["zone_position"].startswith("above_filter"))
        self.assertEqual(sorted((m["venue"], m["symbol"], m["why"]) for m in r["trade_review"]["missed_entries"]),
                         [("BX", "QUXUSDT", "min_sl_distance"), ("HL", "ARB", "pending_expired"),
                          ("HL", "DOGE", "min_sl_distance"), ("HL", "LINK", "min_sl_distance")])
        (ex,) = r["trade_review"]["exits"]
        self.assertEqual((ex["coin"], ex["entry_px"], ex["exit_px"]), ("AVAX", 30.0, 31.5))
        self.assertEqual((ex["mfe_pct"], ex["efficiency"]), (10.0, 0.5))

    def test_round_trips_split_per_flat_and_partial_close(self):
        def f(t, d, px, sz, start, pnl="0"):
            return {"coin": "X", "px": str(px), "sz": str(sz), "time": ms(t), "dir": d, "startPosition": str(start),
                    "closedPnl": pnl, "fee": "0", "oid": t}
        fills = [f("2026-10-03T02:00:00+00:00", "Open Long", 10, 2, 0),
                 f("2026-10-03T03:00:00+00:00", "Close Long", 11, 2, 2, "2"),
                 f("2026-10-03T05:00:00+00:00", "Open Long", 20, 1, 0),
                 f("2026-10-03T06:00:00+00:00", "Open Long", 22, 1, 1),
                 f("2026-10-03T07:00:00+00:00", "Close Long", 23, 1, 2, "2")]
        trips = rules.hl_round_trips(fills, datetime.fromisoformat("2026-10-03T01:00:00+00:00"))
        self.assertEqual([(t["entry_px"], t["exit_px"], t["closed"]) for t in trips], [(10.0, 11.0, True), (21.0, 23.0, False)])
        self.assertIn("partial close", trips[1]["note"])

    def test_candle_requests(self):
        (req,) = rules.candle_requests(self.inp["hl_fills"], self.now)
        self.assertEqual(req["coin"], "AVAX")
        self.assertEqual(req["end_ms"], ms("2026-10-03T20:07:40+00:00"))

    def test_base_fill_not_above_upper_and_sl_too_close(self):
        self.today["executed"][0].update(upper_ref=151.0, hard_sl=149.0)
        (p,) = self.run_()["problems"]
        self.assertEqual((p["code"], p["coin"]), ("RULE_VIOLATION", "SOL"))
        self.assertIn("not above the 1D Upper", p["msg"])
        self.assertIn("< 1.5%", p["msg"])

    def test_pending_fill_below_zone_lower(self):
        self.inp["run_report_yesterday"]["data"]["executed"][0]["zone"] = [2510.0, 2550.0]
        r = self.run_()
        self.assertEqual(codes(r), [("RULE_VIOLATION", "ETH")])
        eth = next(f for f in r["trade_review"]["fills"] if f["symbol"] == "ETH")
        self.assertEqual(eth["zone_position"], "below_lower")

    def test_leverage_outside_band(self):
        self.today["executed"][0]["leverage"] = 7
        self.assertEqual(codes(self.run_()), [("RULE_VIOLATION", "SOL")])

    def test_slippage_high(self):
        self.today["executed"][0]["mid"] = 149.0     # 150.2 vs 149.0 = 80.5 bp
        self.assertEqual(codes(self.run_()), [("SLIPPAGE_HIGH", "SOL")])
        self.assertEqual(self.run_(slippage_max_bp=100)["status"], "ok")

    def test_dry_run_and_out_of_window_fills_are_not_reviewed(self):
        self.today["executed"][0].update(dry_run=True, leverage=9)
        self.inp["run_report_yesterday"]["data"]["executed"][0].update(run_ts="2026-10-03T01:00:00+00:00", leverage=9)
        r = self.run_()
        self.assertEqual(r["status"], "ok")
        self.assertEqual([f["symbol"] for f in r["trade_review"]["fills"]], ["BAZUSDT"])

    def test_bx_sl_not_confirmed(self):
        self.inp["bx_day"]["data"]["orders"][0]["sl_confirmed"] = False
        self.assertEqual(codes(self.run_()), [("RULE_VIOLATION", "BAZUSDT")])

    def test_bx_egress_not_singapore(self):
        self.bx["egress"] = {"ok": True, "countries": {"ipinfo": "JP", "ipapi": "JP"}, "region": "asia-northeast1"}
        self.assertEqual(codes(self.run_()), [("BX_EGRESS", None)])

    def test_bx_breaker_tripped(self):
        self.bx["breaker"] = {"tripped": True, "at": "x", "pnl_usd": -160, "pct_nav": -3.2}
        self.assertIn(("BX_BREAKER", None), codes(self.run_()))

    def test_expect_bx_live(self):
        self.bx.update(bx_live=False, live_ready=False, live_blockers=["BX_LIVE=0 (shadow only)"])
        self.assertEqual(self.run_()["status"], "ok")
        self.assertEqual(codes(self.run_(expect_bx_live=True)), [("BX_LIVE_OFF", None)])

    def test_bx_day_missing_is_info(self):
        self.inp["bx_day"] = {"ok": False, "http": 404, "error": "HTTP 404: no BX day report", "data": None}
        r = self.run_()
        self.assertEqual(r["status"], "ok")
        self.assertIn("BX_DAY_MISSING", [i["code"] for i in r["info"]])

    def test_run_report_unavailable(self):
        self.inp["run_report_today"] = {"ok": False, "http": 404, "error": "HTTP 404", "data": None}
        self.assertEqual(codes(self.run_()), [("DATA_UNAVAILABLE", "run_report_today")])

    def test_missing_candles_do_not_fail(self):
        self.inp["candles"] = {}
        r = self.run_()
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["trade_review"]["exits"][0]["efficiency"])

    def test_markdown(self):
        md = rules.to_markdown(self.run_())
        self.assertIn("Daily live audit", md)
        self.assertIn("| HL | SOL | Base | 150.2 | 149.9 | 20.0 |", md)
        self.assertIn("efficiency 0.5", md)


if __name__ == "__main__":
    unittest.main()
