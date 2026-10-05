"""W40 item 3: BX rejection-reason log (stable codes, [BX_REJECT] lines, bx_reject_<date>.jsonl, read-only
GET /api/bx/rejects). No network: exchange calls go to FakeAPI, market data is stubbed."""
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import timedelta
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import bx_live as L
import bx_service
import bx_shadow
from test_bx_live import APPROVED, LIVE, SG, T0, TIERS, FakeAPI, Tmp, cand, meta

DAY = L.hkt_date(T0)
SECRETS = ("KEY_abcdef123", "SECRET_zyx987")


def stdout_rejects(text):
    return [json.loads(x[len(L.REJECT_PREFIX):]) for x in text.splitlines() if x.startswith(L.REJECT_PREFIX)]


class TestCheckEntryCodes(unittest.TestCase):
    def chk(self, c=None, m=None, live=None, nav=10_000.0, avail=1_000.0, open_live=(), today=0, appr=APPROVED):
        return L.check_entry(c or cand(), m or meta(), live or dict(LIVE), nav, avail, list(open_live), today, appr,
                             TIERS)

    def test_every_branch_has_a_code(self):
        cases = [
            (dict(appr=None), "NO_APPROVAL", "no ENTRY_DESK approval today (no approval -> no order)"),
            (dict(m=meta(ex="HL+BX")), "NOT_ELIGIBLE_OTHER", "listed on HL (HL path only)"),
            (dict(m=meta(asset_class="stock")), "NOT_ELIGIBLE_OTHER", None),
            (dict(m=meta(liq_tier="watch")), "LIQ_TIER", "tier watch (entry tier only)"),
            (dict(m=meta(vol24h_usd=1.5e6)), "VOL24H", "24h volume < $2M"),
            (dict(m=meta(spread_bp=10.0)), "SPREAD", "spread 10.0 bp not < 10"),
            (dict(m=meta(gc_tf="1h")), "NOT_ELIGIBLE_OTHER", None),
            (dict(m=meta(api_supported=False)), "NOT_ELIGIBLE_OTHER", None),
            (dict(m=meta(max_leverage=2)), "NOT_ELIGIBLE_OTHER", None),
            (dict(open_live=[{"bx_symbol": "A"}, {"bx_symbol": "B"}]), "MAX_OPEN", "2 BX positions open (max 2)"),
            (dict(open_live=[{"bx_symbol": "FOOUSDT"}]), "ALREADY_HELD", "already holding this contract"),
            (dict(today=1), "DAILY_CAP", "1 new BX entry already today (max 1)"),
            (dict(live=dict(LIVE, price=None, ask=None)), "MARKET_DATA", "no live price"),
            (dict(live=dict(LIVE, vol24h=1.9e6)), "VOL24H", None),
            (dict(live=dict(LIVE, spread_bp=10.5)), "SPREAD", "live spread 10.5 bp not < 10"),
            (dict(c=cand(close=10.0)), "PRICE_SANITY", None),
            (dict(c=cand(hard_sl=None)), "NO_SL", "no valid Hard SL (4h_filter=None)"),
            (dict(c=cand(hard_sl=2.5)), "NO_SL", None),
            (dict(c=cand(hard_sl=1.98)), "SL_DIST", None),
            (dict(nav=None), "NAV", "NAV unavailable (HL NAV + BX equity)"),
            (dict(avail=50.0), "MARGIN_AVAILABLE", None),
            (dict(m=meta(min_qty="1000000")), "MIN_QTY", None),
            (dict(c=cand(hard_sl=1.30)), "LIQ_VS_SL", None),
        ]
        for kw, code, text in cases:
            r = self.chk(**kw)
            self.assertFalse(r["ok"], kw)
            self.assertEqual(r["code"], code, (kw, r))
            self.assertIn(r["code"], L.REJECT_CODES)
            if text:
                self.assertEqual(r["reason"], text, kw)

    def test_ok_has_no_code_and_decision_unchanged(self):
        r = self.chk()
        self.assertTrue(r["ok"])
        self.assertNotIn("code", r)
        self.assertEqual(L.pilot_eligible(meta()), (True, ""))
        self.assertEqual(L.pilot_eligible(meta(liq_tier="watch")), (False, "tier watch (entry tier only)"))


