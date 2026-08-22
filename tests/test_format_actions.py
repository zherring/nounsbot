"""Tests for bot.subgraph.format_actions — real ABI decoding, no truncation,
loud decode failures. No network access: the token cache is seeded directly."""

from eth_abi import encode as abi_encode

from bot.subgraph import _split_top_level, format_actions

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

NOUN_IDS = [
    11, 26, 82, 89, 279, 408, 548, 559, 801, 861, 1914, 1917, 1929, 1933,
    1942, 1950, 1954, 1957, 1958, 1969, 1980, 1983, 1988, 1989,
]


def test_pull_decodes_all_24_noun_ids():
    calldata = "0x" + abi_encode(["uint256[]"], [NOUN_IDS]).hex()
    prop = {
        "targets": ["0x89ec417fa93f02926bf9c28316da4e7d0f28089b"],
        "values": ["0"],
        "signatures": ["pull(uint256[])"],
        "calldatas": [calldata],
    }
    out = format_actions(prop)
    expected_list = "[" + ", ".join(str(n) for n in NOUN_IDS) + "]"
    assert expected_list in out
    assert "length=24" in out
    assert "UNDECODED" not in out


def test_transfer_renders_human_readable_usdc_amount():
    recipient = "0x98f5d68b1f8e0d5e6c1c1d1a1234567890abcdef"
    amount_raw = 250000_000000  # 250000.0 USDC at 6 decimals
    calldata = "0x" + abi_encode(["address", "uint256"], [recipient, amount_raw]).hex()
    prop = {
        "targets": [USDC],
        "values": ["0"],
        "signatures": ["transfer(address,uint256)"],
        "calldatas": [calldata],
    }
    out = format_actions(prop)
    assert "250000.0 USDC" in out
    assert "USDC" in out
    assert "UNDECODED" not in out


def test_zero_address_noop_is_explicit_and_not_truncated_hex():
    prop = {
        "targets": [ZERO_ADDRESS],
        "values": ["0"],
        "signatures": [""],
        "calldatas": ["0x"],
    }
    out = format_actions(prop)
    assert "NO-OP" in out
    assert "no calldata, no value" in out
    assert "calldata=" not in out


def test_tuple_signature_type_splitter():
    from bot.subgraph import _parse_signature

    name, types = _parse_signature(
        "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
    )
    assert name == "exactInputSingle"
    assert types == ["(address,address,uint24,address,uint256,uint256,uint160)"]
    assert _split_top_level("address,uint256,bool") == ["address", "uint256", "bool"]
    assert _split_top_level("(uint256,address),bool") == ["(uint256,address)", "bool"]


def test_malformed_calldata_for_known_signature_renders_undecoded_without_raising():
    prop = {
        "targets": [USDC],
        "values": ["0"],
        "signatures": ["transfer(address,uint256)"],
        # too short to decode (address,uint256) — must not raise
        "calldatas": ["0x1234"],
    }
    out = format_actions(prop)
    assert "UNDECODED" in out
    assert "agent could not decode" in out


def test_known_selector_with_empty_signature_decodes_delegate():
    # selector=0x5c19a95c is delegate(address) — full calldata (selector + args),
    # empty signature, as it appears onchain for prop 982.
    delegate_to = "0x000000000000000000000000000000000000dead"
    calldata = "0x5c19a95c" + abi_encode(["address"], [delegate_to]).hex()
    prop = {
        "targets": ["0x9c8ff314c9bc7f6e59a9d9225fb22946427edc03"],
        "values": ["0"],
        "signatures": [""],
        "calldatas": [calldata],
    }
    out = format_actions(prop)
    assert "UNDECODED" not in out
    assert "delegate(" in out
    assert delegate_to in out.lower()


def test_unknown_selector_renders_undecoded_with_not_evidence_wording():
    calldata = "0xdeadbeef" + "00" * 32
    prop = {
        "targets": ["0x9c8ff314c9bc7f6e59a9d9225fb22946427edc03"],
        "values": ["0"],
        "signatures": [""],
        "calldatas": [calldata],
    }
    out = format_actions(prop)
    assert "UNDECODED" in out
    assert "0xdeadbeef" in out
    assert "NOT evidence" in out
    assert "not treat it as a mismatch" in out.lower()
