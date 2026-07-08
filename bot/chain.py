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


# --- Candidate sponsorship (EIP-712 signatures the governor accepts in proposeBySigs) ---

DATA_CONTRACT = Web3.to_checksum_address("0xf790A5f59678dd733fb3De93493A91f472ca1365")

DATA_ABI = [
    {
        "name": "addSignature",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "sig", "type": "bytes"},
            {"name": "expirationTimestamp", "type": "uint256"},
            {"name": "proposer", "type": "address"},
            {"name": "slug", "type": "string"},
            {"name": "proposalIdToUpdate", "type": "uint256"},
            {"name": "encodedProp", "type": "bytes"},
            {"name": "reason", "type": "string"},
        ],
        "outputs": [],
    }
]

PROPOSAL_TYPEHASH = Web3.keccak(
    text="Proposal(address proposer,address[] targets,uint256[] values,string[] signatures,bytes[] calldatas,string description,uint256 expiry)"
)
DOMAIN_TYPEHASH = Web3.keccak(text="EIP712Domain(string name,uint256 chainId,address verifyingContract)")


def _pack32(items: list[bytes]) -> bytes:
    return b"".join(items)


def calc_proposal_encode_data(proposer: str, targets, values, signatures, calldatas, description: str) -> bytes:
    """Byte-exact replica of NounsDAOV3Proposals.calcProposalEncodeData —
    the signature commits to the candidate's exact content, so any edit
    invalidates our sponsorship automatically."""
    from eth_abi import encode as abi_encode

    target_packed = _pack32([bytes(12) + bytes.fromhex(t[2:].lower()) for t in targets])
    values_packed = _pack32([int(v).to_bytes(32, "big") for v in values])
    sig_hashes = _pack32([Web3.keccak(text=s or "") for s in signatures])
    calldata_hashes = _pack32([Web3.keccak(hexstr=c or "0x") for c in calldatas])
    return abi_encode(
        ["address", "bytes32", "bytes32", "bytes32", "bytes32", "bytes32"],
        [
            Web3.to_checksum_address(proposer),
            Web3.keccak(target_packed),
            Web3.keccak(values_packed),
            Web3.keccak(sig_hashes),
            Web3.keccak(calldata_hashes),
            Web3.keccak(text=description),
        ],
    )


def sponsorship_digest(encoded_prop: bytes, expiration: int) -> bytes:
    from eth_abi import encode as abi_encode

    struct_hash = Web3.keccak(PROPOSAL_TYPEHASH + encoded_prop + expiration.to_bytes(32, "big"))
    domain = Web3.keccak(
        abi_encode(
            ["bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, Web3.keccak(text="Nouns DAO"), 1, GOVERNOR],
        )
    )
    return Web3.keccak(b"\x19\x01" + domain + struct_hash)


def data_contract(web3: Web3):
    return web3.eth.contract(address=DATA_CONTRACT, abi=DATA_ABI)
