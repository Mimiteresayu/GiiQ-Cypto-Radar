#!/usr/bin/env python3
"""Tests for pending 3-step entries (ADD_ON / CONT) — no network."""
from __future__ import annotations

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

NOW = datetime(2026, 10, 7, 4, 10, tzinfo=timezone.utc)  # 12:10 HKT
KEY = "0x" + "11" * 32
H4 = 4 * 3600 * 1000
T0 = int(NOW.timestamp() * 1000)


def _row(lower, filt, close, trend="Green", upper=None):
    return {"lower": lower, "filter": filt, "close": close, "trend": trend, "upper": upper or filt * 1.1}


class FakeHL(SimExchangeMixin):
    def __init__(self, equity=1000.0, margin_used=0.0, positions=None, mids=None, meta=None, fill=True, lev=None):
        self.equity, self.margin_used, self.positions = equity, margin_used, positions or []
        self.mids = mids or {}
        self._meta = meta or {"AAA": {"szDecimals": 0, "maxLeverage": 5.0}}
        self.fill, self.lev, self.calls = fill, lev, []
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


class TestEvaluate(unittest.TestCase):
    """Tests for the new 3-step evaluation (1D breakout -> 4H retrace -> 4H breakout)."""
    
    def rec(self, kind=pe.ADD_ON, created=NOW - timedelta(days=1), 
            breakout_1d=False, retrace_touched=False, last_retrace_bar_t=None):
        """Create a pending record with 3-step state."""
        r = {"id": "x", "symbol": "AAA", "kind": kind, "status": "pending", 
             "created_at": created.isoformat(),
             "expires_at": (created + timedelta(days=7)).isoformat(),
             "breakout_1d": breakout_1d,
             "retrace_touched": retrace_touched,
             "last_retrace_bar_t": last_retrace_bar_t}
        if breakout_1d:
            r["breakout_date_1d"] = created.strftime("%Y-%m-%d")
        return r

    def bnd(self, d1d_close=1.1, d1d_upper=1.0, d1d_prev_close=0.95, d1d_prev_upper=1.0, d1d_lower=0.9,
            d4h_close=1.05, d4h_upper=1.0, d4h_prev_close=0.95, d4h_prev_upper=1.0, 
            d4h_filter=0.95, d4h_lower=0.9, d4h_trend="Green", d1d_trend="Green"):
        """Create a band dict with 1d and 4h data."""
        return {
            "1d": {
                "close": d1d_close,
                "upper": d1d_upper,
                "prev_close": d1d_prev_close,
                "prev_upper": d1d_prev_upper,
                "lower": d1d_lower,
                "trend": d1d_trend,
                "bar_time": T0,
            },
            "4h": {
                "close": d4h_close,
                "upper": d4h_upper,
                "prev_close": d4h_prev_close,
                "prev_upper": d4h_prev_upper,
                "filter": d4h_filter,
                "lower": d4h_lower,
                "trend": d4h_trend,
                "bar_time": T0,
            }
        }

    HELD = {"AAA"}

    def test_step1_1d_breakout_detected(self):
        """Step 1: 1D dual cross up above 1D Upper (green) -> mark breakout_1d."""
        r = self.rec(breakout_1d=False)
        # 1D close 1.1 > upper 1.0, prev_close 0.95 <= prev_upper 1.0, Green
        bnd = self.bnd(d1d_close=1.1, d1d_upper=1.0, d1d_prev_close=0.95, d1d_prev_upper=1.0, d1d_trend="Green")
        a, why, upd = pe.evaluate(r, bnd, 1.05, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertIn("Step 1 complete: 1D breakout", why)
        self.assertTrue(upd["breakout_1d"])

    def test_step1_waiting_for_1d_breakout(self):
        """Step 1: no 1D cross-up yet -> wait."""
        r = self.rec(breakout_1d=False)
        bnd = self.bnd(d1d_close=0.95, d1d_upper=1.0)  # no cross-up
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertIn("waiting for 1D dual cross up", why)
        self.assertEqual(upd, {})

    def test_step2_4h_retrace_detected(self):
        """Step 2: after 1D breakout, 4H close <= 4H Filter -> mark retrace_touched."""
        r = self.rec(breakout_1d=True, retrace_touched=False)
        # 4H close 0.94 <= filter 0.95
        bnd = self.bnd(d4h_close=0.94, d4h_filter=0.95)
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertIn("Step 2 complete: 4H retrace", why)
        self.assertTrue(upd["retrace_touched"])
        self.assertEqual(upd["last_retrace_bar_t"], T0)

    def test_step2_waiting_for_retrace(self):
        """Step 2: after 1D breakout, no retrace yet -> wait."""
        r = self.rec(breakout_1d=True, retrace_touched=False)
        bnd = self.bnd(d4h_close=1.0, d4h_filter=0.95)  # no retrace
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertIn("waiting for 4H retrace", why)
        self.assertEqual(upd, {})

    def test_step3_4h_breakout_triggers(self):
        """Step 3: after retrace, 4H dual cross up above 4H Upper -> trigger."""
        r = self.rec(breakout_1d=True, retrace_touched=True, last_retrace_bar_t=T0 - H4)
        # 4H close 1.05 > upper 1.0, prev_close 0.95 <= prev_upper 1.0, Green
        bnd = self.bnd(d4h_close=1.05, d4h_upper=1.0, d4h_prev_close=0.95, d4h_prev_upper=1.0)
        a, why, upd = pe.evaluate(r, bnd, 1.02, NOW, self.HELD)
        self.assertEqual(a, "trigger")
        self.assertIn("Step 3 complete: 4H breakout", why)

    def test_step3_waiting_for_4h_breakout(self):
        """Step 3: after retrace, no 4H cross-up yet -> wait."""
        r = self.rec(breakout_1d=True, retrace_touched=True, last_retrace_bar_t=T0 - H4)
        bnd = self.bnd(d4h_close=0.99, d4h_upper=1.0)  # no cross-up
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertIn("waiting for", why.lower())

    def test_1d_close_below_lower_cancels(self):
        """Cancel if 1D close < 1D Lower."""
        r = self.rec(breakout_1d=True)
        bnd = self.bnd(d1d_close=0.85, d1d_lower=0.9)
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "cancel")
        self.assertIn("below 1D Lower", why)

    def test_expiry(self):
        """Expired records are cancelled."""
        r = self.rec(created=NOW - timedelta(days=8))
        bnd = self.bnd()
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "expire")

    def test_cont_coin_already_held_cancels(self):
        """CONT when coin already held -> cancel."""
        r = self.rec(kind=pe.CONT, breakout_1d=True)
        bnd = self.bnd()
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, {"AAA"})
        self.assertEqual(a, "cancel")
        self.assertIn("already held", why)

    def test_addon_base_gone_cancels(self):
        """ADD_ON when base position gone -> cancel."""
        r = self.rec(kind=pe.ADD_ON, breakout_1d=True)
        bnd = self.bnd()
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, set())
        self.assertEqual(a, "cancel")
        self.assertIn("no longer held", why)

    def test_missing_bar_fails_closed(self):
        """Missing bar time -> wait without state change."""
        r = self.rec()
        bnd = self.bnd()
        bnd["4h"]["bar_time"] = None
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertEqual(upd, {})

    def test_rearm_after_new_retrace(self):
        """After a retrace, next 4H cross-up can trigger again."""
        # First retrace
        r1 = self.rec(breakout_1d=True, retrace_touched=True, last_retrace_bar_t=T0 - 2 * H4)
        # New retrace (newer bar)
        bnd = self.bnd(d4h_close=0.94, d4h_filter=0.95)
        bnd["4h"]["bar_time"] = T0  # newer bar
        a, why, upd = pe.evaluate(r1, bnd, 1.0, NOW, self.HELD)
        self.assertEqual(a, "wait")
        self.assertIn("Step 2 complete", why)
        self.assertEqual(upd["last_retrace_bar_t"], T0)

    def test_create_idempotent(self):
        """Test that create_pending is idempotent for same symbol/kind/date."""
        entries = []
        bnd = self.bnd()
        cand = {"symbol": "AAA", "type": "CONT"}
        decision = {"size_pct": 3, "leverage": 2}
        r1, c1 = pe.create_pending(entries, "AAA", pe.ADD_ON, decision, cand, bnd, NOW)
        decision2 = {"size_pct": 3, "leverage": 4}
        r2, c2 = pe.create_pending(entries, "AAA", pe.ADD_ON, decision2, cand, bnd, NOW)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(len(entries), 1)
        self.assertEqual(r1["expires_at"], (NOW + timedelta(days=7)).isoformat())
        # Original values preserved (not overwritten by second call)
        self.assertEqual(r1["size_pct"], 3)
        self.assertEqual(r1["leverage"], 2)


