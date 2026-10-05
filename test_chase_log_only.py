#!/usr/bin/env python3
"""CHASE_MODE=log_only / CHASE_LOG_ONLY_EXISTING: no pending, no order, shadow log + M1; freeze vs keep;
default (live) unchanged. No network: HL and Bitunix are fakes."""
from __future__ import annotations

import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pending_entries as pe  # noqa: E402
import pending_worker as pw  # noqa: E402
import test_live_execution as TL  # noqa: E402
import test_pending_entries as TP  # noqa: E402
from test_chase_pending_params import CHASE_VARS, ref_evaluate  # noqa: E402

H4 = pe.BAR_MS["4h"]
D = pe.BAR_MS["1d"]


def ms(dt):
    return int(dt.timestamp() * 1000)


def m1_bar(now):
    return ms(now) // H4 * H4


class TestConfig(unittest.TestCase):
    def setUp(self):
        pe._warned.clear()

    def test_defaults(self):
        p = pe.chase_params({})
        self.assertEqual((p["chase_mode"], p["log_only_existing"]), ("live", "keep"))
        self.assertFalse(pe.log_only(p) or pe.frozen(p))

    def test_valid(self):
        p = pe.chase_params({"CHASE_MODE": "LOG_ONLY", "CHASE_LOG_ONLY_EXISTING": " freeze "})
        self.assertTrue(pe.log_only(p) and pe.frozen(p))
        self.assertFalse(pe.frozen(pe.chase_params({"CHASE_LOG_ONLY_EXISTING": "freeze"})))  # freeze needs log_only

    def test_invalid_falls_back_with_warning(self):
        for env, key, want in ((({"CHASE_MODE": "paper"}), "chase_mode", "live"),
                               (({"CHASE_MODE": "off"}), "chase_mode", "live"),
                               (({"CHASE_MODE": "log_only", "CHASE_LOG_ONLY_EXISTING": "cancel"}),
                                "log_only_existing", "keep")):
            pe._warned.clear()
            err = io.StringIO()
            with patch("sys.stderr", err):
                p = pe.chase_params(env)
            self.assertEqual(p[key], want, env)
            name = "CHASE_MODE" if key == "chase_mode" else "CHASE_LOG_ONLY_EXISTING"
            self.assertIn(f"[PENDING_CFG] {name}=", err.getvalue())


class EnvCase(unittest.TestCase):
    """Clean CHASE_* env + temp shadow / pending paths for every test."""
    extra: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.shadow = self.tmp / "chase_shadow.jsonl"
        env = {k: v for k, v in os.environ.items() if k not in CHASE_VARS}
        env.update(CHASE_SHADOW_PATH=str(self.shadow), PENDING_PATH=str(self.tmp / "pending_entries.json"),
                   **self.extra)
        self._cenv = patch.dict(os.environ, env, clear=True)
        self._cenv.start()
        self.err = io.StringIO()
        self._cerr = patch("sys.stderr", self.err)
        self._cerr.start()

    def tearDown(self):
        patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lines(self, path=None):
        return pe.read_chase_shadow(path or self.shadow)


