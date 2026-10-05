#!/usr/bin/env python3
"""Chase pending placement params (CHASE_* env): defaults == pre-param behaviour, custom values, invalid fallback.
No network, no keys."""
from __future__ import annotations

import io
import json
import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pending_entries as pe  # noqa: E402
from exec_common import parse_ts, round_price  # noqa: E402
from test_pending_entries import FakeHL, KEY, NOW, T0, H4, _row, bar  # noqa: E402
import pending_worker as pw  # noqa: E402

D = 86_400_000
CHASE_VARS = ("CHASE_PENDING_MODE", "CHASE_PENDING_OFFSET_PCT", "CHASE_PENDING_TTL_DAYS",
              "CHASE_MAX_CHASE_PCT", "CHASE_FILL_SLIPPAGE_PCT", "CHASE_MODE", "CHASE_LOG_ONLY_EXISTING",
              "CHASE_SHADOW_PATH")


def clean_env(**kw):
    env = {k: v for k, v in os.environ.items() if k not in CHASE_VARS}
    env.update(kw)
    return patch.dict(os.environ, env, clear=True)


def _ref_f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def ref_evaluate(rec: dict, bnd: Dict[str, Any], mid: Optional[float], now: datetime,
                 held_long: set, bar: Optional[dict] = None) -> Tuple[str, str, Dict[str, Any]]:
    """Frozen copy of pending_entries.evaluate on main @5255a4c (before CHASE_* params). Oracle only."""
    exp = parse_ts(rec.get("expires_at"))
    if exp and now >= exp:
        return "expire", f"expired after {7} days", {}
    sym, kind = rec.get("symbol"), rec.get("kind")
    if kind == pe.CONTINUATION and sym in held_long:
        return "cancel", "coin already held (idempotent: not adding a CONTINUATION)", {}
    if kind == pe.ADD_ON and sym not in held_long:
        return "cancel", "base position no longer held", {}
    tfk = bnd.get("tf") or pe.BAND_TF.get(kind, "4h")
    tf = tfk.upper()
    lo, fi, tr = bnd.get("lower"), bnd.get("filter"), bnd.get("trend")
    b_t, b_low, b_close = (bar or {}).get("t"), _ref_f((bar or {}).get("l")), _ref_f((bar or {}).get("c"))
    if not b_t or not b_low or not b_close:
        return "wait", f"no closed {tf} bar", {}
    b_t = int(b_t)
    if rec.get("last_bar_t") and int(rec["last_bar_t"]) >= b_t:
        return "wait", f"no new closed {tf} bar yet", {}
    created = parse_ts(rec.get("created_at"))
    if created and b_t + pe.BAR_MS[tfk] <= int(created.timestamp() * 1000):
        return "wait", f"latest {tf} bar closed before the pending was created", {}
    if not lo or not fi:
        return "wait", f"missing {tf} band values", {}
    if bnd.get("bar_time") is not None and int(bnd["bar_time"]) != b_t:
        return "wait", f"{tf} radar band not yet for the latest closed bar", {}
    upd: Dict[str, Any] = {"last_bar_t": b_t}
    if b_close < lo:
        return "cancel", f"{tf} closed {b_close:.6g} below {tf} Lower {lo:.6g}", upd
    setup = rec.get("setup") or None
    if setup and int(setup.get("t", 0)) + pe.BAR_MS[tfk] == b_t:
        n_close = float(setup.get("c"))
        if b_close > lo and b_close > n_close and tr == "Green":
            if not mid or mid <= lo:
                upd["setup"] = None
                return "wait", f"N+1 confirmed but live mid {mid} not above {tf} Lower {lo:.6g}", upd
            upd["setup"] = None
            return "trigger", (f"N+1 confirmed: {tf} close {b_close:.6g} > Lower {lo:.6g} and > N close "
                               f"{n_close:.6g}; enter at live mid {mid:.6g}"), upd
        why_n1 = (f"N+1 not confirmed ({tf} close {b_close:.6g} vs N close {n_close:.6g}, Lower {lo:.6g}, "
                  f"trend {tr})")
    else:
        why_n1 = ""
    if b_low <= fi and b_close > lo:
        upd["setup"] = {"t": b_t, "l": b_low, "c": b_close, "filter": fi, "lower": lo}
        return "wait", ((why_n1 + "; ") if why_n1 else "") + (
            f"bar N set: {tf} low {b_low:.6g} <= Filter {fi:.6g}, close {b_close:.6g} > Lower {lo:.6g}; "
            f"awaiting N+1 close > {max(lo, b_close):.6g}"), upd
    upd["setup"] = None
    return "wait", ((why_n1 + "; ") if why_n1 else "") + (
        f"no pullback: {tf} low {b_low:.6g} > Filter {fi:.6g}"), upd


