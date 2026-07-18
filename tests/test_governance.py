import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import chain, db, poller, subgraph


def proposal(**overrides):
    base = {
        "id": "984",
        "title": "Test proposal",
        "description": "Test",
        "status": "ACTIVE",
        "proposer": {"id": "0x0000000000000000000000000000000000000001"},
        "signers": [{"id": "0x0000000000000000000000000000000000000002"}],
        "targets": [],
        "values": [],
        "signatures": [],
        "calldatas": [],
        "updatePeriodEndBlock": "100",
        "startBlock": "110",
        "endBlock": "200",
        "objectionPeriodEndBlock": "0",
        "forVotes": "10",
        "againstVotes": "1",
        "quorumVotes": "5",
    }
    base.update(overrides)
    return base


def candidate(slug: str, description: str = "same content"):
    return {
        "id": f"0x0000000000000000000000000000000000000001-{slug}",
        "proposer": "0x0000000000000000000000000000000000000001",
        "slug": slug,
        "lastUpdatedTimestamp": "1",
        "latestVersion": {
            "content": {
                "title": "Proposal update",
                "description": description,
                "targets": [],
                "values": [],
                "signatures": [],
                "calldatas": [],
                "proposalIdToUpdate": "984",
                "matchingProposalIds": [],
                "contentSignatures": [],
            }
        },
    }


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db.migrate(conn)
    return conn


class ProposalTimingTests(unittest.TestCase):
    def test_contract_phase_boundaries(self):
        prop = proposal()
        cases = [
            (100, "UPDATABLE", "UPDATABLE"),
            (101, "PENDING", "PENDING"),
            (110, "PENDING", "PENDING"),
            (111, "ACTIVE", "VOTING"),
            (200, "ACTIVE", "VOTING"),
            (201, "CLOSED", "SUCCEEDED_NOT_QUEUED"),
        ]
        for head, phase, outcome in cases:
            with self.subTest(head=head):
                self.assertEqual(subgraph.derive_phase(prop, head), phase)
                self.assertEqual(subgraph.derive_outcome(prop, head), outcome)

    def test_flagged_card_only_offers_cast_after_voting_opens(self):
        verdict = SimpleNamespace(
            vote="FOR",
            confidence=0.8,
            clauses_cited=["I.1"],
            reason="Reason",
            suggestions=[],
            flags=["review"],
            requires_human_review=True,
        )
        early = poller.verdict_card(proposal(), "PENDING", verdict, 160, 110)
        active = poller.verdict_card(proposal(), "VOTING", verdict, 160, 111)
        self.assertNotIn("/cast 984", early)
        self.assertIn("voting opens at block 111", early)
        self.assertIn("/cast 984 to ratify", active)

    def test_manual_cast_is_blocked_before_active(self):
        conn = memory_db()
        prop = proposal()
        db.upsert_proposal(conn, prop, subgraph.content_hash(prop), "PENDING")
        db.upsert_cast(
            conn, 984, state="scheduled", vote="FOR", reason="Reason", cast_block_target=160
        )
        reply = poller.do_cast(conn, 984, forced=True, head=110)
        self.assertIn("voting opens at block 111", reply)
        self.assertIn("no vote sent", reply)
        self.assertEqual(db.get_cast(conn, 984)["state"], "scheduled")

    def test_cached_verdict_is_prompted_when_voting_opens(self):
        conn = memory_db()
        prop = proposal()
        verdict = SimpleNamespace(
            vote="FOR",
            confidence=0.8,
            clauses_cited=["I.1"],
            reason="Reason",
            suggestions=[],
            flags=["review"],
            requires_human_review=True,
        )
        with (
            patch.object(subgraph, "fetch_proposals", return_value=[prop]),
            patch.object(poller, "evaluate", return_value=(verdict, None)) as evaluate,
            patch.object(poller.telegram, "send_message") as send,
        ):
            poller.ingest_and_evaluate(None, conn, head=110)
            poller.ingest_and_evaluate(None, conn, head=111)
            poller.ingest_and_evaluate(None, conn, head=112)

        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("/cast 984", send.call_args_list[0].args[0])
        self.assertIn("🟢 VOTING OPEN", send.call_args_list[1].args[0])
        self.assertIn("/cast 984", send.call_args_list[1].args[0])


