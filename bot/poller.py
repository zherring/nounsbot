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
from urllib.parse import quote

import anthropic

import json

from . import db, publisher, subgraph, telegram
from .evaluator import compose_reason
from .config import (
    ANTHROPIC_MODEL,
    INGEST_INTERVAL_SECONDS,
    MAX_EVALS_PER_DAY,
    MAX_EVALS_PER_PROP_PER_DAY,
    POLL_INTERVAL_SECONDS,
)


def evals_last_24h(conn, prop_id: int | None = None) -> int:
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if prop_id is None:
        row = conn.execute("SELECT COUNT(*) c FROM verdicts WHERE created_at > ?", (cutoff,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM verdicts WHERE prop_id=? AND created_at > ?", (prop_id, cutoff)
        ).fetchone()
    return row["c"]


def spend_guard_alert(conn, key: str, message: str) -> None:
    """Alert once per day per guard, not every tick."""
    today = datetime.now(timezone.utc).date().isoformat()
    if db.kv_get(conn, key) != today:
        db.kv_set(conn, key, today)
        telegram.send_message(message)
        print(message)
from .evaluator import evaluate

CAST_AT_FRACTION = 0.65  # through the voting window
SIGNOFF = "\n\nmore @ nounsvote.com"  # every onchain reason is an ad for the constitution
RATIFY_FLOOR_SECONDS = 24 * 3600
VOTES = {"for": "FOR", "against": "AGAINST", "abstain": "ABSTAIN"}


def latest_verdict(conn, prop_id: int):
    return conn.execute(
        "SELECT * FROM verdicts WHERE prop_id=? ORDER BY created_at DESC LIMIT 1", (prop_id,)
    ).fetchone()


def ingest_candidates(client, conn) -> None:
    """Evaluate open candidates. FOR = sponsor-worthy; sponsorship is NEVER
    automatic — /sponsor c<num> signs and registers it."""
    rev = db.constitution_rev()
    fp = db.constitution_fingerprint()
    for cand in subgraph.fetch_candidates(first=10):
        cand_id = cand["id"]
        chash = subgraph.candidate_content_hash(cand)
        existing = db.get_candidate(conn, cand_id)
        acted = existing and (existing["sponsor_state"] == "sponsored" or existing["signal_tx"])
        edited = existing and existing["content_hash"] != chash
        if acted and not edited:
            continue  # you've already acted onchain; nothing a re-verdict could change
        if existing and existing["content_hash"] == chash and existing["constitution_rev"] == fp:
            continue  # same content, same constitution — already judged
        if evals_last_24h(conn) >= MAX_EVALS_PER_DAY:
            spend_guard_alert(conn, "guard_global",
                f"🛑 daily evaluation budget ({MAX_EVALS_PER_DAY}) exhausted — candidates queue until tomorrow")
            return

        as_prop = subgraph.candidate_as_prop(cand)
        print(f"evaluating candidate {cand['slug'][:50]}…")
        verdict, usage = evaluate(client, as_prop, candidate=True)
        # candidate evals count against the daily budget via a synthetic verdict row
        db.save_verdict(conn, -1, chash, rev, ANTHROPIC_MODEL, verdict, usage)
        fields = dict(
            title=as_prop["title"][:120], content_hash=chash, constitution_rev=fp,
            verdict_json=json.dumps({
                "vote": verdict.vote, "confidence": verdict.confidence,
                "clauses": verdict.clauses_cited, "reason": verdict.reason,
                "suggestions": verdict.suggestions, "flags": verdict.flags,
                "requires_human_review": verdict.requires_human_review,
            }),
            raw=json.dumps(cand),
        )
        prefix = ""
        if acted and edited:
            # edits void a sponsorship signature; feedback can be re-sent
            if existing["sponsor_state"] == "sponsored":
                fields["sponsor_state"] = "none"
                prefix = "⚠️ EDITED after you sponsored — your signature is now VOID. Fresh verdict below:\n"
            else:
                prefix = "⚠️ EDITED after you signaled — re-evaluated:\n"
            fields["signal_tx"] = None
            fields["signal_stance"] = None
        row = db.upsert_candidate(conn, cand_id, **fields)
        sigs = [s for s in cand["latestVersion"]["content"]["contentSignatures"] if not s["canceled"]]
        card = prefix + candidate_card(row["num"], cand, verdict, len(sigs))
        print("\n" + card + "\n")
        telegram.send_message(card)


def candidate_card(num, cand, verdict, sig_count) -> str:
    content = cand["latestVersion"]["content"]
    flags = f"\n⚑ {', '.join(verdict.flags)}" if verdict.flags else ""
    if verdict.vote == "FOR" and not verdict.requires_human_review:
        action = (f"🌱 SPONSOR-WORTHY — /sponsor c{num} to sign toward the ballot, "
                  f"or /signal c{num} to voice support without sponsoring")
    elif verdict.vote == "FOR":
        action = (f"🌱 leans sponsor-worthy but ⚑flagged — /sponsor c{num} if you agree, "
                  f"or /signal c{num} for support-with-reservations (reasoning + suggestions go onchain)")
    else:
        action = f"not sponsor-worthy — /signal c{num} against puts the reasoning on the record (optional)"
    return (
        f"🌿 Candidate c{num}: {content.get('title') or cand['slug']}\n"
        f"https://www.nouns.camp/candidates/{quote(cand['id'], safe='')}\n"
        f"by {cand['proposer'][:10]}… · {sig_count} sponsor sig(s) so far\n"
        f"verdict: {verdict.vote} (conf {verdict.confidence:.2f}) · clauses {', '.join(verdict.clauses_cited)}\n"
        f"{compose_reason(verdict)}{flags}\n{action}"
    )


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
        f"https://www.nouns.camp/proposals/{prop['id']}\n"
        f"state: {outcome}\n"
        f"verdict: {verdict.vote} (conf {verdict.confidence:.2f}) · clauses {', '.join(verdict.clauses_cited)}\n"
        f"{compose_reason(verdict)}{flags}\n{firing}"
    )


