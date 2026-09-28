#!/usr/bin/env python3
"""Unit tests for serve.py Unified mode equity and position computation."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


class TestUnifiedEquityComputation(unittest.TestCase):
    """Test _compute_unified_equity function."""

    def test_unified_equity_spot_usdc(self):
        """In Unified mode, equity = spot USDC total."""
        hl_data = {
            "hl_spot": {
                "balances": [
                    {"coin": "USDC", "total": "10000.50"},
                    {"coin": "BTC", "total": "0.1"},
                ]
            },
            "hl_perp": {
                "marginSummary": {"totalMarginUsed": "2000.00"},
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "unrealizedPnl": "150.25",
                        }
                    }
                ],
            },
            "ts": "2024-01-01T00:00:00Z",
        }
        
        # Import after sys.path modification
        from serve import _compute_unified_equity
        
        result = _compute_unified_equity(hl_data)
        
        self.assertAlmostEqual(result["equity"], 10000.50, places=2)
        self.assertAlmostEqual(result["spot_usdc"], 10000.50, places=2)
        self.assertAlmostEqual(result["margin_used"], 2000.00, places=2)
        self.assertAlmostEqual(result["uPnL_sum"], 150.25, places=2)
        # Free USDC = spot USDC - margin used
        self.assertAlmostEqual(result["spot_usdc_free"], 8000.50, places=2)

    def test_unified_equity_no_margin(self):
        """Free USDC = spot USDC when no margin used."""
        hl_data = {
            "hl_spot": {
                "balances": [
                    {"coin": "USDC", "total": "5000.00"},
                ]
            },
            "hl_perp": {
                "marginSummary": {"totalMarginUsed": "0"},
                "assetPositions": [],
            },
            "ts": "2024-01-01T00:00:00Z",
        }
        
        from serve import _compute_unified_equity
        
        result = _compute_unified_equity(hl_data)
        
        self.assertAlmostEqual(result["equity"], 5000.00, places=2)
        self.assertAlmostEqual(result["spot_usdc_free"], 5000.00, places=2)

    def test_unified_equity_negative_upnl(self):
        """Negative uPnL doesn't affect equity in Unified mode."""
        hl_data = {
            "hl_spot": {
                "balances": [
                    {"coin": "USDC", "total": "10000.00"},
                ]
            },
            "hl_perp": {
                "marginSummary": {"totalMarginUsed": "3000.00"},
                "assetPositions": [
                    {
                        "position": {
                            "coin": "ETH",
                            "unrealizedPnl": "-500.00",
                        }
                    }
                ],
            },
            "ts": "2024-01-01T00:00:00Z",
        }
        
        from serve import _compute_unified_equity
        
        result = _compute_unified_equity(hl_data)
        
        # Equity = spot USDC (not affected by perp uPnL in Unified)
        self.assertAlmostEqual(result["equity"], 10000.00, places=2)
        self.assertAlmostEqual(result["uPnL_sum"], -500.00, places=2)
        self.assertAlmostEqual(result["spot_usdc_free"], 7000.00, places=2)