class CandidateDedupeTests(unittest.TestCase):
    def test_duplicate_backfill_is_idempotent(self):
        conn = memory_db()
        for cand in (candidate("old"), candidate("new")):
            conn.execute(
                "INSERT INTO candidates (cand_id, raw, updated_at) VALUES (?, ?, '')",
                (cand["id"], json.dumps(cand)),
            )
        conn.commit()
        db.migrate(conn)
        db.migrate(conn)
        rows = conn.execute(
            "SELECT num, superseded FROM candidates WHERE logical_id='proposal-update:984' "
            "ORDER BY num"
        ).fetchall()
        self.assertEqual([row["superseded"] for row in rows], [1, 0])

    def test_fetch_collapses_update_slugs_before_limit(self):
        rows = [candidate("new"), candidate("old"), candidate("other")]
        rows[2]["latestVersion"]["content"]["proposalIdToUpdate"] = "985"
        with patch.object(subgraph, "query", return_value={"proposalCandidates": rows}) as query:
            result = subgraph.fetch_candidates(first=2)
        self.assertEqual([c["slug"] for c in result], ["new", "other"])
        self.assertEqual(query.call_args.args[1]["first"], 10)

    def test_reposted_identical_update_reuses_one_row_and_verdict(self):
        conn = memory_db()
        first, repost = candidate("first"), candidate("repost")
        target = proposal()
        verdict = SimpleNamespace(
            vote="FOR",
            confidence=0.8,
            clauses_cited=["I.1"],
            reason="Reason",
            suggestions=[],
            flags=[],
            requires_human_review=False,
        )
        with (
            patch.object(subgraph, "fetch_candidates", side_effect=[[first], [repost]]),
            patch.object(subgraph, "fetch_proposals_by_ids", return_value=[target]),
            patch.object(poller, "evaluate", return_value=(verdict, None)) as evaluate,
            patch.object(poller.telegram, "send_message") as send,
        ):
            poller.ingest_candidates(None, conn, head=50)
            poller.ingest_candidates(None, conn, head=50)

        rows = conn.execute(
            "SELECT * FROM candidates WHERE superseded=0 ORDER BY num"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cand_id"], repost["id"])
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(send.call_count, 1)

    def test_update_card_does_not_offer_sponsorship_to_non_signer(self):
        verdict = SimpleNamespace(
            vote="FOR",
            confidence=0.8,
            clauses_cited=["I.1"],
            reason="Reason",
            suggestions=[],
            flags=[],
            requires_human_review=False,
        )
        can_sponsor, note = poller.update_sponsorship_status(
            candidate("update"),
            proposal(),
            50,
            "0x0000000000000000000000000000000000000003",
        )
        card = poller.candidate_card(1, candidate("update"), verdict, 0, can_sponsor, note)
        self.assertFalse(can_sponsor)
        self.assertNotIn("/sponsor", card)
        self.assertIn("only prop 984's original signer(s)", card)


class UpdateSignatureTests(unittest.TestCase):
    def test_update_digest_uses_update_typehash(self):
        encoded = bytes.fromhex("11" * 32)
        expiry = 123
        new_digest = chain.sponsorship_digest(encoded, expiry)
        update_digest = chain.sponsorship_digest(
            (984).to_bytes(32, "big") + encoded, expiry, proposal_id_to_update=984
        )
        self.assertNotEqual(new_digest, update_digest)
        self.assertNotEqual(chain.PROPOSAL_TYPEHASH, chain.UPDATE_PROPOSAL_TYPEHASH)


if __name__ == "__main__":
    unittest.main()
