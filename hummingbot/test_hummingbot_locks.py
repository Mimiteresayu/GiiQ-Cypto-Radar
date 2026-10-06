import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    # Unique module names: passivbot/ also ships a giiq_entrypoint.py.
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


al = _load("hb_assert_locks", "assert_locks.py")
ge = _load("hb_giiq_entrypoint", "giiq_entrypoint.py")

GOOD_ENV = {
    "CONFIG_PASSWORD": "pw",
    "HL_TESTNET_ADDRESS": "0x" + "1" * 40,
    "HL_TESTNET_API_WALLET_KEY": "0x" + "2" * 64,
    "GIIQ_NAV_USD": "2500",
    "GIIQ_MID_PRICE": "100000",
}


def locked():
    return al.load(al.DEFAULT_CONFIG)


def no_fetch():
    raise AssertionError("mid should come from GIIQ_MID_PRICE")


class TestLockedConfig(unittest.TestCase):
    def test_committed_config_matches_c48_2_locks(self):
        c = locked()
        self.assertEqual(al.check_config(c), [])
        self.assertEqual(c["strategy"], "perpetual_market_making")
        self.assertEqual(c["derivative"], "hyperliquid_perpetual_testnet")
        self.assertEqual(c["market"], "BTC-USD")
        self.assertEqual(c["leverage"], 2)
        self.assertEqual(c["order_levels"], 1)
        self.assertEqual(c["order_level_amount"], 0)
        self.assertEqual(c["stop_loss_spread"], 1.0)
        self.assertEqual(c["long_profit_taking_spread"], 0.5)
        self.assertEqual(c["short_profit_taking_spread"], 0.5)
        self.assertEqual(c["position_mode"], "One-way")
        self.assertEqual(c["template_version"], 6)
        self.assertIsNone(c["order_override"])

    def test_cli_assert_passes_on_committed_config(self):
        self.assertEqual(al.main([str(al.DEFAULT_CONFIG), "--nav", "2500", "--mid", "100000"]), 0)

    def test_each_lock_violation_is_rejected(self):
        cases = [
            ("strategy", "v2_funding_rate_arb"),
            ("strategy", "funding_rate_arb"),
            ("strategy", "pure_market_making"),
            ("leverage", 3),
            ("leverage", 20),
            ("leverage", 1),
            ("order_levels", 2),
            ("order_level_amount", 0.001),
            ("stop_loss_spread", 0),
            ("stop_loss_spread", -1),
            ("stop_loss_spread", None),
            ("long_profit_taking_spread", 0),
            ("short_profit_taking_spread", 0),
            ("derivative", "bitunix_perpetual"),
            ("derivative", "binance_perpetual"),
            ("derivative", "hyperliquid_perpetual"),
            ("market", "ETH-USD"),
            ("position_mode", "Hedge"),
            ("order_amount", 0),
            ("price_source", "external_market"),
            ("order_override", {"x": ["buy", 0.1, 1]}),
        ]
        for key, value in cases:
            c = locked()
            c[key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(al.check_config(c))
        for key in ("strategy", "leverage", "order_levels", "order_level_amount", "stop_loss_spread", "derivative"):
            c = locked()
            del c[key]
            with self.subTest(missing=key):
                self.assertTrue(al.check_config(c))

    def test_notional_sizing(self):
        self.assertAlmostEqual(al.target_order_amount(2500, 100000), 0.0005)
        self.assertEqual(al.check_notional(0.0005, 2500, 100000), [])  # 2% NAV
        self.assertEqual(al.check_notional(0.002, 2500, 100000), [])   # exactly 8%
        self.assertTrue(al.check_notional(0.0021, 2500, 100000))       # > 8%
        self.assertTrue(al.check_notional(0.0005, 0, 100000))


class TestGuard(unittest.TestCase):
    def test_good_env_starts_headless_locked_strategy_and_strips_key(self):
        config, env = ge.prepare(dict(GOOD_ENV), [], fetch_mid=no_fetch)
        self.assertEqual(al.check_config(config), [])
        self.assertEqual(env["HEADLESS_MODE"], "true")
        self.assertEqual(env["CONFIG_FILE_NAME"], ge.STRATEGY_FILE_NAME)
        self.assertNotIn("HL_TESTNET_API_WALLET_KEY", env)
        self.assertEqual(env["CONFIG_PASSWORD"], "pw")

    def test_mid_fetched_when_not_given(self):
        env = dict(GOOD_ENV)
        del env["GIIQ_MID_PRICE"]
        ge.prepare(env, [], fetch_mid=lambda: 100000.0)
        with self.assertRaises(ge.LockViolation):
            ge.prepare(env, [], fetch_mid=lambda: 500000.0)  # 0.0005 * 500k = $250 = 10% NAV

    def test_order_amount_override_is_capped(self):
        config, _ = ge.prepare({**GOOD_ENV, "GIIQ_ORDER_AMOUNT": "0.001"}, [], fetch_mid=no_fetch)
        self.assertEqual(config["order_amount"], 0.001)
        for bad in ("0.01", "0", "-0.001"):
            with self.subTest(order_amount=bad), self.assertRaises(ge.LockViolation):
                ge.prepare({**GOOD_ENV, "GIIQ_ORDER_AMOUNT": bad}, [], fetch_mid=no_fetch)

    def test_refuses_bypasses(self):
        bad_envs = [
            {"SCRIPT_CONFIG": "v2_funding_rate_arb.yml"},
            {"CONFIG_FILE_NAME": "conf_other.yml"},
            {"HL_USE_VAULT": "yes"},
            {"GIIQ_NAV_USD": ""},
            {"CONFIG_PASSWORD": ""},
            {"HL_TESTNET_ADDRESS": ""},
            {"HL_TESTNET_API_WALLET_KEY": ""},
        ]
        for extra in bad_envs:
            with self.subTest(env=extra), self.assertRaises(ge.LockViolation):
                ge.prepare({**GOOD_ENV, **extra}, [], fetch_mid=no_fetch)
        with self.assertRaises(ge.LockViolation):
            ge.prepare(dict(GOOD_ENV), ["--v2", "v2_funding_rate_arb.yml"], fetch_mid=no_fetch)

    def test_refuses_tampered_baked_config(self):
        bad = copy.deepcopy(locked())
        bad["leverage"] = 20
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "conf.yml"
            p.write_text(yaml.safe_dump(bad))
            orig = ge.BAKED_CONFIG
            ge.BAKED_CONFIG = p
            try:
                with self.assertRaises(ge.LockViolation):
                    ge.prepare(dict(GOOD_ENV), [], fetch_mid=no_fetch)
            finally:
                ge.BAKED_CONFIG = orig

    def test_written_strategy_and_kill_switch(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d)
            (conf / "conf_client.yml").write_text("instance_id: abc\nkill_switch_mode: {}\n")
            config, _ = ge.prepare(dict(GOOD_ENV), [], fetch_mid=no_fetch)
            path = ge.write_strategy(config, conf)
            self.assertEqual(al.check_config(al.load(path)), [])
            client = yaml.safe_load(ge.write_kill_switch(conf).read_text())
            self.assertEqual(client["kill_switch_mode"], {"kill_switch_rate": -4.0})
            self.assertEqual(client["instance_id"], "abc")


if __name__ == "__main__":
    unittest.main()