class TestPositionsWithStops(unittest.TestCase):
    """Test _compute_positions_with_stops function."""

    def test_mega_tier_stops(self):
        """Mega/Large: primary exit = 4H Filter, hard SL = 4H Lower."""
        hl_data = {
            "hl_perp": {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0.1",
                            "entryPx": "60000",
                            "positionValue": "6000",
                            "unrealizedPnl": "100",
                            "leverage": {"value": "2"},
                            "liquidationPx": "50000",
                        }
                    }
                ]
            }
        }
        
        radar_1h = {
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "close": 61000,
                    "lower": 58000,
                }
            ]
        }
        
        radar_4h = {
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "close": 61000,
                    "filter": 59000,
                    "lower": 57000,
                }
            ]
        }
        
        from serve import _compute_positions_with_stops
        
        result = _compute_positions_with_stops(hl_data, radar_1h, radar_4h)
        
        self.assertEqual(len(result), 1)
        pos = result[0]
        self.assertEqual(pos["coin"], "BTC")
        self.assertEqual(pos["side"], "LONG")
        self.assertEqual(pos["tier"], "mega")
        self.assertEqual(pos["primary_exit"], 59000)
        self.assertEqual(pos["primary_exit_label"], "4H Filter")
        self.assertEqual(pos["hard_sl"], 57000)
        self.assertEqual(pos["hard_sl_label"], "4H Lower")
        # SL distance: (57000 - 60000) / 60000 * 100 = -5.0%
        self.assertAlmostEqual(pos["sl_dist_pct"], -5.0, places=1)
        self.assertEqual(pos["exit_signal"], "HOLD")
        # Liq (50000) < Hard SL (57000) = safe for LONG
        self.assertTrue(pos["liq_beyond_sl"])

    def test_small_tier_stops(self):
        """Small/Tiny: primary exit = 1H Lower, hard SL = 4H Filter."""
        hl_data = {
            "hl_perp": {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BRETT",
                            "szi": "100000",
                            "entryPx": "0.15",
                            "positionValue": "15000",
                            "unrealizedPnl": "500",
                            "leverage": {"value": "3"},
                            "liquidationPx": "0.12",
                        }
                    }
                ]
            }
        }
        
        radar_1h = {
            "rows": [
                {
                    "symbol": "BRETT",
                    "trend": "Green",
                    "close": 0.16,
                    "lower": 0.14,
                }
            ]
        }
        
        radar_4h = {
            "rows": [
                {
                    "symbol": "BRETT",
                    "trend": "Green",
                    "close": 0.16,
                    "filter": 0.135,
                    "lower": 0.13,
                }
            ]
        }
        
        from serve import _compute_positions_with_stops
        
        result = _compute_positions_with_stops(hl_data, radar_1h, radar_4h)
        
        self.assertEqual(len(result), 1)
        pos = result[0]
        self.assertEqual(pos["coin"], "BRETT")
        self.assertEqual(pos["tier"], "tiny")
        self.assertEqual(pos["primary_exit"], 0.14)
        self.assertEqual(pos["primary_exit_label"], "1H Lower")
        self.assertEqual(pos["hard_sl"], 0.135)
        self.assertEqual(pos["hard_sl_label"], "4H Filter")
        # SL distance: (0.135 - 0.15) / 0.15 * 100 = -10.0%
        self.assertAlmostEqual(pos["sl_dist_pct"], -10.0, places=1)

    def test_exit_signal_mega_4h_below_filter(self):
        """Mega tier: EXIT when 4H close < 4H filter."""
        hl_data = {
            "hl_perp": {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0.1",
                            "entryPx": "60000",
                            "positionValue": "5900",
                            "unrealizedPnl": "-100",
                            "leverage": {"value": "2"},
                            "liquidationPx": "50000",
                        }
                    }
                ]
            }
        }
        
        radar_1h = {"rows": []}
        
        radar_4h = {
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Red",
                    "close": 58000,  # Below filter
                    "filter": 59000,
                    "lower": 57000,
                }
            ]
        }
        
        from serve import _compute_positions_with_stops
        
        result = _compute_positions_with_stops(hl_data, radar_1h, radar_4h)
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["exit_signal"], "EXIT 4H")

    def test_exit_signal_small_1h_below_lower(self):
        """Small tier: EXIT when 1H close < 1H lower."""
        hl_data = {
            "hl_perp": {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BRETT",
                            "szi": "100000",
                            "entryPx": "0.15",
                            "positionValue": "13000",
                            "unrealizedPnl": "-2000",
                            "leverage": {"value": "3"},
                            "liquidationPx": "0.12",
                        }
                    }
                ]
            }
        }
        
        radar_1h = {
            "rows": [
                {
                    "symbol": "BRETT",
                    "trend": "Red",
                    "close": 0.13,  # Below lower
                    "lower": 0.14,
                }
            ]
        }
        
        radar_4h = {
            "rows": [
                {
                    "symbol": "BRETT",
                    "trend": "Red",
                    "close": 0.13,
                    "filter": 0.135,
                    "lower": 0.13,
                }
            ]
        }
        
        from serve import _compute_positions_with_stops
        
        result = _compute_positions_with_stops(hl_data, radar_1h, radar_4h)
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["exit_signal"], "EXIT 1H")

    def test_exit_signal_watch_on_red_trend(self):
        """WATCH signal when trend turns red but not below exit level."""
        hl_data = {
            "hl_perp": {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0.1",
                            "entryPx": "60000",
                            "positionValue": "6100",
                            "unrealizedPnl": "100",
                            "leverage": {"value": "2"},
                            "liquidationPx": "50000",
                        }
                    }
                ]
            }
        }
        
        radar_1h = {"rows": []}
        
        radar_4h = {
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Red",
                    "close": 61000,  # Above filter, but Red
                    "filter": 59000,
                    "lower": 57000,
                }
            ]
        }
        
        from serve import _compute_positions_with_stops
        
        result = _compute_positions_with_stops(hl_data, radar_1h, radar_4h)
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["exit_signal"], "WATCH")

    def test_short_position(self):
        """Short position parses correctly with negative szi."""
        hl_data = {
            "hl_perp": {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "ETH",
                            "szi": "-2.5",
                            "entryPx": "3000",
                            "positionValue": "7500",
                            "unrealizedPnl": "-50",
                            "leverage": {"value": "2"},
                            "liquidationPx": "3200",
                        }
                    }
                ]
            }
        }
        
        radar_1h = {"rows": []}
        radar_4h = {"rows": []}
        
        from serve import _compute_positions_with_stops
        
        result = _compute_positions_with_stops(hl_data, radar_1h, radar_4h)
        
        self.assertEqual(len(result), 1)
        pos = result[0]
        self.assertEqual(pos["side"], "SHORT")
        self.assertAlmostEqual(pos["size"], 2.5, places=2)


if __name__ == "__main__":
    unittest.main()


class TestEntryTabDecisionsAndMode(unittest.TestCase):
    def test_entry_tab_includes_ai_decision(self):
        import os
        from unittest.mock import patch
        import serve
        cd = {"generated_at": "x", "candidates": [
            {"symbol": "YGG", "type": "Chase", "is_chase": True, "is_base": False},
            {"symbol": "MON", "type": "Base", "is_chase": False, "is_base": True},
            {"symbol": "ZZZ", "type": "Chase", "is_chase": True, "is_base": False}]}
        decs = {"YGG": {"decision": "veto", "reason": "too extended"},
                "MON": {"decision": "approve", "reason": "clean cross", "size_pct": 6, "leverage": 2}}
        with patch.object(serve, "get_decisions_for_today", return_value=decs):
            et = serve._entry_tab(cd)
        chase = {r["symbol"]: r for r in et["chase"]}
        self.assertEqual(chase["YGG"]["ai_decision"]["decision"], "VETO")
        self.assertEqual(chase["YGG"]["ai_decision"]["reason"], "too extended")
        self.assertIsNone(chase["ZZZ"]["ai_decision"])
        self.assertEqual(et["base"][0]["ai_decision"]["decision"], "APPROVE")
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0xabc"}):
            self.assertEqual(serve._exec_mode()["mode"], "LIVE")
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1"}):
            self.assertEqual(serve._exec_mode()["mode"], "DRY_RUN")
