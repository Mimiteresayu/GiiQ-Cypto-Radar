"""Harbor 2026-09-29: a pending-entry check skipped because the 4h exits failed must show in the daily line."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import exit_health

NOW = datetime(2026, 9, 29, 12, 35, tzinfo=timezone.utc)


def _check(skips):
    return exit_health.check(perp={"assetPositions": []}, open_orders=[], nav=1000.0,
                             radar_1h={"ts": NOW.isoformat()}, radar_4h={"ts": NOW.isoformat()},
                             tier_for=lambda s: "small",
                             job_status={"1h_scan_exits": {"last_run": NOW.isoformat(), "status": "success"},
                                         "4h_scan_exits": {"last_run": NOW.isoformat(), "status": "success"}},
                             pending=[], now=NOW, pending_skips=skips)


class TestPendingSkipReport(unittest.TestCase):
    def test_recent_skip_in_info_and_summary(self):
        res = _check([{"ts": (NOW - timedelta(hours=3)).isoformat(), "why": "exits step did not complete (exit_worker rc=1)"}])
        self.assertTrue(res["ok"])                       # not a problem by itself (the exit failure is JOB_FAILED)
        self.assertEqual(res["info"][0]["code"], "PENDING_SKIPPED")
        self.assertIn("pending skipped 1x (exits failed)", res["summary"])

    def test_old_skip_ignored_and_default_unchanged(self):
        res = _check([{"ts": (NOW - timedelta(hours=30)).isoformat(), "why": "x"}])
        self.assertNotIn("pending skipped", res["summary"])
        self.assertEqual(_check(None)["summary"], res["summary"])

    def test_serve_notes_skip(self):
        import serve
        tmp = tempfile.mkdtemp()
        orig = serve.PENDING_SKIPS_PATH
        serve.PENDING_SKIPS_PATH = os.path.join(tmp, "pending_skips.json")
        try:
            serve._note_pending_skip("exits step did not complete (x)", now=NOW - timedelta(days=9))
            serve._note_pending_skip("exits step did not complete (y)", now=NOW)
            import json
            items = json.load(open(serve.PENDING_SKIPS_PATH))["skips"]
        finally:
            serve.PENDING_SKIPS_PATH = orig
        self.assertEqual([i["why"][-2:] for i in items], ["y)"])   # older than 7 days dropped


if __name__ == "__main__":
    unittest.main()
