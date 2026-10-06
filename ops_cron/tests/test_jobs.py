"""Mocked-HTTP coverage for every ops_cron job. No network, no trading."""
import io
import json
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from ops_cron import bo_report, decider, desk_missing, gc_levels, harbor, hlparse, main as ops_main, rules, stops
from ops_cron import river_jobs

ENV = {"COCKPIT_URL": "https://cockpit.example.invalid", "COCKPIT_AI_KEY": "ai-key-SECRET-123",
       "HL_ADDRESS": "0x0000000000000000000000000000000000000001",
       "TESTNET_WALLET_ADDRESS": "0x0000000000000000000000000000000000000002",
       "C48_NAV_START": "1000"}
NOW = datetime(2026, 10, 6, 1, 15, tzinfo=timezone.utc)  # 09:15 HKT Tuesday


def _bars(n, step, start=1_700_000_000_000, px=100.0):
    out = []
    for i in range(n):
        out.append({"t": start + i * step, "o": px, "h": px + 1, "l": px - 1, "c": px + i * 0.05})
    return out


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class FakeHTTP:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if getattr(req, "data", None) else None
        self.requests.append({"url": req.full_url, "method": req.get_method(), "headers": dict(req.header_items()),
                              "body": body})
        url = req.full_url
        if "cockpit.example" in url:
            if "/api/public/radar" in url:
                data = {"gc_radar_1d": {"rows": [{"symbol": "SOL", "dual_cross_up": True}]},
                        "gc_radar_4h": {"rows": []}, "gc_radar_1h": {"rows": []}}
            elif "run-report" in url:
                data = {"ok": True, "date": "2026-10-06", "runs": [], "executed": [], "skipped": [], "failed": [],
                        "decisions": {"ok": True, "posted": False, "count": 0, "records": [], "history": []}}
            elif url.endswith("/api/bx/status"):
                data = {"ok": True, "bx_live": False, "breaker": {"tripped": False}, "open": [], "rules": {}}
            else:
                data = {"ok": True}
        elif "bitunix" in url:
            data = {"code": 0, "data": [{"time": 1_700_000_000_000 + i * 3_600_000, "open": "1", "high": "2",
                                          "low": "0.5", "close": "1.2"} for i in range(60)]}
        else:
            t = (body or {}).get("type")
            if t == "clearinghouseState":
                data = {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "10", "totalRawUsd": "1000"},
                        "assetPositions": []}
            elif t == "allMids":
                data = {"BTC": "100"}
            elif t == "spotClearinghouseState":
                data = {"balances": [{"coin": "USDC", "total": "12.5", "hold": "0"}]}
            elif t == "candleSnapshot":
                data = _bars(80, 3_600_000)
            else:
                data = []
        return _Resp(data)


def _run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), patch("urllib.request.urlopen", FakeHTTP()):
        rc = ops_main.main(argv, env or ENV)
    return rc, out.getvalue(), err.getvalue()