def rec(kind=pe.CONTINUATION, created=NOW - timedelta(days=2), setup=None, last=None, ttl=7):
    r = {"id": "CHIP_x", "symbol": "CHIP", "kind": kind, "status": "pending", "created_at": created.isoformat(),
         "expires_at": (created + timedelta(days=ttl)).isoformat()}
    if setup:
        r["setup"] = setup
    if last:
        r["last_bar_t"] = last
    return r


# CHIP 10/5-style fixture (synthetic, same proportions): 1D Upper 1.00, Filter 0.90, Lower 0.80;
# bar N pulled back to 0.89 (<= Filter) and closed 0.95; bar N+1 closed 1.02; live mid 1.079 = +7.9% vs 1D Upper.
CHIP_UP, CHIP_FI, CHIP_LO, CHIP_MID = 1.00, 0.90, 0.80, 1.079
CHIP_N1 = int(NOW.timestamp() * 1000) - D - 3_600_000
CHIP_SETUP = {"t": CHIP_N1 - D, "l": 0.89, "c": 0.95, "filter": CHIP_FI, "lower": CHIP_LO}


def chip_bnd(**kw):
    b = {"tf": "1d", "lower": CHIP_LO, "filter": CHIP_FI, "close": 1.02, "trend": "Green", "bar_time": None,
         "upper": CHIP_UP, "upper_1d": CHIP_UP}
    b.update(kw)
    return b