class TestRealRadarShape(unittest.TestCase):
    """Test with REAL radar row shape (no prev fields, only dual_cross_up flag)."""
    
    def real_row(self, symbol="AAA", close=1.05, upper=1.0, lower=0.9, filter=0.95, 
                 trend="Green", dual_cross_up=False):
        """Real production radar row shape (from /api/ai/candidates DESK_DATA)."""
        return {
            "symbol": symbol,
            "close": close,
            "upper": upper,
            "lower": lower,
            "filter": filter,
            "trend": trend,
            "dual_cross_up": dual_cross_up,
            "dual_cross_down": False,
            "bar_time": T0,
        }
    
    def real_bnd(self, d1d_close=1.1, d1d_upper=1.0, d1d_lower=0.9, d1d_dual_cross_up=True,
                 d4h_close=1.05, d4h_upper=1.0, d4h_filter=0.95, d4h_lower=0.9, d4h_dual_cross_up=False):
        """Real band dict using radar rows with dual_cross_up flag (no prev fields)."""
        row_1d = self.real_row(close=d1d_close, upper=d1d_upper, lower=d1d_lower, 
                               dual_cross_up=d1d_dual_cross_up)
        row_4h = self.real_row(close=d4h_close, upper=d4h_upper, lower=d4h_lower, 
                               filter=d4h_filter, dual_cross_up=d4h_dual_cross_up)
        return pe.band("CONT", row_1d, row_4h)
    
    def rec(self, breakout_1d=False, retrace_touched=False, last_retrace_bar_t=None):
        """Create a pending record."""
        return {
            "id": "test", "symbol": "AAA", "kind": pe.CONT, "status": "pending",
            "created_at": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=6)).isoformat(),
            "breakout_1d": breakout_1d,
            "retrace_touched": retrace_touched,
            "last_retrace_bar_t": last_retrace_bar_t,
        }
    
    def test_step1_with_dual_cross_up_flag(self):
        """Step 1 detects 1D breakout using dual_cross_up flag (real radar shape)."""
        r = self.rec(breakout_1d=False)
        bnd = self.real_bnd(d1d_close=1.1, d1d_upper=1.0, d1d_dual_cross_up=True)
        a, why, upd = pe.evaluate(r, bnd, 1.05, NOW, set())
        self.assertEqual(a, "wait")
        self.assertIn("Step 1 complete", why)
        self.assertTrue(upd["breakout_1d"])
    
    def test_step1_accepts_existing_breakout(self):
        """Step 1 accepts existing breakout if still Green and above Upper."""
        r = self.rec(breakout_1d=False)
        # Close above Upper, Green, but dual_cross_up=False (older breakout)
        bnd = self.real_bnd(d1d_close=1.1, d1d_upper=1.0, d1d_dual_cross_up=False)
        a, why, upd = pe.evaluate(r, bnd, 1.05, NOW, set())
        self.assertEqual(a, "wait")
        self.assertIn("Step 1 complete", why)
        self.assertTrue(upd["breakout_1d"])
    
    def test_step3_with_dual_cross_up_flag(self):
        """Step 3 triggers using dual_cross_up flag (real radar shape)."""
        r = self.rec(breakout_1d=True, retrace_touched=True, last_retrace_bar_t=T0 - H4)
        bnd = self.real_bnd(d4h_close=1.05, d4h_upper=1.0, d4h_dual_cross_up=True)
        a, why, upd = pe.evaluate(r, bnd, 1.02, NOW, set())
        self.assertEqual(a, "trigger")
        self.assertIn("Step 3 complete", why)
    
    def test_no_prev_fields_no_dual_cross_up_waits(self):
        """Without prev fields or dual_cross_up flag, step waits."""
        r = self.rec(breakout_1d=False)
        bnd = self.real_bnd(d1d_close=0.95, d1d_upper=1.0, d1d_dual_cross_up=False)
        a, why, upd = pe.evaluate(r, bnd, 1.0, NOW, set())
        self.assertEqual(a, "wait")
        self.assertIn("waiting for 1d", why.lower())
        self.assertEqual(upd, {})


