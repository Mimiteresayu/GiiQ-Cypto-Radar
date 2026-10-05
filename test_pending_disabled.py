#!/usr/bin/env python3
"""Cove HEALTH FAIL 2026-10-05: (a) CONTINUATION / ADD_ON pending disabled, (b) decision-day candidate
freeze + preflight OK when already pending. HL fully mocked; no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entry_candidates as ec_mod  # noqa: E402
import exec_preflight  # noqa: E402
import executor  # noqa: E402
import pending_entries as pe  # noqa: E402
import pending_worker as pw  # noqa: E402
import test_live_execution as TLE  # noqa: E402

NOW = datetime(2026, 10, 5, 0, 55, tzinfo=timezone.utc)  # 08:55 HKT
SNAP = "entry_candidates_decision_20261005.json"
KEY = "0x" + "11" * 32
LIVE_ENV = {"EXEC_DRY_RUN": "0", "HL_API_PRIVATE_KEY": KEY}


def _active(sym="BTC", kind=pe.CONTINUATION, created=NOW - timedelta(hours=1)):
    return {"id": f"{sym}_{kind}_20261005", "symbol": sym, "kind": kind, "status": pe.ACTIVE,
            "created_at": created.isoformat(), "expires_at": (created + timedelta(days=7)).isoformat()}


def _cands(*items, at=NOW - timedelta(minutes=50)):
    return {"generated_at": at.isoformat(), "stale": False, "count": len(items), "candidates": list(items)}


CHIP = TLE._cand("CHIP", tier="small", filt=0.97, lower=0.95)  # Base: mid 1.0 > 1D Upper 0.95
BTC_CHASE = dict(TLE._cand("BTC", typ="Chase"), is_base=False, is_chase=True)
DECISIONS = {"CHIP": {"decision": "approve", "type": "BASE", "size_pct": 2, "leverage": 3},
             "BTC": {"decision": "approve", "type": "CHASE", "size_pct": 3, "leverage": 3}}
META = {"CHIP": {"szDecimals": 0, "maxLeverage": 3.0}, "BTC": {"szDecimals": 5, "maxLeverage": 40.0}}
MIDS = {"CHIP": 1.0, "BTC": 1.0}


class TmpStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "out"
        self.out.mkdir()
        self._env = patch.dict(os.environ, {"PENDING_PATH": str(self.tmp / "pending.json")})
        self._env.start()
        patch.object(ec_mod, "OUT_DIR", self.out).start()

    def tearDown(self):
        patch.stopall()
        self._env.stop()

    def write(self, name, data):
        (self.out / name).write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------ (a) flag
class TestFlag(unittest.TestCase):
    def test_default_is_disabled(self):
        with patch.dict(os.environ, {}):
            os.environ.pop(pe.DISABLE_ENV, None)
            self.assertTrue(pe.pending_disabled())

    def test_values(self):
        for v, want in (("1", True), ("true", True), ("", True), ("0", False), ("false", False), ("OFF", False)):
            with patch.dict(os.environ, {pe.DISABLE_ENV: v}):
                self.assertEqual(pe.pending_disabled(), want, v)


class TestCancelAndBoot(TmpStore):
    def test_cancel_only_active_pending_kinds(self):
        ents = [_active("BTC"), _active("WLD", pe.ADD_ON), dict(_active("ETHFI"), status="filled")]
        gone = pe.cancel_active_pending(ents, NOW)
        self.assertEqual([e["symbol"] for e in gone], ["BTC", "WLD"])
        self.assertEqual(ents[0]["close_reason"], "disabled by Cove HEALTH FAIL 2026-10-05")
        self.assertEqual(ents[0]["status"], "cancelled")
        self.assertEqual(ents[2]["status"], "filled")

    def test_boot_enforce_clears_store(self):
        pe.save_pending([_active("BTC"), _active("PURR")])
        with patch.dict(os.environ, {pe.DISABLE_ENV: "1"}):
            r = pe.enforce_disabled(NOW)
        self.assertEqual(r["cancelled"], ["BTC_CONTINUATION_20261005", "PURR_CONTINUATION_20261005"])
        self.assertEqual(pe.active(pe.load_pending()), [])

    def test_boot_enforce_noop_when_enabled(self):
        pe.save_pending([_active("BTC")])
        with patch.dict(os.environ, {pe.DISABLE_ENV: "0"}):
            r = pe.enforce_disabled(NOW)
        self.assertFalse(r["disabled"])
        self.assertEqual(len(pe.active(pe.load_pending())), 1)

    def test_serve_boot_hook(self):
        import serve
        pe.save_pending([_active("BTC")])
        with patch.dict(os.environ, {pe.DISABLE_ENV: "1"}):
            r = serve._enforce_pending_disabled_at_boot()
        self.assertEqual(r["cancelled"], ["BTC_CONTINUATION_20261005"])


class TestPendingWorkerDisabled(TmpStore):
    def test_live_cancels_and_never_evaluates(self):
        hl = TLE.FakeHL(equity=1000, meta=META, mids=MIDS)
        ents = [_active("BTC"), _active("WLD", pe.ADD_ON)]
        with patch.dict(os.environ, {**LIVE_ENV, pe.DISABLE_ENV: "1"}):
            res = pw.run_pending(hl=hl, radar_1d={"rows": []}, radar_4h={"rows": []}, now=NOW, entries=ents,
                                 bar_fn=lambda *a: self.fail("must not fetch bars while disabled"))
        self.assertEqual(res["status"], "success")
        self.assertEqual(hl.calls, [])
        self.assertEqual([c["symbol"] for c in res["cancelled"]], ["BTC", "WLD"])
        self.assertEqual({e["status"] for e in ents}, {"cancelled"})
        self.assertIn("DISABLED", res["message"])
        self.assertEqual(res["pending_active"], [])

    def test_live_persists_store(self):
        pe.save_pending([_active("BTC")])
        with patch.dict(os.environ, {**LIVE_ENV, pe.DISABLE_ENV: "1"}):
            pw.run_pending(hl=TLE.FakeHL(), now=NOW)
        self.assertEqual(pe.load_pending()[0]["status"], "cancelled")

    def test_dry_run_does_not_mutate(self):
        ents = [_active("BTC")]
        with patch.dict(os.environ, {"EXEC_DRY_RUN": "1", pe.DISABLE_ENV: "1"}):
            res = pw.run_pending(hl=TLE.FakeHL(), now=NOW, entries=ents)
        self.assertEqual(ents[0]["status"], pe.ACTIVE)
        self.assertIn("would cancel", res["cancelled"][0]["reason"])


class TestExecutorDisabled(TmpStore):
    def setUp(self):
        super().setUp()
        self.log_entry = patch.object(executor, "log_entry").start()

    def run_exec(self, env, cands=None, decisions=None, hl=None):
        hl = hl or TLE.FakeHL(equity=1000, meta=META, mids=MIDS)
        with patch.dict(os.environ, env):
            res = executor.execute_approved_candidates(
                hl=hl, candidates_data=cands or _cands(BTC_CHASE, CHIP), decisions=decisions or DECISIONS,
                radar_1h={}, radar_4h={"rows": []}, radar_1d={"rows": []}, now=NOW)
        return res, hl

    def test_live_chase_no_entry_base_still_fills_with_hard_sl(self):
        pe.save_pending([_active("ETHFI", created=NOW - timedelta(days=3))])
        res, hl = self.run_exec({**LIVE_ENV, pe.DISABLE_ENV: "1"})
        # Base CHIP: unchanged path (IOC entry + reduce-only Hard SL at 4H Filter for small tier)
        self.assertEqual(hl.names(), ["set_leverage", "open_long_ioc", "place_stop_loss"])
        self.assertEqual(hl.calls[2][1], "CHIP")
        self.assertEqual(hl.calls[2][3], 0.97)
        self.assertEqual([x["symbol"] for x in res["executed"]], ["CHIP"])
        # Chase BTC: acknowledged, no entry, no pending, not a skip
        self.assertEqual(res["pending"], [])
        self.assertEqual([n["symbol"] for n in res["no_entry"]], ["BTC"])
        self.assertIn("disabled by Cove HEALTH FAIL 2026-10-05", res["no_entry"][0]["reason"])
        self.assertEqual(res["skipped"], [])
        # existing active CONTINUATION cancelled in the store
        stored = pe.load_pending()
        self.assertEqual([(e["symbol"], e["status"]) for e in stored], [("ETHFI", "cancelled")])
        self.assertEqual(res["pending_cancelled"], ["ETHFI_CONTINUATION_20261005"])
        rep = res["run_report"]
        self.assertEqual(rep["skipped"], [])
        self.assertEqual([n["symbol"] for n in rep["no_entry"]], ["BTC"])
        self.assertEqual(rep["pending_disabled"], pe.DISABLED_REASON)

    def test_live_add_on_also_disabled(self):
        pos = {"coin": "BTC", "szi": "0.01", "entryPx": "0.9", "liquidationPx": "0.3"}
        hl = TLE.FakeHL(equity=1000, meta=META, mids=MIDS, positions=[pos])
        res, hl = self.run_exec({**LIVE_ENV, pe.DISABLE_ENV: "1"}, cands=_cands(BTC_CHASE),
                                decisions={"BTC": DECISIONS["BTC"]}, hl=hl)
        self.assertEqual(hl.calls, [])
        self.assertEqual(res["pending"], [])
        self.assertEqual(pe.load_pending(), [])
        self.assertEqual(res["no_entry"][0]["symbol"], "BTC")

    def test_dry_run_chase_no_would_create(self):
        res, hl = self.run_exec({"EXEC_DRY_RUN": "1", pe.DISABLE_ENV: "1"})
        self.assertEqual(res["pending"], [])
        self.assertEqual(res["no_entry"][0]["symbol"], "BTC")
        self.assertEqual([a["symbol"] for a in res["actions"]], ["CHIP"])

    def test_reenabled_creates_pending_again(self):
        res, _ = self.run_exec({**LIVE_ENV, pe.DISABLE_ENV: "0"})
        self.assertEqual(res["no_entry"], [])
        self.assertEqual([(p["symbol"], p["kind"]) for p in res["pending"]], [("BTC", "CONTINUATION")])
        self.assertEqual(len(pe.active(pe.load_pending())), 1)


# ------------------------------------------------------------------ (b) freeze
class TestFreeze(TmpStore):
    def test_freeze_writes_hkt_dated_snapshot_and_keeps_first(self):
        self.write("entry_candidates_latest.json", _cands(BTC_CHASE, CHIP))
        r = ec_mod.freeze_decision_snapshot(now=NOW, source="1d_scan_candidates")
        self.assertTrue(r["written"])
        self.assertEqual(r["file"], SNAP)
        snap = json.loads((self.out / SNAP).read_text())
        self.assertEqual([c["symbol"] for c in snap["candidates"]], ["BTC", "CHIP"])
        self.assertEqual(snap["frozen_by"], "1d_scan_candidates")
        # later regen drops Chase-only BTC; a POST-time freeze keeps the 08:05 snapshot
        self.write("entry_candidates_latest.json", _cands(CHIP, at=NOW + timedelta(hours=5)))
        r2 = ec_mod.freeze_decision_snapshot(now=NOW + timedelta(hours=5), source="ai_decision_post")
        self.assertFalse(r2["written"])
        snap = json.loads((self.out / SNAP).read_text())
        self.assertEqual([c["symbol"] for c in snap["candidates"]], ["BTC", "CHIP"])
        # scheduled 08:05 run overwrites
        r3 = ec_mod.freeze_decision_snapshot(now=NOW, overwrite=True, source="1d_scan_candidates")
        self.assertTrue(r3["written"])

    def test_hkt_date_boundary(self):
        # 2026-10-05 16:30 UTC = 2026-10-06 00:30 HKT
        self.assertEqual(ec_mod.decision_snapshot_name(datetime(2026, 10, 5, 16, 30, tzinfo=timezone.utc)),
                         "entry_candidates_decision_20261006.json")

    def test_never_freezes_a_list_not_built_today(self):
        self.write("entry_candidates_latest.json", _cands(CHIP, at=NOW - timedelta(days=1)))
        r = ec_mod.freeze_decision_snapshot(now=NOW)
        self.assertFalse(r["ok"])
        self.assertFalse((self.out / SNAP).exists())

    def test_load_falls_back_to_latest_with_label(self):
        self.write("entry_candidates_latest.json", _cands(CHIP))
        data, label, frozen = ec_mod.load_decision_candidates(NOW)
        self.assertFalse(frozen)
        self.assertIn("fallback to latest", label)
        self.assertEqual([c["symbol"] for c in data["candidates"]], ["CHIP"])
        # yesterday's snapshot does not count
        self.write(SNAP, dict(_cands(BTC_CHASE, at=NOW - timedelta(days=1)), frozen_at="x"))
        _d, label, frozen = ec_mod.load_decision_candidates(NOW)
        self.assertFalse(frozen)

    def test_serve_1d_scan_freezes(self):
        import serve
        self.write("entry_candidates_latest.json", _cands(BTC_CHASE, CHIP, at=datetime.now(timezone.utc)))
        with patch.object(serve, "OUT_DIR", str(self.out)), \
             patch.object(serve, "_run_scan", return_value=(True, "ok")), \
             patch.object(serve, "_generate_entry_candidates", return_value=2), \
             patch.object(serve, "_update_job_status"), patch.object(serve, "_log_desk_data"):
            serve._scheduled_1d_scan()
        self.assertTrue((self.out / ec_mod.decision_snapshot_name()).is_file())

    def test_serve_decision_note_lists_chase_no_entry(self):
        import serve
        self.write("entry_candidates_latest.json", _cands(BTC_CHASE, CHIP, at=datetime.now(timezone.utc)))
        stored = [{"symbol": "BTC", "decision": "approve", "type": None},
                  {"symbol": "CHIP", "decision": "approve", "type": "BASE"}]
        with patch.object(serve, "OUT_DIR", str(self.out)), patch.dict(os.environ, {pe.DISABLE_ENV: "1"}):
            note = serve._chase_no_entry_note(stored)
        self.assertEqual(note["chase_no_entry"], ["BTC"])
        with patch.object(serve, "OUT_DIR", str(self.out)), patch.dict(os.environ, {pe.DISABLE_ENV: "0"}):
            self.assertEqual(serve._chase_no_entry_note(stored), {})


class TestExecutorUsesSnapshot(TmpStore):
    def setUp(self):
        super().setUp()
        self.log_entry = patch.object(executor, "log_entry").start()

    def run_exec(self, env, latest):
        hl = TLE.FakeHL(equity=1000, meta=META, mids=MIDS)
        with patch.dict(os.environ, env), patch.object(executor, "_load_candidates", return_value=latest):
            return executor.execute_approved_candidates(
                hl=hl, candidates_data=None, decisions=DECISIONS,
                radar_1h={}, radar_4h={"rows": []}, radar_1d={"rows": []}, now=NOW)

    def test_matches_snapshot_not_regenerated_latest(self):
        self.write(SNAP, dict(_cands(BTC_CHASE, CHIP), frozen_at=NOW.isoformat(), frozen_by="1d_scan_candidates"))
        latest = _cands(CHIP, at=NOW - timedelta(minutes=5))           # regen dropped BTC
        res = self.run_exec({**LIVE_ENV, pe.DISABLE_ENV: "0"}, latest)
        self.assertIn(SNAP, res["candidates_source"])
        self.assertEqual([p["symbol"] for p in res["pending"]], ["BTC"])
        self.assertEqual([x["symbol"] for x in res["executed"]], ["CHIP"])

    def test_snapshot_missing_falls_back_to_latest(self):
        latest = _cands(CHIP)
        self.write("entry_candidates_latest.json", latest)
        res = self.run_exec({**LIVE_ENV, pe.DISABLE_ENV: "1"}, latest)
        self.assertIn("fallback to latest", res["candidates_source"])
        self.assertEqual([x["symbol"] for x in res["executed"]], ["CHIP"])

    def test_freshness_guard_still_on_latest(self):
        self.write(SNAP, dict(_cands(CHIP), frozen_at=NOW.isoformat()))
        res = self.run_exec({**LIVE_ENV, pe.DISABLE_ENV: "1"}, dict(_cands(CHIP), stale=True))
        self.assertEqual(res["status"], "fail_closed")


# ------------------------------------------------------------------ preflight
class PFHL:
    address = "0x" + "cd" * 20

    def agent_status(self, addr):
        return {"ok": True, "name": "Railway Key", "days_left": 300.0, "valid_until_ms": 1, "reason": "approved"}

    def perp_state(self):
        return {"assetPositions": []}

    def spot_state(self):
        return {"balances": [{"coin": "USDC", "total": "1679"}]}

    def probe_signing(self):
        return {"ok": True}

    def meta(self):
        return META

    def all_mids(self):
        return MIDS


class TestPreflight(TmpStore):
    def run_pf(self, env, latest, snap=None, pending=None):
        self.write("entry_candidates_latest.json", latest)
        if snap is not None:
            self.write(SNAP, snap)
        if pending is not None:
            pe.save_pending(pending)
        with patch.object(exec_preflight, "ROOT", self.tmp), patch.dict(os.environ, {**LIVE_ENV, **env}), \
             patch("decisions.get_decisions_for_today", return_value=DECISIONS):
            return exec_preflight.run_preflight(hl=PFHL(), now=NOW)

    @staticmethod
    def guard(res, sym):
        return next(c for c in res["checks"] if c["check"] == f"guard:{sym}")

    def test_btc_missing_but_already_pending_is_ok(self):
        res = self.run_pf({pe.DISABLE_ENV: "0"}, _cands(CHIP), pending=[_active("BTC")])
        g = self.guard(res, "BTC")
        self.assertTrue(g["ok"], g)
        self.assertIn("already pending CONTINUATION", g["detail"])
        self.assertFalse(any("guard:BTC" in w for w in res["warnings"]))
        self.assertTrue(self.guard(res, "CHIP")["ok"])             # Base guard unchanged

    def test_btc_missing_no_pending_still_warns_when_enabled(self):
        res = self.run_pf({pe.DISABLE_ENV: "0"}, _cands(CHIP))
        self.assertTrue(any("guard:BTC: approved but not in the current candidate list" in w
                            for w in res["warnings"]))

    def test_snapshot_keeps_btc_in_list(self):
        res = self.run_pf({pe.DISABLE_ENV: "0"}, _cands(CHIP),
                          snap=dict(_cands(BTC_CHASE, CHIP), frozen_at=NOW.isoformat(), frozen_by="1d_scan"))
        self.assertIn(SNAP, res["candidates_source"])
        self.assertIn("becomes pending CONTINUATION", self.guard(res, "BTC")["detail"])
        self.assertEqual(res["warnings"], [])

    def test_disabled_chase_is_ok_no_entry(self):
        res = self.run_pf({pe.DISABLE_ENV: "1"}, _cands(CHIP),
                          snap=dict(_cands(BTC_CHASE, CHIP), frozen_at=NOW.isoformat()))
        g = self.guard(res, "BTC")
        self.assertTrue(g["ok"])
        self.assertIn("no entry", g["detail"])
        self.assertTrue(res["pending_disabled"])
        self.assertEqual(res["warnings"], [])

    def test_disabled_chase_missing_from_list_after_pending_cleared_is_ok(self):
        # boot after deploy: no snapshot yet, latest dropped BTC, its pending was cancelled -> no WARN
        res = self.run_pf({pe.DISABLE_ENV: "1"}, _cands(CHIP))
        g = self.guard(res, "BTC")
        self.assertTrue(g["ok"], g)
        self.assertIn("no entry", g["detail"])
        self.assertEqual(res["warnings"], [])


if __name__ == "__main__":
    unittest.main()
