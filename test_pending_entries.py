#!/usr/bin/env python3
"""CONT / ADD_ON 3-step entry rule (MMT 2026-10-07) + old-style pending cleanup — no network.

Rule, closed bars only, identical for CONT (no Base position in the coin) and ADD_ON (Base held), HL and BX:
  1. 1D dual cross up above 1D Upper (1D Green)
  2. then a 4H retrace down to 4H Filter or 4H Lower
  3. then a 4H dual cross up above 4H Upper (4H Green) -> immediate IOC entry + Hard SL (no resting order)
  Re-arms on each new retrace. Old N/N+1 records (1D/4H Lower-Filter zone, `setup`, 7-day pullback) are
  cancelled with OLD_STYLE_REPLACED_BY_3STEP and can never trigger an order.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pending_entries as pe  # noqa: E402
from hl_sim import SimExchangeMixin  # noqa: E402
import pending_worker as pw  # noqa: E402

NOW = datetime(2026, 9, 28, 4, 10, tzinfo=timezone.utc)  # 12:10 HKT, 10 min after a 4H close
KEY = "0x" + "11" * 32
H4 = 4 * 3600 * 1000
T4 = int(NOW.timestamp() * 1000) - H4 - 600_000          # open time of the 4H bar that closed at 04:00 UTC
CREATED = NOW - timedelta(days=2)


# ----------------------------------------------------------------------------------------- fixtures
def d1(close=1.05, upper=1.20, filt=0.80, lower=0.70, trend="Green", prev_close=1.04, prev_upper=1.19):
    """1D row; default = trend alive, no fresh cross on this bar."""
    return {"close": close, "upper": upper, "filter": filt, "lower": lower, "trend": trend,
            "prev_close": prev_close, "prev_upper": prev_upper}


def d1_cross():
    """1D dual cross up above 1D Upper on the latest closed 1D bar (step 1)."""
    return d1(close=1.25, upper=1.20, prev_close=1.15, prev_upper=1.18)


def h4(close=1.00, upper=1.04, filt=0.95, lower=0.90, trend="Green", prev_close=0.99, prev_upper=1.03, t=T4):
    """4H row; default = above Filter, below Upper (no retrace, no breakout)."""
    return {"close": close, "upper": upper, "filter": filt, "lower": lower, "trend": trend,
            "prev_close": prev_close, "prev_upper": prev_upper, "bar_time": t}


def h4_retrace_filter(t=T4):
    return h4(close=0.94, t=t)                 # close <= 4H Filter 0.95, above 4H Lower 0.90


def h4_retrace_lower(t=T4):
    return h4(close=0.89, t=t)                 # close <= 4H Lower 0.90


def h4_breakout(t=T4):
    return h4(close=1.06, upper=1.04, prev_close=1.02, prev_upper=1.03, t=t)   # 4H dual cross up


def new_rec(kind=pe.CONT, breakout=False, retrace=False, **kw):
    """A record written by the 3-step create_pending."""
    entries: list = []
    rec, _ = pe.create_pending(entries, "AAA", kind, {"size_pct": kw.get("size", 3), "leverage": kw.get("lev", 2)},
                               {"tier": kw.get("tier", "small")}, pe.band(kind, None, None), CREATED)
    rec["breakout_1d"] = breakout
    rec["retrace_touched"] = retrace
    if retrace:
        rec["last_retrace_bar_t"] = T4 - H4
    return rec


def old_tia_record(created=datetime(2026, 10, 7, 0, 55, tzinfo=timezone.utc)):
    """The live old-style record (TIA_CONTINUATION_20261007), as written by the old N/N+1 executor."""
    return {"id": "TIA_CONTINUATION_20261007", "symbol": "TIA", "kind": "CONTINUATION", "status": "pending",
            "created_at": created.isoformat(), "expires_at": (created + timedelta(days=7)).isoformat(),
            "decision_date": "2026-10-07", "size_pct": 2, "leverage": 2, "reason": "", "tier": "small",
            "signal_type": "Chase", "band_tf": "1d", "zone_at_create": {"lower": 1.0, "filter": 1.2},
            "last_check": None, "history": []}


def bands(r1=None, r4=None):
    return pe.band(pe.CONT, r1 or d1(), r4 or h4())


# ======================================================================================= pure rule
class TestThreeStep(unittest.TestCase):
    def test_step1_1d_dual_cross_up_marks_breakout(self):
        rec = new_rec()
        act, why, upd = pe.evaluate(rec, bands(r1=d1_cross()), 1.0, NOW, set())
        self.assertEqual(act, "wait")
        self.assertTrue(upd["breakout_1d"])
        self.assertIn("Step 1 complete", why)

    def test_no_1d_breakout_no_entry_even_on_4h_breakout(self):
        rec = new_rec()
        act, why, upd = pe.evaluate(rec, bands(r4=h4_breakout()), 1.06, NOW, set())
        self.assertEqual((act, upd), ("wait", {}))
        self.assertIn("1D dual cross up", why)

    def test_retrace_to_4h_filter_then_4h_breakout_triggers(self):
        rec = new_rec(breakout=True)
        act, why, upd = pe.evaluate(rec, bands(r4=h4_retrace_filter(t=T4 - H4)), 0.94, NOW, set())
        self.assertEqual(act, "wait")
        self.assertEqual((upd["retrace_touched"], upd["last_retrace_bar_t"]), (True, T4 - H4))
        rec.update(upd)
        act, why, _ = pe.evaluate(rec, bands(r4=h4_breakout()), 1.06, NOW, set())
        self.assertEqual(act, "trigger")
        self.assertIn("Step 3 complete", why)

    def test_retrace_to_4h_lower_then_4h_breakout_triggers(self):
        rec = new_rec(breakout=True)
        act, _, upd = pe.evaluate(rec, bands(r4=h4_retrace_lower(t=T4 - H4)), 0.89, NOW, set())
        self.assertTrue(upd["retrace_touched"])
        rec.update(upd)
        self.assertEqual(pe.evaluate(rec, bands(r4=h4_breakout()), 1.06, NOW, set())[0], "trigger")

    def test_no_retrace_no_entry(self):
        rec = new_rec(breakout=True)                      # step 1 done, 4H never came down to Filter / Lower
        act, why, upd = pe.evaluate(rec, bands(r4=h4_breakout()), 1.06, NOW, set())
        self.assertEqual((act, upd), ("wait", {}))
        self.assertIn("waiting for 4H retrace", why)

    def test_retrace_without_4h_breakout_waits(self):
        rec = new_rec(breakout=True, retrace=True)
        act, why, _ = pe.evaluate(rec, bands(r4=h4()), 1.0, NOW, set())
        self.assertEqual(act, "wait")
        self.assertIn("4H dual cross up", why)

    def test_4h_breakout_needs_4h_green(self):
        rec = new_rec(breakout=True, retrace=True)
        r4 = dict(h4_breakout(), trend="Red")
        self.assertEqual(pe.evaluate(rec, bands(r4=r4), 1.06, NOW, set())[0], "wait")

    def test_1d_trend_dead_no_entry(self):
        # 1D closed below 1D Lower: the 1D breakout is over -> record cancelled, never an order
        rec = new_rec(breakout=True, retrace=True)
        act, why, _ = pe.evaluate(rec, bands(r1=d1(close=0.65), r4=h4_breakout()), 1.06, NOW, set())
        self.assertEqual(act, "cancel")
        self.assertIn("below 1D Lower", why)

    def test_rearm_each_new_retrace_bar_counted_once(self):
        rec = new_rec(breakout=True)
        _, _, upd = pe.evaluate(rec, bands(r4=h4_retrace_filter(t=T4 - H4)), 0.94, NOW, set())
        rec.update(upd)
        # same retrace bar again: not a new retrace
        self.assertEqual(pe.evaluate(rec, bands(r4=h4_retrace_filter(t=T4 - H4)), 0.94, NOW, set())[2], {})
        # a later retrace bar re-arms (moves the retrace marker forward)
        _, _, upd2 = pe.evaluate(rec, bands(r4=h4_retrace_lower(t=T4)), 0.89, NOW, set())
        self.assertEqual(upd2["last_retrace_bar_t"], T4)

    def test_cont_vs_add_on(self):
        self.assertEqual(pe.classify_chase("AAA", set()), pe.CONT)          # no Base position -> CONT
        self.assertEqual(pe.classify_chase("AAA", {"AAA"}), pe.ADD_ON)      # Base held -> ADD_ON
        # same 3-step trigger for both kinds
        cont = new_rec(pe.CONT, breakout=True, retrace=True)
        add = new_rec(pe.ADD_ON, breakout=True, retrace=True)
        self.assertEqual(pe.evaluate(cont, bands(r4=h4_breakout()), 1.06, NOW, set())[0], "trigger")
        self.assertEqual(pe.evaluate(add, bands(r4=h4_breakout()), 1.06, NOW, {"AAA"})[0], "trigger")
        # CONT once the coin is held / ADD_ON once the Base is gone -> cancelled
        self.assertEqual(pe.evaluate(cont, bands(r4=h4_breakout()), 1.06, NOW, {"AAA"})[0], "cancel")
        self.assertEqual(pe.evaluate(add, bands(r4=h4_breakout()), 1.06, NOW, set())[0], "cancel")

    def test_live_mid_must_be_above_4h_lower(self):
        rec = new_rec(breakout=True, retrace=True)
        act, why, _ = pe.evaluate(rec, bands(r4=h4_breakout()), 0.85, NOW, set())
        self.assertEqual(act, "wait")
        self.assertIn("not above 4H Lower", why)

    def test_bars_closed_before_creation_ignored(self):
        rec = new_rec(breakout=True, retrace=True)
        old_t = int(CREATED.timestamp() * 1000) - 2 * H4
        self.assertEqual(pe.evaluate(rec, bands(r4=h4_breakout(t=old_t)), 1.06, NOW, set())[0], "wait")

    def test_missing_4h_data_fails_closed(self):
        rec = new_rec(breakout=True, retrace=True)
        r4 = h4_breakout()
        r4.pop("bar_time")
        self.assertEqual(pe.evaluate(rec, bands(r4=r4), 1.06, NOW, set())[0], "wait")

    def test_expiry(self):
        rec = new_rec(breakout=True, retrace=True)
        later = CREATED + timedelta(days=pe.PENDING_TTL_DAYS, minutes=1)
        self.assertEqual(pe.evaluate(rec, bands(r4=h4_breakout(t=int(later.timestamp() * 1000) - H4)),
                                     1.06, later, set())[0], "expire")

    def test_create_idempotent_and_records_breakout(self):
        entries: list = []
        r1, c1 = pe.create_pending(entries, "AAA", pe.CONT, {"size_pct": 3, "leverage": 2}, {"tier": "tiny"},
                                   pe.band(pe.CONT, d1_cross(), h4()), NOW)
        r2, c2 = pe.create_pending(entries, "AAA", pe.CONT, {"size_pct": 3}, {}, pe.band(pe.CONT, None, None), NOW)
        self.assertEqual((c1, c2, len(entries)), (True, False, 1))
        self.assertTrue(r1["breakout_1d"])                       # created on the 1D cross bar: step 1 done
        self.assertFalse(pe.is_old_style(r1))


# ======================================================================================= old style
class TestOldStyle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"PENDING_PATH": os.path.join(self.tmp, "p.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def store(self, entries):
        pe.save_pending(entries)

    def test_detection(self):
        self.assertTrue(pe.is_old_style(old_tia_record()))
        self.assertTrue(pe.is_old_style({"id": "x", "status": "pending", "kind": "ADD_ON", "setup": {"t": 1}}))
        self.assertFalse(pe.is_old_style(new_rec()))

    def test_evaluate_refuses_old_style_even_on_a_perfect_signal(self):
        rec = old_tia_record()
        rec.update(breakout_1d=True, retrace_touched=True)         # even if fields look "armed"
        act, why, _ = pe.evaluate(rec, bands(r4=h4_breakout()), 1.06, NOW, set())
        self.assertEqual((act, why), ("cancel", pe.OLD_STYLE_REASON))

    def test_startup_cleanup_cancels_old_style_and_logs_reason(self):
        import serve
        keep = new_rec()
        self.store([old_tia_record(), keep])
        with patch("sys.stderr") as err:
            r = serve._cleanup_old_style_pending_at_boot()
        self.assertEqual(r["cancelled"], ["TIA_CONTINUATION_20261007"])
        logged = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("TIA_CONTINUATION_20261007", logged)
        self.assertIn("OLD_STYLE_REPLACED_BY_3STEP", logged)
        stored = {e["id"]: e for e in pe.load_pending()}
        self.assertEqual(stored["TIA_CONTINUATION_20261007"]["status"], "cancelled")
        self.assertEqual(stored["TIA_CONTINUATION_20261007"]["close_reason"], pe.OLD_STYLE_REASON)
        self.assertEqual(stored[keep["id"]]["status"], "pending")   # 3-step records untouched

    def _live_worker(self, hl, entries=None):
        r1d = {"ts": (NOW - timedelta(minutes=5)).isoformat(), "rows": [dict(symbol="TIA", **d1())]}
        r4h = {"ts": (NOW - timedelta(minutes=5)).isoformat(), "rows": [dict(symbol="TIA", **h4_breakout())]}
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": KEY,
                                     "PENDING_CONTINUATION_DISABLED": "0"}):
            return pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=NOW, entries=entries,
                                  log_entry_fn=lambda **k: None)

    def test_old_style_cancelled_at_startup_never_triggers_an_order(self):
        import serve
        self.store([old_tia_record()])
        serve._cleanup_old_style_pending_at_boot()
        hl = FakeHL(mids={"TIA": 1.06}, meta={"TIA": {"szDecimals": 1, "maxLeverage": 5.0}})
        res = self._live_worker(hl)                                # 4H :10 pass on a "perfect" 4H breakout
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["filled"], [])
        self.assertEqual(pe.load_pending()[0]["status"], "cancelled")

    def test_worker_cancels_old_style_at_start_of_every_run(self):
        self.store([old_tia_record()])
        hl = FakeHL(mids={"TIA": 1.06}, meta={"TIA": {"szDecimals": 1, "maxLeverage": 5.0}})
        res = self._live_worker(hl)
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["cancelled"][0], {"id": "TIA_CONTINUATION_20261007", "symbol": "TIA",
                                               "kind": "CONTINUATION", "reason": pe.OLD_STYLE_REASON})
        self.assertEqual(pe.load_pending()[0]["close_reason"], pe.OLD_STYLE_REASON)

    def test_worker_cannot_act_on_old_style_even_if_cleanup_failed(self):
        ents = [old_tia_record()]
        hl = FakeHL(mids={"TIA": 1.06}, meta={"TIA": {"szDecimals": 1, "maxLeverage": 5.0}})
        with patch.object(pw, "cancel_old_style", lambda *a, **k: []):   # cleanup broken
            res = self._live_worker(hl, entries=ents)
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["filled"], [])
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertEqual(res["checked"][0]["reason"], pe.OLD_STYLE_REASON)

    def test_disabled_worker_reports_old_style_reason(self):
        self.store([old_tia_record()])
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": KEY}):
            os.environ.pop("PENDING_CONTINUATION_DISABLED", None)
            res = pw.run_pending(hl=FakeHL(), radar_1d={}, radar_4h={}, now=NOW)
        self.assertEqual([c["reason"] for c in res["cancelled"]], [pe.OLD_STYLE_REASON])


# ======================================================================================= HL worker
class FakeHL(SimExchangeMixin):
    def __init__(self, equity=1000.0, margin_used=0.0, positions=None, mids=None, meta=None, fill=True, lev=None,
                 sl_ok=True):
        self.equity, self.margin_used, self.positions = equity, margin_used, positions or []
        self.mids = mids or {}
        self._meta = meta or {"AAA": {"szDecimals": 0, "maxLeverage": 5.0}}
        self.fill, self.lev, self.sl_ok, self.calls = fill, lev, sl_ok, []
        self.orders = []

    def spot_state(self):
        return {"balances": [{"coin": "USDC", "total": str(self.equity)}]}

    def perp_state(self):
        aps = []
        for p in self.positions:
            q = dict(p)
            if self.lev:
                q["leverage"] = {"type": "isolated", "value": self.lev}
            aps.append({"position": q})
        return {"marginSummary": {"totalMarginUsed": str(self.margin_used)}, "assetPositions": aps}

    def meta(self):
        return self._meta

    def all_mids(self):
        return self.mids

    def open_orders(self):
        return self.orders


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"PENDING_PATH": os.path.join(self.tmp, "p.json"), "EXEC_DRY_RUN": "0",
                                           "HL_API_PRIVATE_KEY": KEY, "PENDING_CONTINUATION_DISABLED": "0"})
        self.env.start()
        self.log = []

    def tearDown(self):
        self.env.stop()

    def run_w(self, hl, entries, r1=None, r4=None, now=NOW):
        ts = (now - timedelta(minutes=5)).isoformat()
        r1d = {"ts": ts, "rows": [dict(symbol="AAA", **(r1 or d1()))]}
        r4h = {"ts": ts, "rows": [dict(symbol="AAA", **(r4 or h4_breakout()))]}
        return pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=now, entries=entries,
                              log_entry_fn=lambda **k: self.log.append(k))

    def test_cont_full_sequence_three_4h_runs_then_ioc_entry_with_hard_sl(self):
        ents = [new_rec(pe.CONT, size=6)]          # approved 6% -> clamped to 4% cap
        hl = FakeHL(mids={"AAA": 1.06})
        n1, n2, n3 = NOW - timedelta(hours=8), NOW - timedelta(hours=4), NOW
        t = lambda n: int(n.timestamp() * 1000) - H4 - 600_000  # noqa: E731
        self.run_w(hl, ents, r1=d1_cross(), r4=h4(t=t(n1)), now=n1)                 # step 1
        self.assertTrue(ents[0]["breakout_1d"])
        self.run_w(hl, ents, r4=h4_retrace_filter(t=t(n2)), now=n2)                 # step 2
        self.assertTrue(ents[0]["retrace_touched"])
        self.assertEqual(hl.calls, [])                                              # nothing resting / sent
        res = self.run_w(hl, ents, r4=h4_breakout(t=t(n3)), now=n3)                 # step 3 -> entry
        self.assertEqual(res["status"], "success", res)
        self.assertEqual([c[0] for c in hl.calls], ["set_leverage", "open_long_ioc", "place_stop_loss"])
        f = res["filled"][0]
        self.assertEqual((f["size_pct"], f["leverage"], f["hard_sl"]), (4.0, 2, 0.95))  # small -> 4H Filter
        self.assertAlmostEqual(hl.calls[1][3], round(1.06 * 1.005, 6), 5)          # IOC limit = mid + slippage
        self.assertEqual(ents[0]["status"], "filled")
        # idempotent: the next run does nothing
        hl2 = FakeHL(mids={"AAA": 1.06})
        self.run_w(hl2, ents)
        self.assertEqual(hl2.calls, [])

    def test_no_retrace_no_order(self):
        ents = [new_rec(breakout=True)]
        hl = FakeHL(mids={"AAA": 1.06})
        res = self.run_w(hl, ents)
        self.assertEqual(hl.calls, [])
        self.assertIn("waiting for 4H retrace", res["checked"][0]["reason"])

    def test_1d_trend_dead_no_order(self):
        ents = [new_rec(breakout=True, retrace=True)]
        hl = FakeHL(mids={"AAA": 1.06})
        res = self.run_w(hl, ents, r1=d1(close=0.65))
        self.assertEqual(hl.calls, [])
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertIn("below 1D Lower", res["cancelled"][0]["reason"])

    def test_dry_run_places_nothing_and_does_not_mutate(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1"}):
            hl = FakeHL(mids={"AAA": 1.06})
            ents = [new_rec(breakout=True, retrace=True)]
            res = self.run_w(hl, ents)
        self.assertEqual(hl.calls, [])
        self.assertTrue(res["filled"][0]["dry_run"])
        self.assertEqual(ents[0]["status"], "pending")

    def test_sl_failure_closes_fill(self):
        hl = FakeHL(mids={"AAA": 1.06}, sl_ok=False)
        ents = [new_rec(breakout=True, retrace=True)]
        res = self.run_w(hl, ents)
        self.assertEqual([c[0] for c in hl.calls][-1], "market_close")
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertTrue(any("sl_failed_closed" in a for a in res["alerts"]))

    def test_sl_distance_blocks_fill(self):
        hl = FakeHL(mids={"AAA": 1.06})
        ents = [new_rec(breakout=True, retrace=True)]
        res = self.run_w(hl, ents, r4=dict(h4_breakout(), filter=1.05))   # Hard SL 1.05 within 1.5% of 1.06
        self.assertEqual(hl.calls, [])
        self.assertEqual(ents[0]["status"], "pending")
        self.assertIn("SL distance", res["checked"][0]["reason"])

    def test_margin_cap_blocks_fill(self):
        hl = FakeHL(mids={"AAA": 1.06}, margin_used=790)
        ents = [new_rec(breakout=True, retrace=True)]
        res = self.run_w(hl, ents)
        self.assertEqual(hl.calls, [])
        self.assertIn("margin utilization", res["checked"][0]["reason"])

    def test_stale_radar_blocks_fill(self):
        hl = FakeHL(mids={"AAA": 1.06})
        ents = [new_rec(breakout=True, retrace=True)]
        r1d = {"ts": (NOW - timedelta(minutes=5)).isoformat(), "rows": [dict(symbol="AAA", **d1())]}
        r4h = {"ts": (NOW - timedelta(hours=9)).isoformat(), "rows": [dict(symbol="AAA", **h4_breakout())]}
        res = pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=NOW, entries=ents, log_entry_fn=lambda **k: None)
        self.assertEqual(hl.calls, [])
        self.assertIn("stale", res["checked"][0]["reason"])

    def _add_on(self, entry_px=0.9, szi=50):
        pos = {"coin": "AAA", "szi": str(szi), "entryPx": str(entry_px), "liquidationPx": "0.4",
               "returnOnEquity": "0.30", "marginUsed": "15"}
        hl = FakeHL(mids={"AAA": 1.06}, positions=[pos], lev=3)
        ents = [new_rec(pe.ADD_ON, breakout=True, retrace=True, tier="large")]   # large: Hard SL = 4H Lower 0.90
        return hl, ents, self.run_w(hl, ents)

    def test_add_on_same_rule_keeps_existing_leverage(self):
        hl, ents, res = self._add_on()
        self.assertEqual(ents[0]["status"], "filled", res["checked"])
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 3))   # existing isolated leverage
        self.assertEqual(res["filled"][0]["hard_sl"], 0.90)

    def test_add_on_requires_price_gain_10pct(self):
        hl, ents, res = self._add_on(entry_px=1.0)                   # 1.06 / 1.0 = +6%
        self.assertEqual(hl.calls, [])
        self.assertIn("price gain", res["checked"][0]["reason"])

    def test_add_on_cancelled_when_base_closed(self):
        hl = FakeHL(mids={"AAA": 1.06})
        ents = [new_rec(pe.ADD_ON, breakout=True, retrace=True)]
        res = self.run_w(hl, ents)
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertEqual(hl.calls, [])
        self.assertIn("base position no longer held", res["cancelled"][0]["reason"])


# ======================================================================================= BX path
class TestBXSameRule(unittest.TestCase):
    """bx_live.run_live_pending uses the same evaluate(): 3-step trigger -> IOC entry; old style cancelled."""

    def setUp(self):
        import bx_live
        self.L = bx_live
        self.tmp = Path(tempfile.mkdtemp())
        self._out = bx_live.OUT_DIR
        bx_live.OUT_DIR = self.tmp

    def tearDown(self):
        self.L.OUT_DIR = self._out

    def run_bx(self, recs, r4, r1=None):
        import bx_radar
        rows = {"1d": {"rows": [dict(bx_symbol="AAAUSDT", **(r1 or d1()))]},
                "4h": {"rows": [dict(bx_symbol="AAAUSDT", **r4)]}}
        self.L.save_live_pending(recs)
        entered = []

        def fake_enter(api, conn, c, meta_all, acct, nav, now, market, tiers_fn, rep, rec):
            entered.append(rec["symbol"])
            return True
        with patch.object(bx_radar, "load_radar", lambda tf: rows[tf]), \
                patch.object(self.L, "open_live_trades", lambda conn: []), \
                patch.object(self.L, "_try_enter_pending", fake_enter):
            out = self.L.run_live_pending(None, None, NOW, True, [], {"AAAUSDT": {"price": 1.06}}, {}, 1000.0,
                                          None, None)
        return out, entered, self.L.load_live_pending()

    def bx_rec(self, **kw):
        r = new_rec(**kw)
        r.update(id="AAAUSDT_CONT_20260926", symbol="AAAUSDT")
        return r

    def test_bx_three_step_trigger_enters(self):
        out, entered, stored = self.run_bx([self.bx_rec(breakout=True, retrace=True)], h4_breakout())
        self.assertEqual(entered, ["AAAUSDT"])
        self.assertEqual(stored[0]["status"], "filled")

    def test_bx_no_retrace_no_entry(self):
        out, entered, stored = self.run_bx([self.bx_rec(breakout=True)], h4_breakout())
        self.assertEqual(entered, [])
        self.assertEqual(stored[0]["status"], "pending")

    def test_bx_old_style_cancelled_never_entered(self):
        old = dict(old_tia_record(), id="AAAUSDT_CONTINUATION_20261007", symbol="AAAUSDT")
        out, entered, stored = self.run_bx([old], h4_breakout())
        self.assertEqual(entered, [])
        self.assertEqual((stored[0]["status"], stored[0]["close_reason"]), ("cancelled", pe.OLD_STYLE_REASON))
        self.assertEqual(out[0]["reason"], pe.OLD_STYLE_REASON)

    def test_bx_startup_cleanup(self):
        old = dict(old_tia_record(), id="AAAUSDT_CONTINUATION_20261007", symbol="AAAUSDT")
        self.L.save_live_pending([old, self.bx_rec()])
        r = self.L.cleanup_old_style_live_pending(NOW)
        self.assertEqual(r["cancelled"], ["AAAUSDT_CONTINUATION_20261007"])
        st = [e["status"] for e in self.L.load_live_pending()]
        self.assertEqual(st, ["cancelled", "pending"])


if __name__ == "__main__":
    unittest.main()