class TestStops(unittest.TestCase):
    def test_distance_and_loss(self):
        lv = stops.sl_distance(100, 95, 98, 2)
        self.assertAlmostEqual(lv["distance_pct"], 5.0)
        self.assertAlmostEqual(lv["pnl_if_hit"], -6.0)
        self.assertIs(harbor.stops.sl_distance, stops.sl_distance)
        self.assertIs(bo_report.stops.sl_distance, stops.sl_distance)

    def test_harbor_and_cove_share_the_same_levels(self):
        now_ms = 1_700_000_000_000 + 90 * 3_600_000
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        c1 = _bars(80, 3_600_000)
        c4 = _bars(90, 14_400_000)
        state = {"ok": True, "data": {"marginSummary": {"accountValue": "1000", "totalRawUsd": "1000",
                                                        "totalMarginUsed": "20"},
                                      "assetPositions": [{"position": {
                                          "coin": "SOL", "szi": "2", "entryPx": "100", "unrealizedPnl": "4",
                                          "liquidationPx": "50", "leverage": {"type": "isolated", "value": 3},
                                          "positionValue": "210"}}]}}
        orders = {"ok": True, "data": [{"coin": "SOL", "side": "A", "sz": "2", "isTrigger": True,
                                         "triggerPx": "90", "reduceOnly": True}]}
        candles = {"SOL": {"1h": {"ok": True, "data": c1}, "4h": {"ok": True, "data": c4}}}
        mids = {"ok": True, "data": {"SOL": "105"}}
        dec = {"ok": True, "data": {"decisions": {"ok": True, "posted": True, "records": [
            {"symbol": "SOL", "decision": "approve", "source": "fallback", "type": "BASE",
             "timestamp": "2026-10-06T00:10:00+00:00"}], "history": []}}}
        h = harbor.build({"day": "2026-10-06", "hl_state": state, "hl_orders": orders, "all_mids": mids,
                          "fills_7d": {"ok": True, "data": []}, "fills_all": {"ok": True, "data": []},
                          "funding_7d": {"ok": True, "data": []}, "run_report": dec, "pos_candles": candles,
                          "bx_status": {"ok": False, "error": "down"}}, now, {})
        b = bo_report.build({"day": "2026-10-06", "hl_state": state, "hl_orders": orders, "all_mids": mids,
                             "fills": {"ok": True, "data": []}, "run_report": dec, "pos_candles": candles}, now, {})
        self.assertEqual(h["positions"][0]["stops"]["soft"]["price"], b["markdown"].count("1H Lower") and
                         h["positions"][0]["stops"]["soft"]["price"])
        hs, hb = h["positions"][0]["stops"], stops.both_stops(105, 100, 2, c1, c4, int(now.timestamp() * 1000))
        self.assertEqual(hs["soft"]["price"], hb["soft"]["price"])
        self.assertEqual(hs["hard"]["price"], hb["hard"]["price"])
        self.assertIn(f"{hs['soft']['price']:.6g}", b["markdown"])
        self.assertIn("未知", h["markdown"])  # BX / prop / stocks have no number

    def test_gc_matches_scanner(self):
        import scan_gc_radar
        highs = [float(i) + 1 for i in range(60)]
        lows = [float(i) for i in range(60)]
        closes = [float(i) + 0.4 for i in range(60)]
        a = gc_levels.compute_gc(highs, lows, closes, 48)
        b = scan_gc_radar.compute_gc(highs, lows, closes, period=48, reduced_lag=False, fast_response=False)
        self.assertAlmostEqual(a[-1]["filter"], b[-1]["filter"])
        self.assertAlmostEqual(a[-1]["lower"], b[-1]["lower"])


class TestDeskMissing(unittest.TestCase):
    def test_exact_alert_when_no_post(self):
        rep = desk_missing.build({"run_report": {"ok": True, "data": {"decisions": {
            "ok": True, "posted": False, "records": []}}}}, NOW, {})
        self.assertEqual(rep["summary"], desk_missing.ALERT)
        self.assertEqual(rep["notify"], "problem")

    def test_silent_when_a_desk_post_exists(self):
        rep = desk_missing.build({"run_report": {"ok": True, "data": {"decisions": {
            "ok": True, "records": [{"symbol": "SOL", "source": "claude", "timestamp": "2026-10-06T00:10:00+00:00"}]}}}},
            NOW, {})
        self.assertIsNone(rep["notify"])
        self.assertEqual(rep["status"], "ok")

    def test_fallback_alone_is_not_a_claude_post(self):
        rep = desk_missing.build({"run_report": {"ok": True, "data": {"decisions": {
            "ok": True, "records": [{"symbol": "SOL", "source": "fallback"}]}}}}, NOW, {})
        self.assertEqual(rep["summary"], desk_missing.ALERT)


class TestDecider(unittest.TestCase):
    def test_claude_source_alone_is_unknown(self):
        dec = {"records": [{"symbol": "SOL", "source": "claude", "timestamp": "2026-10-06T00:10:00+00:00"}],
               "history": [{"symbol": "SOL", "source": "claude", "timestamp": "2026-10-06T00:10:00+00:00"}]}
        self.assertEqual(decider.decider_for_coin("SOL", dec, "2026-10-06"), "unknown")

    def test_source_is_not_an_actor(self):
        dec = {"history": [{"symbol": "SOL", "source": "fallback", "timestamp": "2026-10-06T00:40:00+00:00"}],
               "records": [{"symbol": "SOL", "source": "forge", "timestamp": "2026-10-06T00:40:00+00:00"}]}
        self.assertEqual(decider.decider_for_coin("SOL", dec, "2026-10-06"), "unknown")

    def test_explicit_actor_is_kept(self):
        dec = {"history": [{"symbol": "SOL", "actor": "Forge", "timestamp": "2026-10-06T00:10:00+00:00"}]}
        self.assertEqual(decider.decider_for_coin("SOL", dec, "2026-10-06"), "Forge")


