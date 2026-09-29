"""CoinGecko client: partial pages are kept, first-seen is cached and budgeted (no network)."""
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import cg_client


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCG(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = cg_client.OUT_DIR
        cg_client.OUT_DIR = self.tmp
        cg_client._budget["first_seen"] = cg_client.FIRST_SEEN_MAX_PER_RUN
        self._sleep = cg_client.time.sleep
        cg_client.time.sleep = lambda s: None

    def tearDown(self):
        cg_client.OUT_DIR = self._orig
        cg_client.time.sleep = self._sleep
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_markets_keeps_partial_pages(self):
        calls = {"n": 0}

        def opener(req, timeout=None):
            calls["n"] += 1
            if "page=1&" in req.full_url:
                return _Resp(json.dumps([{"id": f"c{i}", "symbol": f"s{i}", "current_price": 1, "market_cap": 1,
                                          "ath": 2, "ath_date": "x"} for i in range(250)]).encode())
            raise OSError("429-ish")
        m = cg_client.markets(force=True, opener=opener, sleep=lambda s: None)
        self.assertEqual(m["n"], 250)
        self.assertIn("page 2", m["error"])
        self.assertTrue((self.tmp / "cg_markets.json").exists())

    def test_first_seen_cached_and_budgeted(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req.full_url)
            return _Resp(json.dumps({"prices": [[1_700_000_000_000, 1.0], [1_700_086_400_000, 1.1]]}).encode())
        self.assertEqual(cg_client.first_seen_ms("abc", opener=opener), 1_700_000_000_000)
        self.assertEqual(cg_client.first_seen_ms("abc", opener=opener), 1_700_000_000_000)
        self.assertEqual(len(seen), 1)                            # cached
        cg_client._budget["first_seen"] = 0
        self.assertIsNone(cg_client.first_seen_ms("other", opener=opener))
        self.assertEqual(len(seen), 1)                            # over budget: no call


if __name__ == "__main__":
    unittest.main()
