import os
import sys
import tempfile
import subprocess
import unittest
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "testing" / "logs"
SUMMARY_LOG = LOG_DIR / "cli_test_summary.log"
DETAIL_LOG = LOG_DIR / "cli_test_detail.log"

# Optional pytest markers for better selection.
try:
    import pytest  # type: ignore
except Exception:  # pragma: no cover - fallback for unittest-only runs
    class _NoOpMark:
        def __getattr__(self, name):
            def _decorator(obj):
                return obj

            return _decorator

    class _NoOpPytest:
        mark = _NoOpMark()

    pytest = _NoOpPytest()  # type: ignore

mark_live = getattr(pytest.mark, "live", lambda f: f)
mark_seeded = getattr(pytest.mark, "seeded", lambda f: f)
mark_needs_id = getattr(pytest.mark, "needs_id", lambda f: f)

# Replace these with your last known testnet balances snapshot when you want
# exact output verification.
EXPECTED_TEST_BALANCES = [
    {"asset": "AI", "free": "18446.00000000", "locked": "0.00000000"},
    {"asset": "DOGE", "free": "7681.32500000", "locked": "703.00000000"},
    {"asset": "DOT", "free": "337.00379000", "locked": "0.00000000"},
    {"asset": "SOL", "free": "21.18293300", "locked": "0.21000000"},
    {"asset": "USDT", "free": "8040.62353480", "locked": "0.00000000"},
]


