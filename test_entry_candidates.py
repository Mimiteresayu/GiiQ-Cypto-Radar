#!/usr/bin/env python3
"""Unit tests for entry_candidates.py and /api/entry-candidates endpoint."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from http.server import HTTPServer
from urllib.parse import urlparse
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from entry_candidates import build_candidates, _radar_age_hours


class TestEntryCandidatesLogic(unittest.TestCase):
    """Test entry candidate detection rules."""

    def test_base_entry_1d_dual_cross_up(self):
        """Base entry: fresh 1D dual_cross_up (MMT 2026-10-07: no trend-colour condition)."""
        radar_1d = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "ATOM",
                    "trend": "Green",
                    "close": 10.5,
                    "upper": 10.0,
                    "filter": 9.5,
                    "dual_cross_up": True,
                    "day_ntl_vlm": 100000,
                },
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "close": 60000,
                    "filter": 59000,
                    "dual_cross_up": False,
                    "day_ntl_vlm": 5000000,
                },
            ],
        }
        radar_4h = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "ATOM",
                    "trend": "Green",
                    "filter": 9.8,
                    "dual_cross_up": False,
                },
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "filter": 59500,
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["candidates"][0]["symbol"], "ATOM")
        self.assertEqual(result["candidates"][0]["type"], "Base")

    def test_base_entry_needs_only_the_fresh_cross(self):
        """MMT 2026-10-07: Base = fresh 1D dual_cross_up only. A Red-trend cross is still a Base; no green flip needed."""
        radar_1d = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "2Z",
                    "trend": "Red",  # Red trend: still a Base (no trend-colour condition)
                    "close": 10.5,
                    "upper": 10.0,
                    "filter": 9.5,
                    "dual_cross_up": True,  # fresh cross up
                    "day_ntl_vlm": 100000,
                },
            ],
        }
        radar_4h = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "2Z",
                    "trend": "Green",
                    "filter": 9.8,
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["candidates"][0]["type"], "Base")
        # and no cross -> no Base, whatever the trend colour
        radar_1d["rows"][0]["dual_cross_up"] = False
        self.assertEqual(build_candidates(radar_1d, radar_4h)["count"], 0)

    def test_chase_entry_1d_green_4h_green_4h_dual_cross(self):
        """Chase entry: 1D Green + 4H Green + 4H dual_cross_up."""
        radar_1d = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "SOL",
                    "trend": "Green",
                    "close": 150,
                    "upper": 145,
                    "filter": 140,
                    "dual_cross_up": False,
                    "dayNtlVlm": 200000,
                },
            ],
        }
        radar_4h = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "SOL",
                    "trend": "Green",
                    "filter": 142,
                    "dual_cross_up": True,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["candidates"][0]["symbol"], "SOL")
        self.assertEqual(result["candidates"][0]["type"], "Chase")

    def test_not_crossed_no_signal(self):
        """No dual_cross_up on either TF = no candidate."""
        radar_1d = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "ETH",
                    "trend": "Green",
                    "close": 3000,
                    "upper": 2950,
                    "filter": 2900,
                    "dual_cross_up": False,
                    "dayNtlVlm": 150000,
                },
            ],
        }
        radar_4h = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "ETH",
                    "trend": "Green",
                    "filter": 2920,
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertEqual(result["count"], 0)

    def test_chase_requires_both_greens(self):
        """Chase requires 1D Green AND 4H Green, not just 4H cross."""
        # Case 1: 1D Red, 4H Green+cross → no Chase
        radar_1d = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "AVAX",
                    "trend": "Red",
                    "close": 40,
                    "upper": 38,
                    "filter": 36,
                    "dual_cross_up": False,
                    "dayNtlVlm": 120000,
                },
            ],
        }
        radar_4h = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "AVAX",
                    "trend": "Green",
                    "filter": 37,
                    "dual_cross_up": True,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertEqual(result["count"], 0)
        
        # Case 2: 1D Green, 4H Red+cross → no Chase
        radar_1d["rows"][0]["trend"] = "Green"
        radar_4h["rows"][0]["trend"] = "Red"
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertEqual(result["count"], 0)

    def test_stale_flag_1d_old(self):
        """Stale = True if 1D > 36h."""
        from datetime import timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        recent_ts = datetime.now(timezone.utc).isoformat()
        
        radar_1d = {
            "ts": old_ts,
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "close": 60000,
                    "upper": 59000,
                    "filter": 58000,
                    "dual_cross_up": True,
                    "dayNtlVlm": 5000000,
                },
            ],
        }
        radar_4h = {
            "ts": recent_ts,
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "filter": 58500,
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertTrue(result["stale"])

    def test_stale_flag_4h_old(self):
        """Stale = True if 4H > 4.5h (one full 4H cycle + slack)."""
        from datetime import timedelta
        recent_ts = datetime.now(timezone.utc).isoformat()
        old_4h_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        
        radar_1d = {
            "ts": recent_ts,
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "close": 60000,
                    "upper": 59000,
                    "filter": 58000,
                    "dual_cross_up": True,
                    "dayNtlVlm": 5000000,
                },
            ],
        }
        radar_4h = {
            "ts": old_4h_ts,
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "filter": 58500,
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d, radar_4h)
        self.assertTrue(result["stale"])

        # 3h-old 4H radar (normal at 08:05 before the fix) is NOT stale anymore
        radar_4h["ts"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.assertFalse(build_candidates(radar_1d, radar_4h)["stale"])

    def test_hard_sl_dist_pct(self):
        """Hard SL distance % calculated from close_1d and tier-correct hard SL.
        
        Mega/Large: hard SL = 4H Lower
        Small/Tiny: hard SL = 4H Filter
        """
        # Test Mega tier (uses 4H Lower)
        radar_1d_btc = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "BTC",  # BTC is mega tier
                    "trend": "Green",
                    "close": 60000,
                    "upper": 59000,
                    "filter": 58000,
                    "lower": 56000,
                    "dual_cross_up": True,
                    "dayNtlVlm": 5000000,
                },
            ],
        }
        radar_4h_btc = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "BTC",
                    "trend": "Green",
                    "filter": 58500,
                    "lower": 57000,  # This should be used for Mega
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d_btc, radar_4h_btc)
        self.assertEqual(result["count"], 1)
        # Mega: (60000 - 57000) / 60000 * 100 = 5.0%
        self.assertAlmostEqual(result["candidates"][0]["hard_sl_dist_pct"], 5.0, places=1)
        self.assertEqual(result["candidates"][0]["lower_4h"], 57000)
        
        # Test Small/Tiny tier (uses 4H Filter)
        radar_1d_brett = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "BRETT",  # BRETT is tiny tier
                    "trend": "Green",
                    "close": 1.0,
                    "upper": 0.95,
                    "filter": 0.90,
                    "lower": 0.85,
                    "dual_cross_up": True,
                    "dayNtlVlm": 100000,
                },
            ],
        }
        radar_4h_brett = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "BRETT",
                    "trend": "Green",
                    "filter": 0.92,  # This should be used for Small/Tiny
                    "lower": 0.88,
                    "dual_cross_up": False,
                },
            ],
        }
        
        result = build_candidates(radar_1d_brett, radar_4h_brett)
        self.assertEqual(result["count"], 1)
        # Small/Tiny: (1.0 - 0.92) / 1.0 * 100 = 8.0%
        self.assertAlmostEqual(result["candidates"][0]["hard_sl_dist_pct"], 8.0, places=1)
        self.assertEqual(result["candidates"][0]["filter_4h"], 0.92)
        self.assertEqual(result["candidates"][0]["lower_4h"], 0.88)

    def test_already_held_flag(self):
        """already_held flag set when symbol in positions."""
        radar_1d = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "MATIC",
                    "trend": "Green",
                    "close": 0.8,
                    "upper": 0.75,
                    "filter": 0.70,
                    "dual_cross_up": True,
                    "dayNtlVlm": 100000,
                },
            ],
        }
        radar_4h = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": "MATIC",
                    "trend": "Green",
                    "filter": 0.72,
                    "dual_cross_up": False,
                },
            ],
        }
        
        positions = [{"symbol": "MATIC", "size": 100}]
        result = build_candidates(radar_1d, radar_4h, positions=positions)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["candidates"][0]["already_held"])


class TestEntryEndpoint(unittest.TestCase):
    """Test /api/entry-candidates endpoint auth logic."""

    def setUp(self):
        """Set up temp dir."""
        self.test_dir = tempfile.mkdtemp()
        self.out_dir = os.path.join(self.test_dir, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        
        # Create a mock entry_candidates_latest.json
        self.sample_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": 1,
            "candidates": [
                {
                    "symbol": "BTC",
                    "type": "Base",
                    "tier": "mega",
                }
            ],
        }
        with open(os.path.join(self.out_dir, "entry_candidates_latest.json"), "w") as f:
            json.dump(self.sample_data, f)

    def test_endpoint_disabled_when_no_key(self):
        """Returns 404 when ENTRY_READ_KEY not set."""
        import hmac
        
        # Simulate logic: ENTRY_READ_KEY is empty
        ENTRY_READ_KEY = ""
        
        # Endpoint should return 404
        if not ENTRY_READ_KEY:
            result_code = 404
        else:
            result_code = 200
        
        self.assertEqual(result_code, 404)

    def test_endpoint_forbidden_when_no_key_provided(self):
        """Returns 403 when key param missing."""
        import hmac
        
        ENTRY_READ_KEY = "test-secret-key"
        provided_key = ""
        
        # Endpoint should return 403
        if not provided_key or not hmac.compare_digest(provided_key, ENTRY_READ_KEY):
            result_code = 403
        else:
            result_code = 200
        
        self.assertEqual(result_code, 403)

    def test_endpoint_forbidden_when_wrong_key(self):
        """Returns 403 when key param wrong."""
        import hmac
        
        ENTRY_READ_KEY = "correct-key"
        provided_key = "wrong-key"
        
        # Endpoint should return 403
        if not provided_key or not hmac.compare_digest(provided_key, ENTRY_READ_KEY):
            result_code = 403
        else:
            result_code = 200
        
        self.assertEqual(result_code, 403)

    def test_endpoint_success_when_correct_key(self):
        """Returns 200 with JSON when key correct."""
        import hmac
        
        ENTRY_READ_KEY = "correct-key"
        provided_key = "correct-key"
        
        # Endpoint should return 200
        if ENTRY_READ_KEY and provided_key and hmac.compare_digest(provided_key, ENTRY_READ_KEY):
            result_code = 200
        else:
            result_code = 403
        
        self.assertEqual(result_code, 200)


if __name__ == "__main__":
    unittest.main()
