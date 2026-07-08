"""Nouns subgraph client. Read-only; every governance-relevant field comes from here."""

import hashlib
import json

import requests

from .config import SUBGRAPH_URL

PROPOSAL_FIELDS = """
  id
  title
  description
  status
  proposer { id }
  signers { id }
  targets
  values
  signatures
  calldatas
  createdTimestamp
  createdBlock
  lastUpdatedTimestamp
  startBlock
  endBlock
  updatePeriodEndBlock
  objectionPeriodEndBlock
  voteSnapshotBlock
  proposalThreshold
  quorumVotes
  forVotes
  againstVotes
  abstainVotes
  totalSupply
  executedBlock
  canceledBlock
  vetoedBlock
  queuedBlock
  clientId
"""


def query(gql: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        SUBGRAPH_URL,
        json={"query": gql, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"subgraph error: {body['errors']}")
    return body["data"]


def current_block() -> int:
    data = query("{ _meta { block { number } } }")
    return int(data["_meta"]["block"]["number"])


def fetch_proposals(first: int = 20, skip: int = 0, order: str = "desc") -> list[dict]:
    gql = f"""
    query($first: Int!, $skip: Int!) {{
      proposals(first: $first, skip: $skip, orderBy: createdBlock, orderDirection: {order}) {{
        {PROPOSAL_FIELDS}
      }}
    }}"""
    return query(gql, {"first": first, "skip": skip})["proposals"]


def fetch_proposal_range(from_id: int, to_id: int) -> list[dict]:
    gql = f"""
    query($from: BigInt!, $to: BigInt!) {{
      proposals(first: 1000, where: {{ id_gte: $from, id_lte: $to }}, orderBy: createdBlock, orderDirection: asc) {{
        {PROPOSAL_FIELDS}
      }}
    }}"""
    props = query(gql, {"from": str(from_id), "to": str(to_id)})["proposals"]
    # subgraph compares string IDs lexicographically ("97" sits inside "955".."980")
    return [p for p in props if from_id <= int(p["id"]) <= to_id]


def content_hash(prop: dict) -> str:
    """Pin verdicts to exact proposal content — props are editable in the updatable window."""
    material = json.dumps(
        {
            "description": prop.get("description", ""),
            "targets": prop.get("targets", []),
            "values": prop.get("values", []),
            "signatures": prop.get("signatures", []),
            "calldatas": prop.get("calldatas", []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def derive_outcome(prop: dict, chain_head: int) -> str:
    """Final outcome from subgraph state. Defeated props keep status ACTIVE forever,
    so voting-math derivation is required, not optional."""
    status = prop["status"]
    if status == "EXECUTED":
        return "EXECUTED"
    if status == "CANCELLED":
        return "CANCELLED"
    if status == "VETOED":
        return "VETOED"
    if status == "QUEUED":
        return "QUEUED"
    end_block = int(prop["endBlock"])
    objection_end = int(prop.get("objectionPeriodEndBlock") or 0)
    voting_over = chain_head > max(end_block, objection_end)
    if not voting_over:
        return "VOTING" if chain_head >= int(prop["startBlock"]) else "PENDING"
    for_votes = int(prop["forVotes"])
    against = int(prop["againstVotes"])
    quorum = int(prop["quorumVotes"])
    if for_votes <= against or for_votes < quorum:
        return "DEFEATED"
    return "SUCCEEDED_NOT_QUEUED"  # passed the vote but expired/never queued


def format_actions(prop: dict) -> str:
    """Human-readable transaction list. M0: no ABI decoding yet — signatures + ETH
    values + raw calldata prefix. The calldata is still shown so mismatch with prose
    is detectable; full decoding lands with the enricher."""
    lines = []
    targets = prop.get("targets") or []
    values = prop.get("values") or []
    sigs = prop.get("signatures") or []
    datas = prop.get("calldatas") or []
    for i, target in enumerate(targets):
        value_wei = int(values[i]) if i < len(values) and values[i] else 0
        eth = value_wei / 1e18
        sig = sigs[i] if i < len(sigs) else ""
        data = datas[i] if i < len(datas) else "0x"
        line = f"  {i + 1}. target={target}"
        if eth:
            line += f" value={eth:.4f} ETH"
        if sig:
            line += f" call={sig}"
        if data and data != "0x":
            line += f" calldata={data[:74]}{'…' if len(data) > 74 else ''}"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no onchain actions)"


CANDIDATE_FIELDS = """
  id
  proposer
  slug
  createdTimestamp
  lastUpdatedTimestamp
  canceled
  latestVersion {
    content {
      title
      description
      targets
      values
      signatures
      calldatas
      proposalIdToUpdate
      matchingProposalIds
      contentSignatures { signer { id } canceled expirationTimestamp }
    }
  }
"""


def fetch_candidates(first: int = 10) -> list[dict]:
    """Open candidates, newest activity first. Skips canceled and already-promoted."""
    gql = f"""
    query($first: Int!) {{
      proposalCandidates(first: $first, orderBy: lastUpdatedTimestamp, orderDirection: desc,
                         where: {{ canceled: false }}) {{
        {CANDIDATE_FIELDS}
      }}
    }}"""
    out = []
    for c in query(gql, {"first": first})["proposalCandidates"]:
        content = (c.get("latestVersion") or {}).get("content") or {}
        if content.get("matchingProposalIds"):
            continue  # already became a proposal
        out.append(c)
    return out


def candidate_content_hash(cand: dict) -> str:
    content = cand["latestVersion"]["content"]
    material = json.dumps(
        {k: content.get(k) for k in ("description", "targets", "values", "signatures", "calldatas")},
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def candidate_as_prop(cand: dict) -> dict:
    """Adapt a candidate to the shape the evaluator expects."""
    content = cand["latestVersion"]["content"]
    return {
        "id": f"candidate {cand['slug']}",
        "proposer": {"id": cand["proposer"]},
        "title": content.get("title") or cand["slug"],
        "description": content.get("description"),
        "targets": content.get("targets") or [],
        "values": content.get("values") or [],
        "signatures": content.get("signatures") or [],
        "calldatas": content.get("calldatas") or [],
    }