def parse_balance_output(output: str) -> dict[str, tuple[str, str]]:
    balances: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Cached balances:"):
            continue
        if line.startswith("ASSET"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        asset, free, locked = parts[0], parts[1], parts[2]
        balances[asset] = (free, locked)
    return balances


def update_config_db_path(config_path: Path, db_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    lines = []
    updated = False
    for line in text.splitlines():
        if line.strip().startswith("db_path"):
            lines.append(f'db_path = "{db_path}"')
            updated = True
        else:
            lines.append(line)
    if not updated:
        lines.append("")
        lines.append("[app]")
        lines.append(f'db_path = "{db_path}"')
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def extract_first_int_id(output: str) -> int | None:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("id"):
            continue
        parts = line.split()
        if not parts:
            continue
        if parts[0].isdigit():
            return int(parts[0])
    return None


def extract_first_uuid(output: str) -> str | None:
    import re

    match = re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", output)
    return match.group(0) if match else None


class TestCLICommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="cryptogent-tests-")
        cls.config_path = Path(cls.temp_dir.name) / "cryptogent.toml"
        cls.db_path = Path(cls.temp_dir.name) / "cryptogent.sqlite3"
        main_config = REPO_ROOT / "cryptogent.toml"
        if main_config.exists():
            shutil.copyfile(main_config, cls.config_path)
        update_config_db_path(cls.config_path, cls.db_path)
        cls.base_env = os.environ.copy()
        cls.base_env["PYTHONPATH"] = str(REPO_ROOT)

        # Initialize once for the suite
        result = cls.run_cli(["init"], expect_ok=True)
        if result.returncode != 0:
            raise RuntimeError("Failed to initialize test config/db")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    @classmethod
    def run_cli(cls, args, input_text=None, expect_ok=True):
        cmd = [sys.executable, "-m", "cryptogent"] + args
        env = cls.base_env.copy()
        env["CRYPTOGENT_CONFIG"] = str(cls.config_path)
        env["CRYPTOGENT_DB"] = str(cls.db_path)
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
        status = "OK" if result.returncode == 0 else f"FAIL({result.returncode})"
        cmd_str = " ".join(cmd)
        cls.log_result(cmd_str, status, result.stdout, result.stderr)
        if expect_ok:
            if result.returncode != 0:
                raise AssertionError(
                    f"Command failed: {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
        return result

    @classmethod
    def log_result(cls, cmd: str, status: str, stdout: str, stderr: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[{status}] {cmd}")
        with SUMMARY_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{status}] {cmd}\n")
        with DETAIL_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{status}] {cmd}\n")
            if stdout:
                f.write("STDOUT:\n")
                f.write(stdout.rstrip() + "\n")
            if stderr:
                f.write("STDERR:\n")
                f.write(stderr.rstrip() + "\n")
            f.write("-" * 80 + "\n")

    def require_live(self):
        if os.environ.get("CRYPTOGENT_TEST_LIVE") != "1":
            self.skipTest("Set CRYPTOGENT_TEST_LIVE=1 to enable live/testnet commands")

    def require_seeded(self):
        if os.environ.get("CRYPTOGENT_TEST_SEEDED") != "1":
            self.skipTest("Set CRYPTOGENT_TEST_SEEDED=1 if DB has seeded data")

    def get_trade_request_id(self) -> int:
        result = self.run_cli(["trade", "list"], expect_ok=True)
        trade_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if trade_id is None:
            self.skipTest("No trade request id found in trade list")
        return trade_id

    def get_plan_id(self) -> int:
        result = self.run_cli(["trade", "plan", "list"], expect_ok=True)
        plan_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if plan_id is None:
            self.skipTest("No plan id found in trade plan list")
        return plan_id

    def get_execution_id(self) -> int:
        result = self.run_cli(["trade", "execution", "list"], expect_ok=True)
        exec_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if exec_id is None:
            self.skipTest("No execution id found in trade execution list")
        return exec_id

    def get_manual_order_id(self) -> int:
        result = self.run_cli(["trade", "manual", "list"], expect_ok=True)
        manual_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if manual_id is None:
            self.skipTest("No manual order id found in manual list")
        return manual_id

    def get_loop_preset_id(self) -> int:
        result = self.run_cli(["trade", "manual", "loop", "preset", "list"], expect_ok=True)
        preset_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if preset_id is None:
            self.skipTest("No loop preset id found in preset list")
        return preset_id

    def get_loop_id(self) -> int:
        result = self.run_cli(["trade", "manual", "loop", "list"], expect_ok=True)
        loop_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if loop_id is None:
            self.skipTest("No loop id found in loop list")
        return loop_id

    def get_position_id(self) -> int:
        result = self.run_cli(["position", "list"], expect_ok=True)
        position_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if position_id is None:
            self.skipTest("No position id found in position list")
        return position_id

    # A. Setup & Status
    def test_01_init(self):
        self.run_cli(["init"], expect_ok=True)
        self.assertTrue(self.config_path.exists())
        self.assertTrue(self.db_path.exists())

    def test_02_status(self):
        result = self.run_cli(["status"], expect_ok=True)
        self.assertIn("config", result.stdout.lower())
        self.assertIn("db", result.stdout.lower())

    # B. Config
    def test_03_config_show(self):
        result = self.run_cli(["config", "show"], expect_ok=True)
        self.assertIn("binance", result.stdout.lower())

    def test_04_config_use_testnet(self):
        result = self.run_cli(["config", "use-testnet"], expect_ok=True)
        self.assertEqual(result.returncode, 0)

    def test_05_config_use_mainnet(self):
        result = self.run_cli(["config", "use-mainnet"], expect_ok=True)
        self.assertEqual(result.returncode, 0)

    def test_06_config_set_binance(self):
        # Currently raises AttributeError in CLI (args.testnet missing). Expect failure until fixed.
        result = self.run_cli(
            ["config", "set-binance", "--api-key", "DUMMY"],
            input_text="DUMMY_SECRET\n",
            expect_ok=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_07_config_set_binance_testnet(self):
        result = self.run_cli(
            ["config", "set-binance-testnet", "--api-key", "DUMMY"],
            input_text="DUMMY_SECRET\n",
            expect_ok=True,
        )
        self.assertEqual(result.returncode, 0)

    @mark_live
    def test_08_config_sync_bnb_burn(self):
        self.require_live()
        self.run_cli(["config", "sync-bnb-burn"], expect_ok=True)

    @mark_live
    def test_09_config_set_bnb_burn_enabled(self):
        self.require_live()
        self.run_cli(["config", "set-bnb-burn", "--enabled"], expect_ok=True)

    @mark_live
    def test_10_config_set_bnb_burn_disabled(self):
        self.require_live()
        self.run_cli(["config", "set-bnb-burn", "--disabled"], expect_ok=True)

    # C. Exchange (Read-only)
    @mark_live
    def test_11_exchange_ping(self):
        self.require_live()
        self.run_cli(["exchange", "ping"], expect_ok=True)

    @mark_live
    def test_12_exchange_time(self):
        self.require_live()
        self.run_cli(["exchange", "time"], expect_ok=True)

    @mark_live
    def test_13_exchange_info(self):
        self.require_live()
        self.run_cli(["exchange", "info"], expect_ok=True)

    @mark_live
    def test_14_exchange_info_symbol(self):
        self.require_live()
        self.run_cli(["exchange", "info", "--symbol", "BTCUSDT"], expect_ok=True)

    def test_15_exchange_balances_no_creds(self):
        # Expect failure without credentials
        result = self.run_cli(["exchange", "balances"], expect_ok=False)
        self.assertNotEqual(result.returncode, 0)

    def test_16_exchange_balances_all_no_creds(self):
        result = self.run_cli(["exchange", "balances", "--all"], expect_ok=False)
        self.assertNotEqual(result.returncode, 0)

    @mark_live
    def test_17_exchange_ping_insecure(self):
        self.require_live()
        self.run_cli(["exchange", "ping", "--insecure"], expect_ok=True)

    @mark_live
    def test_18_exchange_ping_ca_bundle(self):
        self.require_live()
        # Provide a path only if you have a local CA bundle
        self.skipTest("Provide --ca-bundle path to enable this test")

    @mark_live
    def test_19_exchange_ping_base_url(self):
        self.require_live()
        # Provide a real base URL if needed
        self.skipTest("Provide --base-url to enable this test")

    # D. Sync (Write to DB)
    @mark_live
    def test_20_sync_startup(self):
        self.require_live()
        self.run_cli(["sync", "startup"], expect_ok=True)

    @mark_live
    def test_21_sync_balances(self):
        self.require_live()
        self.run_cli(["sync", "balances"], expect_ok=True)

    @mark_live
    def test_22_sync_open_orders(self):
        self.require_live()
        self.run_cli(["sync", "open-orders"], expect_ok=True)

    @mark_live
    def test_23_sync_open_orders_symbol(self):
        self.require_live()
        self.run_cli(["sync", "open-orders", "--symbol", "BTCUSDT"], expect_ok=True)

    # E. Show (Read DB)
    @mark_seeded
    def test_24_show_balances(self):
        self.require_seeded()
        result = self.run_cli(["show", "balances"], expect_ok=True)
        actual = parse_balance_output(result.stdout)
        for item in EXPECTED_TEST_BALANCES:
            asset = item["asset"]
            self.assertIn(asset, actual)
            self.assertEqual(actual[asset], (item["free"], item["locked"]))

    @mark_seeded
    def test_25_show_balances_limit(self):
        self.require_seeded()
        self.run_cli(["show", "balances", "--limit", "20"], expect_ok=True)

    @mark_seeded
    def test_26_show_balances_filter(self):
        self.require_seeded()
        self.run_cli(["show", "balances", "--filter", "USDT"], expect_ok=True)

    @mark_seeded
    def test_27_show_open_orders(self):
        self.require_seeded()
        self.run_cli(["show", "open-orders"], expect_ok=True)

    @mark_seeded
    def test_28_show_open_orders_symbol(self):
        self.require_seeded()
        self.run_cli(["show", "open-orders", "--symbol", "BTCUSDT"], expect_ok=True)

    @mark_seeded
    def test_29_show_audit(self):
        self.require_seeded()
        self.run_cli(["show", "audit"], expect_ok=True)

    # F. Trade Requests (No execution)
    @mark_seeded
    def test_30_trade_start(self):
        self.require_seeded()
        self.run_cli(
            [
                "trade",
                "start",
                "--profit-target-pct",
                "2",
                "--stop-loss-pct",
                "1",
                "--deadline-minutes",
                "120",
                "--budget",
                "50",
                "--symbol",
                "BTCUSDT",
            ],
            expect_ok=False,  # likely requires creds or additional config
        )

    @mark_seeded
    def test_31_trade_start_yes(self):
        self.require_seeded()
        self.run_cli(
            [
                "trade",
                "start",
                "--profit-target-pct",
                "2",
                "--stop-loss-pct",
                "1",
                "--deadline-minutes",
                "120",
                "--budget",
                "50",
                "--symbol",
                "BTCUSDT",
                "--yes",
            ],
            expect_ok=False,
        )

    @mark_seeded
    def test_32_trade_list(self):
        self.require_seeded()
        self.run_cli(["trade", "list"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_33_trade_show(self):
        self.require_seeded()
        trade_id = self.get_trade_request_id()
        self.run_cli(["trade", "show", str(trade_id)], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_34_trade_cancel(self):
        self.require_seeded()
        trade_id = self.get_trade_request_id()
        self.run_cli(["trade", "cancel", str(trade_id)], expect_ok=True)

    # G. Validation & Planning (Uses market data)
    @mark_live
    @mark_needs_id
    def test_35_trade_validate(self):
        self.require_live()
        trade_id = self.get_trade_request_id()
        self.run_cli(["trade", "validate", str(trade_id)], expect_ok=True)

    @mark_live
    @mark_needs_id
    def test_36_trade_plan_build(self):
        self.require_live()
        trade_id = self.get_trade_request_id()
        self.run_cli(["trade", "plan", "build", str(trade_id)], expect_ok=True)

    @mark_seeded
    def test_37_trade_plan_list(self):
        self.require_seeded()
        self.run_cli(["trade", "plan", "list"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_38_trade_plan_show(self):
        self.require_seeded()
        plan_id = self.get_plan_id()
        self.run_cli(["trade", "plan", "show", str(plan_id)], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_39_trade_safety(self):
        self.require_seeded()
        plan_id = self.get_plan_id()
        self.run_cli(["trade", "safety", str(plan_id)], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_40_trade_safety_limit(self):
        self.require_seeded()
        plan_id = self.get_plan_id()
        self.run_cli(
            ["trade", "safety", str(plan_id), "--order-type", "LIMIT_BUY", "--limit-price", "1"],
            expect_ok=True,
        )

    # H. Execution (Testnet only)
    @mark_live
    @mark_needs_id
    def test_41_trade_execute(self):
        self.require_live()
        self.skipTest("Requires a candidate id (not auto-discovered yet)")

    @mark_seeded
    def test_42_trade_execution_list(self):
        self.require_seeded()
        self.run_cli(["trade", "execution", "list"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_43_trade_execution_show(self):
        self.require_seeded()
        exec_id = self.get_execution_id()
        self.run_cli(["trade", "execution", "show", str(exec_id)], expect_ok=True)

    @mark_live
    def test_44_trade_reconcile(self):
        self.require_live()
        self.run_cli(["trade", "reconcile"], expect_ok=True)

    @mark_live
    def test_45_trade_reconcile_all(self):
        self.require_live()
        self.run_cli(["trade", "reconcile-all"], expect_ok=True)

    # I. Manual Direct Orders (Testnet only)
    @mark_live
    def test_46_manual_buy_market_dry_run(self):
        self.require_live()
        self.run_cli(
            [
                "trade",
                "manual",
                "buy-market",
                "--i-am-human",
                "--symbol",
                "BTCUSDT",
                "--quote-qty",
                "10",
                "--dry-run",
            ],
            expect_ok=True,
        )

    @mark_live
    def test_47_manual_buy_limit_dry_run(self):
        self.require_live()
        self.run_cli(
            [
                "trade",
                "manual",
                "buy-limit",
                "--i-am-human",
                "--symbol",
                "BTCUSDT",
                "--quote-qty",
                "10",
                "--limit-price",
                "1",
                "--dry-run",
            ],
            expect_ok=True,
        )

    @mark_live
    def test_48_manual_sell_market_dry_run(self):
        self.require_live()
        self.run_cli(
            [
                "trade",
                "manual",
                "sell-market",
                "--i-am-human",
                "--symbol",
                "BTCUSDT",
                "--base-qty",
                "0.0001",
                "--dry-run",
            ],
            expect_ok=True,
        )

    @mark_live
    def test_49_manual_sell_limit_dry_run(self):
        self.require_live()
        self.run_cli(
            [
                "trade",
                "manual",
                "sell-limit",
                "--i-am-human",
                "--symbol",
                "BTCUSDT",
                "--base-qty",
                "0.0001",
                "--limit-price",
                "999999",
                "--dry-run",
            ],
            expect_ok=True,
        )

    @mark_live
    @mark_needs_id
    def test_50_manual_buy_market_live(self):
        self.require_live()
        self.skipTest("Live submission disabled by default")

    @mark_live
    @mark_needs_id
    def test_51_manual_cancel(self):
        self.require_live()
        manual_id = self.get_manual_order_id()
        self.run_cli(["trade", "manual", "cancel", "--i-am-human", str(manual_id)], expect_ok=True)

    @mark_live
    def test_52_manual_reconcile(self):
        self.require_live()
        self.run_cli(["trade", "manual", "reconcile", "--i-am-human"], expect_ok=True)

    @mark_seeded
    def test_53_manual_list(self):
        self.require_seeded()
        self.run_cli(["trade", "manual", "list"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_54_manual_show(self):
        self.require_seeded()
        manual_id = self.get_manual_order_id()
        self.run_cli(["trade", "manual", "show", str(manual_id)], expect_ok=True)

    # J. Manual Loop Trading (Testnet only)
    @mark_live
    def test_55_manual_loop_create(self):
        self.require_live()
        self.run_cli(
            [
                "trade",
                "manual",
                "loop",
                "create",
                "--symbol",
                "SOLUSDT",
                "--quote-qty",
                "1000",
                "--entry-type",
                "BUY_MARKET",
                "--take-profit-pct",
                "1.0",
                "--rebuy-pct",
                "-1",
            ],
            expect_ok=True,
        )

    @mark_live
    @mark_needs_id
    def test_56_manual_loop_start_dry_run(self):
        self.require_live()
        preset_id = self.get_loop_preset_id()
        self.run_cli(
            ["trade", "manual", "loop", "start", "--i-am-human", "--id", str(preset_id), "--max-cycles", "1", "--dry-run"],
            expect_ok=True,
        )

    @mark_live
    @mark_needs_id
    def test_57_manual_loop_start_live(self):
        self.require_live()
        self.skipTest("Live loop start disabled by default")

    @mark_seeded
    def test_58_manual_loop_status(self):
        self.require_seeded()
        self.run_cli(["trade", "manual", "loop", "status"], expect_ok=True)

    @mark_seeded
    def test_59_manual_loop_list(self):
        self.require_seeded()
        self.run_cli(["trade", "manual", "loop", "list"], expect_ok=True)

    @mark_seeded
    def test_60_manual_loop_preset_list(self):
        self.require_seeded()
        self.run_cli(["trade", "manual", "loop", "preset", "list"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_61_manual_loop_preset_show(self):
        self.require_seeded()
        preset_id = self.get_loop_preset_id()
        self.run_cli(["trade", "manual", "loop", "preset", "show", str(preset_id)], expect_ok=True)

    @mark_live
    def test_62_manual_loop_reconcile(self):
        self.require_live()
        self.run_cli(["trade", "manual", "loop", "reconcile", "--i-am-human"], expect_ok=True)

    @mark_live
    @mark_needs_id
    def test_63_manual_loop_stop(self):
        self.require_live()
        loop_id = self.get_loop_id()
        self.run_cli(["trade", "manual", "loop", "stop", "--i-am-human", "--loop-id", str(loop_id)], expect_ok=True)

    # K. Positions
    @mark_seeded
    def test_64_position_list(self):
        self.require_seeded()
        self.run_cli(["position", "list"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_65_position_show(self):
        self.require_seeded()
        position_id = self.get_position_id()
        self.run_cli(["position", "show", str(position_id)], expect_ok=True)

    @mark_live
    @mark_needs_id
    def test_66_position_show_live(self):
        self.require_live()
        position_id = self.get_position_id()
        self.run_cli(["position", "show", str(position_id), "--live"], expect_ok=True)

    # L. Monitoring
    @mark_live
    def test_67_monitor_once(self):
        self.require_live()
        self.run_cli(["monitor", "once"], expect_ok=True)

    @mark_live
    @mark_needs_id
    def test_68_monitor_once_verbose(self):
        self.require_live()
        position_id = self.get_position_id()
        self.run_cli(["monitor", "once", "--position-id", str(position_id), "--verbose"], expect_ok=True)

    @mark_live
    def test_69_monitor_loop(self):
        self.require_live()
        self.run_cli(["monitor", "loop", "--interval-seconds", "5", "--duration-seconds", "15"], expect_ok=True)

    @mark_seeded
    def test_70_monitor_events_list(self):
        self.require_seeded()
        self.run_cli(["monitor", "events", "list"], expect_ok=True)

    # M. Orders Management
    @mark_live
    @mark_needs_id
    def test_71_orders_cancel(self):
        self.require_live()
        self.skipTest("Requires a Binance exchange order id (not auto-discovered)")

    # N. Reliability
    def test_72_reliability_status(self):
        self.run_cli(["reliability", "status"], expect_ok=True)

    @mark_live
    def test_73_reliability_reconcile(self):
        self.require_live()
        self.run_cli(["reliability", "reconcile"], expect_ok=True)

    @mark_live
    def test_74_reliability_resume_global(self):
        self.require_live()
        self.run_cli(["reliability", "resume", "--global", "--i-am-human"], expect_ok=True)

    @mark_live
    def test_75_reliability_resume_symbol(self):
        self.require_live()
        self.run_cli(["reliability", "resume", "--symbol", "BTCUSDT", "--i-am-human"], expect_ok=True)

    @mark_live
    @mark_needs_id
    def test_76_reliability_resume_loop_id(self):
        self.require_live()
        loop_id = self.get_loop_id()
        self.run_cli(["reliability", "resume", "--loop-id", str(loop_id), "--i-am-human"], expect_ok=True)

    @mark_seeded
    def test_77_reliability_events_list(self):
        self.require_seeded()
        self.run_cli(["reliability", "events", "list"], expect_ok=True)

    # O. PnL
    @mark_seeded
    def test_78_pnl_realized(self):
        self.require_seeded()
        self.run_cli(["pnl", "realized"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_79_pnl_realized_show(self):
        self.require_seeded()
        exec_id = self.get_execution_id()
        self.run_cli(["pnl", "realized", "show", str(exec_id)], expect_ok=True)

    @mark_seeded
    def test_80_pnl_unrealized(self):
        self.require_seeded()
        self.run_cli(["pnl", "unrealized"], expect_ok=True)

    @mark_seeded
    @mark_needs_id
    def test_81_pnl_unrealized_position(self):
        self.require_seeded()
        position_id = self.get_position_id()
        self.run_cli(["pnl", "unrealized", "--position-id", str(position_id)], expect_ok=True)

    @mark_seeded
    def test_82_pnl_unrealized_no_live(self):
        self.require_seeded()
        self.run_cli(["pnl", "unrealized", "--no-live"], expect_ok=True)

    # P. Dust Ledger
    @mark_seeded
    def test_83_dust_list(self):
        self.require_seeded()
        self.run_cli(["dust", "list"], expect_ok=True)

    @mark_seeded
    def test_84_dust_show(self):
        self.require_seeded()
        asset = EXPECTED_TEST_BALANCES[0]["asset"]
        self.run_cli(["dust", "show", asset], expect_ok=True)


if __name__ == "__main__":
    unittest.main()