# ------------------------------------------------------------------ HL executor (08:55)
class TestHLExecutor(TL.EnvMixin, EnvCase):
    META = {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}}

    def setUp(self):
        EnvCase.setUp(self)
        TL.EnvMixin.setUp(self)
        os.environ["EXEC_DRY_RUN"] = "0"
        os.environ["HL_API_PRIVATE_KEY"] = "0x" + "11" * 32

    def tearDown(self):
        TL.EnvMixin.tearDown(self)
        EnvCase.tearDown(self)

    def chase(self, hl, positions=None):
        r1d = {"rows": [{"symbol": "AAA", "lower": 0.7, "filter": 0.8, "upper": 0.9, "close": 1.0, "trend": "Green"}]}
        r4h = {"rows": [{"symbol": "AAA", "lower": 0.85, "filter": 0.92, "upper": 0.97, "close": 1.0, "trend": "Green"}]}
        return TL.executor.execute_approved_candidates(
            hl=hl, candidates_data=TL._cands(TL._cand("AAA", typ="Chase")),
            decisions={"AAA": {"decision": "approve", "size_pct": 3, "leverage": 2, "reason": "ok"}},
            radar_1h={}, radar_4h=r4h, radar_1d=r1d, now=TL.NOW)

    def base(self, hl):
        return TL.executor.execute_approved_candidates(
            hl=hl, candidates_data=TL._cands(TL._cand("AAA", tier="small", filt=0.97, lower=0.95)),
            decisions={"AAA": {"decision": "approve", "size_pct": 6, "leverage": 2}},
            radar_1h={}, radar_4h={"rows": []}, now=TL.NOW)

    def test_default_live_creates_pending_and_no_shadow(self):
        res = self.chase(TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}))
        self.assertTrue(res["pending"][0]["created"])
        self.assertNotIn("chase_shadow", res)
        self.assertEqual(len(pe.load_pending()), 1)
        self.assertFalse(self.shadow.exists())

    def test_log_only_no_pending_no_order_shadow_written(self):
        os.environ["CHASE_MODE"] = "log_only"
        hl = TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        res = self.chase(hl)
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["pending"], [])
        self.assertEqual(pe.load_pending(), [])
        self.assertTrue(res["chase_shadow"][0]["logged"])
        [rec] = self.lines()
        self.assertEqual((rec["event"], rec["venue"], rec["symbol"], rec["kind"]), ("decision", "HL", "AAA", "CONTINUATION"))
        self.assertEqual((rec["upper_1d"], rec["filter_1d"], rec["upper_4h"]), (0.9, 0.8, 0.97))
        self.assertEqual(rec["decision_time"], TL.NOW.isoformat())
        w = rec["would_be_pending"]
        self.assertEqual((w["band_tf"], w["zone"], w["touch_level"], w["touch_label"]), ("1d", [0.7, 0.8], 0.8, "Filter"))
        self.assertEqual(w["expires_at"], (TL.NOW + timedelta(days=7)).isoformat())
        self.assertEqual((rec["m1"]["bar_t"], rec["m1"]["status"]), (m1_bar(TL.NOW), "pending_eval"))
        self.assertIn("[CHASE_SHADOW] HL AAA CONTINUATION", self.err.getvalue())
        # idempotent re-run
        res2 = self.chase(TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}))
        self.assertFalse(res2["chase_shadow"][0]["logged"])
        self.assertEqual(len(self.lines()), 1)

    def test_log_only_add_on_kind_on_held_long(self):
        os.environ["CHASE_MODE"] = "log_only"
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.3"}
        hl = TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}, positions=[pos])
        self.chase(hl)
        self.assertEqual(hl.calls, [])
        self.assertEqual(self.lines()[0]["would_be_pending"]["zone"], [0.85, 0.92])

    def test_log_only_dry_run_writes_nothing(self):
        os.environ.update(CHASE_MODE="log_only", EXEC_DRY_RUN="1")
        res = self.chase(TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0}))
        self.assertTrue(res["chase_shadow"][0]["would_log"])
        self.assertFalse(self.shadow.exists())

    def test_base_untouched_by_log_only(self):
        hl_a = TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        ra = self.base(hl_a)
        os.environ.update(CHASE_MODE="log_only", CHASE_LOG_ONLY_EXISTING="freeze")
        hl_b = TL.FakeHL(equity=1000, meta=self.META, mids={"AAA": 1.0})
        rb = self.base(hl_b)
        self.assertEqual(hl_a.calls, hl_b.calls)
        self.assertEqual(hl_b.names(), ["set_leverage", "open_long_ioc", "place_stop_loss"])
        self.assertEqual(len(ra["executed"]), len(rb["executed"]))
        self.assertFalse(self.shadow.exists())


