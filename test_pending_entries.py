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


H4 = 4 * 3600 * 1000
T0 = int(NOW.timestamp() * 1000) - H4 - 600_000  # a 4H bar that closed 10 min before NOW


def bar(low, close, t=T0):
    return lambda hl, coin, tf, now: {"t": t, "l": low, "c": close}


class TestEvaluate(unittest.TestCase):
    def rec(self, kind=pe.ADD_ON, created=NOW - timedelta(days=1), setup=None, last=None):
        r = {"id": "x", "symbol": "AAA", "kind": kind, "status": "pending", "created_at": created.isoformat(),
             "expires_at": (created + timedelta(days=7)).isoformat()}
        if setup:
            r["setup"] = setup
        if last:
            r["last_bar_t"] = last
        return r

    def b(self, lower=0.8, filt=0.9, close=1.0, trend="Green", tf="4h", bar_time=None):
        return {"tf": tf, "lower": lower, "filter": filt, "close": close, "trend": trend, "bar_time": bar_time}

    HELD = {"AAA"}

    def test_bar_n_detected_then_n1_confirms(self):
        a, why, upd = pe.evaluate(self.rec(), self.b(), 0.9, NOW, self.HELD, {"t": T0 - H4, "l": 0.88, "c": 0.93})
        self.assertEqual(a, "wait")
        self.assertIn("bar N set", why)
        self.assertEqual(upd["setup"]["c"], 0.93)
        r = self.rec(setup=upd["setup"], last=upd["last_bar_t"])
        a, why, upd2 = pe.evaluate(r, self.b(), 0.95, NOW, self.HELD, {"t": T0, "l": 0.91, "c": 0.95})
        self.assertEqual(a, "trigger", why)
        self.assertIsNone(upd2["setup"])

    def test_n1_not_higher_than_n_close_no_trigger_and_becomes_new_n(self):
        r = self.rec(setup={"t": T0 - H4, "l": 0.88, "c": 0.93})
        a, why, upd = pe.evaluate(r, self.b(), 0.9, NOW, self.HELD, {"t": T0, "l": 0.87, "c": 0.92})
        self.assertEqual(a, "wait")
        self.assertIn("N+1 not confirmed", why)
        self.assertEqual(upd["setup"]["t"], T0)          # re-armed as new bar N

    def test_n1_must_be_the_next_bar(self):
        r = self.rec(setup={"t": T0 - 3 * H4, "l": 0.88, "c": 0.93})  # stale setup (gap)
        a, why, upd = pe.evaluate(r, self.b(), 0.95, NOW, self.HELD, {"t": T0, "l": 0.95, "c": 0.97})
        self.assertEqual(a, "wait")
        self.assertIsNone(upd["setup"])

    def test_same_bar_processed_once(self):
        a, why, upd = pe.evaluate(self.rec(last=T0), self.b(), 0.9, NOW, self.HELD, {"t": T0, "l": 0.88, "c": 0.93})
        self.assertEqual((a, upd), ("wait", {}))
        self.assertIn("no new closed", why)

    def test_close_below_lower_cancels(self):
        a, _, _ = pe.evaluate(self.rec(), self.b(), 0.75, NOW, self.HELD, {"t": T0, "l": 0.7, "c": 0.79})
        self.assertEqual(a, "cancel")

    def test_n1_red_trend_no_trigger(self):
        r = self.rec(setup={"t": T0 - H4, "l": 0.88, "c": 0.93})
        a, _, _ = pe.evaluate(r, self.b(trend="Red"), 0.95, NOW, self.HELD, {"t": T0, "l": 0.91, "c": 0.95})
        self.assertEqual(a, "wait")

    def test_bars_before_creation_ignored(self):
        r = self.rec(created=NOW)
        a, why, upd = pe.evaluate(r, self.b(), 0.9, NOW, self.HELD, {"t": T0, "l": 0.88, "c": 0.93})
        self.assertEqual((a, upd), ("wait", {}))
        self.assertIn("before the pending was created", why)

    def test_radar_misaligned_waits_without_consuming_bar(self):
        a, why, upd = pe.evaluate(self.rec(), self.b(bar_time=T0 - H4), 0.9, NOW, self.HELD, {"t": T0, "l": 0.88, "c": 0.93})
        self.assertEqual((a, upd), ("wait", {}))

    def test_continuation_uses_1d_bars(self):
        D = 86400 * 1000
        d0 = int(NOW.timestamp() * 1000) - D - 3600_000
        r = self.rec(kind=pe.CONTINUATION, created=NOW - timedelta(days=3), setup={"t": d0 - D, "l": 0.88, "c": 0.93})
        a, why, _ = pe.evaluate(r, self.b(tf="1d"), 0.95, NOW, set(), {"t": d0, "l": 0.9, "c": 0.96})
        self.assertEqual(a, "trigger", why)
        self.assertIn("1D", why)

    def test_expiry(self):
        a, _, _ = pe.evaluate(self.rec(created=NOW - timedelta(days=8)), self.b(), 0.85, NOW, self.HELD, None)
        self.assertEqual(a, "expire")

    def test_idempotency_rules(self):
        self.assertEqual(pe.evaluate(self.rec(pe.CONTINUATION), self.b(tf="1d"), 0.85, NOW, {"AAA"}, None)[0], "cancel")
        self.assertEqual(pe.evaluate(self.rec(pe.ADD_ON), self.b(), 0.85, NOW, set(), None)[0], "cancel")

    def test_missing_bar_fails_closed(self):
        self.assertEqual(pe.evaluate(self.rec(), self.b(), 0.85, NOW, self.HELD, None)[0], "wait")

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
        # 4H Filter 0.88 = tiny Hard SL ~2.7% below mid 0.9 (SoT-2 needs liq >= 2x SL distance below it)
        r4h = {"ts": ts, "rows": [dict(symbol="AAA", **(h4 or _row(0.85, 0.88, 0.95)))]}
        return r1d, r4h

    def entry(self, kind=pe.CONTINUATION, size=3, lev=2, tier="tiny", setup_close=0.89):
        D = 86400 * 1000 if kind == pe.CONTINUATION else H4
        created = NOW - timedelta(days=2)
        return [{"id": f"AAA_{kind}_20260928", "symbol": "AAA", "kind": kind, "status": "pending", "tier": tier,
                 "size_pct": size, "leverage": lev, "created_at": created.isoformat(),
                 "expires_at": (created + timedelta(days=7)).isoformat(),
                 "setup": {"t": T0 - D, "l": 0.85, "c": setup_close}}]

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
        self.assertEqual(f["size_pct"], 4.0)                  # SoT-2 hard cap 4%
        self.assertEqual(f["leverage"], 3)                    # AI 2x lifted to the SoT-2 3x floor
        self.assertEqual(f["hard_sl"], 0.88)                  # tiny -> 4H Filter
        self.assertLessEqual(f["risk_pct"], 1.5)
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
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.4",
               "returnOnEquity": "0.25", "marginUsed": "15"}  # ROE +25%, coin margin 1.5% NAV
        hl = FakeHL(mids={"AAA": 1.01}, positions=[pos], lev=3)
        ents = self.entry(kind=pe.ADD_ON, lev=2, tier="large")  # large: Hard SL = 4H Lower 0.95
        # 4H zone [0.95, 1.00]; bar low 0.99 touched, close 1.02 > Lower; mid 1.01 <= 1.0*1.01
        res = self.run_w(hl, ents, bar(0.99, 1.02), h4=_row(0.95, 1.00, 1.03), d1=_row(0.5, 0.6, 1.0))
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 3))   # keeps existing isolated leverage
        self.assertEqual(ents[0]["status"], "filled")

    def test_add_on_small_tiny_blocked_by_sl_distance(self):
        # Small/Tiny Hard SL = 4H Filter = top of the ADD_ON zone -> SL distance < 1.5% -> never fills (fail-closed)
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.4",
               "returnOnEquity": "0.25", "marginUsed": "15"}
        hl = FakeHL(mids={"AAA": 1.0}, positions=[pos], lev=2)
        ents = self.entry(kind=pe.ADD_ON, tier="tiny")
        res = self.run_w(hl, ents, bar(0.99, 1.02), h4=_row(0.95, 1.00, 1.03))
        self.assertEqual(hl.calls, [])
        self.assertIn("SL distance", res["checked"][0]["reason"])
        self.assertEqual(ents[0]["status"], "pending")

    def _add_on_run(self, roe, margin_used):
        pos = {"coin": "AAA", "szi": "50", "entryPx": "0.9", "liquidationPx": "0.4",
               "returnOnEquity": str(roe), "marginUsed": str(margin_used)}
        hl = FakeHL(mids={"AAA": 1.01}, positions=[pos], lev=3)
        ents = self.entry(kind=pe.ADD_ON, lev=2, tier="large")
        res = self.run_w(hl, ents, bar(0.99, 1.02), h4=_row(0.95, 1.00, 1.03), d1=_row(0.5, 0.6, 1.0))
        return hl, ents, res

    def test_add_on_requires_roe_10pct(self):
        hl, ents, res = self._add_on_run(0.08, 15)  # ROE +8%
        self.assertEqual(hl.calls, [])
        self.assertEqual(ents[0]["status"], "pending")
        self.assertIn("ROE 8.0% < +10%", res["checked"][0]["reason"])
        self.assertTrue(any("ROE 8.0%" in x["reason"] for x in res["run_report"]["skipped"]))

    def test_add_on_coin_exposure_cap_5_5pct(self):
        hl, ents, res = self._add_on_run(0.30, 40)  # 4.0% NAV used + 2% min add > 5.5%
        self.assertEqual(hl.calls, [])
        self.assertIn("5.5% cap", res["checked"][0]["reason"])

    def test_add_on_margin_limited_by_exposure_room(self):
        hl, ents, res = self._add_on_run(0.30, 30)  # 3.0% used -> room 2.5% (< 4% cap)
        self.assertEqual(ents[0]["status"], "filled", res["checked"])
        self.assertAlmostEqual(res["filled"][0]["size_pct"], 2.5, 3)
        self.assertEqual(hl.calls[0], ("set_leverage", "AAA", 3))

    def test_live_state_persisted_bar_n_then_n1_fill(self):
        # no setup yet: first run sets bar N (no order); next run (N+1) fills
        hl = FakeHL(mids={"AAA": 0.93})
        ents = self.entry()
        ents[0].pop("setup")
        D = 86400 * 1000
        self.run_w(hl, ents, bar(0.88, 0.91, t=T0 - D))
        self.assertEqual(hl.calls, [])
        self.assertEqual(ents[0]["setup"]["c"], 0.91)
        self.assertEqual(ents[0]["last_bar_t"], T0 - D)
        res = self.run_w(hl, ents, bar(0.9, 0.93, t=T0))
        self.assertEqual(ents[0]["status"], "filled", res["checked"])
        self.assertEqual([c[0] for c in hl.calls], ["set_leverage", "open_long_ioc", "place_stop_loss"])

    def test_add_on_cancelled_when_base_closed(self):
        hl = FakeHL(mids={"AAA": 0.98})
        ents = self.entry(kind=pe.ADD_ON)
        res = self.run_w(hl, ents, bar(0.97, 0.99), h4=_row(0.95, 1.0, 1.0))
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["cancelled"][0]["symbol"], "AAA")

    def test_closed_bar_picks_last_closed(self):
        class H:
            def info(self, payload):
                t0 = int(NOW.timestamp() * 1000) - 4 * 3600 * 1000 - 600_000  # closed 10 min ago
                return [{"t": t0 - 4 * 3600 * 1000, "l": "1", "c": "2"}, {"t": t0, "l": "3", "c": "4"},
                        {"t": t0 + 4 * 3600 * 1000, "l": "5", "c": "6"}]  # forming
        b = pw.closed_bar(H(), "AAA", "4h", NOW)
        self.assertEqual((b["l"], b["c"]), (3.0, 4.0))


if __name__ == "__main__":
    unittest.main()