class TestDefaultsReproduceCurrent(unittest.TestCase):
    def test_params_defaults(self):
        with clean_env():
            p = pe.chase_params()
        self.assertEqual(p, {"mode": "filter", "offset_pct": 0.0, "ttl_days": 7, "max_chase_pct": None,
                             "fill_slippage_pct": None, "chase_mode": "live", "log_only_existing": "keep"})

    def test_randomized_evaluate_equals_frozen_reference(self):
        rnd = random.Random(20261005)
        actions = set()
        with clean_env():
            for i in range(6000):
                kind = rnd.choice([pe.ADD_ON, pe.CONTINUATION])
                tf = pe.BAND_TF[kind]
                bms = pe.BAR_MS[tf]
                lo = round(rnd.uniform(0.5, 1.0), rnd.choice([2, 4, 6]))
                fi = round(lo * rnd.uniform(1.0, 1.2), 6)
                up = round(fi * rnd.uniform(1.0, 1.2), 6)
                bt = T0 - rnd.choice([0, 1, 2]) * bms
                setup = None
                if rnd.random() < 0.6:
                    setup = {"t": bt - rnd.choice([1, 1, 2]) * bms, "l": round(fi * rnd.uniform(0.9, 1.05), 6),
                             "c": round(lo * rnd.uniform(0.98, 1.3), 6)}
                r = rec(kind, created=NOW - timedelta(days=rnd.choice([0, 1, 3, 3, 3, 8])), setup=setup,
                        last=rnd.choice([None, None, None, bt - bms, bt]))
                bnd = {"tf": tf, "lower": rnd.choice([lo] * 5 + [None]), "filter": fi, "close": None,
                       "trend": rnd.choice(["Green", "Green", "Red"]),
                       "bar_time": rnd.choice([None, None, bt, bt, bt - bms]), "upper": up, "upper_1d": up}
                b = None if rnd.random() < 0.1 else {"t": bt, "l": round(rnd.uniform(lo * 0.9, up * 1.1), 6),
                                                     "c": round(rnd.uniform(lo * 0.95, up * 1.3), 6)}
                mid = rnd.choice([None, round(rnd.uniform(lo * 0.9, up * 1.3), 6)] + [round(up * 1.079, 6)] * 2)
                in_pos = rnd.random() < 0.9
                held = {"CHIP"} if (kind == pe.ADD_ON) == in_pos else set()
                got = pe.evaluate(dict(r), bnd, mid, NOW, held, b)
                want = ref_evaluate(dict(r), bnd, mid, NOW, held, b)
                self.assertEqual(got, want, (i, r, bnd, b, mid, held))
                actions.add(got[0] + (":bar N" if "bar N set" in got[1] else ""))
        self.assertEqual(actions, {"expire", "cancel", "wait", "wait:bar N", "trigger"})

    def test_chip_default_triggers_like_today(self):
        with clean_env():
            r = rec(setup=CHIP_SETUP)
            b = {"t": CHIP_N1, "l": 0.97, "c": 1.02}
            got = pe.evaluate(r, chip_bnd(), CHIP_MID, NOW, set(), b)
        self.assertEqual(got, ref_evaluate(r, chip_bnd(), CHIP_MID, NOW, set(), b))
        self.assertEqual(got[0], "trigger")
        self.assertIn("enter at live mid 1.079", got[1])

    def test_create_pending_ttl_unchanged(self):
        with clean_env():
            e = []
            r, _ = pe.create_pending(e, "CHIP", pe.CONTINUATION, {"size_pct": 3}, {}, {"tf": "1d"}, NOW)
        self.assertEqual(r["expires_at"], (NOW + timedelta(days=7)).isoformat())

    def test_band_keeps_existing_keys(self):
        b = pe.band(pe.ADD_ON, _row(0.5, 0.6, 1.0, upper=0.7), _row(0.8, 0.9, 1.0, upper=0.95))
        self.assertEqual((b["tf"], b["lower"], b["filter"], b["close"], b["trend"]), ("4h", 0.8, 0.9, 1.0, "Green"))
        self.assertEqual((b["upper"], b["upper_1d"]), (0.95, 0.7))

    def test_summary_trigger_text_unchanged(self):
        ents = [rec()]
        with clean_env():
            s = pe.summary(ents, {"CHIP": _row(0.8, 0.9, 1.0)}, {}, {})
        self.assertEqual(s[0]["trigger"], "waiting for 1D bar N: low <= Filter 0.9 and close > Lower 0.8; "
                                          "then N+1 close > Lower and > N close")