# ------------------------------------------------------------------ M1 resolver
class TestM1(EnvCase):
    T = datetime(2026, 10, 5, 0, 56, tzinfo=timezone.utc)     # 08:56 HKT -> M1 bar 00:00-04:00 UTC

    def log(self, venue="HL", sym="CHIP", when=None):
        p = pe.chase_params({})
        rec = pe.chase_shadow_record(venue, sym, pe.CONTINUATION, {"reason": "x"},
                                     {"upper": 1.0, "filter": 0.9, "lower": 0.8}, {"upper": 1.05}, when or self.T, p)
        pe.log_chase_shadow(self.shadow, rec)
        return rec

    def resolve(self, row, now, venue="HL"):
        return pe.resolve_chase_m1(self.shadow, venue, {"CHIP": row} if row else {}, now)

    def test_m1_bar_is_first_4h_close_after_approval(self):
        rec = self.log()
        self.assertEqual(rec["m1"]["bar_t"], ms(datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)))
        self.assertEqual(rec["m1"]["bar_close_at"], "2026-10-05T04:00:00+00:00")

    def test_triggered(self):
        rec = self.log()
        [ev] = self.resolve({"bar_time": rec["m1"]["bar_t"], "close": 1.12, "upper": 1.06}, self.T + timedelta(hours=3, minutes=20))
        self.assertEqual((ev["status"], ev["entry_px"]), ("triggered", 1.12))
        self.assertEqual(self.resolve({"bar_time": rec["m1"]["bar_t"], "close": 1.0, "upper": 1.06},
                                      self.T + timedelta(hours=4)), [])   # resolved once
        self.assertEqual([r["event"] for r in self.lines()], ["decision", "m1"])

    def test_no_trigger(self):
        rec = self.log()
        [ev] = self.resolve({"bar_time": rec["m1"]["bar_t"], "close": 1.05, "upper": 1.06}, self.T + timedelta(hours=3, minutes=20))
        self.assertEqual((ev["status"], ev["entry_px"]), ("no_trigger", None))

    def test_waits_for_bar_then_no_data(self):
        rec = self.log()
        t = rec["m1"]["bar_t"]
        self.assertEqual(self.resolve({"bar_time": t - H4, "close": 2, "upper": 1}, self.T + timedelta(hours=2)), [])
        self.assertEqual(self.resolve(None, self.T + timedelta(hours=8)), [])
        [ev] = self.resolve(None, datetime.fromtimestamp((t + 3 * H4) / 1000, tz=timezone.utc))
        self.assertEqual(ev["status"], "no_data")

    def test_missed_bar_no_data(self):
        rec = self.log()
        [ev] = self.resolve({"bar_time": rec["m1"]["bar_t"] + H4, "close": 2, "upper": 1}, self.T + timedelta(hours=8))
        self.assertEqual(ev["status"], "no_data")

    def test_venue_scoped(self):
        self.log(venue="BX")
        self.assertEqual(self.resolve({"bar_time": m1_bar(self.T), "close": 2, "upper": 1}, self.T + timedelta(hours=4)), [])

    def test_pending_worker_resolves_hl_even_without_pendings(self):
        rec = self.log()
        r4h = {"ts": self.T.isoformat(), "rows": [{"symbol": "CHIP", "bar_time": rec["m1"]["bar_t"], "close": 1.2, "upper": 1.1}]}
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": TP.KEY}):
            res = pw.run_pending(hl=TP.FakeHL(), radar_1d={"rows": []}, radar_4h=r4h, now=self.T + timedelta(hours=3, minutes=10),
                                 entries=[], log_entry_fn=lambda **k: None)
        self.assertEqual(res["message"], "no active pending entries")
        self.assertEqual(res["chase_shadow_m1"][0]["status"], "triggered")

    def test_pending_worker_dry_run_does_not_resolve(self):
        self.log()
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1"}):
            res = pw.run_pending(hl=TP.FakeHL(), radar_1d={"rows": []}, radar_4h={"rows": []}, now=self.T + timedelta(hours=20),
                                 entries=[], log_entry_fn=lambda **k: None)
        self.assertNotIn("chase_shadow_m1", res)
        self.assertEqual(len(self.lines()), 1)