class TestBuildCandidates(Tmp):
    def test_not_eligible_gets_codes_and_log_lines(self):
        metas = [meta(), meta("WATCHUSDT", liq_tier="watch"), meta("THINUSDT", vol24h_usd=1e6),
                 meta("WIDEUSDT", spread_bp=25.0)]
        (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": metas}))
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": [
            {"bx_symbol": m["bx_symbol"], "trend": "Green"} for m in metas]}))
        (self.tmp / "bx_radar_4h.json").write_text(json.dumps({"rows": [
            {"bx_symbol": m["bx_symbol"], "trend": "Red", "filter": 1.86} for m in metas]}))
        sig = {"type": "Base", "gc_tf": "1d", "row": {"close": 2.0}}
        out = io.StringIO()
        with patch.object(bx_shadow, "classify_signal", return_value=sig), redirect_stdout(out):
            doc = L.build_candidates(now=T0)
        self.assertEqual([c["symbol"] for c in doc["candidates"]], ["FOOUSDT"])
        codes = {x["symbol"]: x["code"] for x in doc["not_eligible"]}
        self.assertEqual(codes, {"WATCHUSDT": "LIQ_TIER", "THINUSDT": "VOL24H", "WIDEUSDT": "SPREAD"})
        self.assertEqual(doc["not_eligible"][0]["reason"], "tier watch (entry tier only)")
        logged = L.read_rejects(DAY)
        self.assertEqual({r["symbol"]: r["code"] for r in logged}, codes)
        self.assertEqual(stdout_rejects(out.getvalue()), logged)
        r = next(x for x in logged if x["symbol"] == "WIDEUSDT")
        self.assertEqual((r["job"], r["stage"], r["type"], r["date"]), ("candidates", "eligibility", "Base", DAY))
        self.assertEqual(r["values"]["spread_bp"], 25.0)
        self.assertEqual((r["values"]["trend_1d"], r["values"]["trend_4h"]), ("Green", "Red"))


