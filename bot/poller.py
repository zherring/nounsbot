"""The agent loop: ingest -> evaluate -> notify -> (ratify window) -> cast.

Optimistic execution per PRD §6.4: every verdict is reported with its scheduled
cast time; unflagged verdicts default-fire at ~65% through the voting window if
(a) not held, and (b) the verdict is >= 24h old. Flagged verdicts never
default-fire — they need an explicit /cast or /override. A held prop whose
window closes simply doesn't vote, and the miss is logged.

Paper mode (no BOT_PRIVATE_KEY): everything runs except the final send.
Run: python -m bot.poller
"""

import time
import traceback
from datetime import datetime, timezone

import anthropic

from . import db, publisher, subgraph, telegram
from .config import ANTHROPIC_MODEL, POLL_INTERVAL_SECONDS
from .evaluator import evaluate

CAST_AT_FRACTION = 0.65  # through the voting window
RATIFY_FLOOR_SECONDS = 24 * 3600
VOTES = {"for": "FOR", "against": "AGAINST", "abstain": "ABSTAIN"}


def latest_verdict(conn, prop_id: int):
    return conn.execute(
        "SELECT * FROM verdicts WHERE prop_id=? ORDER BY created_at DESC LIMIT 1", (prop_id,)
    ).fetchone()


def verdict_card(prop, outcome, verdict, cast_target_block, head):
    blocks_away = max(0, cast_target_block - head)
    eta_h = blocks_away * 12 / 3600
    flags = f"\n⚑ {', '.join(verdict.flags)}" if verdict.flags else ""
    if verdict.requires_human_review:
        firing = f"⏸ WILL NOT auto-cast (flagged) — /cast {prop['id']} to ratify, /override to change"
    else:
        firing = f"🕒 auto-casts in ~{eta_h:.0f}h (block {cast_target_block}) — /hold {prop['id']} to stop"
    return (
        f"📜 Prop {prop['id']}: {prop.get('title', '(untitled)')}\n"
        f"state: {outcome}\n"
        f"verdict: {verdict.vote} (conf {verdict.confidence:.2f}) · clauses {', '.join(verdict.clauses_cited)}\n"
        f"{verdict.reason}{flags}\n{firing}"
    )


def ingest_and_evaluate(client, conn, head: int) -> None:
    rev = db.constitution_rev()
    for prop in subgraph.fetch_proposals(first=15):
        outcome = subgraph.derive_outcome(prop, head)
        if outcome not in {"PENDING", "VOTING"}:
            continue
        pid = int(prop["id"])
        chash = subgraph.content_hash(prop)
        prior = conn.execute("SELECT content_hash FROM proposals WHERE id=?", (pid,)).fetchone()
        edited = prior and prior["content_hash"] != chash
        db.upsert_proposal(conn, prop, chash, outcome)
        if db.get_verdict(conn, pid, chash, rev, ANTHROPIC_MODEL):
            continue

        print(f"evaluating prop {pid} ({prop.get('title', '')[:60]})…")
        verdict, usage = evaluate(client, prop)
        db.save_verdict(conn, pid, chash, rev, ANTHROPIC_MODEL, verdict, usage)

        start, end = int(prop["startBlock"]), int(prop["endBlock"])
        target = start + int((end - start) * CAST_AT_FRACTION)
        existing = db.get_cast(conn, pid)
        state = existing["state"] if existing else "scheduled"
        if state not in {"held", "cast"}:  # edits don't un-hold, never re-cast
            db.upsert_cast(conn, pid, state="scheduled", vote=verdict.vote,
                           reason=verdict.reason, cast_block_target=target)
        prefix = "✏️ PROPOSAL EDITED — re-evaluated:\n" if edited else ""
        card = prefix + verdict_card(prop, outcome, verdict, target, head)
        print("\n" + card + "\n")
        telegram.send_message(card)


def handle_commands(conn) -> None:
    offset = int(db.kv_get(conn, "tg_offset", "0"))
    for update in telegram.get_updates(offset + 1):
        offset = max(offset, update["update_id"])
        parsed = telegram.parse_command(update)
        if not parsed:
            continue
        cmd, args = parsed
        try:
            reply = run_command(conn, cmd, args)
        except Exception as exc:
            reply = f"⚠️ {cmd} failed: {exc}"
        if reply:
            telegram.send_message(reply)
    db.kv_set(conn, "tg_offset", str(offset))


