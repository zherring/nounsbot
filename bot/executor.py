"""Vote execution. Signs with BOT_PRIVATE_KEY (vote-only EOA) and casts via
castRefundableVoteWithReason — gas is refunded by the DAO, reasoning is public."""

import os

from eth_account import Account
from web3 import Web3

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


def sponsor_candidate(cand: dict, reason: str) -> str:
    """Sign the EIP-712 sponsorship for a candidate and register it via
    NounsDAOData.addSignature. Simulated first — a bad digest reverts in
    eth_call before any gas is spent. Returns tx hash.

    The signature commits to the candidate's exact current content; any
    subsequent edit by the proposer invalidates it automatically. v1 supports
    new candidates only (proposalIdToUpdate == 0)."""
    import time

    key = os.environ.get("BOT_PRIVATE_KEY")
    if not key:
        raise RuntimeError("BOT_PRIVATE_KEY not set — paper mode")
    content = cand["latestVersion"]["content"]
    if int(content.get("proposalIdToUpdate") or 0) != 0:
        raise RuntimeError("update-candidates not supported yet (proposalIdToUpdate != 0)")

    account = Account.from_key(key)
    web3 = chain.w3()

    encoded = chain.calc_proposal_encode_data(
        cand["proposer"], content["targets"], content["values"],
        content["signatures"], content["calldatas"], content["description"],
    )
    expiration = int(time.time()) + 30 * 24 * 3600  # 30 days
    digest = chain.sponsorship_digest(encoded, expiration)
    sign_fn = getattr(Account, "unsafe_sign_hash", None) or Account._sign_hash
    signature = sign_fn(digest, private_key=key).signature

    fn = chain.data_contract(web3).functions.addSignature(
        bytes(signature), expiration, Web3.to_checksum_address(cand["proposer"]),
        cand["slug"], 0, encoded, reason,
    )
    fn.call({"from": account.address})  # simulate: digest/candidate validity check

    latest = web3.eth.get_block("latest")
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": web3.eth.get_transaction_count(account.address),
        "maxFeePerGas": latest["baseFeePerGas"] * 2 + web3.to_wei(1, "gwei"),
        "maxPriorityFeePerGas": web3.to_wei(1, "gwei"),
        "chainId": 1,
    })
    signed = account.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"sponsorship tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()
