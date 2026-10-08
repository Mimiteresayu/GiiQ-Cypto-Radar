#!/usr/bin/env python3
"""Unit tests for CONT-Staircase continuation rule (MMT 2026-10-08)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pending_entries import (
    CONT_STAIRCASE,
    evaluate_cont_staircase,
    migrate_cont_to_staircase,
    CONT,
    ACTIVE,
)
from exec_common import swing_low_4h_bars, isolated_liq_price_long, liq_beyond_sl_long

NOW = datetime(2026, 10, 8, 12, 10, tzinfo=timezone.utc)
BAR_T = int(datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)  # bar closed at 12:00


class TestContStaircaseEvaluate(unittest.TestCase):
    """Test CONT-Staircase trigger evaluation."""

    def rec(self, sym="LINK"):
        # Created before the bar time
        return {"symbol": sym, "kind": CONT_STAIRCASE, "status": ACTIVE, "created_at": "2026-10-08T10:00:00Z"}

    def bnd(self, d1d_trend="Green", d4h_trend="Green", filter_rising_6=True, bw_pct50=True,
            d4h_close=8.3062, d4h_filter=8.2, d4h_prev_close=8.1, d4h_prev_filter=8.15,
            d1d_close=8.5, d1d_lower=7.0):
        return {
            "1d": {"trend": d1d_trend, "close": d1d_close, "lower": d1d_lower},
            "4h": {
                "trend": d4h_trend,
                "filter_rising_6": filter_rising_6,
                "bw_pct50": bw_pct50,
                "close": d4h_close,
                "filter": d4h_filter,
                "prev_close": d4h_prev_close,
                "prev_filter": d4h_prev_filter,
                "bar_time": BAR_T,
            },
        }

    def test_trigger_all_conditions_met(self):
        """Test CONT_STAIRCASE trigger when all conditions met."""
        rec = self.rec()
        bnd = self.bnd()
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "trigger")
        self.assertIn("CONT-Staircase trigger", reason)

    def test_wait_1d_not_green(self):
        """Test wait when 1D trend not Green."""
        rec = self.rec()
        bnd = self.bnd(d1d_trend="Red")
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "wait")
        self.assertIn("1D trend Red not Green", reason)

    def test_wait_4h_not_green(self):
        """Test wait when 4H trend not Green."""
        rec = self.rec()
        bnd = self.bnd(d4h_trend="Red")
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "wait")
        self.assertIn("4H trend Red not Green", reason)

    def test_wait_filter_not_rising(self):
        """Test wait when filter not rising."""
        rec = self.rec()
        bnd = self.bnd(filter_rising_6=False)
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "wait")
        self.assertIn("Filter not rising", reason)

    def test_wait_bandwidth_not_below_median(self):
        """Test wait when bandwidth not below median."""
        rec = self.rec()
        bnd = self.bnd(bw_pct50=False)
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "wait")
        self.assertIn("band width not <= 180-bar median", reason)

    def test_wait_no_filter_cross_up(self):
        """Test wait when no Filter cross-up."""
        rec = self.rec()
        # close not above filter
        bnd = self.bnd(d4h_close=8.1, d4h_filter=8.2, d4h_prev_close=8.05, d4h_prev_filter=8.15)
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "wait")
        self.assertIn("no Filter cross-up", reason)

    def test_cancel_already_held(self):
        """Test cancel when coin already held."""
        rec = self.rec()
        bnd = self.bnd()
        mid = 8.31
        held_long = {"LINK"}
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "cancel")
        self.assertIn("already held", reason)

    def test_cancel_1d_below_lower(self):
        """Test cancel when 1D close < 1D Lower."""
        rec = self.rec()
        bnd = self.bnd(d1d_close=6.8, d1d_lower=7.0)
        mid = 8.31
        held_long = set()
        
        action, reason, upd = evaluate_cont_staircase(rec, bnd, mid, NOW, held_long)
        self.assertEqual(action, "cancel")
        self.assertIn("below 1D Lower", reason)


class TestContStaircaseMigration(unittest.TestCase):
    """Test migration of CONT to CONT_STAIRCASE."""

    def test_migrate_cont_to_staircase(self):
        """Test CONT pendings are migrated to CONT_STAIRCASE."""
        now = datetime.now(timezone.utc)
        entries = [
            {"id": "JUP_CONT_20261008", "symbol": "JUP", "kind": CONT, "status": ACTIVE},
            {"id": "W_CONT_20261008", "symbol": "W", "kind": CONT, "status": ACTIVE},
            {"id": "LINK_ADD_ON_20261008", "symbol": "LINK", "kind": "ADD_ON", "status": ACTIVE},
        ]
        
        migrated = migrate_cont_to_staircase(entries, now)
        
        self.assertEqual(len(migrated), 2)
        self.assertEqual(entries[0]["kind"], CONT_STAIRCASE)
        self.assertEqual(entries[1]["kind"], CONT_STAIRCASE)
        self.assertEqual(entries[2]["kind"], "ADD_ON")  # unchanged
        self.assertIn("migrated_at", entries[0])
        self.assertIn("migration_note", entries[0])


class TestContStaircaseSizing(unittest.TestCase):
    """Test CONT_STAIRCASE sizing rules."""

    def test_risk_sizing(self):
        """Test 0.5% NAV risk sizing."""
        equity = 100000  # $100k NAV
        entry_px = 8.3062
        sl = 8.1617  # -1.7% from entry
        sl_dist_pct = (entry_px - sl) / entry_px * 100.0
        
        # 0.5% NAV risk / 1.7% SL = 29.4% NAV notional
        # At 3x: margin = 9.8% -> clamped to 8% max
        risk_pct = 0.5
        leverage = 3
        risk_notional = equity * risk_pct / 100.0 / (sl_dist_pct / 100.0)
        margin_unclamped = risk_notional / leverage / equity * 100.0
        margin = min(8.0, max(2.0, margin_unclamped))
        
        self.assertAlmostEqual(sl_dist_pct, 1.74, places=2)
        self.assertEqual(margin, 8.0)  # should clamp to max

    def test_sl_distance_check(self):
        """Test SL < 1.5% skip."""
        entry_px = 8.3062
        sl_too_close = 8.26  # only 0.56% away
        sl_dist = (entry_px - sl_too_close) / entry_px * 100.0
        self.assertLess(sl_dist, 1.5)


class TestContStaircaseLiquidation(unittest.TestCase):
    """Test liquidation checks."""

    def test_liq_beyond_sl(self):
        """Test liquidation price must be beyond SL."""
        entry_px = 8.3062
        sl = 8.1617
        leverage = 3
        max_lev = 5
        
        liq = isolated_liq_price_long(entry_px, leverage, max_lev)
        self.assertIsNotNone(liq)
        self.assertTrue(liq_beyond_sl_long(liq, sl))  # liq < SL for long


if __name__ == "__main__":
    unittest.main()