REMIND_INTERVAL_SECONDS = 6 * 3600
REMIND_FINAL_HOURS = 12


def format_duration(hours: float) -> str:
    """~2 days / ~14h / ~90min — sensible units for a countdown."""
    if hours >= 24:
        days = hours / 24
        return f"~{days:.0f} day{'s' if round(days) != 1 else ''}"
    if hours >= 1:
        return f"~{hours:.0f}h"
    return f"~{hours * 60:.0f}min"


def _reason_snippet(reason: str, limit: int = 200) -> str:
    """First sentence or first ~200 chars, whichever is shorter, with ellipsis if truncated."""
    reason = (reason or "").strip()
    if not reason:
        return ""
    period = reason.find(". ")
    sentence = reason[: period + 1] if period != -1 else reason
    snippet = sentence if len(sentence) <= limit else reason[:limit].rstrip()
    if len(snippet) < len(reason):
        snippet = snippet.rstrip(".") + "…"
    return snippet


def flagged_reminder_card(pid: int, prop: dict, verdict, hours_left: float, final: bool = False) -> str:
    """Compact context card for a flagged, past-cast-time prop — not a bare nag."""
    title = prop.get("title", "(untitled)")
    clauses = json.loads(verdict["clauses"] or "[]")
    flags = json.loads(verdict["flags"] or "[]")
    flag_str = f" · ⚑ {', '.join(flags)}" if flags else ""
    snippet = _reason_snippet(verdict["reason"])
    if final:
        header = (
            f"🚨 LAST CALL — prop {pid} voting closes in ~{hours_left:.0f}h and it is flagged; "
            f"without /cast {pid} or /override it will NOT vote and the miss goes on the public record\n"
            f"{title}\n"
        )
    else:
        header = f"⏳ flagged & waiting ({format_duration(hours_left)} left): prop {pid} — {title}\n"
    return (
        f"{header}"
        f"verdict: {verdict['vote']} ({verdict['confidence']:.2f}) · clauses {', '.join(clauses)}{flag_str}\n"
        f"\"{snippet}\"\n"
        f"https://www.nouns.camp/proposals/{pid}\n"
        f"/cast {pid} to ratify · /override {pid} for|against|abstain <reason> to change"
    )


