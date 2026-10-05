import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import giiq_entrypoint as ge  # noqa: E402

CONFIG_PATH = HERE / "configs" / "giiq_c48_1_paper.json"


def locked_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class TestLockedConfig(unittest.TestCase):
    def test_committed_config_matches_c48_1_locks(self):
        c = locked_config()
        self.assertEqual(ge.check_config(c, "hyperliquid_01"), [])
        self.assertEqual(c["live"]["leverage"], 2)
        self.assertEqual(c["bot"]["long"]["risk"]["total_wallet_exposure_limit"], 0.02)
        self.assertEqual(c["bot"]["long"]["risk"]["n_positions"], 1)
        self.assertIs(c["live"]["filter_by_min_effective_cost"], False)
        self.assertAlmostEqual(ge.worst_case_single_coin_we(c["bot"]["long"]["risk"]), 0.02)
        self.assertIs(c["bot"]["long"]["hsl"]["enabled"], True)
        self.assertEqual(c["bot"]["long"]["hsl"]["red_threshold"], 0.08)
        self.assertEqual(c["bot"]["long"]["hsl"]["restart_after_red_policy"], "always")
        self.assertIs(c["live"]["market_orders_allowed"], False)
        self.assertEqual(c["live"]["user"], "hyperliquid_01")
        self.assertEqual(c["config_version"], "v8.6.0")

    def test_each_lock_violation_is_rejected(self):
        cases = [
            (("live", "leverage"), 4),
            (("live", "leverage"), 10),
            (("live", "market_orders_allowed"), True),
            (("bot", "long", "risk", "total_wallet_exposure_limit"), 0.09),
            (("bot", "short", "risk", "total_wallet_exposure_limit"), 0.5),
            (("bot", "long", "hsl", "enabled"), False),
            (("bot", "long", "hsl", "red_threshold"), 0.15),
            (("live", "hsl_signal_mode"), "unified"),
            (("coin_overrides",), {"BTC": {"bot": {"long": {"risk": {"total_wallet_exposure_limit": 1}}}}}),
        ]
        for path, value in cases:
            c = copy.deepcopy(locked_config())
            node = c
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
            with self.subTest(path=path, value=value):
                self.assertTrue(ge.check_config(c))

    def test_worst_case_single_coin_exposure(self):
        def risk(twel, n, pct, mode):
            return {"total_wallet_exposure_limit": twel, "n_positions": n,
                    "we_excess_allowance_pct": pct, "we_excess_allowance_mode": mode}

        self.assertAlmostEqual(ge.worst_case_single_coin_we(risk(0.02, 1, 0.37, "bounded")), 0.02)
        self.assertAlmostEqual(ge.worst_case_single_coin_we(risk(0.02, 3, 0.37, "bounded")), 0.02 / 3 * 1.37)
        self.assertAlmostEqual(ge.worst_case_single_coin_we(risk(0.08, 1, 0.37, "bounded")), 0.08)
        self.assertAlmostEqual(ge.worst_case_single_coin_we(risk(0.07, 1, 0.37, "legacy_raw")), 0.07 * 1.37)
        self.assertAlmostEqual(ge.worst_case_single_coin_we(risk(0.02, 1, 0.37, None)), 0.02)

        for bad in (risk(0.07, 1, 0.37, "legacy_raw"), risk(0.06, 2, 3.0, "legacy_raw"),
                    risk(0.02, 1, 0.0, "mystery"), risk(0.02, 0, 0.0, "bounded")):
            c = copy.deepcopy(locked_config())
            c["bot"]["long"]["risk"].update(bad)
            with self.subTest(risk=bad):
                errors = ge.check_config(c)
                self.assertTrue(any("single-coin" in e for e in errors), errors)

        c = copy.deepcopy(locked_config())
        c["bot"]["long"]["risk"].update(risk(0.08, 1, 0.37, "bounded"))
        self.assertEqual(ge.check_config(c), [])

    def test_user_must_match(self):
        self.assertTrue(ge.check_config(locked_config(), "bitunix_01"))


class TestApiKeys(unittest.TestCase):
    def test_example_file_is_placeholder_non_vault(self):
        keys = json.loads((HERE / "api-keys.json.example").read_text(encoding="utf-8"))
        self.assertEqual(list(keys), ["hyperliquid_01"])
        self.assertEqual(ge.check_api_keys(keys, "hyperliquid_01"), [])
        self.assertIn("YOUR", keys["hyperliquid_01"]["private_key"])

    def test_vault_and_other_exchanges_rejected(self):
        for entry in (
            {"exchange": "hyperliquid", "is_vault": True},
            {"exchange": "hyperliquid", "is_vault": "true"},
            {"exchange": "bybit", "is_vault": False},
        ):
            with self.subTest(entry=entry):
                self.assertTrue(ge.check_api_keys({"hyperliquid_01": entry}, "hyperliquid_01"))
        self.assertTrue(ge.check_api_keys({}, "hyperliquid_01"))


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.keys_out = os.path.join(self.tmp, "run", "keys.json")
        self._orig = ge.RENDERED_KEYS_PATH
        ge.render_hl_keys.__defaults__ = (self.keys_out,)

    def tearDown(self):
        ge.render_hl_keys.__defaults__ = (self._orig,)

    def base_env(self, **extra):
        env = {"PB_USER": "hyperliquid_01", "PB_CONFIG_PATH": str(CONFIG_PATH)}
        env.update(extra)
        return env

    def test_renders_hl_keys_from_env_without_vault(self):
        env = ge.prepare(self.base_env(HL_WALLET_ADDRESS="0xabc", HL_PRIVATE_KEY="0xdef"), [])
        self.assertEqual(env["PB_API_KEYS_PATH"], self.keys_out)
        self.assertNotIn("HL_PRIVATE_KEY", env)
        rendered = json.loads(Path(self.keys_out).read_text(encoding="utf-8"))
        self.assertEqual(
            rendered,
            {"hyperliquid_01": {"exchange": "hyperliquid", "wallet_address": "0xabc",
                                "private_key": "0xdef", "is_vault": False}},
        )
        self.assertEqual(os.stat(self.keys_out).st_mode & 0o777, 0o600)

    def test_missing_credentials_rejected(self):
        with self.assertRaises(ge.LockViolation):
            ge.prepare(self.base_env(), [])

    def test_mounted_vault_keys_rejected(self):
        p = os.path.join(self.tmp, "api-keys.json")
        Path(p).write_text(json.dumps({"hyperliquid_01": {"exchange": "hyperliquid", "is_vault": True}}))
        with self.assertRaises(ge.LockViolation):
            ge.prepare(self.base_env(PB_API_KEYS_PATH=p), [])

    def test_bypasses_rejected(self):
        creds = dict(HL_WALLET_ADDRESS="0xabc", HL_PRIVATE_KEY="0xdef")
        with self.assertRaises(ge.LockViolation):
            ge.prepare(self.base_env(**creds), ["--live.leverage", "10"])
        for name in ge.FORBIDDEN_ENV:
            with self.subTest(name=name), self.assertRaises(ge.LockViolation):
                ge.prepare(self.base_env(**creds, **{name: "x"}), [])


if __name__ == "__main__":
    unittest.main()