class TestEntriesLog(Tmp):
    def run_entries(self, api, egress=SG, market=None, now=T0):
        out = io.StringIO()
        with redirect_stdout(out):
            rep = L.run_entries(now=now, trade_api=api, egress=egress, nav_fn=lambda: 10_000.0,
                                market=market or (lambda s: dict(LIVE)), tiers_fn=lambda s: TIERS, conn=self.conn)
        return rep, stdout_rejects(out.getvalue())

    def test_gate_closed_logs_gate_for_every_candidate(self):
        self.seed([cand(), cand("BARUSDT")], [meta(), meta("BARUSDT")], [{"symbol": "FOOUSDT", "decision": "approve"}])
        with patch.dict(os.environ, {"BX_LIVE": "0"}):
            rep, lines = self.run_entries(FakeAPI())
        self.assertEqual([(r["symbol"], r["code"], r["stage"]) for r in lines],
                         [("FOOUSDT", "GATE", "gate"), ("BARUSDT", "GATE", "gate")])
        self.assertEqual([s["code"] for s in rep["skipped"]], ["GATE", "GATE"])
        self.assertIn("BX_LIVE=0", lines[0]["reason"])
        self.assertEqual(rep["skipped"][0]["reason"], lines[0]["reason"])
        self.assertEqual(L.read_rejects(DAY), lines)

    def test_breaker_tripped_logs_breaker(self):
        self.seed([cand(), cand("BARUSDT")], [meta(), meta("BARUSDT")], [{"symbol": "FOOUSDT", "decision": "approve"}])
        L.trip_breaker({"pnl_usd": -301}, set_var=lambda: "x")
        api = FakeAPI()
        rep, lines = self.run_entries(api)
        self.assertEqual(api.orders(), [])
        self.assertEqual([r["code"] for r in lines], ["BREAKER", "BREAKER"])
        self.assertEqual([r["code"] for r in L.read_rejects(DAY)], ["BREAKER", "BREAKER"])

    def test_account_read_failure_logs_gate(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])
        rep, lines = self.run_entries(FakeAPI(account_fails=True))
        self.assertEqual(rep["skipped"], [])                                  # report shape unchanged
        self.assertEqual([(r["code"], r["stage"]) for r in lines], [("GATE", "gate")])
        self.assertIn("10004", lines[0]["reason"])

    def test_approval_pending_and_check_entry_codes(self):
        self.seed([cand(), cand("BARUSDT"), cand("CHAUSDT", type="Chase"), cand("NOPEUSDT")],
                  [meta(), meta("BARUSDT"), meta("CHAUSDT"), meta("NOPEUSDT")],
                  [{"symbol": s, "decision": "approve"} for s in ("FOOUSDT", "BARUSDT", "CHAUSDT")])
        api = FakeAPI()
        rep, lines = self.run_entries(api)
        self.assertEqual(rep["entered"][0]["status"], "filled")
        got = {r["symbol"]: (r["code"], r["stage"]) for r in lines}
        self.assertEqual(got, {"BARUSDT": ("DAILY_CAP", "check_entry"), "CHAUSDT": ("PENDING", "pending_create"),
                               "NOPEUSDT": ("NO_APPROVAL", "approval")})
        self.assertNotIn("FOOUSDT", got)
        self.assertEqual({s["symbol"]: s["code"] for s in rep["skipped"]}, {"BARUSDT": "DAILY_CAP",
                                                                           "NOPEUSDT": "NO_APPROVAL"})
        bar = next(r for r in lines if r["symbol"] == "BARUSDT")
        self.assertEqual(bar["values"]["sl_dist_pct"], 7.0)
        self.assertEqual((bar["values"]["spread_bp"], bar["values"]["vol24h"]), (2.0, 8e6))

    def test_market_data_error_redacted(self):
        self.seed([cand()], [meta()], [{"symbol": "FOOUSDT", "decision": "approve"}])

        def boom(s):
            raise OSError(f"depth failed {SECRETS[0]} {SECRETS[1]}")
        rep, lines = self.run_entries(FakeAPI(), market=boom)
        self.assertEqual([(r["code"], r["stage"]) for r in lines], [("MARKET_DATA", "market_data")])
        text = json.dumps(lines) + (self.tmp / "bx_reject" / f"bx_reject_{DAY}.jsonl").read_text()
        for s in SECRETS:
            self.assertNotIn(s, text)

    def test_no_secrets_in_any_line(self):
        self.seed([cand(), cand("BARUSDT")], [meta(), meta("BARUSDT")], [{"symbol": "FOOUSDT", "decision": "approve"}])
        rep, lines = self.run_entries(FakeAPI())
        self.assertTrue(lines)
        blob = json.dumps(lines) + (self.tmp / "bx_reject" / f"bx_reject_{DAY}.jsonl").read_text()
        for s in SECRETS:
            self.assertNotIn(s, blob)

    def test_write_failure_never_breaks_trading(self):
        self.seed([cand(), cand("BARUSDT")], [meta(), meta("BARUSDT")],
                  [{"symbol": "FOOUSDT", "decision": "approve"}, {"symbol": "BARUSDT", "decision": "approve"}])
        (self.tmp / "bx_reject").write_text("not a directory")
        api = FakeAPI()
        err = io.StringIO()
        with patch("sys.stderr", err):
            rep, lines = self.run_entries(api)
        self.assertEqual(rep["entered"][0]["status"], "filled")
        self.assertEqual(len(api.orders()), 1)
        self.assertEqual(rep["skipped"][0]["code"], "DAILY_CAP")
        self.assertEqual([r["code"] for r in lines], ["DAILY_CAP"])                # stdout line still emitted
        self.assertIn("[BX_REJECT] write failed", err.getvalue())

    def test_stdout_failure_never_breaks_trading(self):
        self.seed([cand()], [meta()], decisions=[])

        class Broken(io.StringIO):
            def write(self, s):
                raise OSError("closed pipe")
        with patch("sys.stdout", Broken()):
            rep = L.run_entries(now=T0, trade_api=FakeAPI(), egress=SG, nav_fn=lambda: 10_000.0,
                                market=lambda s: dict(LIVE), tiers_fn=lambda s: TIERS, conn=self.conn)
        self.assertEqual(rep["skipped"][0]["code"], "NO_APPROVAL")
        self.assertEqual(len(L.read_rejects(DAY)), 1)