class TestRiver(unittest.TestCase):
    def test_scoreboard_closed_window_is_silent(self):
        late = datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)
        rep = river_jobs.build_scoreboard({}, late, ENV)
        self.assertIsNone(rep["notify"])
        self.assertNotIn("brain_rows", rep)

    def test_scoreboard_flags_leverage_and_writes_a_row(self):
        state = {"ok": True, "data": {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "10"},
                                      "assetPositions": [{"position": {
                                          "coin": "BTC", "szi": "0.01", "entryPx": "100", "unrealizedPnl": "0",
                                          "positionValue": "50", "leverage": {"value": 5, "type": "isolated"}}}]}}
        rep = river_jobs.build_scoreboard({"state": state, "orders": {"ok": True, "data": [
            {"coin": "BTC", "isTrigger": True, "triggerPx": "90"}]}, "fills": {"ok": True, "data": []},
            "nav_start": "1000"}, NOW, ENV)
        self.assertEqual(rep["notify"], "problem")
        self.assertIn("C48_LEVERAGE", [p["code"] for p in rep["problems"]])
        self.assertEqual(rep["brain_rows"][0]["table"], "raw.river_c48_ft_score")
        sql_cols = rep["brain_rows"][0]["columns"]
        self.assertNotIn("UPDATE", " ".join(sql_cols).upper())

    def test_veto_row_shape_and_journal_decider(self):
        veto = river_jobs.build_veto({
            "day": "2026-10-06",
            "radar": {"ok": True, "data": {"gc_radar_1d": {"rows": [{"symbol": "SOL", "dual_cross_up": True}]},
                                           "gc_radar_4h": {"rows": []}}},
            "decisions": {"ok": True, "data": {"decisions": {"ok": True, "records": [
                {"symbol": "SOL", "decision": "veto", "source": "claude", "type": "BASE",
                 "timestamp": "2026-10-06T00:10:00+00:00"}], "history": [
                {"symbol": "SOL", "source": "claude", "timestamp": "2026-10-06T00:10:00+00:00"}]}}},
            "symbols": ["SOL", "BTC"],
            "candles": {"SOL": {"1h": {"ok": True, "data": _bars(80, 3_600_000)},
                                "4h": {"ok": True, "data": _bars(90, 14_400_000)}},
                        "BTC": {"1h": {"ok": True, "data": _bars(80, 3_600_000)},
                                "4h": {"ok": True, "data": _bars(90, 14_400_000)}}},
            "bx": {},
        }, datetime(2026, 10, 6, 1, 5, tzinfo=timezone.utc), {})
        row = veto["rows"][0]
        for key in ("coin", "strategy", "desk_decision", "decider", "ret_48h", "ret_48h_sl_aware",
                    "excess_vs_btc", "hard_sl_hit", "outcome"):
            self.assertIn(key, row)
        self.assertEqual(row["decider"], "unknown")
        self.assertEqual(row["desk_decision"], "veto")
        self.assertEqual(veto["brain_rows"][0]["table"], "raw.river_desk_veto")

    def test_journal_inserts_decider_and_alerts_on_hard_sl(self):
        # Flat candles so 4H filter sits near 100; a close at 1 is through it.
        bars = _bars(90, 3_600_000, px=100)
        bars4 = _bars(90, 14_400_000, px=100)
        close_ms = int(datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
        rep = river_jobs.build_journal({
            "day": "2026-10-06",
            "fills": {"ok": True, "data": [{"coin": "SOL", "px": "1", "sz": "1", "side": "A", "time": close_ms,
                                            "dir": "Close Long", "closedPnl": "-5", "fee": "0.1"}]},
            "state": {"ok": True, "data": {"assetPositions": []}},
            "orders": {"ok": True, "data": []},
            "mids": {"ok": True, "data": {}},
            "run_report": {"ok": True, "data": {"decisions": {"ok": True, "records": [
                {"symbol": "ETH", "decision": "approve", "source": "forge", "timestamp": "2026-10-06T00:05:00+00:00"}],
                "history": [{"symbol": "ETH", "source": "forge", "timestamp": "2026-10-06T00:05:00+00:00"}]}}},
            "candles": {"SOL": {"1h": {"ok": True, "data": bars}, "4h": {"ok": True, "data": bars4}},
                        "ETH": {"1h": {"ok": False}, "4h": {"ok": False}}},
        }, NOW, {})
        self.assertEqual(rep["brain_rows"][0]["table"], "raw.river_trade_log")
        deciders = [r["decider"] for r in rep["rows"]]
        self.assertTrue(deciders)
        self.assertTrue(all(d == "unknown" for d in deciders))
        self.assertEqual(rep["notify"], "problem")
        self.assertTrue(any(p["code"] == "HARD_SL_HIT" for p in rep["problems"]))


class TestEntrypoint(unittest.TestCase):
    def test_each_job_is_get_only_and_keeps_the_key_out_of_the_url(self):
        fake = FakeHTTP()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("urllib.request.urlopen", fake):
            for job in ("exit-monitor", "daily-audit", "desk-missing", "harbor-pnl", "bo-report",
                        "c48-scoreboard", "desk-veto", "trade-journal"):
                rc = ops_main.main([job, "--dry-run", "--now", NOW.isoformat()], ENV)
                self.assertEqual(rc, 0, job)
        for r in fake.requests:
            self.assertNotIn("ai-key-SECRET-123", r["url"])
            self.assertNotIn("key=", r["url"].lower())
            if "cockpit.example" in r["url"]:
                self.assertEqual(r["method"], "GET")
                if "/api/public/radar" not in r["url"]:
                    self.assertEqual(r["headers"].get("X-ai-key"), ENV["COCKPIT_AI_KEY"])
            if "hyperliquid" in r["url"]:
                self.assertIn(r["body"]["type"], {"clearinghouseState", "spotClearinghouseState",
                                                   "frontendOpenOrders", "userFills", "userFillsByTime",
                                                   "userFunding", "candleSnapshot", "allMids"})
        blob = out.getvalue() + err.getvalue()
        self.assertNotIn("ai-key-SECRET-123", blob)

    def test_readme_references_entry_read_key(self):
        from pathlib import Path
        text = Path("ops_cron/README.md").read_text(encoding="utf-8")
        self.assertIn("${{cockpit.ENTRY_READ_KEY}}", text)
        self.assertNotIn("cockpit.AI_DECISION_KEY", text)
        self.assertIn("python -m ops_cron.main", text)
        self.assertIn("HL_ADDRESS", text)


def _hl_book(mark="100", szi="2", order=None):
    state = {"ok": True, "data": {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "10"},
                                  "assetPositions": [{"position": {
                                      "coin": "SOL", "szi": szi, "entryPx": "100", "unrealizedPnl": "1",
                                      "positionValue": str(float(mark) * float(szi)),
                                      "leverage": {"type": "isolated", "value": 2}}}]}}
    orders = {"ok": True, "data": [] if order is None else [order]}
    mids = {"ok": True, "data": {"SOL": mark}}
    return state, orders, mids