class TestHLWorkerFillPrice(unittest.TestCase):
    """HL pending fill = IOC limit at round_price(mid * (1 + slip%)); slip inherits EXEC_ENTRY_SLIPPAGE_PCT."""

    def run_w(self, mid, env=None, d1=None):
        ts = (NOW - timedelta(minutes=5)).isoformat()
        r1d = {"ts": ts, "rows": [dict(symbol="AAA", **(d1 or _row(0.80, 0.90, 1.00, upper=1.0)))]}
        r4h = {"ts": ts, "rows": [dict(symbol="AAA", **_row(0.85, 0.88, 0.95))]}
        created = NOW - timedelta(days=2)
        ents = [{"id": "AAA_CONTINUATION_x", "symbol": "AAA", "kind": pe.CONTINUATION, "status": "pending",
                 "tier": "small", "size_pct": 3, "leverage": 3, "created_at": created.isoformat(),
                 "expires_at": (created + timedelta(days=7)).isoformat(),
                 "setup": {"t": T0 - D, "l": 0.85, "c": 0.89}}]
        hl = FakeHL(mids={"AAA": mid})
        base = {"PENDING_PATH": "/nonexistent/p.json", "EXEC_DRY_RUN": "1", "HL_API_PRIVATE_KEY": KEY}
        with clean_env(**base, **(env or {})):
            res = pw.run_pending(hl=hl, radar_1d=r1d, radar_4h=r4h, now=NOW, entries=ents,
                                 log_entry_fn=lambda **k: None, bar_fn=bar(0.88, 0.93))
        return res

    def test_default_limit_price_is_mid_plus_half_pct(self):
        res = self.run_w(0.9)
        self.assertEqual(res["filled"][0]["limit_px"], round_price(0.9 * 1.005, 0))
        self.assertEqual(res["chase_params"]["fill_slippage_pct"], None)

    def test_inherits_exec_entry_slippage_when_unset(self):
        res = self.run_w(0.9, {"EXEC_ENTRY_SLIPPAGE_PCT": "1.2"})
        self.assertEqual(res["filled"][0]["limit_px"], round_price(0.9 * 1.012, 0))

    def test_chase_fill_slippage_override(self):
        res = self.run_w(0.9, {"EXEC_ENTRY_SLIPPAGE_PCT": "1.2", "CHASE_FILL_SLIPPAGE_PCT": "0.2"})
        self.assertEqual(res["filled"][0]["limit_px"], round_price(0.9 * 1.002, 0))

    def test_invalid_chase_fill_slippage_falls_back_to_inherited(self):
        res = self.run_w(0.9, {"CHASE_FILL_SLIPPAGE_PCT": "5"})
        self.assertEqual(res["filled"][0]["limit_px"], round_price(0.9 * 1.005, 0))

    def test_chip_max_chase_on_hl_path(self):
        # 1D Upper 1.0 in the radar; mid 1.079 (+7.9%)
        self.assertEqual(len(self.run_w(CHIP_MID)["filled"]), 1)                      # default: fills as today
        res = self.run_w(CHIP_MID, {"CHASE_MAX_CHASE_PCT": "5"})
        self.assertEqual(res["filled"], [])
        self.assertIn("7.90% above 1D Upper", res["checked"][0]["reason"])
        self.assertEqual(len(self.run_w(CHIP_MID, {"CHASE_MAX_CHASE_PCT": "8"})["filled"]), 1)