class TestPendingLog(Tmp):
    def pending(self, action, gate_ok, gate_why=()):
        L.save_live_pending([{"id": "p1", "symbol": "CHAUSDT", "status": "pending", "kind": "CONTINUATION",
                              "cand": cand("CHAUSDT", type="Chase"), "approved_at": T0.isoformat()}])
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": []}))
        out = io.StringIO()
        with patch("pending_entries.evaluate", return_value=(action, f"{action} reason", {})), redirect_stdout(out):
            res = L.run_live_pending(FakeAPI(), self.conn, T0, gate_ok, list(gate_why), {"CHAUSDT": meta("CHAUSDT")},
                                     {"available": "1000"}, 10_000.0, lambda s: dict(LIVE), lambda s: TIERS)
        return res, stdout_rejects(out.getvalue())

    def test_wait_expire_cancel_logged_as_pending(self):
        for action in ("wait", "expire", "cancel"):
            res, lines = self.pending(action, True)
            self.assertEqual(len(lines), 1, action)
            r = lines[0]
            self.assertEqual((r["code"], r["stage"], r["job"], r["type"]), ("PENDING", "pending_eval", "4h", "Chase"))
            self.assertEqual((r["values"]["action"], r["values"]["pending_id"]), (action, "p1"))
            self.assertEqual(res[0]["reason"], f"{action} reason")

    def test_trigger_with_breaker_logs_breaker(self):
        res, lines = self.pending("trigger", False, ["circuit breaker tripped at x"])
        self.assertEqual([(r["code"], r["stage"]) for r in lines], [("BREAKER", "gate")])
        res, lines = self.pending("trigger", False, ["BX_LIVE=0 (shadow only)"])
        self.assertEqual([r["code"] for r in lines], ["GATE"])
        self.assertIn("not sent: BX_LIVE=0", res[0]["reason"])

    def test_trigger_filled_logs_nothing_and_rejected_trigger_logs_code(self):
        res, lines = self.pending("trigger", True)
        self.assertEqual(lines, [])
        self.assertEqual(L.load_live_pending()[0]["status"], "filled")
        res, lines = self.pending("trigger", True)                           # already holding now
        self.assertEqual([(r["code"], r["stage"]) for r in lines], [("ALREADY_HELD", "check_entry")])
        self.assertNotIn("'code'", res[0]["reason"])                           # pending reason text unchanged


class TestJsonlFile(Tmp):
    def test_written_and_read_back_by_date(self):
        with redirect_stdout(io.StringIO()):
            L.log_reject("entries", "gate", cand(), "GATE", "BX_LIVE=0", T0, {"spread_bp": 2.0})
            L.log_reject("entries", "gate", cand("BARUSDT"), "GATE", "BX_LIVE=0", T0 + timedelta(days=1))
        self.assertTrue((self.tmp / "bx_reject" / f"bx_reject_{DAY}.jsonl").exists())
        a, b = L.read_rejects(DAY), L.read_rejects(L.hkt_date(T0 + timedelta(days=1)))
        self.assertEqual([r["symbol"] for r in a], ["FOOUSDT"])
        self.assertEqual([r["symbol"] for r in b], ["BARUSDT"])
        self.assertEqual(set(a[0]), {"date", "ts", "job", "symbol", "type", "stage", "code", "reason", "values"})
        self.assertEqual(L.read_rejects("2020-01-01"), [])
        self.assertEqual(L.reject_summary(DAY), {"count": 1, "by_code": {"GATE": 1},
                                                 "link": f"/api/bx/rejects?date={DAY}"})

    def test_keeps_14_days(self):
        with redirect_stdout(io.StringIO()):
            for i in range(20):
                L.log_reject("entries", "gate", cand(), "GATE", "x", T0 + timedelta(days=i))
        files = sorted(p.name for p in (self.tmp / "bx_reject").glob("*.jsonl"))
        self.assertEqual(len(files), 14)
        self.assertEqual(files[-1], f"bx_reject_{L.hkt_date(T0 + timedelta(days=19))}.jsonl")


