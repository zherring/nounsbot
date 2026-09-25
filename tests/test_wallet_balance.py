"""Low-balance warning: vote cost math, the cast_vote guard rail, and the
main-loop debounce logic (crossing below threshold warns once, stays quiet
for 24h, warns again after 24h, and resets with a top-up notice)."""

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from web3 import Web3
from web3.exceptions import ContractLogicError

from bot import chain, db, executor, poller


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db.migrate(conn)
    return conn


def fake_web3(base_fee_gwei=1, balance_wei=0):
    """A MagicMock standing in for Web3 with just enough real behavior
    (to_wei/from_wei are plain functions, not RPC calls) to exercise the fee
    and cost math without a network."""
    web3 = MagicMock()
    web3.to_wei = Web3.to_wei
    web3.from_wei = Web3.from_wei
    web3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(base_fee_gwei, "gwei")}
    web3.eth.get_balance.return_value = balance_wei
    return web3


def past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class FormatEthTests(unittest.TestCase):
    def test_dust_amounts_use_two_significant_figures(self):
        self.assertEqual(chain.format_eth(121465625553468), "0.00012 ETH")
        self.assertEqual(chain.format_eth(291122461500000), "0.00029 ETH")

    def test_normal_amounts_use_four_fixed_decimals(self):
        self.assertEqual(chain.format_eth(20087449843512345), "0.0201 ETH")
        self.assertEqual(chain.format_eth(1234500000000000000), "1.2345 ETH")

    def test_zero(self):
        self.assertEqual(chain.format_eth(0), "0.0000 ETH")


class VoteCostEstimateTests(unittest.TestCase):
    def test_cost_per_vote_is_gas_budget_times_current_fee(self):
        web3 = fake_web3(base_fee_gwei=1, balance_wei=5 * 10**14)
        balance, cost_per_vote = chain.vote_cost_estimate(web3, "0x" + "22" * 20)
        expected_fee = Web3.to_wei(1, "gwei") * 2 + Web3.to_wei(1, "gwei")
        self.assertEqual(balance, 5 * 10**14)
        self.assertEqual(cost_per_vote, chain.VOTE_GAS_BUDGET * expected_fee)

    def test_fee_formula_matches_build_vote_tx(self):
        # regression guard: the estimate and the real tx must never drift apart
        web3 = fake_web3(base_fee_gwei=3)
        self.assertEqual(
            chain.vote_max_fee_per_gas(web3),
            Web3.to_wei(3, "gwei") * 2 + Web3.to_wei(1, "gwei"),
        )