def _sl_order(**over):
    order = {"coin": "SOL", "side": "A", "sz": "2", "isTrigger": True, "reduceOnly": True,
             "triggerPx": "95", "orderType": "Stop Market"}
    order.update(over)
    return order


class TestHardSL(unittest.TestCase):
    def test_tp_only_alerts(self):
        st = hlparse.hard_sl_status([_sl_order(triggerPx="110")], "SOL", "LONG", 2, 100)
        self.assertEqual(st["status"], "NO")
        state, orders, mids = _hl_book(order=_sl_order(triggerPx="110"))
        h = harbor.build({"day": "2026-10-06", "hl_state": state, "hl_spot": {"ok": False},
                          "hl_orders": orders, "all_mids": mids, "fills_7d": {"ok": True, "data": []},
                          "fills_all": {"ok": True, "data": []}, "funding_7d": {"ok": True, "data": []},
                          "run_report": {"ok": True, "data": {}}, "bx_status": {"ok": False}}, NOW, {})
        self.assertTrue(any(p["code"] == "NO_SL" for p in h["problems"]))
        self.assertIn("Hard SL: NO", h["markdown"])
        self.assertNotIn("Hard SL: YES", h["markdown"])

    def test_proper_sl_is_ok_with_distance(self):
        st = hlparse.hard_sl_status([_sl_order()], "SOL", "LONG", 2, 100)
        self.assertTrue(st["ok"])
        self.assertAlmostEqual(st["distance_pct"], 5.0)
        state, orders, mids = _hl_book(order=_sl_order())
        h = harbor.build({"day": "2026-10-06", "hl_state": state, "hl_spot": {"ok": True, "data": {
                          "balances": [{"coin": "USDC", "total": "12.5"}]}},
                          "hl_orders": orders, "all_mids": mids, "fills_7d": {"ok": True, "data": []},
                          "fills_all": {"ok": True, "data": []}, "funding_7d": {"ok": True, "data": []},
                          "run_report": {"ok": True, "data": {}}, "bx_status": {"ok": False}}, NOW, {})
        self.assertFalse(any(p["code"] == "NO_SL" for p in h["problems"]))
        self.assertIn("Hard SL: YES 5.00% from mark", h["markdown"])

    def test_sl_on_the_winning_side_alerts(self):
        st = hlparse.hard_sl_status([_sl_order(triggerPx="110")], "SOL", "LONG", 2, 100)
        self.assertFalse(st["ok"])
        state, orders, mids = _hl_book(order=_sl_order(triggerPx="110"))
        rep = rules.exit_monitor(_exit_with(orders["data"], mids["data"]), NOW, {"bx_enabled": False, "lookback_min": 65})
        self.assertIn(("NO_SL", "SOL"), [(p["code"], p["coin"]) for p in rep["problems"]])

    def test_non_reduce_only_stop_alerts(self):
        order = _sl_order(reduceOnly=False, isPositionTpsl=False)
        st = hlparse.hard_sl_status([order], "SOL", "LONG", 2, 100)
        self.assertFalse(st["ok"])
        state, orders, mids = _hl_book(order=order)
        h = harbor.build({"day": "2026-10-06", "hl_state": state, "hl_orders": orders, "all_mids": mids,
                          "fills_7d": {"ok": True, "data": []}, "fills_all": {"ok": True, "data": []},
                          "funding_7d": {"ok": True, "data": []}, "run_report": {"ok": True, "data": {}},
                          "bx_status": {"ok": False}}, NOW, {})
        self.assertTrue(any(p["code"] == "NO_SL" for p in h["problems"]))

    def test_daily_audit_uses_the_same_helper(self):
        tp = _sl_order(triggerPx="110")
        good = _sl_order()
        self.assertTrue(any(p["code"] == "NO_SL" for p in rules.daily_audit(_audit_with([tp]), NOW, {"bx_enabled": True})["problems"]))
        self.assertFalse(any(p["code"] == "NO_SL" for p in rules.daily_audit(_audit_with([good]), NOW, {"bx_enabled": True})["problems"]))