# ------------------------------------------------------------------ existing pendings: keep vs freeze
class TestExistingPendings(EnvCase):
    def run_w(self, entries, env, bar_fn=TP.bar(0.88, 0.93), h4=None):
        ts = (TP.NOW - timedelta(minutes=5)).isoformat()
        r1d = {"ts": ts, "rows": [dict(symbol="AAA", **TP._row(0.80, 0.90, 1.00))]}
        r4h = {"ts": ts, "rows": [dict(symbol="AAA", **(h4 or TP._row(0.85, 0.88, 0.95)))]}
        hl = TP.FakeHL(mids={"AAA": 0.9})
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": TP.KEY, **env}):
            res = pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=TP.NOW, entries=entries,
                                 log_entry_fn=lambda **k: None, bar_fn=bar_fn)
        return hl, res

    def entry(self, created=None):
        created = created or TP.NOW - timedelta(days=2)
        return [{"id": "AAA_CONTINUATION_x", "symbol": "AAA", "kind": pe.CONTINUATION, "status": "pending",
                 "tier": "small", "size_pct": 3, "leverage": 3, "created_at": created.isoformat(),
                 "expires_at": (created + timedelta(days=7)).isoformat(),
                 "setup": {"t": TP.T0 - D, "l": 0.85, "c": 0.89}}]

    def test_keep_fills_as_today(self):
        for env in ({}, {"CHASE_MODE": "log_only"}, {"CHASE_MODE": "log_only", "CHASE_LOG_ONLY_EXISTING": "keep"}):
            ents = self.entry()
            hl, res = self.run_w(ents, env)
            self.assertEqual(hl.names() if hasattr(hl, "names") else [c[0] for c in hl.calls],
                             ["set_leverage", "open_long_ioc", "place_stop_loss"], env)
            self.assertEqual(ents[0]["status"], "filled", env)

    def test_freeze_no_fill_record_intact(self):
        ents = self.entry()
        before = json.loads(json.dumps(ents[0]))
        hl, res = self.run_w(ents, {"CHASE_MODE": "log_only", "CHASE_LOG_ONLY_EXISTING": "freeze"})
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["filled"], [])
        self.assertIn("frozen", res["checked"][0]["reason"])
        self.assertEqual({k: v for k, v in ents[0].items() if k != "last_check"}, before)

    def test_freeze_never_cancels_or_expires(self):
        env = {"CHASE_MODE": "log_only", "CHASE_LOG_ONLY_EXISTING": "freeze"}
        ents = self.entry(created=TP.NOW - timedelta(days=9))                    # past expiry
        self.run_w(ents, env)
        self.assertEqual(ents[0]["status"], "pending")
        ents = self.entry()
        self.run_w(ents, env, bar_fn=TP.bar(0.7, 0.75))                         # close below 1D Lower
        self.assertEqual(ents[0]["status"], "pending")
        self.assertEqual(self.run_w(self.entry(created=TP.NOW - timedelta(days=9)), {})[1]["cancelled"][0]["reason"],
                         "expired after 7 days")                                  # default: expires as today

    def test_freeze_ignored_in_live_mode(self):
        ents = self.entry()
        hl, _ = self.run_w(ents, {"CHASE_LOG_ONLY_EXISTING": "freeze"})
        self.assertEqual(ents[0]["status"], "filled")

    def test_frozen_evaluate_never_matches_trigger(self):
        p = pe.chase_params({"CHASE_MODE": "log_only", "CHASE_LOG_ONLY_EXISTING": "freeze"})
        rnd = random.Random(7)
        for _ in range(500):
            r = {"id": "x", "symbol": "AAA", "kind": rnd.choice([pe.ADD_ON, pe.CONTINUATION]), "status": "pending",
                 "created_at": (TP.NOW - timedelta(days=rnd.choice([1, 9]))).isoformat(),
                 "expires_at": (TP.NOW - timedelta(days=rnd.choice([-6, 2]))).isoformat()}
            a, _, upd = pe.evaluate(r, {"tf": "4h", "lower": 0.8, "filter": 0.9}, rnd.uniform(0.7, 1.2), TP.NOW,
                                    rnd.choice([set(), {"AAA"}]), {"t": TP.T0, "l": rnd.uniform(0.7, 1), "c": rnd.uniform(0.7, 1.2)}, p)
            self.assertEqual((a, upd), ("wait", {}))

    def test_default_still_matches_frozen_main_reference(self):
        r = {"id": "x", "symbol": "AAA", "kind": pe.ADD_ON, "status": "pending",
             "created_at": (TP.NOW - timedelta(days=1)).isoformat(), "expires_at": (TP.NOW + timedelta(days=6)).isoformat(),
             "setup": {"t": TP.T0 - H4, "l": 0.88, "c": 0.93}}
        b = {"tf": "4h", "lower": 0.8, "filter": 0.9, "trend": "Green"}
        bar = {"t": TP.T0, "l": 0.91, "c": 0.95}
        self.assertEqual(pe.evaluate(dict(r), b, 0.95, TP.NOW, {"AAA"}, bar),
                         ref_evaluate(dict(r), b, 0.95, TP.NOW, {"AAA"}, bar))


