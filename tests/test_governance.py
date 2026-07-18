import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import chain, db, evaluator, poller, subgraph


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

    def test_failed_voting_open_delivery_is_retried(self):
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
            patch.object(poller.telegram, "send_message", side_effect=[False, True]) as send,
        ):
            poller.ingest_and_evaluate(None, conn, head=111)
            poller.ingest_and_evaluate(None, conn, head=112)
            poller.ingest_and_evaluate(None, conn, head=113)

        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(send.call_count, 2)

    def test_against_schedule_remains_open_during_objection_period(self):
        conn = memory_db()
        prop = proposal(objectionPeriodEndBlock="250")
        db.upsert_proposal(conn, prop, subgraph.content_hash(prop), "OBJECTION")
        db.upsert_cast(
            conn, 984, state="scheduled", vote="AGAINST", reason="Reason", cast_block_target=160
        )
        poller.check_schedule(conn, head=201)
        self.assertEqual(db.get_cast(conn, 984)["state"], "scheduled")


class CandidateDedupeTests(unittest.TestCase):
    def test_candidate_change_context_is_compact_and_specific(self):
        old = subgraph.candidate_as_prop(candidate("same", "Budget is 10 ETH."))
        new = subgraph.candidate_as_prop(candidate("same", "Budget is 12 ETH."))
        context = evaluator.candidate_change_context(old, new)
        self.assertIn("-Budget is 10 ETH.", context)
        self.assertIn("+Budget is 12 ETH.", context)
        self.assertIn("Onchain actions unchanged", context)
        self.assertLessEqual(len(context), 6000)

    def test_duplicate_backfill_is_idempotent(self):
        conn = memory_db()
        newest = candidate("new")
        newest["lastUpdatedTimestamp"] = "2"
        old = candidate("old")
        for cand in (newest, old):
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
        self.assertEqual([row["superseded"] for row in rows], [0, 1])
        row = db.upsert_candidate(
            conn,
            old["id"],
            logical_id="proposal-update:984",
            raw=json.dumps(old),
        )
        self.assertEqual(row["cand_id"], old["id"])
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) c FROM candidates WHERE logical_id='proposal-update:984' "
                "AND superseded=0"
            ).fetchone()["c"],
            1,
        )

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

    def test_sponsored_edit_keeps_revocable_signature_and_summarizes_change(self):
        conn = memory_db()
        old = candidate("same", "Budget is 10 ETH.")
        new = candidate("same", "Budget is 12 ETH.")
        old_hash = subgraph.candidate_content_hash(old)
        db.upsert_candidate(
            conn,
            old["id"],
            logical_id=subgraph.candidate_logical_id(old),
            title="Proposal update",
            content_hash=old_hash,
            constitution_rev=db.constitution_fingerprint(),
            verdict_json=json.dumps({"vote": "FOR", "reason": "Old reason"}),
            raw=json.dumps(old),
            sponsor_state="sponsored",
            sig_tx="0xoldtx",
            sig_bytes="0x1234",
            signed_content_hash=old_hash,
        )
        verdict = SimpleNamespace(
            vote="AGAINST",
            confidence=0.9,
            clauses_cited=["IV.1"],
            reason="The higher budget is not justified.",
            suggestions=[],
            flags=[],
            requires_human_review=False,
            tldr="Raises the requested budget to 12 ETH.",
            change_summary="Budget increased from 10 ETH to 12 ETH.",
            change_materiality="minor",
        )
        with (
            patch.object(subgraph, "fetch_candidates", return_value=[new]),
            patch.object(subgraph, "fetch_proposals_by_ids", return_value=[proposal()]),
            patch.object(poller, "evaluate", return_value=(verdict, None)) as evaluate,
            patch.object(poller.telegram, "send_message") as send,
        ):
            poller.ingest_candidates(None, conn, head=50)

        row = db.get_candidate_by_num(conn, 1)
        self.assertEqual(row["sponsor_state"], "stale")
        self.assertEqual(row["sig_bytes"], "0x1234")
        self.assertEqual(
            evaluate.call_args.kwargs["previous_prop"]["description"], "Budget is 10 ETH."
        )
        card = send.call_args.args[0]
        self.assertIn("TL;DR: Raises the requested budget", card)
        self.assertIn("changed (minor): Budget increased", card)
        self.assertIn("/revoke c1", card)

    def test_revoke_command_invalidates_stored_signature(self):
        conn = memory_db()
        cand = candidate("same")
        db.upsert_candidate(
            conn,
            cand["id"],
            logical_id=subgraph.candidate_logical_id(cand),
            title="Proposal update",
            content_hash=subgraph.candidate_content_hash(cand),
            constitution_rev=db.constitution_fingerprint(),
            verdict_json=json.dumps({"vote": "FOR", "reason": "Reason"}),
            raw=json.dumps(cand),
            sponsor_state="stale",
            sig_tx="0xoldtx",
            sig_bytes="0x1234",
        )
        with patch(
            "bot.executor.revoke_candidate_signature", return_value="0xrevoked"
        ) as revoke:
            reply = poller.run_command(conn, "revoke", ["c1"])
        revoke.assert_called_once_with("0x1234")
        row = db.get_candidate_by_num(conn, 1)
        self.assertEqual(row["sponsor_state"], "revoked")
        self.assertEqual(row["revoke_tx"], "0xrevoked")
        self.assertIn("revoked sponsorship", reply)

    def test_revoke_recovers_pre_migration_signature_from_receipt(self):
        conn = memory_db()
        cand = candidate("legacy")
        db.upsert_candidate(
            conn,
            cand["id"],
            logical_id=subgraph.candidate_logical_id(cand),
            title="Proposal update",
            content_hash=subgraph.candidate_content_hash(cand),
            constitution_rev=db.constitution_fingerprint(),
            verdict_json=json.dumps({"vote": "FOR", "reason": "Reason"}),
            raw=json.dumps(cand),
            sponsor_state="stale",
            sig_tx="0xlegacy",
        )
        with (
            patch(
                "bot.executor.recover_candidate_signature",
                return_value=("0xabcd", 9999999999),
            ) as recover,
            patch(
                "bot.executor.revoke_candidate_signature", return_value="0xrevoked"
            ) as revoke,
        ):
            reply = poller.run_command(conn, "revoke", ["c1"])
        recover.assert_called_once_with("0xlegacy")
        revoke.assert_called_once_with("0xabcd")
        row = db.get_candidate_by_num(conn, 1)
        self.assertEqual(row["sig_bytes"], "0xabcd")
        self.assertEqual(row["sponsor_state"], "revoked")
        self.assertIn("revoked sponsorship", reply)

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

    def test_canceled_update_is_not_actionable(self):
        target = proposal(status="CANCELLED")
        can_sponsor, note = poller.update_sponsorship_status(
            candidate("update"),
            target,
            50,
            "0x0000000000000000000000000000000000000002",
        )
        self.assertFalse(can_sponsor)
        self.assertIn("window has closed", note)


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

    def test_governor_abi_exposes_signature_cancellation(self):
        self.assertIn("cancelSig", {entry.get("name") for entry in chain.GOVERNOR_ABI})


if __name__ == "__main__":
    unittest.main()