class TestSpotUsdc(unittest.TestCase):
    def test_spot_failure_is_unknown_not_zero_or_perp(self):
        state, orders, mids = _hl_book(order=_sl_order())
        h = harbor.build({"day": "2026-10-06", "hl_state": state, "hl_spot": {"ok": False, "error": "down"},
                          "hl_orders": orders, "all_mids": mids, "fills_7d": {"ok": True, "data": []},
                          "fills_all": {"ok": True, "data": []}, "funding_7d": {"ok": True, "data": []},
                          "run_report": {"ok": True, "data": {}}, "bx_status": {"ok": False}}, NOW, {})
        self.assertIn("spot USDC unknown", h["markdown"])
        self.assertIn("perp accountValue 1,000.00", h["markdown"])
        self.assertNotIn("spot USDC 0", h["markdown"])
        self.assertNotIn("spot USDC 1,000.00", h["markdown"])

    def test_bo_nav_unreadable_is_unknown(self):
        rep = bo_report.build({"day": "2026-10-06", "hl_state": {"ok": False, "error": "down"},
                               "hl_spot": {"ok": False, "error": "down"},
                               "hl_orders": {"ok": True, "data": []}, "all_mids": {"ok": True, "data": {}},
                               "fills": {"ok": True, "data": []},
                               "run_report": {"ok": True, "data": {"decisions": {"ok": True, "records": [
                                   {"symbol": "BTC", "source": "claude", "timestamp": "2026-10-06T00:10:00+00:00"}]}}}},
                              NOW, {})
        self.assertIn("perp accountValue unknown", rep["markdown"])
        self.assertIn("spot USDC unknown", rep["markdown"])
        self.assertNotIn("accountValue 0", rep["markdown"])

    def test_bo_tp_only_is_no_and_proper_sl_prints_distance(self):
        state, orders, mids = _hl_book(order=_sl_order(triggerPx="110"))
        spot = {"ok": True, "data": {"balances": [{"coin": "USDC", "total": "4"}]}}
        common = {"day": "2026-10-06", "hl_state": state, "hl_spot": spot, "all_mids": mids,
                  "fills": {"ok": True, "data": []},
                  "run_report": {"ok": True, "data": {"decisions": {"ok": True, "posted": True, "records": [
                      {"symbol": "SOL", "source": "claude", "timestamp": "2026-10-06T00:10:00+00:00"}]}}}}
        tp = bo_report.build({**common, "hl_orders": orders}, NOW, {})
        self.assertIn("Hard SL: NO", tp["markdown"])
        self.assertNotIn("Hard SL: YES", tp["markdown"])
        good = bo_report.build({**common, "hl_orders": {"ok": True, "data": [_sl_order()]}}, NOW, {})
        self.assertIn("Hard SL: YES 5.00% from mark", good["markdown"])
        self.assertIn("spot USDC 4.00", good["markdown"])
        self.assertIn("perp accountValue 1,000.00", good["markdown"])