# ------------------------------------------------------------------ Bitunix live (08:56) + 4h manage
class TestBXLive(EnvCase):
    def setUp(self):
        super().setUp()
        import bx_live as L
        import bx_radar
        import bx_shadow
        import test_bx_live as TB
        self.L, self.TB = L, TB
        self._orig = (L.OUT_DIR, bx_radar.OUT_DIR, bx_shadow.OUT_DIR)
        L.OUT_DIR = bx_radar.OUT_DIR = bx_shadow.OUT_DIR = self.tmp
        os.environ.update(TB.LIVE_ENV, BX_LEDGER_PATH=str(self.tmp / "bx.db"))
        self.conn = L.connect()

    def tearDown(self):
        import bx_radar
        import bx_shadow
        self.conn.close()
        self.L.OUT_DIR, bx_radar.OUT_DIR, bx_shadow.OUT_DIR = self._orig
        super().tearDown()

    def go(self, typ):
        TB, L = self.TB, self.L
        c = TB.cand(type=typ, upper_1d=1.9, filter_1d=1.8, lower_1d=1.7, upper_4h=1.95, trend_1d="Green")
        day = L.hkt_date(TB.T0)
        (self.tmp / "bx_candidates_latest.json").write_text(json.dumps({"date": day, "candidates": [c]}))
        (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": [TB.meta()]}))
        (self.tmp / "bx_radar_4h.json").write_text(json.dumps({"rows": []}))
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": []}))
        L.store_decisions([{"symbol": "FOOUSDT", "decision": "approve"}], "claude", now=TB.T0 - timedelta(minutes=30))
        api = TB.FakeAPI()
        rep = L.run_entries(now=TB.T0, trade_api=api, egress=TB.SG, nav_fn=lambda: 10_000.0,
                            market=lambda s: dict(TB.LIVE), tiers_fn=lambda s: TB.TIERS, conn=self.conn)
        return api, rep

    def test_default_chase_creates_live_pending(self):
        api, rep = self.go("Chase")
        self.assertEqual(api.orders(), [])
        self.assertTrue(rep["pending"][0]["created"])
        self.assertEqual(len(self.L.load_live_pending()), 1)
        self.assertFalse(self.L.chase_shadow_path().exists())

    def test_log_only_chase_no_pending_no_order_shadow_written(self):
        os.environ["CHASE_MODE"] = "log_only"
        api, rep = self.go("Chase")
        self.assertEqual(api.orders(), [])
        self.assertEqual(self.L.load_live_pending(), [])
        self.assertTrue(rep["pending"][0]["log_only"])
        [rec] = self.lines(self.L.chase_shadow_path())
        self.assertEqual((rec["venue"], rec["symbol"], rec["upper_1d"], rec["filter_1d"]), ("BX", "FOOUSDT", 1.9, 1.8))
        self.assertEqual(rec["would_be_pending"]["zone"], [1.7, 1.8])
        self.assertFalse(self.shadow.exists())                     # BX log is separate from the HL log
        self.assertNotIn("SECRET_zyx987", self.err.getvalue())
        self.assertNotIn("KEY_abcdef123", self.err.getvalue())
        self.assertNotIn("SECRET_zyx987", self.L.chase_shadow_path().read_text())

    def test_log_only_base_still_enters(self):
        os.environ["CHASE_MODE"] = "log_only"
        api, rep = self.go("Base")
        self.assertEqual(rep["entered"][0]["status"], "filled")
        self.assertFalse(self.L.chase_shadow_path().exists())

    def test_4h_manage_resolves_m1(self):
        os.environ["CHASE_MODE"] = "log_only"
        self.go("Chase")
        t = m1_bar(self.TB.T0)
        (self.tmp / "bx_radar_4h.json").write_text(json.dumps({"rows": [
            {"symbol": "FOO", "bx_symbol": "FOOUSDT", "bar_time": t, "close": 2.0, "upper": 1.97}]}))
        rep = self.L.run_manage("4h", now=self.TB.T0 + timedelta(hours=3, minutes=10), trade_api=self.TB.FakeAPI(),
                                egress=self.TB.SG, nav_fn=lambda: 10_000.0, market=lambda s: dict(self.TB.LIVE),
                                tiers_fn=lambda s: self.TB.TIERS, conn=self.conn)
        self.assertEqual(rep["chase_shadow_m1"][0]["status"], "triggered")

    def test_freeze_blocks_existing_live_pending_fill(self):
        L = self.L
        r = {"id": "FOOUSDT_CONTINUATION_x", "symbol": "FOOUSDT", "kind": pe.CONTINUATION, "status": "pending",
             "created_at": (TP.NOW - timedelta(days=2)).isoformat(),
             "expires_at": (TP.NOW + timedelta(days=5)).isoformat(), "approved_at": TP.NOW.isoformat(),
             "setup": {"t": TP.T0 - D, "l": 1.79, "c": 1.85}}
        L.save_live_pending([r])
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": [
            {"symbol": "FOO", "bx_symbol": "FOOUSDT", "lower": 1.7, "filter": 1.8, "upper": 1.9, "close": 1.93,
             "low": 1.84, "trend": "Green", "bar_time": TP.T0}]}))
        args = (None, self.conn, TP.NOW, False, ["gate"], {"FOOUSDT": {"price": 1.95}}, {}, None, None, None)
        self.assertEqual(L.run_live_pending(*args)[0]["action"], "trigger")      # keep/default: as today
        L.save_live_pending([dict(r)])
        os.environ.update(CHASE_MODE="log_only", CHASE_LOG_ONLY_EXISTING="freeze")
        out = L.run_live_pending(*args)
        self.assertEqual(out[0]["action"], "wait")
        self.assertIn("frozen", out[0]["reason"])
        [kept] = L.load_live_pending()
        self.assertEqual((kept["status"], kept["setup"]), ("pending", r["setup"]))