class TestCustomValues(unittest.TestCase):
    def test_max_chase_blocks_chip(self):
        r = rec(setup=CHIP_SETUP)
        b = {"t": CHIP_N1, "l": 0.97, "c": 1.02}
        p = dict(pe.chase_params({}), max_chase_pct=5.0)
        a, why, upd = pe.evaluate(r, chip_bnd(), CHIP_MID, NOW, set(), b, p)
        self.assertEqual(a, "wait")
        self.assertIn("1.079 is 7.90% above 1D Upper 1 (max chase 5%)", why)
        self.assertIsNone(upd["setup"])
        self.assertEqual(upd["last_bar_t"], CHIP_N1)

    def test_max_chase_allows_within_cap(self):
        p = dict(pe.chase_params({}), max_chase_pct=8.0)
        a, _, _ = pe.evaluate(rec(setup=CHIP_SETUP), chip_bnd(), CHIP_MID, NOW, set(),
                              {"t": CHIP_N1, "l": 0.97, "c": 1.02}, p)
        self.assertEqual(a, "trigger")

    def test_max_chase_without_1d_upper_fails_closed(self):
        p = dict(pe.chase_params({}), max_chase_pct=8.0)
        a, why, _ = pe.evaluate(rec(setup=CHIP_SETUP), chip_bnd(upper_1d=None), CHIP_MID, NOW, set(),
                                {"t": CHIP_N1, "l": 0.97, "c": 1.02}, p)
        self.assertEqual(a, "wait")
        self.assertIn("no 1D Upper", why)

    def _bar_n(self, p, low):
        return pe.evaluate(rec(), chip_bnd(), 0.95, NOW, set(), {"t": CHIP_N1, "l": low, "c": 0.95}, p)

    def test_offset_requires_deeper_pullback(self):
        p = pe.chase_params({"CHASE_PENDING_OFFSET_PCT": "2"})       # touch = 0.90 * 0.98 = 0.882
        a, why, upd = self._bar_n(p, 0.885)
        self.assertNotIn("setup", {k for k, v in upd.items() if v})
        self.assertIn("no pullback: 1D low 0.885 > Filter -2% 0.882", why)
        a, why, upd = self._bar_n(p, 0.882)
        self.assertEqual(upd["setup"]["touch"], 0.9 * 0.98)
        self.assertIn("<= Filter -2% 0.882", why)

    def test_mode_upper_shallow_touch(self):
        p = pe.chase_params({"CHASE_PENDING_MODE": "upper"})
        self.assertIn("no pullback", self._bar_n(dict(pe.chase_params({})), 0.97)[1])  # default: 0.97 > Filter
        a, why, upd = self._bar_n(p, 0.97)                                              # 0.97 <= Upper 1.0
        self.assertEqual(upd["setup"]["touch"], 1.0)
        self.assertIn("<= Upper 1", why)

    def test_mode_upper_with_offset(self):
        p = pe.chase_params({"CHASE_PENDING_MODE": "upper", "CHASE_PENDING_OFFSET_PCT": "3"})
        self.assertEqual(pe.touch_level(chip_bnd(), p), (1.0 * 0.97, "Upper -3%"))

    def test_mode_lower(self):
        p = pe.chase_params({"CHASE_PENDING_MODE": "lower"})
        self.assertIn("no pullback", self._bar_n(p, 0.85)[1])
        self.assertIn("<= Lower 0.8", self._bar_n(p, 0.80)[1])

    def test_mode_upper_missing_band_upper_waits_without_consuming(self):
        p = pe.chase_params({"CHASE_PENDING_MODE": "upper"})
        self.assertEqual(pe.evaluate(rec(), chip_bnd(upper=None), 0.95, NOW, set(),
                                     {"t": CHIP_N1, "l": 0.9, "c": 0.95}, p),
                         ("wait", "missing 1D band values", {}))

    def test_ttl_days(self):
        with clean_env(CHASE_PENDING_TTL_DAYS="3.5"):
            r, _ = pe.create_pending([], "CHIP", pe.CONTINUATION, {}, {}, {"tf": "1d"}, NOW)
        self.assertEqual(r["expires_at"], (NOW + timedelta(days=3.5)).isoformat())
        a, why, _ = pe.evaluate(r, chip_bnd(), 1.0, NOW + timedelta(days=4), set(), None, pe.chase_params({}))
        self.assertEqual((a, why), ("expire", "expired after 3.5 days"))

    def test_existing_record_keeps_its_stored_expiry(self):
        r = rec(created=NOW - timedelta(days=6))
        a, _, _ = pe.evaluate(r, chip_bnd(), 1.0, NOW, set(), None, pe.chase_params({"CHASE_PENDING_TTL_DAYS": "1"}))
        self.assertEqual(a, "wait")