class TestOldStyleCleanup(unittest.TestCase):
    """Tests for old-style (N/N+1 zone trigger) pending cleanup."""
    
    def test_is_old_style_with_setup_field(self):
        """Records with 'setup' dict are old-style."""
        e = {"status": "pending", "kind": pe.CONT, "setup": {"t": T0, "l": 0.8, "c": 0.9}}
        self.assertTrue(pe.is_old_style_record(e))
    
    def test_is_old_style_with_zone_at_create(self):
        """Records with 'zone_at_create' are old-style."""
        e = {"status": "pending", "kind": pe.CONT, "zone_at_create": [0.8, 0.9]}
        self.assertTrue(pe.is_old_style_record(e))
    
    def test_is_old_style_missing_new_fields(self):
        """Records missing new 3-step fields are old-style."""
        e = {"status": "pending", "kind": pe.CONT}
        self.assertTrue(pe.is_old_style_record(e))
    
    def test_is_not_old_style_with_new_fields(self):
        """Records with new 3-step fields are NOT old-style."""
        e = {"status": "pending", "kind": pe.CONT, "breakout_1d": False, "last_retrace_bar_t": None}
        self.assertFalse(pe.is_old_style_record(e))
    
    def test_cancel_old_style_pending(self):
        """cancel_old_style_pending cancels all old-style records."""
        entries = [
            {"id": "old1", "status": "pending", "kind": pe.CONT, "setup": {"t": T0}},
            {"id": "new1", "status": "pending", "kind": pe.CONT, "breakout_1d": False, "last_retrace_bar_t": None},
            {"id": "old2", "status": "pending", "kind": pe.ADD_ON, "zone_at_create": [0.8, 0.9]},
        ]
        gone = pe.cancel_old_style_pending(entries, NOW)
        self.assertEqual(len(gone), 2)
        self.assertEqual({e["id"] for e in gone}, {"old1", "old2"})
        self.assertEqual(entries[0]["status"], "cancelled")
        self.assertEqual(entries[0]["close_reason"], "OLD_STYLE_REPLACED_BY_3STEP")
        self.assertEqual(entries[1]["status"], "pending")  # new one unchanged


