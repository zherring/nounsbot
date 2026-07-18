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


def sponsor_candidate(cand: dict, reason: str, target_prop: dict | None = None) -> str:
    """Sign the EIP-712 sponsorship for a candidate and register it via
    NounsDAOData.addSignature. Simulated first — a bad digest reverts in
    eth_call before any gas is spent. Returns tx hash.

    The signature commits to the candidate's exact current content; any
    subsequent edit by the proposer invalidates it automatically. Update
    candidates use the governor's distinct UpdateProposal typed-data payload
    and may only be re-signed by the original proposal signers."""
    import time

    key = os.environ.get("BOT_PRIVATE_KEY")
    if not key:
        raise RuntimeError("BOT_PRIVATE_KEY not set — paper mode")
    content = cand["latestVersion"]["content"]
    proposal_id = int(content.get("proposalIdToUpdate") or 0)

    account = Account.from_key(key)
    web3 = chain.w3()
    if proposal_id:
        if not target_prop or int(target_prop["id"]) != proposal_id:
            raise RuntimeError("target proposal data is required for update-candidate sponsorship")
        original_signers = {s["id"].lower() for s in target_prop.get("signers") or []}
        if account.address.lower() not in original_signers:
            raise RuntimeError(
                f"only prop {proposal_id}'s original signers can sign its update; "
                "this delegate was not an original signer"
            )
        # NounsDAOTypes.ProposalState.Updatable is enum value 10.
        if chain.proposal_state(web3, proposal_id) != 10:
            raise RuntimeError(f"prop {proposal_id} is no longer updatable")

    encoded = chain.calc_proposal_encode_data(
        cand["proposer"], content["targets"], content["values"],
        content["signatures"], content["calldatas"], content["description"],
    )
    if proposal_id:
        # updateProposalBySigs uses abi.encodePacked(proposalId, encodedProp).
        encoded = proposal_id.to_bytes(32, "big") + encoded
    expiration = int(time.time()) + 30 * 24 * 3600  # 30 days
    digest = chain.sponsorship_digest(encoded, expiration, proposal_id)
    sign_fn = getattr(Account, "unsafe_sign_hash", None) or Account._sign_hash
    signature = sign_fn(digest, private_key=key).signature

    fn = chain.data_contract(web3).functions.addSignature(
        bytes(signature), expiration, Web3.to_checksum_address(cand["proposer"]),
        cand["slug"], proposal_id, encoded, reason,
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


def signal_candidate(cand: dict, support: str, reason: str) -> str:
    """Onchain feedback on a candidate (CandidateFeedbackSent) — voice + reasoning
    with our delegated weight behind it, WITHOUT sponsoring toward the ballot.
    Plain transaction, no EIP-712; gas is not refunded (feedback isn't a vote)."""
    key = os.environ.get("BOT_PRIVATE_KEY")
    if not key:
        raise RuntimeError("BOT_PRIVATE_KEY not set — paper mode")
    account = Account.from_key(key)
    web3 = chain.w3()

    fn = chain.data_contract(web3).functions.sendCandidateFeedback(
        Web3.to_checksum_address(cand["proposer"]), cand["slug"], chain.SUPPORT[support], reason
    )
    fn.call({"from": account.address})  # simulate first

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
        raise RuntimeError(f"feedback tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()