class TestInvalidFallBack(unittest.TestCase):
    def setUp(self):
        pe._warned.clear()

    def params(self, **env):
        err = io.StringIO()
        with patch("sys.stderr", err):
            p = pe.chase_params(env)
        return p, err.getvalue()

    def test_invalid_values_fall_back_with_warning(self):
        defaults = pe.chase_params({})
        cases = [
            ("CHASE_PENDING_MODE", "market", "mode"),
            ("CHASE_PENDING_OFFSET_PCT", "-1", "offset_pct"),
            ("CHASE_PENDING_OFFSET_PCT", "abc", "offset_pct"),
            ("CHASE_PENDING_OFFSET_PCT", "nan", "offset_pct"),
            ("CHASE_PENDING_OFFSET_PCT", "60", "offset_pct"),
            ("CHASE_PENDING_TTL_DAYS", "0", "ttl_days"),
            ("CHASE_PENDING_TTL_DAYS", "-2", "ttl_days"),
            ("CHASE_PENDING_TTL_DAYS", "inf", "ttl_days"),
            ("CHASE_PENDING_TTL_DAYS", "45", "ttl_days"),
            ("CHASE_MAX_CHASE_PCT", "-3", "max_chase_pct"),
            ("CHASE_MAX_CHASE_PCT", "x", "max_chase_pct"),
            ("CHASE_FILL_SLIPPAGE_PCT", "-0.1", "fill_slippage_pct"),
            ("CHASE_FILL_SLIPPAGE_PCT", "3", "fill_slippage_pct"),
        ]
        for name, raw, key in cases:
            pe._warned.clear()
            p, err = self.params(**{name: raw})
            self.assertEqual(p[key], defaults[key], (name, raw))
            self.assertIn(f"[PENDING_CFG] {name}=", err, (name, raw))

    def test_blank_is_default_without_warning(self):
        p, err = self.params(CHASE_PENDING_MODE=" ", CHASE_PENDING_OFFSET_PCT="", CHASE_MAX_CHASE_PCT="")
        self.assertEqual(p, pe.chase_params({}))
        self.assertEqual(err, "")

    def test_valid_edges_accepted(self):
        p, err = self.params(CHASE_PENDING_MODE="UPPER", CHASE_PENDING_OFFSET_PCT="0", CHASE_PENDING_TTL_DAYS="30",
                             CHASE_MAX_CHASE_PCT="0", CHASE_FILL_SLIPPAGE_PCT="2")
        self.assertEqual(p, {"mode": "upper", "offset_pct": 0.0, "ttl_days": 30.0, "max_chase_pct": 0.0,
                             "fill_slippage_pct": 2.0, "chase_mode": "live", "log_only_existing": "keep"})
        self.assertEqual(err, "")

    def test_warning_logged_once(self):
        _, e1 = self.params(CHASE_PENDING_TTL_DAYS="0")
        _, e2 = self.params(CHASE_PENDING_TTL_DAYS="0")
        self.assertTrue(e1)
        self.assertEqual(e2, "")

    def test_invalid_env_evaluate_still_matches_reference(self):
        r = rec(setup=CHIP_SETUP)
        b = {"t": CHIP_N1, "l": 0.97, "c": 1.02}
        with clean_env(CHASE_PENDING_MODE="bogus", CHASE_PENDING_OFFSET_PCT="-5", CHASE_MAX_CHASE_PCT="-1"), \
                patch("sys.stderr", io.StringIO()):
            got = pe.evaluate(r, chip_bnd(), CHIP_MID, NOW, set(), b)
        self.assertEqual(got, ref_evaluate(r, chip_bnd(), CHIP_MID, NOW, set(), b))