def ingest_and_evaluate(client, conn, head: int) -> None:
    rev = db.constitution_rev()
    fp = db.constitution_fingerprint()
    for prop in subgraph.fetch_proposals(first=15):
        outcome = subgraph.derive_outcome(prop, head)
        pid = int(prop["id"])
        chash = subgraph.content_hash(prop)
        prior = conn.execute("SELECT content_hash FROM proposals WHERE id=?", (pid,)).fetchone()
        edited = prior and prior["content_hash"] != chash
        # always refresh status/outcome — cancellations must reach the scheduler
        db.upsert_proposal(conn, prop, chash, outcome)
        if outcome not in {"PENDING", "VOTING"}:
            continue
        if db.get_verdict(conn, pid, chash, fp, ANTHROPIC_MODEL):
            continue
        cast_row = db.get_cast(conn, pid)
        if cast_row and cast_row["state"] == "cast" and not edited:
            continue  # vote is spent — a new constitution can't change it, don't re-ping

        # spend guards: edit-spam and global runaway protection
        if evals_last_24h(conn, pid) >= MAX_EVALS_PER_PROP_PER_DAY:
            spend_guard_alert(conn, f"guard_prop_{pid}",
                f"🛑 prop {pid} re-evaluated {MAX_EVALS_PER_PROP_PER_DAY}x in 24h (edit spam?) — "
                f"pausing evaluations for it until tomorrow; latest verdict stands\n"
                f"https://www.nouns.camp/proposals/{pid}")
            continue
        if evals_last_24h(conn) >= MAX_EVALS_PER_DAY:
            spend_guard_alert(conn, "guard_global",
                f"🛑 daily evaluation budget ({MAX_EVALS_PER_DAY}) exhausted — "
                f"new props queue until tomorrow")
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
                           reason=compose_reason(verdict), cast_block_target=target)
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
    if cmd == "signal":
        # /signal c<num> [for|against|abstain] [custom reason...]
        if not args or not args[0].lstrip("c").isdigit():
            return "usage: /signal c<num> [for|against|abstain] [reason — defaults to the verdict's]"
        row = db.get_candidate_by_num(conn, int(args[0].lstrip("c")))
        if not row:
            return f"candidate {args[0]}: unknown"
        if row["signal_tx"]:
            return (f"candidate c{row['num']} already signaled {row['signal_stance']} ({row['signal_tx']})\n"
                    f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}")
        from .executor import signal_candidate

        v = json.loads(row["verdict_json"] or "{}")
        stance = v.get("vote", "ABSTAIN")
        rest = args[1:]
        if rest and rest[0].lower() in VOTES:
            stance = VOTES[rest[0].lower()]
            rest = rest[1:]
        if rest:
            reason = " ".join(rest)
        else:
            reason = v.get("reason", "")
            if v.get("suggestions"):
                reason += "\n\n[ suggestions ]\n" + "\n".join(f"- {s}" for s in v["suggestions"])
        fresh = [c for c in subgraph.fetch_candidates(first=20) if c["id"] == row["cand_id"]]
        if not fresh:
            return (f"candidate c{row['num']} no longer open (canceled or promoted)\n"
                    f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}")
        tx = signal_candidate(fresh[0], stance, reason + SIGNOFF)
        db.upsert_candidate(conn, row["cand_id"], signal_tx=tx, signal_stance=stance)
        return (f"📣 signaled {stance} on candidate c{row['num']} with reasoning "
                f"(feedback, not sponsorship)\n"
                f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}\n"
                f"tx: https://etherscan.io/tx/0x{tx.removeprefix('0x')}")

    if cmd == "prop":
        # /prop <id> → replay the full verdict + cast context for any judged proposal
        if not args or not args[0].isdigit():
            return "usage: /prop <id>"
        pid = int(args[0])
        prop_row = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        verdict = latest_verdict(conn, pid)
        if not prop_row or not verdict:
            return f"prop {pid}: not judged yet\nhttps://www.nouns.camp/proposals/{pid}"
        clauses = json.loads(verdict["clauses"] or "[]")
        flags = json.loads(verdict["flags"] or "[]")
        flag_str = f"\n⚑ {', '.join(flags)}" if flags else ""
        reason = verdict["reason"] or ""
        suggestions = json.loads(verdict["suggestions"] or "[]")
        if suggestions:
            reason += "\n\n[ suggestions ]\n" + "\n".join(f"- {s}" for s in suggestions)
        cast_row = db.get_cast(conn, pid)
        if cast_row:
            cast_line = f"\ncast: {cast_row['state']}"
            if cast_row["cast_block_target"]:
                cast_line += f" (target block {cast_row['cast_block_target']})"
            if cast_row["tx_hash"]:
                cast_line += f"\ntx: https://etherscan.io/tx/0x{cast_row['tx_hash'].removeprefix('0x')}"
        else:
            cast_line = "\ncast: nothing scheduled"
        return (
            f"📜 Prop {pid}: {prop_row['title'] or '(untitled)'}\n"
            f"status: {prop_row['status']} · outcome: {prop_row['outcome']}\n"
            f"verdict: {verdict['vote']} (conf {verdict['confidence']:.2f}) · clauses {', '.join(clauses)}{flag_str}\n"
            f"{reason}\n"
            f"https://www.nouns.camp/proposals/{pid}"
            f"{cast_line}"
        )

    if cmd == "candidates":
        # /candidates → every candidate we've judged; /candidates c<num> → replay its card
        if args and args[0].lstrip("c").isdigit():
            row = db.get_candidate_by_num(conn, int(args[0].lstrip("c")))
            if not row:
                return f"candidate {args[0]}: unknown"
            from types import SimpleNamespace

            cand = json.loads(row["raw"])
            v = json.loads(row["verdict_json"] or "{}")
            verdict = SimpleNamespace(
                vote=v.get("vote", "?"),
                confidence=v.get("confidence") or 0.0,
                clauses_cited=v.get("clauses", []),
                reason=v.get("reason", ""),
                suggestions=v.get("suggestions", []),
                flags=v.get("flags", []),
                requires_human_review=v.get("requires_human_review", False),
            )
            sigs = [s for s in cand["latestVersion"]["content"]["contentSignatures"] if not s["canceled"]]
            card = candidate_card(row["num"], cand, verdict, len(sigs))
            if row["sponsor_state"] == "sponsored":
                card += f"\n(already sponsored: {row['sig_tx']})"
            elif row["signal_tx"]:
                card += f"\n(already signaled {row['signal_stance']}: {row['signal_tx']})"
            return card
        rows = conn.execute("SELECT * FROM candidates ORDER BY num DESC LIMIT 15").fetchall()
        if not rows:
            return "no candidates seen yet"
        open_ids = {c["id"] for c in subgraph.fetch_candidates(first=20)}
        lines = []
        for r in rows:
            v = json.loads(r["verdict_json"] or "{}")
            conf = f" ({v['confidence']:.2f})" if v.get("confidence") is not None else ""
            if r["sponsor_state"] == "sponsored":
                did = "🌱 sponsored"
            elif r["signal_tx"]:
                did = f"📣 signaled {r['signal_stance']}"
            elif r["cand_id"] not in open_ids:
                did = "closed"
            else:
                did = "open"
            lines.append(f"c{r['num']}: {v.get('vote', '?')}{conf} [{did}] — {(r['title'] or '')[:45]}")
        return ("\n".join(lines)
                + "\n\n/candidates c<num> replays the full verdict card (with link) · "
                  "/signal c<num> [for|against|abstain] [reason] · /sponsor c<num>")

    if cmd == "sponsor":
        if not args or not args[0].lstrip("c").isdigit():
            return "usage: /sponsor c<num> (from the candidate card)"
        row = db.get_candidate_by_num(conn, int(args[0].lstrip("c")))
        if not row:
            return f"candidate {args[0]}: unknown"
        if row["sponsor_state"] == "sponsored":
            return (f"candidate c{row['num']} already sponsored ({row['sig_tx']})\n"
                    f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}")
        from .executor import sponsor_candidate

        cand = json.loads(row["raw"])
        v = json.loads(row["verdict_json"])
        # re-check content: an edit since evaluation means we'd sign unseen content
        fresh = [c for c in subgraph.fetch_candidates(first=20) if c["id"] == row["cand_id"]]
        if not fresh:
            return (f"candidate c{row['num']} no longer open (canceled or promoted)\n"
                    f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}")
        if subgraph.candidate_content_hash(fresh[0]) != row["content_hash"]:
            return (f"candidate c{row['num']} was EDITED since evaluation — wait for the re-evaluation card\n"
                    f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}")
        reason = v["reason"]
        if v.get("suggestions"):
            reason += "\n\n[ suggestions ]\n" + "\n".join(f"- {s}" for s in v["suggestions"])
        tx = sponsor_candidate(fresh[0], reason + SIGNOFF)
        db.upsert_candidate(conn, row["cand_id"], sponsor_state="sponsored", sig_tx=tx)
        return (f"🌱 sponsored candidate c{row['num']} with our delegated weight\n"
                f"https://www.nouns.camp/candidates/{quote(row['cand_id'], safe='')}\n"
                f"tx: https://etherscan.io/tx/0x{tx.removeprefix('0x')}\n"
                f"(signature auto-invalidates if the proposer edits)")

    if cmd == "status":
        rows = conn.execute(
            "SELECT c.*, p.title, p.status FROM casts c JOIN proposals p ON p.id=c.prop_id "
            "WHERE c.state IN ('scheduled','held') ORDER BY c.prop_id"
        ).fetchall()
        lines = []
        for r in rows:
            v = latest_verdict(conn, r["prop_id"])
            flag = " ⚑review" if v and v["requires_human_review"] else ""
            lines.append(f"{r['prop_id']}: {r['vote']} [{r['state']}]{flag} → block {r['cast_block_target']} — {r['title'][:40]}\n"
                         f"   https://www.nouns.camp/proposals/{r['prop_id']}")
        cands = conn.execute(
            "SELECT * FROM candidates WHERE sponsor_state='none' ORDER BY num DESC LIMIT 8"
        ).fetchall()
        for c in cands:
            v = json.loads(c["verdict_json"] or "{}")
            if v.get("vote") == "FOR":
                lines.append(f"c{c['num']}: 🌱 sponsor-worthy — /sponsor c{c['num']} — {c['title'][:40]}\n"
                             f"   https://www.nouns.camp/candidates/{quote(c['cand_id'], safe='')}")
        if not lines:
            return "nothing pending — all quiet"
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
    return f"unknown command /{cmd} — try /status /prop /candidates /hold /release /override /cast /sponsor /signal"