class TestServiceStdoutSplit(unittest.TestCase):
    def test_reject_lines_reemitted_and_json_still_parsed(self):
        result = json.dumps({"job": "entries", "skipped": [{"symbol": "FOO"}]}, indent=1)
        raw = f'{L.REJECT_PREFIX}{{"code":"GATE"}}\n{L.REJECT_PREFIX}{{"code":"NAV"}}\n{result}\n'
        out = io.StringIO()
        with redirect_stdout(out):
            rest = bx_service.split_reject_lines(raw)
        self.assertEqual(json.loads(rest)["job"], "entries")
        self.assertEqual([json.loads(x[len(L.REJECT_PREFIX):])["code"] for x in out.getvalue().splitlines()],
                         ["GATE", "NAV"])
        self.assertEqual(bx_service.split_reject_lines(result), result)


class TestServiceEndpoint(Tmp):
    KEY = "bx-service-test-key"

    def setUp(self):
        super().setUp()
        self._out = bx_service.OUT_DIR
        bx_service.OUT_DIR = self.tmp
        patch.dict(os.environ, {"BX_SERVICE_KEY": self.KEY}).start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), bx_service.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        with redirect_stdout(io.StringIO()):
            L.log_reject("entries", "gate", cand(), "GATE", "BX_LIVE=0", T0)
            L.log_reject("entries", "approval", cand("BARUSDT"), "NO_APPROVAL", "no ENTRY_DESK approval", T0)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        bx_service.OUT_DIR = self._out
        patch.stopall()
        super().tearDown()

    def get(self, path, key=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}{path}",
                                     headers={"X-BX-Key": key} if key else {})
        with patch("sys.stderr", io.StringIO()):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

    def test_auth_and_dates(self):
        self.assertEqual(self.get(f"/api/bx/rejects?date={DAY}")[0], 403)
        self.assertEqual(self.get(f"/api/bx/rejects?date={DAY}", "wrong")[0], 403)
        code, body = self.get(f"/api/bx/rejects?date={DAY}", self.KEY)
        self.assertEqual(code, 200)
        self.assertEqual((body["date"], body["count"]), (DAY, 2))
        self.assertEqual([r["code"] for r in body["rejects"]], ["GATE", "NO_APPROVAL"])
        for bad in ("../x", "2026-9-30", "20260930"):
            self.assertEqual(self.get(f"/api/bx/rejects?date={bad}", self.KEY)[0], 400, bad)
        code, body = self.get("/api/bx/rejects?date=2020-01-01", self.KEY)
        self.assertEqual((code, body["count"], body["rejects"]), (200, 0, []))

    def test_day_report_carries_reject_summary(self):
        (self.tmp / "bx_day").mkdir()
        (self.tmp / "bx_day" / f"bx_day_{DAY.replace('-', '')}.json").write_text(json.dumps({"date": DAY}))
        code, body = self.get(f"/api/bx/day?date={DAY}", self.KEY)
        self.assertEqual(code, 200)
        self.assertEqual(body["rejects"], {"count": 2, "by_code": {"GATE": 1, "NO_APPROVAL": 1},
                                           "link": f"/api/bx/rejects?date={DAY}"})


if __name__ == "__main__":
    unittest.main()
