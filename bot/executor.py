"""Vote execution. Signs with BOT_PRIVATE_KEY (vote-only EOA) and casts via
castRefundableVoteWithReason — gas is refunded by the DAO, reasoning is public."""

import os

from eth_account import Account

from . import chain


def bot_address() -> str | None:
    key = os.environ.get("BOT_PRIVATE_KEY")
    return Account.from_key(key).address if key else None


def cast_vote(prop_id: int, vote: str, reason: str) -> str:
    """Signs and broadcasts. Returns tx hash. Raises on any failure —
    the caller records the miss and alerts."""
    key = os.environ.get("BOT_PRIVATE_KEY")
    if not key:
        raise RuntimeError("BOT_PRIVATE_KEY not set — still in paper mode")
    account = Account.from_key(key)
    web3 = chain.w3()

    # simulate first: a revert here costs nothing and gives us the reason
    chain.simulate_vote(web3, account.address, prop_id, vote, reason)

    tx = chain.build_vote_tx(web3, account.address, prop_id, vote, reason)
    signed = account.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"vote tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()
