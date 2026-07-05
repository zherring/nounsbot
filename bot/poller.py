"""M0 live loop: watch for new/edited proposals, evaluate, report. No keys, no casting —
the human votes by hand. Run: python -m bot.poller"""

import time
import traceback

import anthropic

from . import db, subgraph
from .config import ANTHROPIC_MODEL, POLL_INTERVAL_SECONDS
from .telegram import send_message


def verdict_card(prop: dict, outcome: str, verdict) -> str:
    flags = f"\n⚑ flags: {', '.join(verdict.flags)}" if verdict.flags else ""
    review = "\n👤 REQUIRES HUMAN REVIEW — do not treat as final" if verdict.requires_human_review else ""
    return (
        f"📜 Prop {prop['id']}: {prop.get('title', '(untitled)')}\n"
        f"state: {outcome}\n"
        f"verdict: {verdict.vote} (confidence {verdict.confidence:.2f})\n"
        f"clauses: {', '.join(verdict.clauses_cited)}\n"
        f"reason: {verdict.reason}{flags}{review}\n"
        f"(M0 paper mode — cast this vote by hand)"
    )


def tick(client: anthropic.Anthropic, conn) -> None:
    head = subgraph.current_block()
    rev = db.constitution_rev()
    for prop in subgraph.fetch_proposals(first=15):
        outcome = subgraph.derive_outcome(prop, head)
        if outcome not in {"PENDING", "VOTING"}:
            continue
        chash = subgraph.content_hash(prop)
        db.upsert_proposal(conn, prop, chash, outcome)
        if db.get_verdict(conn, int(prop["id"]), chash, rev, ANTHROPIC_MODEL):
            continue  # already evaluated this exact content under this constitution
        verdict, usage = evaluate_with_log(client, prop)
        db.save_verdict(conn, int(prop["id"]), chash, rev, ANTHROPIC_MODEL, verdict, usage)
        card = verdict_card(prop, outcome, verdict)
        print("\n" + card + "\n")
        send_message(card)


def evaluate_with_log(client, prop):
    from .evaluator import evaluate

    print(f"evaluating prop {prop['id']} ({prop.get('title', '')[:60]})…")
    return evaluate(client, prop)


def main() -> None:
    client = anthropic.Anthropic()
    conn = db.connect()
    print(f"nounsbot M0 poller — every {POLL_INTERVAL_SECONDS}s, model {ANTHROPIC_MODEL}")
    while True:
        try:
            tick(client, conn)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
