#!/usr/bin/env python3
"""Tests for exec_preflight (no network)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exec_preflight  # noqa: E402

KEY = "0x" + "11" * 32


class FakeHL:
    address = "0x" + "cd" * 20

    def __init__(self, agent_ok=True, probe_ok=True, days_left=300.0):
        self.agent_ok, self.probe_ok, self.days_left = agent_ok, probe_ok, days_left

    def agent_status(self, addr):
        return {"ok": self.agent_ok, "name": "Railway Key" if self.agent_ok else None, "days_left": self.days_left,
                "valid_until_ms": 1, "reason": "approved agent 'Railway Key'" if self.agent_ok else "not an approved agent"}

    def perp_state(self):
        return {"assetPositions": []}

    def spot_state(self):
        return {"balances": [{"coin": "USDC", "total": "500"}]}

    def probe_signing(self):
        return {"ok": self.probe_ok, "error": None if self.probe_ok else "User or API Wallet does not exist"}

    def meta(self):
        return {"AAA": {"szDecimals": 0, "maxLeverage": 3.0}}


class TestPreflight(unittest.TestCase):
    def run_pf(self, hl, env=None):
        e = {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": KEY}
        e.update(env or {})
        with patch.dict(os.environ, e), patch("decisions.get_decisions_for_today", return_value={}):
            return exec_preflight.run_preflight(hl=hl)

    def test_ok(self):
        r = self.run_pf(FakeHL())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["mode"], "LIVE")

    def test_agent_not_approved_fails(self):
        r = self.run_pf(FakeHL(agent_ok=False))
        self.assertFalse(r["ok"])
        self.assertIn("agent", [c["check"] for c in r["checks"] if not c["ok"]])

    def test_probe_fail(self):
        r = self.run_pf(FakeHL(probe_ok=False))
        self.assertFalse(r["ok"])

    def test_expiry_warning_not_blocking(self):
        r = self.run_pf(FakeHL(days_left=3.0))
        self.assertTrue(r["ok"])
        self.assertTrue(any("expires" in w for w in r["warnings"]))

    def test_dry_run_reported(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1"}):
            r = exec_preflight.run_preflight(hl=FakeHL())
        self.assertFalse(r["ok"])
        self.assertEqual(r["mode"], "DRY_RUN")

    def test_never_prints_key(self):
        r = self.run_pf(FakeHL())
        self.assertNotIn("11" * 32, repr(r))


if __name__ == "__main__":
    unittest.main()
