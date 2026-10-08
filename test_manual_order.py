"""Tests for manual order HTTP endpoints (HL and BX). MMT 2026-10-08."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestHLManualEntry(unittest.TestCase):
    """Test POST /api/hl/manual_entry in serve.py"""
    
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update({
            "AI_DECISION_KEY": "test-key-123",
            "EXEC_DRY_RUN": "1",
            "EXEC_RADAR_MIN_ROWS": "0"
        })
    
    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
    
    def test_auth_missing_key(self):
        """Auth fails when X-AI-Key header is missing"""
        from serve import Handler
        from io import BytesIO
        from http.server import HTTPServer
        
        handler = Handler(MagicMock(), ("127.0.0.1", 8787), HTTPServer)
        handler.rfile = BytesIO(b'{"symbol": "BTC", "size_pct": 3.0, "leverage": 4}')
        handler.headers = {"Content-Length": "49", "Content-Type": "application/json"}
        handler.path = "/api/hl/manual_entry"
        handler.wfile = BytesIO()
        
        handler._hl_manual_entry()
        
        # Should send 403
        # Check that wfile contains forbidden error
        handler.wfile.seek(0)
        response = handler.wfile.read().decode()
        self.assertIn("403", response) or self.assertIn("forbidden", response.lower())
    
    def test_auth_query_param_rejected(self):
        """Auth via ?key= query param is rejected"""
        from serve import Handler
        from io import BytesIO
        from http.server import HTTPServer
        
        handler = Handler(MagicMock(), ("127.0.0.1", 8787), HTTPServer)
        handler.rfile = BytesIO(b'{"symbol": "BTC", "size_pct": 3.0, "leverage": 4}')
        handler.headers = {"Content-Length": "49", "Content-Type": "application/json"}
        handler.path = "/api/hl/manual_entry?key=test-key-123"
        handler.wfile = BytesIO()
        handler._send_json = MagicMock()
        
        handler._hl_manual_entry()
        
        # Should call _send_json with 403 and query param rejection message
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 403)
        self.assertIn("query param", args[1].get("error", "").lower())
    
    def test_dry_run_preview(self):
        """dry_run=true returns preview without executing"""
        import manual_order
        with patch.object(manual_order, "manual_entry_hl") as mock_entry:
            mock_entry.return_value = {"ok": True, "mode": "DRY_RUN", "symbol": "BTC"}
            
            from serve import Handler
            from io import BytesIO
            from http.server import HTTPServer
            
            handler = Handler(MagicMock(), ("127.0.0.1", 8787), HTTPServer)
            body = b'{"symbol": "BTC", "size_pct": 3.0, "leverage": 4, "dry_run": true}'
            handler.rfile = BytesIO(body)
            handler.headers = {
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
                "X-AI-Key": "test-key-123"
            }
            handler.path = "/api/hl/manual_entry"
            handler.wfile = BytesIO()
            handler._send_json = MagicMock()
            
            handler._hl_manual_entry()
            
            # Should call manual_entry_hl with dry_run=True
            mock_entry.assert_called_once()
            args = mock_entry.call_args[1]
            self.assertEqual(args["dry_run"], True)
            self.assertEqual(args["symbol"], "BTC")


class TestBXManualEntry(unittest.TestCase):
    """Test POST /api/bx/manual_entry in bx_service.py"""
    
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update({
            "BX_SERVICE_KEY": "bx-test-key",
            "BX_DRY_RUN": "1",
            "BX_ENABLED": "1"
        })
    
    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
    
    def test_auth_header_only(self):
        """BX manual entry requires X-BX-Key header, rejects query param"""
        from bx_service import Handler
        from io import BytesIO
        from http.server import HTTPServer
        
        handler = Handler(MagicMock(), ("127.0.0.1", 8788), HTTPServer)
        body = b'{"symbol": "BTCUSDT", "size_pct": 3.0, "leverage": 4}'
        handler.rfile = BytesIO(body)
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.path = "/api/bx/manual_entry?key=bx-test-key"
        handler.wfile = BytesIO()
        handler._send = MagicMock()
        
        handler._bx_manual_entry(json.loads(body))
        
        # Should send 403 for query param auth
        handler._send.assert_called_once()
        args = handler._send.call_args[0]
        self.assertEqual(args[0], 403)
        self.assertIn("query param", args[1].get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
