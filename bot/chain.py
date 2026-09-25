"""Onchain layer: the governor contract and the vote-only hot wallet.

The key held here can cast votes. It can never transfer the Noun — delegation
is not custody. Worst case on compromise: bad votes until re-delegation.
"""

import math
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
    {
        "name": "cancelSig",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "sig", "type": "bytes"}],
        "outputs": [],
    },
]

SUPPORT = {"AGAINST": 0, "FOR": 1, "ABSTAIN": 2}
CLIENT_ID = int(os.environ.get("NOUNS_CLIENT_ID", "0"))

# Conservative ceiling for castRefundableVoteWithReason with the bot's (often
# long) reasons. web3's build_transaction runs eth_estimateGas, which caps the
# estimate at balance/maxFeePerGas — if the wallet is too thin that cap lands
# below what the call actually needs and the tx reverts with no data at all
# (looked like a contract bug; was really an empty wallet). 250k is well above
# normal usage; bump via env if a reason gets exotic.
VOTE_GAS_BUDGET = int(os.environ.get("VOTE_GAS_BUDGET", "250000"))


def w3() -> Web3:
    return Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))


def governor(web3: Web3):
    return web3.eth.contract(address=GOVERNOR, abi=GOVERNOR_ABI)


def format_eth(wei: int) -> str:
    """Human-friendly ETH amount for Telegram messages — ~2 significant
    figures for dust-sized balances (so 121465625553468 wei reads as
    '0.00012 ETH', not an 18-decimal wall of digits) and a plain 4 decimals
    otherwise. One formatter so every message it's used in can't drift from
    the others: 0.00012 ETH / 0.0201 ETH / 1.2345 ETH."""
    eth = float(Web3.from_wei(wei, "ether"))
    if eth != 0 and abs(eth) < 0.01:
        exponent = math.floor(math.log10(abs(eth)))
        decimals = max(0, 1 - exponent)  # 2 significant figures
        return f"{eth:.{decimals}f} ETH"
    return f"{eth:.4f} ETH"


def vote_max_fee_per_gas(web3: Web3, base_fee: int | None = None) -> int:
    """The fee build_vote_tx pays — factored out so the live cost estimate
    below can never drift from what a cast actually sends onchain. Pass
    base_fee to reuse a block read the caller already made (e.g. /gas)."""
    base = base_fee if base_fee is not None else web3.eth.get_block("latest")["baseFeePerGas"]
    tip = web3.to_wei(1, "gwei")
    return base * 2 + tip


def build_vote_tx(web3: Web3, sender: str, prop_id: int, vote: str, reason: str) -> dict:
    fn = governor(web3).functions.castRefundableVoteWithReason(
        prop_id, SUPPORT[vote], reason, CLIENT_ID
    )
    return fn.build_transaction(
        {
            "from": Web3.to_checksum_address(sender),
            "nonce": web3.eth.get_transaction_count(Web3.to_checksum_address(sender)),
            "maxFeePerGas": vote_max_fee_per_gas(web3),
            "maxPriorityFeePerGas": web3.to_wei(1, "gwei"),
            "chainId": 1,
        }
    )


def vote_cost_estimate(web3: Web3, sender: str) -> tuple[int, int]:
    """(balance_wei, cost_per_vote_wei) for the bot wallet at current gas.

    Nouns refunds vote gas, but only *after* the vote lands onchain, and the
    refund doesn't cover the full cost — so the wallet must front the gas
    upfront, out of its own balance, every time, and that balance drains a
    little on every vote until it's topped up."""
    balance = web3.eth.get_balance(Web3.to_checksum_address(sender))
    cost_per_vote = VOTE_GAS_BUDGET * vote_max_fee_per_gas(web3)
    return balance, cost_per_vote


def gas_status(web3: Web3, sender: str) -> tuple[int, int, int]:
    """(balance_wei, cost_per_vote_wei, base_fee_wei) for /gas — the same fee
    formula as vote_cost_estimate, sharing one block read, plus the raw base
    fee so it can be shown in gwei."""
    base_fee = web3.eth.get_block("latest")["baseFeePerGas"]
    cost_per_vote = VOTE_GAS_BUDGET * vote_max_fee_per_gas(web3, base_fee=base_fee)
    balance = web3.eth.get_balance(Web3.to_checksum_address(sender))
    return balance, cost_per_vote, base_fee


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
        "name": "sendCandidateFeedback",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "proposer", "type": "address"},
            {"name": "slug", "type": "string"},
            {"name": "support", "type": "uint8"},
            {"name": "reason", "type": "string"},
        ],
        "outputs": [],
    },
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
    },
    {
        "name": "SignatureAdded",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "signer", "type": "address", "indexed": True},
            {"name": "sig", "type": "bytes", "indexed": False},
            {"name": "expirationTimestamp", "type": "uint256", "indexed": False},
            {"name": "proposer", "type": "address", "indexed": False},
            {"name": "slug", "type": "string", "indexed": False},
            {"name": "proposalIdToUpdate", "type": "uint256", "indexed": False},
            {"name": "encodedPropHash", "type": "bytes32", "indexed": False},
            {"name": "sigDigest", "type": "bytes32", "indexed": False},
            {"name": "reason", "type": "string", "indexed": False},
        ],
    },
]

PROPOSAL_TYPEHASH = Web3.keccak(
    text="Proposal(address proposer,address[] targets,uint256[] values,string[] signatures,bytes[] calldatas,string description,uint256 expiry)"
)
UPDATE_PROPOSAL_TYPEHASH = Web3.keccak(
    text="UpdateProposal(uint256 proposalId,address proposer,address[] targets,uint256[] values,string[] signatures,bytes[] calldatas,string description,uint256 expiry)"
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


def sponsorship_digest(encoded_prop: bytes, expiration: int, proposal_id_to_update: int = 0) -> bytes:
    from eth_abi import encode as abi_encode

    typehash = UPDATE_PROPOSAL_TYPEHASH if proposal_id_to_update else PROPOSAL_TYPEHASH
    struct_hash = Web3.keccak(typehash + encoded_prop + expiration.to_bytes(32, "big"))
    domain = Web3.keccak(
        abi_encode(
            ["bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, Web3.keccak(text="Nouns DAO"), 1, GOVERNOR],
        )
    )
    return Web3.keccak(b"\x19\x01" + domain + struct_hash)


def data_contract(web3: Web3):
    return web3.eth.contract(address=DATA_CONTRACT, abi=DATA_ABI)


def proposal_state(web3: Web3, prop_id: int) -> int:
    """Raw NounsDAOTypes.ProposalState enum from the governor."""
    return int(governor(web3).functions.state(prop_id).call())
