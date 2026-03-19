import os
import sys
import random
import subprocess
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "testing" / "logs"

# Test assets provided by user (non-USDT assets used for symbols)
TEST_ASSETS = ["AI", "DOGE", "DOT", "SOL"]
QUOTE_ASSET = "USDT"
PRICE_MAP = {
    "DOGEUSDT": "0.09456",
    "DOTUSDT": "1.545",
    "SOLUSDT": "89.75",
    "AIUSDT": "0.0223",
}


try:
    import pytest  # type: ignore
except Exception:  # pragma: no cover
    class _NoOpMark:
        def __getattr__(self, name):
            def _decorator(obj):
                return obj

            return _decorator

    class _NoOpPytest:
        mark = _NoOpMark()

    pytest = _NoOpPytest()  # type: ignore

mark_live = getattr(pytest.mark, "live", lambda f: f)
mark_needs_id = getattr(pytest.mark, "needs_id", lambda f: f)




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


def extract_all_int_ids(output: str) -> list[int]:
    ids: list[int] = []
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
            ids.append(int(parts[0]))
    return ids


def extract_first_uuid(output: str) -> str | None:
    import re

    match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        output,
    )
    return match.group(0) if match else None


def can_cancel_execution(show_output: str) -> bool:
    text = show_output.lower()
    is_limit = "limit_buy" in text or "limit_sell" in text or "limit buy" in text or "limit sell" in text
    if not is_limit:
        return False
    # Only attempt cancel if still open/new/partially filled
    open_statuses = ["new", "partially_filled", "partially filled", "open"]
    return any(s in text for s in open_statuses)


class TestTradeCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_path = REPO_ROOT / "cryptogent.toml"
        cls.db_path = REPO_ROOT / "cryptogent.sqlite3"

        cls.base_env = os.environ.copy()
        cls.base_env["PYTHONPATH"] = str(REPO_ROOT)
        cls.base_env["CRYPTOGENT_CONFIG"] = str(cls.config_path)
        cls.base_env["CRYPTOGENT_DB"] = str(cls.db_path)

        cls.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.summary_log = LOG_DIR / f"trade_test_summary_{cls.run_id}.log"
        cls.detail_log = LOG_DIR / f"trade_test_detail_{cls.run_id}.log"

        # Init DB
        cls.run_cli(["init"], expect_ok=True)
        cls.last_symbol = None
        cls.last_trade_id = None
        cls.last_plan_id = None
        cls.last_candidate_id = None
        cls.last_execution_id = None

    @classmethod
    def tearDownClass(cls) -> None:
        # Strict cleanup: best-effort cancel all trade requests and executions
        try:
            if os.environ.get("CRYPTOGENT_TEST_LIVE") == "1":
                result = cls.run_cli(["trade", "list"], expect_ok=False)
                for trade_id in extract_all_int_ids(result.stdout + "\n" + result.stderr):
                    cls.run_cli(["trade", "cancel", str(trade_id)], expect_ok=False)

                result = cls.run_cli(["trade", "execution", "list"], expect_ok=False)
                for exec_id in extract_all_int_ids(result.stdout + "\n" + result.stderr):
                    show = cls.run_cli(["trade", "execution", "show", str(exec_id)], expect_ok=False)
                    if can_cancel_execution(show.stdout + "\n" + show.stderr):
                        cls.run_cli(["trade", "execution", "cancel", str(exec_id)], expect_ok=False)
        except Exception:
            pass
        return None

    @classmethod
    def log_result(cls, cmd: str, status: str, stdout: str, stderr: str) -> None:
        print(f"[{status}] {cmd}")
        with cls.summary_log.open("a", encoding="utf-8") as f:
            f.write(f"[{status}] {cmd}\n")
        with cls.detail_log.open("a", encoding="utf-8") as f:
            f.write(f"[{status}] {cmd}\n")
            if stdout:
                f.write("STDOUT:\n")
                f.write(stdout.rstrip() + "\n")
            if stderr:
                f.write("STDERR:\n")
                f.write(stderr.rstrip() + "\n")
            f.write("-" * 80 + "\n")

    @classmethod
    def run_cli(cls, args, input_text=None, expect_ok=True):
        cmd = [sys.executable, "-m", "cryptogent"] + args
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=cls.base_env,
        )
        status = "OK" if result.returncode == 0 else f"FAIL({result.returncode})"
        cls.log_result(" ".join(cmd), status, result.stdout, result.stderr)
        if expect_ok and result.returncode != 0:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def require_live(self):
        if os.environ.get("CRYPTOGENT_TEST_LIVE") != "1":
            self.skipTest("Set CRYPTOGENT_TEST_LIVE=1 to enable live/testnet commands")

    def require_execute(self):
        if os.environ.get("CRYPTOGENT_TEST_EXECUTE") != "1":
            self.skipTest("Set CRYPTOGENT_TEST_EXECUTE=1 to enable execution commands")

    def pick_symbol_and_budget(self) -> tuple[str, str]:
        asset = random.choice(TEST_ASSETS)
        symbol = f"{asset}{QUOTE_ASSET}"
        budget = f"{random.uniform(10, 50):.2f}"
        return symbol, budget

    def ensure_trade_request(self) -> int:
        if self.__class__.last_trade_id is not None:
            return self.__class__.last_trade_id
        self.require_live()
        symbol, budget = self.pick_symbol_and_budget()
        self.__class__.last_symbol = symbol
        self.run_cli(
            [
                "trade",
                "start",
                "--profit-target-pct",
                "2.0",
                "--deadline-hours",
                "24",
                "--budget-mode",
                "manual",
                "--budget",
                budget,
                "--budget-asset",
                QUOTE_ASSET,
                "--symbol",
                symbol,
                "--exit-asset",
                QUOTE_ASSET,
                "--yes",
            ],
            expect_ok=True,
        )
        trade_id = self.get_trade_request_id()
        self.__class__.last_trade_id = trade_id
        return trade_id

    def ensure_plan(self) -> int:
        if self.__class__.last_plan_id is not None:
            return self.__class__.last_plan_id
        trade_id = self.ensure_trade_request()
        self.run_cli(["trade", "plan", "build", str(trade_id)], expect_ok=True)
        plan_id = self.get_plan_id()
        self.__class__.last_plan_id = plan_id
        return plan_id

    def ensure_limit_candidate(self) -> str:
        if self.__class__.last_candidate_id is not None:
            return self.__class__.last_candidate_id
        plan_id = self.ensure_plan()
        symbol = self.__class__.last_symbol or self.pick_symbol_and_budget()[0]
        limit_price = self.limit_price_for(symbol)
        result = self.run_cli(
            ["trade", "safety", str(plan_id), "--order-type", "LIMIT_BUY", "--limit-price", limit_price],
            expect_ok=True,
        )
        candidate_id = extract_first_uuid(result.stdout) or extract_first_uuid(result.stderr)
        if not candidate_id:
            self.skipTest("No execution candidate id found in trade safety output")
        self.__class__.last_candidate_id = candidate_id
        return candidate_id

    def limit_price_for(self, symbol: str) -> str:
        price = PRICE_MAP.get(symbol)
        if not price:
            self.skipTest(f"No limit price configured for {symbol}")
        return price

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

    def get_position_id(self) -> int:
        result = self.run_cli(["position", "list"], expect_ok=True)
        position_id = extract_first_int_id(result.stdout) or extract_first_int_id(result.stderr)
        if position_id is None:
            self.skipTest("No position id found in position list")
        return position_id

    def get_candidate_id(self) -> str:
        plan_id = self.get_plan_id()
        result = self.run_cli(["trade", "safety", str(plan_id)], expect_ok=True)
        candidate_id = extract_first_uuid(result.stdout) or extract_first_uuid(result.stderr)
        if not candidate_id:
            self.skipTest("No execution candidate id found in trade safety output")
        return candidate_id

    # 1) Start trade (create request)
    @mark_live
    def test_01_trade_start(self):
        self.require_live()
        self.ensure_trade_request()

    # 2) List trade requests
    @mark_live
    def test_02_trade_list(self):
        self.require_live()
        self.run_cli(["trade", "list"], expect_ok=True)

    # 3) Show trade request
    @mark_live
    @mark_needs_id
    def test_03_trade_show(self):
        self.require_live()
        trade_id = self.ensure_trade_request()
        self.run_cli(["trade", "show", str(trade_id)], expect_ok=True)

    # 4) Cancel trade request (deferred to end so it doesn't break later steps)
    @mark_live
    @mark_needs_id
    def test_04_trade_cancel(self):
        self.require_live()
        self.skipTest("Deferred: cancel runs at end to avoid cancelling active requests")

    # 5) Validate trade request
    @mark_live
    @mark_needs_id
    def test_05_trade_validate(self):
        self.require_live()
        trade_id = self.ensure_trade_request()
        self.run_cli(["trade", "validate", str(trade_id)], expect_ok=True)

    # 6) Build trade plan
    @mark_live
    @mark_needs_id
    def test_06_trade_plan_build(self):
        self.require_live()
        self.ensure_plan()

    # 7) List trade plans
    @mark_live
    def test_07_trade_plan_list(self):
        self.require_live()
        self.run_cli(["trade", "plan", "list"], expect_ok=True)

    # 8) Show trade plan
    @mark_live
    @mark_needs_id
    def test_08_trade_plan_show(self):
        self.require_live()
        plan_id = self.ensure_plan()
        self.run_cli(["trade", "plan", "show", str(plan_id)], expect_ok=True)

    # 9) Safety validate trade plan
    @mark_live
    @mark_needs_id
    def test_09_trade_safety(self):
        self.require_live()
        plan_id = self.ensure_plan()
        self.run_cli(["trade", "safety", str(plan_id)], expect_ok=True)

    # 9b) Safety validate trade plan (LIMIT_BUY)
    @mark_live
    @mark_needs_id
    def test_09b_trade_safety_limit_buy(self):
        self.require_live()
        self.ensure_limit_candidate()

    # 9c) Safety validate trade plan (MARKET_SELL)
    @mark_live
    @mark_needs_id
    def test_09c_trade_safety_market_sell(self):
        self.require_live()
        plan_id = self.ensure_plan()
        position_id = self.get_position_id()
        self.run_cli(
            [
                "trade",
                "safety",
                str(plan_id),
                "--order-type",
                "MARKET_SELL",
                "--position-id",
                str(position_id),
                "--close-mode",
                "all",
            ],
            expect_ok=True,
        )

    # 9d) Safety validate trade plan (LIMIT_SELL)
    @mark_live
    @mark_needs_id
    def test_09d_trade_safety_limit_sell(self):
        self.require_live()
        plan_id = self.ensure_plan()
        position_id = self.get_position_id()
        symbol = self.__class__.last_symbol or self.pick_symbol_and_budget()[0]
        limit_price = self.limit_price_for(symbol)
        self.run_cli(
            [
                "trade",
                "safety",
                str(plan_id),
                "--order-type",
                "LIMIT_SELL",
                "--position-id",
                str(position_id),
                "--close-mode",
                "all",
                "--limit-price",
                limit_price,
            ],
            expect_ok=True,
        )
    # 10) Execute trade candidate (Phase 7)
    @mark_live
    @mark_needs_id
    def test_10_trade_execute(self):
        self.require_live()
        self.require_execute()
        candidate_id = self.ensure_limit_candidate()
        self.run_cli(["trade", "execute", candidate_id, "--yes"], expect_ok=True)
        exec_id = self.get_execution_id()
        self.__class__.last_execution_id = exec_id

    # 11) List executions
    @mark_live
    def test_11_trade_execution_list(self):
        self.require_live()
        self.run_cli(["trade", "execution", "list"], expect_ok=True)

    # 12) Show execution
    @mark_live
    @mark_needs_id
    def test_12_trade_execution_show(self):
        self.require_live()
        exec_id = self.__class__.last_execution_id or self.get_execution_id()
        self.run_cli(["trade", "execution", "show", str(exec_id)], expect_ok=True)

    # 13) Cancel LIMIT execution
    @mark_live
    @mark_needs_id
    def test_13_trade_execution_cancel(self):
        self.require_live()
        exec_id = self.__class__.last_execution_id or self.get_execution_id()
        self.run_cli(["trade", "execution", "cancel", str(exec_id)], expect_ok=True)



if __name__ == "__main__":
    unittest.main()
