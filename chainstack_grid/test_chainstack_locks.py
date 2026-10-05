"""C48-3 lock / patch / entrypoint tests. No Docker or network needed.

Tests marked "upstream" need GIIQ_UPSTREAM_DIR = a patched checkout of the pinned SHA with
its venv (the Docker build runs them); they skip otherwise.
"""
import copy
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, path):
    # Unique module names: hummingbot/ also ships assert_locks.py / giiq_entrypoint.py.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


al = _load("cg_assert_locks", HERE / "assert_locks.py")
ge = _load("cg_giiq_entrypoint", HERE / "giiq_entrypoint.py")
ap = _load("cg_apply_patches", HERE / "apply_patches.py")

UPSTREAM_SHA = "7e930aa6b0d296f029943834c2133beff6174d80"
MASTER = "0x" + "a" * 40
SIGNER = "0x" + "b" * 40
KEY = "0x" + "1" * 64
GOOD_ENV = {
    "HYPERLIQUID_TESTNET": "true",
    "HYPERLIQUID_TESTNET_PRIVATE_KEY": KEY,
    "TESTNET_WALLET_ADDRESS": MASTER,
}
UPSTREAM = Path(os.environ.get("GIIQ_UPSTREAM_DIR", "/nonexistent"))
HAVE_UPSTREAM = (UPSTREAM / "src" / "run_bot.py").exists()
UPSTREAM_PY = UPSTREAM / ".venv" / "bin" / "python"


def locked():
    return al.load(al.DEFAULT_CONFIG)


def state(equity=1500.0, positions=()):
    return {"marginSummary": {"accountValue": str(equity)},
            "assetPositions": [{"position": p} for p in positions]}


def btc_pos(lev=2, mode="isolated", szi="0.0002", value="17.0", coin="BTC"):
    return {"coin": coin, "szi": szi, "positionValue": value,
            "leverage": {"type": mode, "value": lev}}


class FakeClient:
    def __init__(self, key=KEY, master=MASTER, signer=SIGNER, approved=True,
                 setting=(2, "isolated"), equity=1500.0, positions=(), update_status="ok"):
        self.signer_address = signer
        self.master = master
        self.calls = []
        self.approved = approved
        self.setting = setting
        self.equity = equity
        self.positions = list(positions)
        self.update_status = update_status
        self.orders = []
        self.cancelled = 0
        self.fail_reads = False

    def update_leverage(self, leverage, coin, is_cross):
        self.calls.append(("update_leverage", leverage, coin, is_cross))
        return {"status": self.update_status}

    def user_state(self):
        self.calls.append(("user_state",))
        if self.fail_reads:
            raise ConnectionError("boom")
        return state(self.equity, self.positions)

    def active_asset_data(self, coin):
        self.calls.append(("active_asset_data", coin))
        return {"coin": coin, "leverage": {"type": self.setting[1], "value": self.setting[0]}}

    def extra_agents(self):
        return [{"address": self.signer_address}] if self.approved else []

    def open_orders(self):
        return list(self.orders)

    def cancel_all(self, coin):
        self.cancelled += 1
        n = len([o for o in self.orders if o.get("coin") == coin])
        self.orders = []
        return n


