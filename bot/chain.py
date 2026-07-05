"""Onchain layer: the governor contract and the vote-only hot wallet.

The key held here can cast votes. It can never transfer the Noun — delegation
is not custody. Worst case on compromise: bad votes until re-delegation.
"""

import os

from web3 import Web3

RPC_URL = os.environ.get("RPC_URL", "https://ethereum-rpc.publicnode.com")

# Nouns DAO governor proxy (logic is DAO-upgradable; address is stable)
GOVERNOR = Web3.to_checksum_address("0x6f3E6272A167e8AcCb32072d08E0957F9c79223d")

GOVERNOR_ABI = [
    {
        "name": "castRefundableVoteWithReason",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "proposalId", "type": "uint256"},
            {"name": "support", "type": "uint8"},
            {"name": "reason", "type": "string"},
            {"name": "clientId", "type": "uint32"},
        ],
        "outputs": [],
    },
    {
        "name": "state",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

SUPPORT = {"AGAINST": 0, "FOR": 1, "ABSTAIN": 2}
CLIENT_ID = int(os.environ.get("NOUNS_CLIENT_ID", "0"))


def w3() -> Web3:
    return Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))


def governor(web3: Web3):
    return web3.eth.contract(address=GOVERNOR, abi=GOVERNOR_ABI)


def build_vote_tx(web3: Web3, sender: str, prop_id: int, vote: str, reason: str) -> dict:
    fn = governor(web3).functions.castRefundableVoteWithReason(
        prop_id, SUPPORT[vote], reason, CLIENT_ID
    )
    latest = web3.eth.get_block("latest")
    base = latest["baseFeePerGas"]
    tip = web3.to_wei(1, "gwei")
    return fn.build_transaction(
        {
            "from": Web3.to_checksum_address(sender),
            "nonce": web3.eth.get_transaction_count(Web3.to_checksum_address(sender)),
            "maxFeePerGas": base * 2 + tip,
            "maxPriorityFeePerGas": tip,
            "chainId": 1,
        }
    )


def simulate_vote(web3: Web3, sender: str, prop_id: int, vote: str, reason: str):
    """eth_call the vote from an arbitrary sender — validates encoding + vote
    eligibility without a key or gas. Raises ContractLogicError with the
    governor's revert reason if ineligible."""
    governor(web3).functions.castRefundableVoteWithReason(
        prop_id, SUPPORT[vote], reason, CLIENT_ID
    ).call({"from": Web3.to_checksum_address(sender)})
