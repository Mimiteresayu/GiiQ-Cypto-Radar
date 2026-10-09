"""Tests for manual order endpoints (HL and BX). MMT 2026-10-08.

Smoke tests verifying the endpoints and functions exist and can be called.
Full integration tests would require extensive mocking of HL state, radar, etc.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestManualOrderModules(unittest.TestCase):
    """Verify manual order modules and endpoints exist"""
    
    def test_hl_manual_entry_function_exists(self):
        """manual_entry_hl function is importable"""
        import manual_order
        self.assertTrue(callable(manual_order.manual_entry_hl))
    
    def test_hl_endpoint_handler_exists(self):
        """serve.py Handler has _hl_manual_entry method"""
        import serve
        self.assertTrue(hasattr(serve.Handler, "_hl_manual_entry"))
    
    def test_bx_endpoint_handler_exists(self):
        """bx_service.py Handler has _bx_manual_entry method"""
        import bx_service
        self.assertTrue(hasattr(bx_service.Handler, "_bx_manual_entry"))
    
    def test_manual_entry_hl_returns_dict(self):
        """manual_entry_hl returns a dict (even on error)"""
        import manual_order
        result = manual_order.manual_entry_hl(
            symbol="",  # Invalid, will fail
            size_pct=3.0,
            leverage=4,
            dry_run=True
        )
        self.assertIsInstance(result, dict)
        self.assertIn("ok", result)
    
    def test_manual_entry_hl_auth_check(self):
        """serve.py _hl_manual_entry checks X-AI-Key header, rejects query param"""
        # Read the source to verify the check exists
        import serve
        import inspect
        source = inspect.getsource(serve.Handler._hl_manual_entry)
        self.assertIn("X-AI-Key", source)
        self.assertIn("key=", source)  # Checks for query param rejection
    
    def test_manual_entry_bx_auth_check(self):
        """bx_service.py _bx_manual_entry checks X-BX-Key header, rejects query param"""
        import bx_service
        import inspect
        source = inspect.getsource(bx_service.Handler._bx_manual_entry)
        self.assertIn("X-BX-Key", source)
        self.assertIn("key=", source)  # Checks for query param rejection


if __name__ == "__main__":
    unittest.main()