class TestLockedYaml(unittest.TestCase):
    def test_committed_yaml_passes_all_locks(self):
        self.assertEqual(al.check_config(locked()), [])
        self.assertEqual(al.main([str(al.DEFAULT_CONFIG)]), 0)

    def test_lock_values(self):
        c = locked()
        rm = c["risk_management"]
        self.assertIs(c["exchange"]["testnet"], True)
        self.assertLessEqual(c["account"]["max_allocation_pct"], 2)
        self.assertIs(rm["stop_loss_enabled"], True)
        self.assertTrue(4 <= rm["stop_loss_pct"] <= 8)
        self.assertLessEqual(rm["max_drawdown_pct"], 4)
        self.assertLessEqual(rm["max_position_size_pct"], 8)
        self.assertEqual(rm["tpsl_mode"], "polling")
        self.assertEqual(c["grid"]["symbol"], "BTC")
        self.assertEqual(c["grid"]["levels"], 10)
        self.assertEqual(c["grid"]["price_range"]["auto"]["range_pct"], 5.0)

    def test_each_violation_is_rejected(self):
        cases = [
            (("exchange", "testnet"), False),
            (("exchange", "testnet"), "true"),
            (("exchange", "testnet"), None),
            (("exchange", "type"), "bitunix"),
            (("exchange", "dex"), "felix"),
            (("exchange", "account_address"), MASTER),
            (("account", "max_allocation_pct"), 2.01),
            (("account", "max_allocation_pct"), 10),
            (("account", "max_allocation_pct"), True),
            (("risk_management", "stop_loss_enabled"), False),
            (("risk_management", "stop_loss_pct"), 3.9),
            (("risk_management", "stop_loss_pct"), 8.1),
            (("risk_management", "stop_loss_pct"), None),
            (("risk_management", "max_drawdown_pct"), 4.01),
            (("risk_management", "max_drawdown_pct"), 15),
            (("risk_management", "max_position_size_pct"), 8.01),
            (("risk_management", "max_position_size_pct"), 40),
            (("risk_management", "tpsl_mode"), "grouped"),
            (("grid", "symbol"), "ETH"),
            (("active",), False),
            (("private_key",), KEY),
            (("testnet_private_key",), KEY),
            (("mainnet_private_key",), KEY),
            (("mainnet_key_file",), "/k"),
        ]
        for path, value in cases:
            c = copy.deepcopy(locked())
            node = c
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
            with self.subTest(path=path, value=value):
                self.assertNotEqual(al.check_config(c), [])

    def test_missing_sections_rejected(self):
        for section in ("exchange", "account", "grid", "risk_management"):
            c = copy.deepcopy(locked())
            del c[section]
            with self.subTest(section=section):
                self.assertNotEqual(al.check_config(c), [])


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.text = (HERE / "Dockerfile").read_text(encoding="utf-8")

    def test_no_volume_instruction(self):
        self.assertEqual(len(re.findall(r"^\s*VOLUME", self.text, re.M | re.I)), 0)

    def test_upstream_pinned_to_sha_matching_readme(self):
        self.assertIn(f"ARG UPSTREAM_SHA={UPSTREAM_SHA}", self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$UPSTREAM_SHA"', self.text)
        self.assertIn(UPSTREAM_SHA, (HERE / "README.md").read_text(encoding="utf-8"))
        self.assertIn(UPSTREAM_SHA, (HERE / "bots" / "c48_3_btc_grid_locked.yaml").read_text(encoding="utf-8"))

    def test_base_images_pinned_by_digest(self):
        froms = re.findall(r"^FROM\s+(\S+)", self.text, re.M)
        self.assertGreaterEqual(len(froms), 3)
        for image in froms:
            with self.subTest(image=image):
                self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
        self.assertTrue(any(f.startswith("python:3.13-slim@") for f in froms))

    def test_build_runs_patches_locks_validate_and_tests(self):
        for needle in ("uv sync --frozen", "apply_patches.py /app/upstream", "assert_locks.py",
                       "src/run_bot.py --validate /app/giiq/bots/c48_3_btc_grid_locked.yaml",
                       "unittest -v test_chainstack_locks"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)
        self.assertIn('ENTRYPOINT ["/app/upstream/.venv/bin/python", "/app/giiq/giiq_entrypoint.py"]', self.text)

    def test_railway_never_auto_restarts(self):
        cfg = json.loads((HERE / "railway.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["build"]["builder"], "DOCKERFILE")
        self.assertEqual(cfg["deploy"]["restartPolicyType"], "NEVER")


class TestTestnetOnly(unittest.TestCase):
    def test_good_env_passes(self):
        ge.check_env(dict(GOOD_ENV), [])

    def test_refuses_mainnet_or_ambiguous_env(self):
        bad = [
            {"HYPERLIQUID_TESTNET": "false"},
            {"HYPERLIQUID_TESTNET": ""},
            {"HYPERLIQUID_TESTNET": "1"},
            {"HYPERLIQUID_MAINNET_PRIVATE_KEY": KEY},
            {"HYPERLIQUID_MAINNET_KEY_FILE": "/k"},
            {"MAINNET_WALLET_ADDRESS": MASTER},
            {"HYPERLIQUID_PRIVATE_KEY": KEY},
            {"HYPERLIQUID_PRIVATE_KEY_FILE": "/k"},
            {"HYPERLIQUID_TESTNET_KEY_FILE": "/k"},
            {"HYPERLIQUID_PUBLIC_INFO_URL": "https://api.hyperliquid.xyz/info"},
            {"HYPERLIQUID_CHAINSTACK_INFO_URL": "https://x"},
            {"HYPERLIQUID_TESTNET_PUBLIC_INFO_URL": "https://api.hyperliquid-testnet.xyz/info"},
            {"HYPERLIQUID_TESTNET_CHAINSTACK_INFO_URL": "https://x"},
            {"SOME_URL": "https://api.hyperliquid.xyz"},
            {"HYPERLIQUID_TESTNET_PRIVATE_KEY": ""},
            {"HYPERLIQUID_TESTNET_PRIVATE_KEY": "nothex"},
            {"TESTNET_WALLET_ADDRESS": ""},
            {"TESTNET_WALLET_ADDRESS": "0x123"},
        ]
        for override in bad:
            with self.subTest(override=override):
                with self.assertRaises(ge.LockViolation):
                    ge.check_env({**GOOD_ENV, **override}, [])

    def test_refuses_extra_cli_args(self):
        with self.assertRaises(ge.LockViolation):
            ge.check_env(dict(GOOD_ENV), ["bots/btc_conservative.yaml"])

    def test_sdk_client_hardwired_to_testnet(self):
        src = (HERE / "giiq_entrypoint.py").read_text(encoding="utf-8")
        self.assertEqual(ge.TESTNET_API_URL, "https://api.hyperliquid-testnet.xyz")
        self.assertIn("Info(TESTNET_API_URL", src)
        self.assertIn("Exchange(wallet, TESTNET_API_URL", src)
        self.assertNotIn("MAINNET_API_URL", src)
        self.assertNotIn("api.hyperliquid.xyz", src)

    def test_patched_run_bot_refuses_non_testnet(self):
        b3 = next(new for tag, _, _, new in ap.PATCHES if tag == "B3")
        self.assertIn("if self.config.exchange.testnet is not True:", b3)

    def test_entrypoint_runs_only_locked_yaml(self):
        self.assertEqual(ge.LOCKED_YAML, al.DEFAULT_CONFIG)


class TestLeverage(unittest.TestCase):
    def test_sets_2x_isolated_then_reads_back(self):
        c = FakeClient()
        self.assertEqual(ge.enforce_leverage(c), 2)
        self.assertEqual(c.calls[0], ("update_leverage", 2, "BTC", False))
        self.assertIn(("active_asset_data", "BTC"), c.calls)

    def test_aborts_when_setting_above_3(self):
        c = FakeClient(setting=(20, "cross"), update_status="err")
        with self.assertRaises(ge.LockViolation):
            ge.enforce_leverage(c)

    def test_aborts_when_open_position_leverage_above_3(self):
        c = FakeClient(setting=(2, "isolated"), positions=[btc_pos(lev=5, mode="cross")])
        with self.assertRaises(ge.LockViolation):
            ge.enforce_leverage(c)

    def test_exactly_3_allowed(self):
        self.assertEqual(ge.enforce_leverage(FakeClient(setting=(3, "isolated"))), 3)

    def test_unreadable_leverage_aborts(self):
        c = FakeClient()
        c.active_asset_data = lambda coin: {}
        with self.assertRaises(ge.LockViolation):
            ge.enforce_leverage(c)


class TestAllocation(unittest.TestCase):
    def test_real_equity_sizes_two_percent(self):
        alloc, levels = ge.size_allocation(1451.04, locked())
        self.assertAlmostEqual(alloc, 29.0208, places=3)
        self.assertEqual(levels, 2)

    def test_large_equity_keeps_all_levels(self):
        self.assertEqual(ge.size_allocation(10000, locked())[1], 10)

    def test_too_small_equity_aborts(self):
        for equity in (0, -5, 1000, 1049):
            with self.subTest(equity=equity):
                with self.assertRaises(ge.LockViolation):
                    ge.size_allocation(equity, locked())

    def test_allocation_over_2pct_aborts(self):
        c = locked()
        c["account"]["max_allocation_pct"] = 3
        with self.assertRaises(ge.LockViolation):
            ge.size_allocation(10000, c)


class TestStartup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.order = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_startup(self, env=None, **client_kw):
        def factory(key, master):
            self.order.append("client")
            self.client = FakeClient(key, master, **client_kw)
            return self.client
        return ge.startup(env or dict(GOOD_ENV), [], client_factory=factory,
                          validate=lambda: self.order.append("validate"), data_dir=self.data)

    def test_happy_path_order_and_child_env(self):
        _, cfg, child_env, equity, levels = self.run_startup({**GOOD_ENV, "GIIQ_NAV_USD": "999999"})
        self.assertEqual(self.order, ["validate", "client"])
        self.assertEqual(self.client.calls[0], ("update_leverage", 2, "BTC", False))
        self.assertEqual(child_env["GIIQ_NAV_USD"], "1500.000000")
        self.assertEqual(child_env["HYPERLIQUID_TESTNET"], "true")
        self.assertEqual((equity, levels), (1500.0, 2))

    def test_env_failure_stops_before_validate(self):
        with self.assertRaises(ge.LockViolation):
            self.run_startup({**GOOD_ENV, "HYPERLIQUID_TESTNET": "false"})
        self.assertEqual(self.order, [])

    def test_halt_marker_blocks_start(self):
        (self.data / ge.HALT_MARKER_NAME).write_text("x")
        with self.assertRaises(ge.LockViolation):
            self.run_startup()
        self.assertEqual(self.order, [])

    def test_master_key_refused(self):
        with self.assertRaises(ge.LockViolation):
            self.run_startup(signer=MASTER)

    def test_unapproved_api_wallet_refused(self):
        with self.assertRaises(ge.LockViolation):
            self.run_startup(approved=False)

    def test_high_leverage_refused_before_launch(self):
        with self.assertRaises(ge.LockViolation):
            self.run_startup(setting=(20, "cross"))

    def test_low_equity_refused(self):
        with self.assertRaises(ge.LockViolation):
            self.run_startup(equity=500.0)


class FakeProc:
    def __init__(self, exit_after=None):
        self.returncode = None
        self.exit_after = exit_after
        self.polls = 0
        self.signals = []

    def poll(self):
        self.polls += 1
        if self.exit_after is not None and self.polls > self.exit_after:
            self.returncode = 0
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.cfg = locked()

    def tearDown(self):
        self.tmp.cleanup()

    def risk(self, st, lev=2, peak=1500.0, orders=()):
        return ge.check_risk(st, lev, peak, list(orders), 20, self.cfg)

    def test_healthy_state_no_breach(self):
        self.assertEqual(self.risk(state(1500, [btc_pos(value="20")])), [])

    def test_breaches(self):
        cases = {
            "lev": self.risk(state(1500), lev=4),
            "dd": self.risk(state(1440), peak=1500),
            "notional": self.risk(state(1500, [btc_pos(value="121")])),
            "coin": self.risk(state(1500, [btc_pos(coin="ETH")])),
            "orders": self.risk(state(1500), orders=[{"coin": "BTC"}] * 21),
            "other_orders": self.risk(state(1500), orders=[{"coin": "ETH"}]),
        }
        for name, breaches in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(breaches, [])

    def test_dd_just_under_limit_ok(self):
        self.assertEqual(self.risk(state(1440.1), peak=1500), [])

    def test_breach_stops_bot_cancels_and_halts(self):
        c = FakeClient(setting=(5, "cross"))
        c.orders = [{"coin": "BTC", "oid": 1}]
        proc = FakeProc()
        rc = ge.watchdog(proc, c, self.cfg, 1500.0, 2, 0, data_dir=self.data)
        self.assertEqual(rc, 4)
        self.assertEqual(proc.signals, [signal.SIGTERM])
        self.assertEqual(c.cancelled, 1)
        self.assertTrue((self.data / ge.HALT_MARKER_NAME).exists())

    def test_reconciliation_failure_stops(self):
        c = FakeClient()
        c.fail_reads = True
        rc = ge.watchdog(FakeProc(), c, self.cfg, 1500.0, 2, 0, data_dir=self.data)
        self.assertEqual(rc, 4)
        self.assertEqual(c.cancelled, 1)

    def test_bot_exit_cancels_and_returns_nonzero(self):
        c = FakeClient()
        rc = ge.watchdog(FakeProc(exit_after=2), c, self.cfg, 1500.0, 2, 0, data_dir=self.data)
        self.assertEqual(rc, 3)
        self.assertEqual(c.cancelled, 1)

    def test_signal_stop_does_not_halt(self):
        ge.stop_bot(FakeProc(), FakeClient(), self.data, "signal 15", halt=False)
        self.assertFalse((self.data / ge.HALT_MARKER_NAME).exists())


class TestSecrets(unittest.TestCase):
    def test_log_scrubs_private_key(self):
        ge._secrets.append(KEY)
        try:
            from io import StringIO
            from contextlib import redirect_stdout
            buf = StringIO()
            with redirect_stdout(buf):
                ge.log(f"error with {KEY}")
            self.assertNotIn(KEY, buf.getvalue())
        finally:
            ge._secrets.remove(KEY)

    def test_redact_address(self):
        self.assertEqual(ge.redact(MASTER), "0xaaaa...aaaa")


@unittest.skipUnless(HAVE_UPSTREAM, "needs GIIQ_UPSTREAM_DIR (patched upstream checkout)")
class TestUpstreamPatched(unittest.TestCase):
    """B1/B2/B3 against the real (patched) upstream code."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(UPSTREAM / "src"))
        cls.enhanced = importlib.import_module("core.enhanced_config")
        cls.risk = importlib.import_module("core.risk_manager")
        handlers = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        cls.run_bot = _load("cg_upstream_run_bot", UPSTREAM / "src" / "run_bot.py")
        cls.handlers = handlers

    def convert(self, path=al.DEFAULT_CONFIG, nav="1451.04"):
        bot = self.run_bot.GridTradingBot(str(path))
        signal.signal(signal.SIGINT, self.handlers[0])
        signal.signal(signal.SIGTERM, self.handlers[1])
        bot.config = self.enhanced.EnhancedBotConfig.from_yaml(Path(path))
        old = os.environ.get("GIIQ_NAV_USD")
        try:
            if nav is None:
                os.environ.pop("GIIQ_NAV_USD", None)
            else:
                os.environ["GIIQ_NAV_USD"] = nav
            return bot._convert_config()
        finally:
            if old is None:
                os.environ.pop("GIIQ_NAV_USD", None)
            else:
                os.environ["GIIQ_NAV_USD"] = old

    def write_yaml(self, mutate):
        import yaml
        c = locked()
        mutate(c)
        fd, name = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(c, f)
        self.addCleanup(os.unlink, name)
        return Path(name)

    def test_b1_locked_yaml_loads_and_validates(self):
        cfg = self.enhanced.EnhancedBotConfig.from_yaml(al.DEFAULT_CONFIG)
        self.assertEqual(cfg.risk_management.max_drawdown_pct, 4.0)
        self.assertEqual(cfg.risk_management.max_position_size_pct, 8.0)

    def test_b1_cli_validate_exits_zero(self):
        py = str(UPSTREAM_PY if UPSTREAM_PY.exists() else sys.executable)
        proc = subprocess.run([py, "src/run_bot.py", "--validate", str(al.DEFAULT_CONFIG)],
                              cwd=UPSTREAM, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_b1_validator_still_rejects_out_of_range(self):
        for key, value in (("max_drawdown_pct", 0.5), ("max_drawdown_pct", 60),
                           ("max_position_size_pct", 0.5)):
            path = self.write_yaml(lambda c: c["risk_management"].__setitem__(key, value))
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.enhanced.EnhancedBotConfig.from_yaml(path)

    def test_b2_engine_config_carries_risk_management(self):
        rm_yaml = locked()["risk_management"]
        engine = self.convert()
        rm = engine["risk_management"]
        for key in ("stop_loss_enabled", "stop_loss_pct", "take_profit_enabled", "take_profit_pct",
                    "tpsl_mode", "max_drawdown_pct", "max_position_size_pct"):
            with self.subTest(key=key):
                self.assertEqual(rm[key], rm_yaml[key])
        self.assertIs(rm["stop_loss_enabled"], True)
        self.assertLessEqual(rm["stop_loss_pct"], 8)
        self.assertLessEqual(rm["max_drawdown_pct"], 4)
        self.assertLessEqual(rm["max_position_size_pct"], 8)

    def test_b2_risk_manager_builds_locked_rules(self):
        rules = {r.name: r for r in self.risk.RiskManager(self.convert()).rules}
        self.assertEqual(rules["stop_loss"].loss_pct, 6.0)
        self.assertEqual(rules["max_drawdown"].max_drawdown_pct, 4.0)
        self.assertEqual(rules["max_position_size"].max_position_size_pct, 8.0)

    def test_b3_allocation_from_real_nav(self):
        engine = self.convert(nav="1451.04")
        self.assertAlmostEqual(engine["strategy"]["total_allocation"], 1451.04 * 0.02, places=6)
        self.assertIs(engine["exchange"]["testnet"], True)

    def test_b3_missing_nav_raises(self):
        with self.assertRaises(ValueError):
            self.convert(nav=None)

    def test_patched_run_bot_refuses_testnet_false(self):
        path = self.write_yaml(lambda c: c["exchange"].__setitem__("testnet", False))
        with self.assertRaises(ValueError):
            self.convert(path=path)

    def test_patches_already_applied(self):
        for tag, rel, old, new in ap.PATCHES:
            text = (UPSTREAM / rel).read_text(encoding="utf-8")
            with self.subTest(tag=tag):
                self.assertEqual(text.count(new), 1)
        self.assertNotIn("base_allocation_usd = 1000.0", (UPSTREAM / "src/run_bot.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