# ------------------------------------------------------------------ Bitunix shadow book
class TestBXShadowBook(EnvCase):
    T = datetime(2026, 9, 29, 0, 20, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        import bx_radar
        import bx_shadow as S
        self.S, self.R = S, bx_radar
        self._orig = (bx_radar.OUT_DIR, S.OUT_DIR)
        bx_radar.OUT_DIR = S.OUT_DIR = self.tmp
        os.environ["BX_LEDGER_PATH"] = str(self.tmp / "bx.db")
        self.conn = S.connect()

    def tearDown(self):
        self.conn.close()
        self.R.OUT_DIR, self.S.OUT_DIR = self._orig
        super().tearDown()

    def row(self, **kw):
        r = {"symbol": "FOO", "bx_symbol": "FOOUSDT", "close": 2.0, "low": 1.95, "high": 2.05, "filter": 1.8,
             "upper": 1.9, "lower": 1.7, "trend": "Green", "dual_cross_up": False, "bar_time": ms(self.T) - D}
        r.update(kw)
        return r

    def write(self, r1d, r4h):
        m = {"symbol": "FOO", "bx_symbol": "FOOUSDT", "ex": "BX", "asset_class": "crypto", "liq_tier": "tradeable",
             "gc_tf": "1d", "price": 2.0, "tier": "small", "spread_bp": 4.0, "vol24h_usd": 3e6,
             "max_notional_usd": 15000.0, "asset_age": "old"}
        (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": [m]}))
        for tf, rows in (("1d", r1d), ("4h", r4h), ("1h", [])):
            (self.tmp / f"bx_radar_{tf}.json").write_text(json.dumps({"rows": rows}))

    def test_default_chase_creates_shadow_pending(self):
        self.write([self.row()], [self.row(dual_cross_up=True)])
        self.S.run("daily", now=self.T, nav_usd=10_000, conn=self.conn)
        self.assertEqual(len(self.S.load_pending()), 1)
        self.assertFalse(self.S.chase_shadow_path().exists())

    def test_log_only_chase_logged_not_pending_then_m1(self):
        os.environ["CHASE_MODE"] = "log_only"
        self.write([self.row()], [self.row(dual_cross_up=True)])
        rep = self.S.run("daily", now=self.T, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["opened"], [])
        self.assertEqual(self.S.load_pending(), [])
        [rec] = self.lines(self.S.chase_shadow_path())
        self.assertEqual((rec["venue"], rec["kind"], rec["upper_1d"]), ("BX_SHADOW", "CONTINUATION", 1.9))
        later = self.T + timedelta(hours=3, minutes=50)
        self.write([self.row()], [self.row(bar_time=rec["m1"]["bar_t"], close=1.85, upper=1.9)])
        rep = self.S.run("4h", now=later, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["chase_shadow_m1"][0]["status"], "no_trigger")

    def test_log_only_base_still_opens(self):
        os.environ["CHASE_MODE"] = "log_only"
        self.write([self.row(dual_cross_up=True, trend="Green")], [self.row()])
        rep = self.S.run("daily", now=self.T, nav_usd=10_000, conn=self.conn)
        self.assertEqual(rep["opened"][0]["kind"], "Base")
        self.assertFalse(self.S.chase_shadow_path().exists())


# ------------------------------------------------------------------ preflight preview
class TestPreflightPreview(EnvCase):
    def preview(self):
        import test_exec_preflight as TE
        t = TE.TestPreflight("test_entry_guard_preview_chase_is_pending")
        return t._preview([{"symbol": "AAA", "type": "Chase", "upper_4h": 1.05}], {"AAA": {"decision": "approve"}})

    def checks(self, r):
        return " | ".join(f"{c.get('name')}: {c.get('detail')}" for c in r.get("checks", []))

    def test_default_says_pending(self):
        r = self.preview()
        self.assertNotIn("chase_mode", r["entry_guard"]["AAA"])
        self.assertIn("becomes pending CONTINUATION", self.checks(r))

    def test_log_only_says_shadow_only(self):
        os.environ["CHASE_MODE"] = "log_only"
        r = self.preview()
        self.assertEqual(r["entry_guard"]["AAA"]["chase_mode"], "log_only")
        self.assertIn("CHASE_MODE=log_only: no pending, no order", self.checks(r))


if __name__ == "__main__":
    unittest.main()
