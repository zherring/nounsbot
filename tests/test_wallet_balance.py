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
            patch("bot.chain.vote_cost_estimate", return_value=(1, 10**15)),
            patch("bot.chain.build_vote_tx") as build_tx,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                executor.cast_vote(998, "FOR", "reason")
        build_tx.assert_not_called()
        self.assertIn("bot wallet too low to vote", str(ctx.exception))
        self.assertIn("top up", str(ctx.exception))

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
        self.assertIn("low wallet balance", str(ctx.exception))

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
        self.assertIn("bot wallet low", send.call_args.args[0])
        self.assertIn(addr, send.call_args.args[0])
        self.assertEqual(db.kv_get(conn, "wallet_low"), "1")

        # 2) still low, well within the 24h debounce window -> no repeat warning
        # (bypass the RPC throttle by backdating the last-checked timestamp)
        send = run(low, checked_at_override=past(700))
        send.assert_not_called()

        # 3) still low, but the last warning was >24h ago -> warns again
        send = run(low, checked_at_override=past(700), warned_at_override=past(25 * 3600))
        send.assert_called_once()
        self.assertIn("bot wallet low", send.call_args.args[0])

        # 4) balance recovers -> resets state and sends a short top-up notice
        send = run(recovered, checked_at_override=past(700))
        send.assert_called_once()
        self.assertIn("topped up", send.call_args.args[0])
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


if __name__ == "__main__":
    unittest.main()