def run_command(conn, cmd: str, args: list[str]) -> str:
    if cmd == "status":
        rows = conn.execute(
            "SELECT c.*, p.title, p.status FROM casts c JOIN proposals p ON p.id=c.prop_id "
            "WHERE c.state IN ('scheduled','held') ORDER BY c.prop_id"
        ).fetchall()
        if not rows:
            return "nothing pending — all quiet"
        lines = []
        for r in rows:
            v = latest_verdict(conn, r["prop_id"])
            flag = " ⚑review" if v and v["requires_human_review"] else ""
            lines.append(f"{r['prop_id']}: {r['vote']} [{r['state']}]{flag} → block {r['cast_block_target']} — {r['title'][:40]}")
        return "\n".join(lines)

    if cmd in {"hold", "release", "cast", "override"}:
        if not args:
            return f"usage: /{cmd} <prop_id> …"
        pid = int(args[0])
        cast_row = db.get_cast(conn, pid)
        if not cast_row:
            return f"prop {pid}: nothing scheduled"
        if cast_row["state"] == "cast":
            return f"prop {pid}: already cast ({cast_row['tx_hash']})"

        if cmd == "hold":
            db.upsert_cast(conn, pid, state="held")
            return f"prop {pid} HELD — will not cast. /release {pid} to resume; if the window closes, it just doesn't vote."
        if cmd == "release":
            db.upsert_cast(conn, pid, state="scheduled")
            return f"prop {pid} released — back on schedule."
        if cmd == "override":
            if len(args) < 3 or args[1].lower() not in VOTES:
                return "usage: /override <prop> <for|against|abstain> <reason — mandatory>"
            vote, reason = VOTES[args[1].lower()], " ".join(args[2:])
            db.upsert_cast(conn, pid, vote=vote, reason=reason, override_by="human", state="scheduled")
            return f"prop {pid} overridden to {vote} — reason logged, casts on schedule."
        if cmd == "cast":
            return do_cast(conn, pid, forced=True)
    return f"unknown command /{cmd} — try /status /hold /release /override /cast"


def do_cast(conn, pid: int, forced: bool = False) -> str:
    from .executor import bot_address, cast_vote

    row = db.get_cast(conn, pid)
    vote, reason = row["vote"], row["reason"]
    if not bot_address():
        db.upsert_cast(conn, pid, state="skipped")
        return f"📝 paper mode: would cast {vote} on prop {pid} — no key configured"
    try:
        tx = cast_vote(pid, vote, reason)
    except Exception as exc:
        telegram.send_message(f"🚨 cast FAILED for prop {pid}: {exc}")
        raise
    db.upsert_cast(conn, pid, state="cast", tx_hash=tx)
    who = "forced by /cast" if forced else "auto-fired"
    return f"🗳 cast {vote} on prop {pid} ({who})\nreason: {reason}\ntx: https://etherscan.io/tx/0x{tx.removeprefix('0x')}"


def check_schedule(conn, head: int) -> None:
    rows = conn.execute("SELECT * FROM casts WHERE state IN ('scheduled','held')").fetchall()
    for row in rows:
        pid = row["prop_id"]
        prop_row = conn.execute("SELECT raw FROM proposals WHERE id=?", (pid,)).fetchone()
        if not prop_row:
            continue
        import json as _json

        prop = _json.loads(prop_row["raw"])
        end = int(prop["endBlock"])
        if head > end:
            db.upsert_cast(conn, pid, state="missed")
            telegram.send_message(
                f"⏹ prop {pid} window closed without a cast "
                f"({'held' if row['state'] == 'held' else 'not ratified'}) — logged as a public miss"
            )
            continue
        if row["state"] == "held" or head < row["cast_block_target"]:
            continue

        verdict = latest_verdict(conn, pid)
        flagged = verdict and verdict["requires_human_review"] and not row["override_by"]
        verdict_age = (
            (datetime.now(timezone.utc) - datetime.fromisoformat(verdict["created_at"])).total_seconds()
            if verdict else 0
        )
        if flagged:
            if head % 300 < 5:  # gentle reminder roughly hourly
                telegram.send_message(f"⏳ prop {pid} is past cast time but flagged — /cast {pid} or /override, else it won't vote")
            continue
        if verdict_age < RATIFY_FLOOR_SECONDS:
            continue  # 24h floor: too fresh to default-fire
        msg = do_cast(conn, pid)
        telegram.send_message(msg)
        print(msg)


def main() -> None:
    import os

    client = anthropic.Anthropic()
    conn = db.connect()
    if os.environ.get("PORT"):  # Railway public networking
        from . import web

        web.start(int(os.environ["PORT"]))
    from .executor import bot_address

    addr = bot_address()
    mode = f"live, casting from {addr}" if addr else "paper mode (no key)"
    banner = f"nounsbot loop up — {mode}, every {POLL_INTERVAL_SECONDS}s, judge {ANTHROPIC_MODEL}"
    print(banner)
    telegram.send_message(f"🤖 {banner}")
    while True:
        try:
            head = subgraph.current_block()
            ingest_and_evaluate(client, conn, head)
            handle_commands(conn)
            check_schedule(conn, head)
            publisher.publish(conn)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