class TestHlAddress(unittest.TestCase):
    def test_every_job_alerts_when_the_address_is_missing(self):
        env = {k: v for k, v in ENV.items() if k != "HL_ADDRESS"}
        jobs = ("exit-monitor", "daily-audit", "desk-missing", "harbor-pnl", "bo-report",
                "c48-scoreboard", "desk-veto", "trade-journal")
        for job in jobs:
            with patch("ops_cron.alerts.send", return_value={"telegram": "sent"}) as send, \
                    patch("urllib.request.urlopen", side_effect=AssertionError(job)):
                rc = ops_main.main([job, "--now", NOW.isoformat()], env)
            self.assertEqual(rc, 0, job)
            text = send.call_args.args[1]
            self.assertIn("HL_ADDRESS not set", send.call_args.args[0])
            self.assertIn("unknown", text)


class TestRiverFixes(unittest.TestCase):
    def test_veto_measures_48h_after_the_d2_signal(self):
        signal = datetime(2026, 10, 4, 0, 55, tzinfo=timezone.utc)
        now = datetime(2026, 10, 6, 1, 5, tzinfo=timezone.utc)
        sig_ms = int(signal.timestamp() * 1000)
        fwd_end = sig_ms + 48 * 3_600_000
        start = signal - timedelta(hours=60)
        bars = []
        t = int(start.timestamp() * 1000)
        while t < int(now.timestamp() * 1000):
            close_at = t + 3_600_000
            if close_at <= sig_ms:
                c = 100.0 if close_at > sig_ms - 48 * 3_600_000 else 50.0
            elif close_at <= fwd_end:
                c = 110.0
            else:
                c = 300.0
            bars.append({"t": t, "o": c, "h": c, "l": c, "c": c})
            t += 3_600_000
        before = river_jobs._ret_forward(bars, sig_ms - 48 * 3_600_000, sig_ms)
        after = river_jobs._ret_forward(bars, sig_ms, min(int(now.timestamp() * 1000), fwd_end))
        self.assertAlmostEqual(before, 1.0)
        self.assertAlmostEqual(after, 0.1)
        self.assertNotAlmostEqual(before, after)
        veto = river_jobs.build_veto({
            "day": "2026-10-06",
            "signal_day": "2026-10-04",
            "radar": {"ok": True, "data": {"gc_radar_1d": {"rows": [{"symbol": "ETH", "dual_cross_up": True}]}}},
            "decisions": {"ok": True, "data": {"decisions": {"ok": True, "records": [
                {"symbol": "SOL", "decision": "veto", "source": "claude", "type": "BASE",
                 "timestamp": "2026-10-04T00:55:00+00:00"}]}}},
            "candles": {"SOL": {"1h": {"ok": True, "data": bars}, "4h": {"ok": False}},
                        "BTC": {"1h": {"ok": True, "data": bars}, "4h": {"ok": False}}},
            "bx": {},
        }, now, {})
        coins = [r["coin"] for r in veto["rows"]]
        self.assertEqual(coins, ["SOL"])
        self.assertNotIn("ETH", coins)
        self.assertAlmostEqual(veto["rows"][0]["ret_48h"], after)
        self.assertNotAlmostEqual(veto["rows"][0]["ret_48h"], before)
        self.assertEqual(veto["rows"][0]["decider"], "unknown")

    def test_journal_window_and_size_signs(self):
        self.assertEqual(river_jobs.journal_window_start(NOW, None), NOW - timedelta(hours=26))
        last = datetime(2026, 10, 5, 1, 15, tzinfo=timezone.utc)
        self.assertEqual(river_jobs.journal_window_start(NOW, last), last)
        late_yesterday = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
        self.assertGreater(late_yesterday, last)
        self.assertEqual(river_jobs.signed_fill_size({"sz": "2", "dir": "Open Long", "side": "B"}), 2)
        self.assertEqual(river_jobs.signed_fill_size({"sz": "3", "dir": "Open Short", "side": "A"}), -3)
        rep = river_jobs.build_journal({
            "day": "2026-10-06",
            "fills": {"ok": True, "data": [
                {"coin": "SOL", "px": "10", "sz": "2", "side": "B", "time": int(NOW.timestamp() * 1000),
                 "dir": "Open Long", "closedPnl": "0", "fee": "0"},
                {"coin": "ETH", "px": "10", "sz": "3", "side": "A", "time": int(NOW.timestamp() * 1000),
                 "dir": "Open Short", "closedPnl": "0", "fee": "0"}]},
            "state": {"ok": True, "data": {"assetPositions": []}},
            "orders": {"ok": True, "data": []},
            "mids": {"ok": True, "data": {}},
            "run_report": {"ok": True, "data": {"decisions": {"ok": True, "records": [], "history": []}}},
            "candles": {},
        }, NOW, {})
        sizes = {r["coin"]: r["size"] for r in rep["rows"]}
        self.assertEqual(sizes["SOL"], 2)
        self.assertEqual(sizes["ETH"], -3)
        self.assertTrue(all(r["decider"] == "unknown" for r in rep["rows"]))

    def test_scoreboard_includes_the_2040_hkt_run(self):
        t = datetime(2026, 10, 7, 12, 40, tzinfo=timezone.utc)
        rep = river_jobs.build_scoreboard({
            "state": {"ok": True, "data": {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "1"},
                                           "assetPositions": []}},
            "orders": {"ok": True, "data": []},
            "fills": {"ok": True, "data": []},
            "nav_start": "1000",
        }, t, ENV)
        self.assertIn("brain_rows", rep)
        self.assertNotIn("window closed", rep["summary"])


