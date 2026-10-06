#!/usr/bin/env python3
"""End-to-end DRY-RUN pilot (scripts/pilot_e2e.py) + the HL order wire the live entry relies on.
No network: the pilot runs in a clean subprocess on a temp copy of the repo behind a 127.0.0.1-only guard."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import hl_exec  # noqa: E402


class TestHLOrderWire(unittest.TestCase):
    def test_cloid_is_signed_as_c_field_and_order_type_is_clean(self):
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils.signing import order_request_to_order_wire

        class Stub:
            wires = []

            def order(self, *a, **k):
                return Exchange.order(self, *a, **k)

            def bulk_orders(self, reqs, builder=None, grouping="na"):
                self.wires += [order_request_to_order_wire(o, 0) for o in reqs]
                return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                    {"filled": {"totalSz": "0.001", "avgPx": "62300", "oid": 1}}]}}}

        c = hl_exec.HLClient("0x" + "00" * 20)
        c._exchange = Stub()
        cloid = hl_exec._make_cloid("BTC", "2026-10-07")
        res = c.open_long_ioc("BTC", 0.001, 62300.0, cloid=cloid)
        w = c._exchange.wires[0]
        self.assertEqual(w["t"], {"limit": {"tif": "Ioc"}})   # nothing extra inside the signed order type
        self.assertEqual(w["c"], cloid)
        self.assertRegex(cloid, r"^0x[0-9a-f]{32}$")
        self.assertEqual(res["status"], "filled")

    def test_cloid_deterministic(self):
        a = hl_exec._make_cloid("ETH", "2026-10-07")
        self.assertEqual(a, hl_exec._make_cloid("ETH", "2026-10-07"))
        self.assertNotEqual(a, hl_exec._make_cloid("ETH", "2026-10-08"))


class TestPilotE2E(unittest.TestCase):
    def test_pilot_has_no_fail(self):
        out = Path(tempfile.mkdtemp()) / "pilot.json"
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "pilot_e2e.py"), "--json", str(out)],
                           capture_output=True, text=True, timeout=900)
        self.assertTrue(out.is_file(), p.stdout[-3000:] + p.stderr[-3000:])
        rep = json.loads(out.read_text())
        fails = [(sid, c["label"], c.get("detail")) for sid, st in rep["steps"].items()
                 for c in st["checks"] if c["status"] == "FAIL"]
        self.assertEqual(fails, [], p.stdout[-4000:])
        self.assertEqual(rep["errors"], [])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(sorted(rep["steps"], key=int), [str(i) for i in range(1, 9)])


if __name__ == "__main__":
    unittest.main()
