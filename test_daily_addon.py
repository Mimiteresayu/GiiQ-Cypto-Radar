"""Signum-style daily ADD_ON top-up (daily_addon.py): rules, safety, idempotency. No network."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_addon as D  # noqa: E402
from test_live_execution import FakeHL  # noqa: E402

NOW = datetime(2026, 10, 7, 0, 57, tzinfo=timezone.utc)      # 08:57 HKT
META = {"AAA": {"szDecimals": 2, "maxLeverage": 5}}


def radar(rows):
    return {"ts": NOW.isoformat(), "rows": rows}


R1D = radar([{"symbol": "AAA", "close": 1.0, "upper": 0.95}])
R4H = radar([{"symbol": "AAA", "filter": 0.90, "lower": 0.80, "close": 1.0}])


def pos(entry="0.9", size="50"):
    return {"coin": "AAA", "szi": size, "entryPx": entry, "liquidationPx": "0.3", "marginUsed": "25",
            "leverage": {"type": "isolated", "value": 2}}


class T(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update({"EXEC_RADAR_MIN_ROWS": "0", "DAILY_ADDON_ENABLED": "1", "EXEC_DRY_RUN": "1",
                           "DAILY_ADDON_STATE": os.path.join(tempfile.mkdtemp(), "state.json")})
        os.environ.pop("HL_API_PRIVATE_KEY", None)
        self.log = MagicMock()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def run_it(self, hl, r1d=R1D, r4h=R4H):
        return D.run_daily_addons(hl=hl, radar_1d=r1d, radar_4h=r4h, now=NOW, log_entry_fn=self.log,
                                  tier_fn=lambda s: "tiny")

    def hl(self, **kw):
        return FakeHL(equity=1000, meta=META, mids={"AAA": 1.0}, positions=[kw.pop("p", pos())], **kw)

    def test_off_by_default(self):
        os.environ["DAILY_ADDON_ENABLED"] = "0"
        hl = self.hl()
        res = self.run_it(hl)
        self.assertIn("OFF", res["message"])
        self.assertEqual(hl.calls, [])

    def test_dry_run_would_top_up_a_winner(self):
        hl = self.hl()                                   # +11% price gain, 1D close above Upper
        res = self.run_it(hl)
        self.assertEqual(res["mode"], "DRY_RUN")
        self.assertEqual([f["symbol"] for f in res["filled"]], ["AAA"])
        self.assertEqual(res["filled"][0]["leverage"], 2)              # keeps the existing 2x
        self.assertEqual(res["filled"][0]["rule"], "signum_topup")
        self.assertEqual(hl.calls, [])                                 # dry run: no orders

    def test_not_up_10pct_no_top_up(self):
        res = self.run_it(self.hl(p=pos(entry="0.95")))                # +5.3%
        self.assertEqual(res["filled"], [])
        self.assertIn("price gain", res["skipped"][0]["reason"])

    def test_1d_close_not_above_upper_no_top_up(self):
        res = self.run_it(self.hl(), r1d=radar([{"symbol": "AAA", "close": 0.94, "upper": 0.95}]))
        self.assertEqual(res["filled"], [])
        self.assertIn("not above 1D Upper", res["skipped"][0]["reason"])

    def test_coin_you_do_not_hold_is_ignored(self):
        hl = FakeHL(equity=1000, meta=META, mids={"AAA": 1.0}, positions=[])
        res = self.run_it(hl)
        self.assertEqual((res["checked"], res["filled"]), ([], []))     # no CONT: only Base opens new coins

    def test_live_top_up_once_per_day(self):
        os.environ.update({"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": "0x" + "1" * 64})
        hl = self.hl()
        res = self.run_it(hl)
        self.assertEqual(hl.names()[:3], ["set_leverage", "open_long_ioc", "place_stop_loss"], hl.names())
        self.assertEqual([f["symbol"] for f in res["filled"]], ["AAA"])
        self.log.assert_called_once()
        n = len(hl.calls)
        res2 = self.run_it(hl)                                         # same day again: idempotent
        self.assertEqual(len(hl.calls), n)
        self.assertEqual(res2["skipped"][0]["reason"], "already topped up today")

    def test_stale_radar_fails_closed(self):
        old = {"ts": "2026-10-05T00:00:00+00:00", "rows": R1D["rows"]}
        res = self.run_it(self.hl(), r1d=old)
        self.assertEqual(res["status"], "fail_closed")


if __name__ == "__main__":
    unittest.main()
