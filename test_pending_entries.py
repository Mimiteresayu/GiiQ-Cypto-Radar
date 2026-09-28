#!/usr/bin/env python3
"""Tests for pending pullback entries (ADD_ON / CONTINUATION) — no network."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pending_entries as pe  # noqa: E402
import pending_worker as pw  # noqa: E402

NOW = datetime(2026, 9, 28, 4, 10, tzinfo=timezone.utc)  # 12:10 HKT
KEY = "0x" + "11" * 32


def _row(lower, filt, close, trend="Green", upper=None):
    return {"lower": lower, "filter": filt, "close": close, "trend": trend, "upper": upper or filt * 1.1}


class FakeHL:
    def __init__(self, equity=1000.0, margin_used=0.0, positions=None, mids=None, meta=None, fill=True, lev=None):
        self.equity, self.margin_used, self.positions = equity, margin_used, positions or []
        self.mids = mids or {}
        self._meta = meta or {"AAA": {"szDecimals": 0, "maxLeverage": 5.0}}
        self.fill, self.lev, self.calls = fill, lev, []

    def spot_state(self):
        return {"balances": [{"coin": "USDC", "total": str(self.equity)}]}

    def perp_state(self):
        aps = []
        for p in self.positions:
            q = dict(p)
            if self.lev:
                q["leverage"] = {"type": "isolated", "value": self.lev}
            aps.append({"position": q})
        return {"marginSummary": {"totalMarginUsed": str(self.margin_used)}, "assetPositions": aps}

    def meta(self):
        return self._meta

    def all_mids(self):
        return self.mids

    def set_leverage(self, coin, lev):
        self.calls.append(("set_leverage", coin, lev))
        return {"ok": True}

    def open_long_ioc(self, coin, qty, px):
        self.calls.append(("open_long_ioc", coin, qty, px))
        if not self.fill:
            return {"status": "error", "error": "no match", "filled_sz": 0.0}
        return {"status": "filled", "filled_sz": qty, "avg_px": px, "oid": 1}

    def place_stop_loss(self, coin, qty, trig, szd):
        self.calls.append(("place_stop_loss", coin, qty, trig))
        return {"status": "resting", "oid": 9, "trigger_px": trig}

    def market_close(self, coin, qty):
        self.calls.append(("market_close", coin, qty))
        return {"status": "filled", "filled_sz": qty}


def bar(low, close):
    return lambda hl, coin, now: {"t": 0, "l": low, "c": close}


class TestEvaluate(unittest.TestCase):
    def rec(self, kind=pe.CONTINUATION, created=NOW - timedelta(days=1)):
        return {"id": "x", "symbol": "AAA", "kind": kind, "status": "pending",
                "expires_at": (created + timedelta(days=7)).isoformat()}

    def b(self, lower=0.8, filt=0.9, close=1.0, trend="Green", tf="1d"):
        return {"tf": tf, "lower": lower, "filter": filt, "close": close, "trend": trend}

    def test_trigger_touch_and_mid_window(self):
        a, _ = pe.evaluate(self.rec(), self.b(), 0.905, NOW, set(), {"l": 0.89, "c": 0.95})
        self.assertEqual(a, "trigger")  # 0.905 <= 0.9*1.01

    def test_no_touch_waits(self):
        a, why = pe.evaluate(self.rec(), self.b(), 0.85, NOW, set(), {"l": 0.91, "c": 0.95})
        self.assertEqual(a, "wait")
        self.assertIn("did not touch", why)

    def test_touch_but_close_below_lower_waits(self):
        a, why = pe.evaluate(self.rec(), self.b(), 0.85, NOW, set(), {"l": 0.75, "c": 0.79})
        self.assertEqual(a, "wait")
        self.assertIn("<=", why)

    def test_mid_above_slack_waits(self):
        a, why = pe.evaluate(self.rec(), self.b(), 0.92, NOW, set(), {"l": 0.89, "c": 0.95})
        self.assertEqual(a, "wait")
        self.assertIn("above Filter*1.01", why)

    def test_band_tf_close_below_lower_cancels(self):
        a, _ = pe.evaluate(self.rec(), self.b(close=0.79), 0.85, NOW, set(), {"l": 0.7, "c": 0.8})
        self.assertEqual(a, "cancel")

    def test_expiry(self):
        a, _ = pe.evaluate(self.rec(created=NOW - timedelta(days=8)), self.b(), 0.85, NOW, set(), {"l": 0.8, "c": 0.9})
        self.assertEqual(a, "expire")

    def test_red_trend_waits(self):
        a, _ = pe.evaluate(self.rec(), self.b(trend="Red"), 0.85, NOW, set(), {"l": 0.8, "c": 0.9})
        self.assertEqual(a, "wait")

    def test_idempotency_rules(self):
        self.assertEqual(pe.evaluate(self.rec(pe.CONTINUATION), self.b(), 0.85, NOW, {"AAA"}, None)[0], "cancel")
        self.assertEqual(pe.evaluate(self.rec(pe.ADD_ON), self.b(tf="4h"), 0.85, NOW, set(), None)[0], "cancel")

    def test_missing_bar_fails_closed(self):
        self.assertEqual(pe.evaluate(self.rec(), self.b(), 0.85, NOW, set(), None)[0], "wait")

    def test_create_idempotent(self):
        entries = []
        r1, c1 = pe.create_pending(entries, "AAA", pe.ADD_ON, {"size_pct": 3, "leverage": 2}, {"tier": "tiny"},
                                   {"tf": "4h", "lower": 1, "filter": 2}, NOW)
        r2, c2 = pe.create_pending(entries, "AAA", pe.ADD_ON, {"size_pct": 3}, {}, {"tf": "4h"}, NOW)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(len(entries), 1)
        self.assertEqual(r1["expires_at"], (NOW + timedelta(days=7)).isoformat())


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"PENDING_PATH": os.path.join(self.tmp, "p.json"),
                                           "EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": KEY})
        self.env.start()
        self.log = []

    def tearDown(self):
        self.env.stop()

    def radars(self, d1=None, h4=None):
        ts = (NOW - timedelta(minutes=5)).isoformat()
        r1d = {"ts": ts, "rows": [dict(symbol="AAA", **(d1 or _row(0.80, 0.90, 1.00)))]}
        r4h = {"ts": ts, "rows": [dict(symbol="AAA", **(h4 or _row(0.70, 0.75, 0.95)))]}
        return r1d, r4h

    def entry(self, kind=pe.CONTINUATION, size=3, lev=2, tier="tiny"):
        return [{"id": f"AAA_{kind}_20260928", "symbol": "AAA", "kind": kind, "status": "pending", "tier": tier,
                 "size_pct": size, "leverage": lev, "created_at": NOW.isoformat(),
                 "expires_at": (NOW + timedelta(days=7)).isoformat()}]

    def run_w(self, hl, entries, bar_fn, d1=None, h4=None):
        r1d, r4h = self.radars(d1, h4)
        return pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=NOW, entries=entries,
                              log_entry_fn=lambda **k: self.log.append(k), bar_fn=bar_fn)

    def test_continuation_fill_live_with_sl_and_size_band(self):
        hl = FakeHL(mids={"AAA": 0.9})
        ents = self.entry(size=6)  # approved 6% -> clamped to Continuation band 4%
        res = self.run_w(hl, ents, bar(0.88, 0.93))
        self.assertEqual(res["status"], "success", res)
        self.assertEqual([c[0] for c in hl.calls], ["set_leverage", "open_long_ioc", "place_stop_loss"])
        f = res["filled"][0]
        self.assertEqual(f["size_pct"], 4.0)
        self.assertEqual(f["hard_sl"], 0.75)                  # tiny -> 4H Filter
        self.assertEqual(ents[0]["status"], "filled")
        self.assertEqual(self.log[0]["entry_type"], "CONTINUATION")
        # idempotent: second run does nothing
        hl2 = FakeHL(mids={"AAA": 0.9})
        res2 = self.run_w(hl2, ents, bar(0.88, 0.93))
        self.assertEqual(hl2.calls, [])
        self.assertIn("no active", res2.get("message", ""))

    def test_dry_run_places_nothing_and_does_not_mutate(self):
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1"}):
            hl = FakeHL(mids={"AAA": 0.9})
            ents = self.entry()
            res = self.run_w(hl, ents, bar(0.88, 0.93))
        self.assertEqual(hl.calls, [])
        self.assertTrue(res["filled"][0]["dry_run"])
        self.assertEqual(ents[0]["status"], "pending")

    def test_sl_distance_blocks_fill(self):
        # Hard SL (4H Filter 0.89) within 1.5% of mid 0.9 -> no order, stays pending
        hl = FakeHL(mids={"AAA": 0.9})
        ents = self.entry()
        res = self.run_w(hl, ents, bar(0.88, 0.93), h4=_row(0.85, 0.89, 0.95))
        self.assertEqual(hl.calls, [])
        self.assertEqual(ents[0]["status"], "pending")
        self.assertIn("SL distance", res["checked"][0]["reason"])

    def test_margin_cap_blocks_fill(self):
        hl = FakeHL(mids={"AAA": 0.9}, margin_used=790)
        ents = self.entry()
        res = self.run_w(hl, ents, bar(0.88, 0.93))
        self.assertEqual(hl.calls, [])
        self.assertIn("margin utilization", res["checked"][0]["reason"])

    def test_stale_radar_blocks_fill(self):
        hl = FakeHL(mids={"AAA": 0.9})
        ents = self.entry()
        r1d, r4h = self.radars()
        r4h["ts"] = (NOW - timedelta(hours=9)).isoformat()
        res = pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=NOW, entries=ents,
                             log_entry_fn=lambda **k: None, bar_fn=bar(0.88, 0.93))
        self.assertEqual(hl.calls, [])
        self.assertIn("stale", res["checked"][0]["reason"])

    def test_add_on_uses_4h_zone_and_existing_leverage(self):
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.4"}
        hl = FakeHL(mids={"AAA": 1.01}, positions=[pos], lev=3)
        ents = self.entry(kind=pe.ADD_ON, lev=2, tier="large")  # large: Hard SL = 4H Lower 0.95
        # 4H zone [0.95, 1.00]; bar low 0.99 touched, close 1.02 > Lower; mid 1.01 <= 1.0*1.01
        res = self.run_w(hl, ents, bar(0.99, 1.02), h4=_row(0.95, 1.00, 1.03), d1=_row(0.5, 0.6, 1.0))
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 3))   # keeps existing isolated leverage
        self.assertEqual(ents[0]["status"], "filled")

    def test_add_on_small_tiny_blocked_by_sl_distance(self):
        # Small/Tiny Hard SL = 4H Filter = top of the ADD_ON zone -> SL distance < 1.5% -> never fills (fail-closed)
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.4"}
        hl = FakeHL(mids={"AAA": 1.0}, positions=[pos], lev=2)
        ents = self.entry(kind=pe.ADD_ON, tier="tiny")
        res = self.run_w(hl, ents, bar(0.99, 1.02), h4=_row(0.95, 1.00, 1.03))
        self.assertEqual(hl.calls, [])
        self.assertIn("SL distance", res["checked"][0]["reason"])
        self.assertEqual(ents[0]["status"], "pending")

    def test_add_on_cancelled_when_base_closed(self):
        hl = FakeHL(mids={"AAA": 0.98})
        ents = self.entry(kind=pe.ADD_ON)
        res = self.run_w(hl, ents, bar(0.97, 0.99), h4=_row(0.95, 1.0, 1.0))
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["cancelled"][0]["symbol"], "AAA")

    def test_closed_4h_bar_picks_last_closed(self):
        class H:
            def info(self, payload):
                t0 = int(NOW.timestamp() * 1000) - 4 * 3600 * 1000 - 600_000  # closed 10 min ago
                return [{"t": t0 - 4 * 3600 * 1000, "l": "1", "c": "2"}, {"t": t0, "l": "3", "c": "4"},
                        {"t": t0 + 4 * 3600 * 1000, "l": "5", "c": "6"}]  # forming
        b = pw.closed_4h_bar(H(), "AAA", NOW)
        self.assertEqual((b["l"], b["c"]), (3.0, 4.0))


if __name__ == "__main__":
    unittest.main()