def _exit_with(orders, mids):
    fx = json.loads(Path("ops_cron/tests/fixtures/exit_ok.json").read_text(encoding="utf-8"))
    data = fx["inputs"]
    data["hl_orders"]["data"] = orders
    data["all_mids"]["data"] = mids
    data.pop("bx_status", None)
    return data


def _audit_with(orders):
    bx = {"ok": True, "breaker": {"tripped": False}, "open": [], "bx_live": False, "rules": {},
          "jobs": {}, "problems": [], "egress": {"ok": True}}
    return {
        "bx_status": {"ok": True, "data": bx},
        "bx_day": {"ok": True, "data": {"orders": [], "skipped": []}},
        "run_report_today": {"ok": True, "data": {"executed": [], "skipped": [], "failed": [], "runs": []}},
        "run_report_yesterday": {"ok": True, "data": {"executed": [], "skipped": [], "failed": []}},
        "pending": {"ok": True, "data": {"all": []}},
        "hl_fills": {"ok": True, "data": []},
        "hl_state": {"ok": True, "data": {"marginSummary": {"accountValue": "1000"}, "assetPositions": [{
            "position": {"coin": "SOL", "szi": "2", "entryPx": "100", "positionValue": "200",
                         "leverage": {"value": 2}}}]}},
        "hl_orders": {"ok": True, "data": orders},
        "all_mids": {"ok": True, "data": {"SOL": "100"}},
    }


if __name__ == "__main__":
    unittest.main()