class TestBXFallback(unittest.TestCase):
    """Tests for BX RAILWAY_FALLBACK logic."""
    
    def test_build_fallback_decisions(self):
        """BX fallback approves all candidates at 2% / 2x."""
        import bx_live
        cand_doc = {
            "date": "2026-10-07",
            "candidates": [
                {"symbol": "BTC", "type": "Base"},
                {"symbol": "ETH", "type": "Base"},
            ]
        }
        fb_list, why = bx_live.build_fallback_decisions(cand_doc, NOW)
        self.assertEqual(len(fb_list), 2)
        self.assertEqual(fb_list[0]["symbol"], "BTC")
        self.assertEqual(fb_list[0]["decision"], "approve")
        self.assertEqual(fb_list[0]["size_pct"], 2.0)
        self.assertEqual(fb_list[0]["leverage"], 2)
        self.assertIn("RAILWAY_FALLBACK", fb_list[0]["reason"])
    
    def test_fallback_wrong_date_no_decisions(self):
        """Fallback rejects candidates from wrong date."""
        import bx_live
        cand_doc = {
            "date": "2026-10-06",  # wrong date
            "candidates": [{"symbol": "BTC", "type": "Base"}]
        }
        fb_list, why = bx_live.build_fallback_decisions(cand_doc, NOW)
        self.assertEqual(len(fb_list), 0)
        self.assertIn("not generated today", why)


if __name__ == "__main__":
    unittest.main()