class TestBitunixPaths(unittest.TestCase):
    """bx_live.run_live_pending and bx_shadow pass the 1D Upper into evaluate (max-chase works on BX)."""

    def setUp(self):
        import shutil
        import tempfile
        import bx_live as L
        import bx_radar
        import bx_shadow
        self.L, self.R, self.S, self.shutil = L, bx_radar, bx_shadow, shutil
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (L.OUT_DIR, bx_radar.OUT_DIR, bx_shadow.OUT_DIR)
        L.OUT_DIR = bx_radar.OUT_DIR = bx_shadow.OUT_DIR = self.tmp
        self._ledger = os.environ.get("BX_LEDGER_PATH")
        os.environ["BX_LEDGER_PATH"] = str(self.tmp / "bx.db")

    def tearDown(self):
        self.L.OUT_DIR, self.R.OUT_DIR, self.S.OUT_DIR = self._orig
        if self._ledger is None:
            os.environ.pop("BX_LEDGER_PATH", None)
        else:
            os.environ["BX_LEDGER_PATH"] = self._ledger
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def _bx_live(self, env):
        L = self.L
        sym = "CHIPUSDT"
        r = rec(setup=CHIP_SETUP)
        r["symbol"], r["approved_at"] = sym, NOW.isoformat()
        L.save_live_pending([r])
        (self.tmp / "bx_radar_1d.json").write_text(json.dumps({"rows": [
            {"symbol": "CHIP", "bx_symbol": sym, "lower": CHIP_LO, "filter": CHIP_FI, "upper": CHIP_UP,
             "close": 1.02, "low": 0.97, "trend": "Green", "bar_time": CHIP_N1}]}))
        conn = L.connect()
        try:
            with clean_env(**env):
                return L.run_live_pending(None, conn, NOW, False, ["BX_LIVE=0 (shadow only)"],
                                          {sym: {"price": CHIP_MID}}, {}, None, None, None)
        finally:
            conn.close()

    def test_bx_live_default_triggers_and_cap_waits(self):
        out = self._bx_live({})
        self.assertEqual(out[0]["action"], "trigger")
        out = self._bx_live({"CHASE_MAX_CHASE_PCT": "5"})
        self.assertEqual(out[0]["action"], "wait")
        self.assertIn("7.90% above 1D Upper", out[0]["reason"])

    def _bx_shadow(self, env):
        S = self.S
        T = datetime(2026, 9, 29, 0, 20, tzinfo=timezone.utc)
        ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731

        def row(**kw):
            r = {"symbol": "FOO", "bx_symbol": "FOOUSDT", "close": 2.0, "low": 1.95, "high": 2.05, "filter": 1.8,
                 "upper": 1.9, "lower": 1.7, "trend": "Green", "dual_cross_up": False, "bar_time": ms(T) - D}
            r.update(kw)
            return r

        def meta(**kw):
            m = {"symbol": "FOO", "bx_symbol": "FOOUSDT", "ex": "BX", "asset_class": "crypto",
                 "liq_tier": "tradeable", "gc_tf": "1d", "price": 2.0, "tier": "small", "spread_bp": 4.0,
                 "vol24h_usd": 3e6, "max_notional_usd": 15000.0, "asset_age": "old"}
            m.update(kw)
            return m

        def write(metas, r1d, r4h):
            (self.tmp / "bx_meta.json").write_text(json.dumps({"scanned": metas}))
            for tf, rows in (("1d", r1d), ("4h", r4h), ("1h", [])):
                (self.tmp / f"bx_radar_{tf}.json").write_text(json.dumps({"rows": rows}))

        conn = S.connect(str(self.tmp / f"shadow_{len(env)}.db"))
        try:
            with clean_env(**env, BX_LEDGER_PATH=str(self.tmp / "bx.db")):
                S.pending_path().unlink(missing_ok=True)
                write([meta()], [row()], [row(dual_cross_up=True)])
                S.run("daily", now=T, nav_usd=10_000, conn=conn)
                d1 = T + timedelta(days=1)
                write([meta()], [row(low=1.79, close=1.85, bar_time=ms(d1) - D)], [row()])
                S.run("daily", now=d1, nav_usd=10_000, conn=conn)
                d2 = T + timedelta(days=2)
                write([meta(price=1.95)], [row(low=1.84, close=1.93, bar_time=ms(d2) - D)], [row()])
                rep = S.run("daily", now=d2, nav_usd=10_000, conn=conn)
            return rep
        finally:
            conn.close()

    def test_bx_shadow_default_fills_and_cap_waits(self):
        rep = self._bx_shadow({})                                  # price 1.95 = +2.63% vs 1D Upper 1.9
        self.assertEqual(rep["opened"][0]["kind"], "Chase")
        rep = self._bx_shadow({"CHASE_MAX_CHASE_PCT": "2"})
        self.assertEqual(rep["opened"], [])
        self.assertIn("above 1D Upper", rep["pending"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