def do_cast(conn, pid: int, forced: bool = False) -> str:
    from .executor import bot_address, cast_vote

    row = db.get_cast(conn, pid)
    vote, reason = row["vote"], (row["reason"] or "") + SIGNOFF
    if not bot_address():
        db.upsert_cast(conn, pid, state="skipped")
        return (f"📝 paper mode: would cast {vote} on prop {pid} — no key configured\n"
                f"https://www.nouns.camp/proposals/{pid}")
    try:
        tx = cast_vote(pid, vote, reason)
    except Exception as exc:
        telegram.send_message(f"🚨 cast FAILED for prop {pid}: {exc}\nhttps://www.nouns.camp/proposals/{pid}")
        raise
    db.upsert_cast(conn, pid, state="cast", tx_hash=tx)
    who = "forced by /cast" if forced else "auto-fired"
    return (f"🗳 cast {vote} on prop {pid} ({who})\n"
            f"https://www.nouns.camp/proposals/{pid}\n"
            f"reason: {reason}\ntx: https://etherscan.io/tx/0x{tx.removeprefix('0x')}")


def check_schedule(conn, head: int) -> None:
    rows = conn.execute("SELECT * FROM casts WHERE state IN ('scheduled','held')").fetchall()
    for row in rows:
        pid = row["prop_id"]
        prop_row = conn.execute("SELECT raw FROM proposals WHERE id=?", (pid,)).fetchone()
        if not prop_row:
            continue
        import json as _json

        prop = _json.loads(prop_row["raw"])
        # a cancelled/vetoed prop must never reach a cast attempt
        current = conn.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()
        if current and current["status"] in ("CANCELLED", "VETOED"):
            db.upsert_cast(conn, pid, state="skipped")
            telegram.send_message(
                f"⏹ prop {pid} was {current['status'].lower()} — cast cancelled, nothing to do\n"
                f"https://www.nouns.camp/proposals/{pid}"
            )
            continue
        end = int(prop["endBlock"])
        if head > end:
            db.upsert_cast(conn, pid, state="missed")
            telegram.send_message(
                f"⏹ prop {pid} window closed without a cast "
                f"({'held' if row['state'] == 'held' else 'not ratified'}) — logged as a public miss\n"
                f"https://www.nouns.camp/proposals/{pid}"
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
            now = datetime.now(timezone.utc)
            hours_left = max(0.0, (end - head) * 12 / 3600)
            final_key = f"remind_final_{pid}"
            if hours_left < REMIND_FINAL_HOURS and not db.kv_get(conn, final_key):
                card = flagged_reminder_card(pid, prop, verdict, hours_left, final=True)
                telegram.send_message(card)
                print(card)
                db.kv_set(conn, final_key, now.isoformat())
                db.kv_set(conn, f"remind_{pid}", now.isoformat())  # final counts as a reminder too
                continue
            remind_key = f"remind_{pid}"
            last_sent = db.kv_get(conn, remind_key)
            if not last_sent or (now - datetime.fromisoformat(last_sent)).total_seconds() >= REMIND_INTERVAL_SECONDS:
                card = flagged_reminder_card(pid, prop, verdict, hours_left, final=False)
                telegram.send_message(card)
                print(card)
                db.kv_set(conn, remind_key, now.isoformat())
            continue
        if verdict_age < RATIFY_FLOOR_SECONDS:
            continue  # 24h floor: too fresh to default-fire
        msg = do_cast(conn, pid)
        telegram.send_message(msg)
        print(msg)


def main() -> None:
    # Deliberately NO web server here: the box that holds the key accepts no
    # inbound connections. The site is static (GitHub Pages); the record gets
    # there via contents-scoped git commits (publisher).
    client = anthropic.Anthropic()
    conn = db.connect()
    from .executor import bot_address

    addr = bot_address()
    mode = f"live, casting from {addr}" if addr else "paper mode (no key)"
    ingest_desc = (f"{INGEST_INTERVAL_SECONDS // 3600}h" if INGEST_INTERVAL_SECONDS >= 3600
                   else f"{INGEST_INTERVAL_SECONDS}s")
    banner = (f"nounsbot loop up — {mode} · commands/casts every {POLL_INTERVAL_SECONDS}s, "
              f"ingest every {ingest_desc}, judge {ANTHROPIC_MODEL}")
    print(banner)
    telegram.send_message(f"🤖 {banner}")
    last_ingest = 0.0
    while True:
        try:
            head = subgraph.current_block()
            if time.time() - last_ingest >= INGEST_INTERVAL_SECONDS:
                ingest_and_evaluate(client, conn, head)
                ingest_candidates(client, conn)
                last_ingest = time.time()
            handle_commands(conn)
            check_schedule(conn, head)
            publisher.publish(conn)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