class CastVoteLowBalanceTests(unittest.TestCase):
    def test_raises_clear_error_before_building_tx_when_balance_too_low(self):
        with (
            patch.dict("os.environ", {"BOT_PRIVATE_KEY": "0x" + "11" * 32}),
            patch("bot.chain.w3", return_value=fake_web3()),
            patch("bot.chain.simulate_vote", return_value=None),
            patch("bot.chain.vote_cost_estimate", return_value=(121465625553468, 291122461500000)),
            patch("bot.chain.build_vote_tx") as build_tx,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                executor.cast_vote(998, "FOR", "reason")
        build_tx.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("bot wallet too low to vote: 0.00012 ETH", message)
        self.assertIn("needs ~0.00029 ETH at current gas", message)
        self.assertIn("top up", message)

    def test_rewraps_no_data_contract_logic_error_with_balance_hint(self):
        with (
            patch.dict("os.environ", {"BOT_PRIVATE_KEY": "0x" + "11" * 32}),
            patch("bot.chain.w3", return_value=fake_web3(balance_wei=10**18)),
            patch("bot.chain.simulate_vote", return_value=None),
            patch("bot.chain.vote_cost_estimate", return_value=(10**18, 10**15)),
            patch(
                "bot.chain.build_vote_tx",
                side_effect=ContractLogicError("execution reverted", "no data"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                executor.cast_vote(998, "FOR", "reason")
        message = str(ctx.exception)
        self.assertIn("low wallet balance", message)
        self.assertIn("1.0000 ETH", message)

    def test_other_contract_logic_errors_pass_through_unwrapped(self):
        with (
            patch.dict("os.environ", {"BOT_PRIVATE_KEY": "0x" + "11" * 32}),
            patch("bot.chain.w3", return_value=fake_web3(balance_wei=10**18)),
            patch("bot.chain.simulate_vote", return_value=None),
            patch("bot.chain.vote_cost_estimate", return_value=(10**18, 10**15)),
            patch(
                "bot.chain.build_vote_tx",
                side_effect=ContractLogicError("some other revert", "0xdeadbeef"),
            ),
        ):
            with self.assertRaises(ContractLogicError):
                executor.cast_vote(998, "FOR", "reason")


class CheckWalletBalanceDebounceTests(unittest.TestCase):
    def test_skips_entirely_in_paper_mode(self):
        conn = memory_db()
        with (
            patch("bot.executor.bot_address", return_value=None),
            patch("bot.chain.w3") as w3,
            patch("bot.telegram.send_message") as send,
        ):
            poller.check_wallet_balance(conn)
        w3.assert_not_called()
        send.assert_not_called()

    def test_rpc_error_is_logged_and_swallowed(self):
        conn = memory_db()
        with (
            patch("bot.executor.bot_address", return_value="0x" + "aa" * 20),
            patch("bot.chain.w3", side_effect=RuntimeError("rpc down")),
            patch("bot.telegram.send_message") as send,
        ):
            poller.check_wallet_balance(conn)  # must not raise
        send.assert_not_called()

    def test_warns_once_then_debounces_for_24h_then_warns_again_and_resets(self):
        conn = memory_db()
        addr = "0x" + "aa" * 20
        low = (0, 10**15)  # 0 votes left
        recovered = (10**18, 10**15)  # plenty of votes left

        def run(cost_estimate, checked_at_override=None, warned_at_override=None):
            if checked_at_override is not None:
                db.kv_set(conn, "wallet_balance_checked_at", checked_at_override)
            if warned_at_override is not None:
                db.kv_set(conn, "wallet_low_warned_at", warned_at_override)
            with (
                patch("bot.executor.bot_address", return_value=addr),
                patch("bot.chain.w3", return_value=fake_web3()),
                patch("bot.chain.vote_cost_estimate", return_value=cost_estimate),
                patch("bot.telegram.send_message") as send,
            ):
                poller.check_wallet_balance(conn)
            return send

        # 1) crosses below threshold: warns immediately
        send = run(low)
        send.assert_called_once()
        self.assertEqual(
            send.call_args.args[0],
            f"⛽ bot wallet low: 0.0000 ETH ≈ 0 votes left at current gas. Top up {addr}",
        )
        self.assertEqual(db.kv_get(conn, "wallet_low"), "1")

        # 2) still low, well within the 24h debounce window -> no repeat warning
        # (bypass the RPC throttle by backdating the last-checked timestamp)
        send = run(low, checked_at_override=past(700))
        send.assert_not_called()

        # 3) still low, but the last warning was >24h ago -> warns again
        send = run(low, checked_at_override=past(700), warned_at_override=past(25 * 3600))
        send.assert_called_once()
        self.assertIn("bot wallet low", send.call_args.args[0])

        # 4) balance recovers -> resets state and sends a richer top-up notice
        send = run(recovered, checked_at_override=past(700))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "⛽ bot wallet topped up ✅ 1.0000 ETH ≈ 1000 votes left")
        self.assertEqual(db.kv_get(conn, "wallet_low"), "0")

        # 5) staying healthy afterwards -> quiet
        send = run(recovered, checked_at_override=past(700))
        send.assert_not_called()

    def test_throttles_rpc_calls_within_the_check_interval(self):
        conn = memory_db()
        addr = "0x" + "aa" * 20
        with (
            patch("bot.executor.bot_address", return_value=addr),
            patch("bot.chain.w3", return_value=fake_web3()) as w3,
            patch("bot.chain.vote_cost_estimate", return_value=(0, 10**15)),
            patch("bot.telegram.send_message"),
        ):
            poller.check_wallet_balance(conn)
            poller.check_wallet_balance(conn)  # immediate repeat: throttled, no new RPC call
        self.assertEqual(w3.call_count, 1)


class GasCommandTests(unittest.TestCase):
    """/gas: an unthrottled, on-demand version of the same balance check."""

    ADDR = "0xF6e7501dFe7003299108020c5830C4c5B3CA6aA9"

    def test_reports_ok_status_with_gas_snapshot(self):
        conn = memory_db()
        balance = 20087449843512345  # -> "0.0201 ETH"
        cost_per_vote = 291122461500000  # -> "0.00029 ETH"
        base_fee = 200000000  # 0.20 gwei
        with (
            patch("bot.executor.bot_address", return_value=self.ADDR),
            patch("bot.chain.w3", return_value=fake_web3()),
            patch("bot.chain.gas_status", return_value=(balance, cost_per_vote, base_fee)),
        ):
            reply = poller.run_command(conn, "gas", [])
        self.assertEqual(
            reply,
            "⛽ bot wallet: 0.0201 ETH ≈ 69 votes left\n"
            "gas now: 0.20 gwei base · ~0.00029 ETH per vote (250k gas budget)\n"
            "low-balance alert below 2 votes · currently OK\n"
            f"{self.ADDR}",
        )

    def test_reports_low_alert_under_threshold(self):
        conn = memory_db()
        cost_per_vote = 291122461500000
        balance = cost_per_vote  # exactly 1 vote left, below LOW_BALANCE_VOTES (2)
        with (
            patch("bot.executor.bot_address", return_value=self.ADDR),
            patch("bot.chain.w3", return_value=fake_web3()),
            patch("bot.chain.gas_status", return_value=(balance, cost_per_vote, 200000000)),
        ):
            reply = poller.run_command(conn, "gas", [])
        self.assertIn("≈ 1 votes left", reply)
        self.assertIn("currently ⚠️ LOW", reply)

    def test_paper_mode(self):
        conn = memory_db()
        with (
            patch("bot.executor.bot_address", return_value=None),
            patch("bot.chain.w3") as w3,
        ):
            reply = poller.run_command(conn, "gas", [])
        self.assertEqual(reply, "⛽ paper mode — no bot wallet configured")
        w3.assert_not_called()

    def test_rpc_error_replies_without_raising(self):
        conn = memory_db()
        with (
            patch("bot.executor.bot_address", return_value=self.ADDR),
            patch("bot.chain.w3", side_effect=RuntimeError("rpc down")),
        ):
            reply = poller.run_command(conn, "gas", [])  # must not raise
        self.assertIn("gas check failed", reply)
        self.assertIn("rpc down", reply)


if __name__ == "__main__":
    unittest.main()
