"""Backtest the constitution against historical proposals.

Usage:
  python -m bot.backtest --last 20              # most recent 20 finalized props
  python -m bot.backtest --from-id 940 --to-id 970
  python -m bot.backtest --last 20 --dry-run    # no API calls: fetch + cost estimate

Agreement definition: the constitution "agrees" with history when it voted FOR a prop
that passed, or AGAINST a prop that failed. Disagreement is not error — the whole
thesis is that recent history is wrong — but the table shows exactly where and why
this constitution diverges from what the DAO actually did.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from . import db, subgraph
from .config import ANTHROPIC_MODEL, REPO_ROOT
from .evaluator import Verdict, build_system_prompt, build_user_prompt, evaluate, first_sentence

FINAL_OUTCOMES = {"EXECUTED", "DEFEATED", "VETOED", "QUEUED", "SUCCEEDED_NOT_QUEUED"}
PASSED = {"EXECUTED", "QUEUED", "SUCCEEDED_NOT_QUEUED"}
FAILED = {"DEFEATED", "VETOED"}

# dry-run estimate only; real runs price per-model via UsageAgg
PRICE_IN, PRICE_OUT = 5.00, 25.00


def agreement(vote: str, outcome: str) -> str:
    if vote == "FOR" and outcome in PASSED:
        return "agree"
    if vote == "AGAINST" and outcome in FAILED:
        return "agree"
    if vote == "ABSTAIN":
        return "abstain"
    return "DIVERGE"


def estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough; real counts come from the API


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=20, help="evaluate the N most recent finalized props")
    ap.add_argument("--from-id", type=int)
    ap.add_argument("--to-id", type=int)
    ap.add_argument("--dry-run", action="store_true", help="fetch and estimate cost; no API calls")
    ap.add_argument("--include-cancelled", action="store_true")
    args = ap.parse_args()

    head = subgraph.current_block()
    if args.from_id and args.to_id:
        props = subgraph.fetch_proposal_range(args.from_id, args.to_id)
    else:
        # over-fetch, then keep the N most recent finalized
        props = subgraph.fetch_proposals(first=args.last * 2 + 10)

    rows = []
    for p in props:
        outcome = subgraph.derive_outcome(p, head)
        if outcome == "CANCELLED" and not args.include_cancelled:
            continue
        if outcome not in FINAL_OUTCOMES:
            continue
        rows.append((p, outcome))
    if not (args.from_id and args.to_id):
        rows = rows[: args.last]
        rows.reverse()  # oldest first

    if not rows:
        print("no finalized proposals in range", file=sys.stderr)
        sys.exit(1)

    print(f"chain head {head}; evaluating {len(rows)} finalized proposals "
          f"({rows[0][0]['id']}..{rows[-1][0]['id']}) with {ANTHROPIC_MODEL}\n")

    if args.dry_run:
        sys_tokens = estimate_tokens(build_system_prompt())
        total_in = 0
        for p, outcome in rows:
            t = estimate_tokens(build_user_prompt(p))
            total_in += t
            print(f"  prop {p['id']:>4}  {outcome:<20} ~{t:>6} input tokens  {p.get('title', '')[:60]}")
        total_in += sys_tokens * len(rows)
        est_out = 900 * len(rows)
        cost = total_in / 1e6 * PRICE_IN + est_out / 1e6 * PRICE_OUT
        print(f"\nestimated: ~{total_in:,} input + ~{est_out:,} output tokens ≈ ${cost:.2f} "
              f"(before prompt-cache savings on the constitution)")
        return

    client = anthropic.Anthropic()
    conn = db.connect()
    rev = db.constitution_rev()

    results = []
    tin = tout = 0
    cost = 0.0
    for p, outcome in rows:
        chash = subgraph.content_hash(p)
        db.upsert_proposal(conn, p, chash, outcome)
        cached = db.get_verdict(conn, int(p["id"]), chash, rev, ANTHROPIC_MODEL)
        if cached:
            verdict = Verdict(
                vote=cached["vote"], confidence=cached["confidence"],
                clauses_cited=json.loads(cached["clauses"]),
                tldr=cached["tldr"] or first_sentence(cached["reason"]),
                reason=cached["reason"],
                flags=json.loads(cached["flags"]),
                requires_human_review=bool(cached["requires_human_review"]),
                suggestions=json.loads(cached["suggestions"] or "[]"),
            )
            print(f"prop {p['id']:>4}  [cached]  {verdict.vote:<7} vs {outcome}")
        else:
            verdict, usage = evaluate(client, p)
            db.save_verdict(conn, int(p["id"]), chash, rev, ANTHROPIC_MODEL, verdict, usage)
            tin += usage.input_tokens
            tout += usage.output_tokens
            cost += usage.cost_usd
            print(f"prop {p['id']:>4}  {verdict.vote:<7} conf={verdict.confidence:.2f} "
                  f"vs {outcome:<20} {agreement(verdict.vote, outcome)}"
                  f"{'  ⚑' + ','.join(verdict.flags) if verdict.flags else ''}")
        results.append((p, outcome, verdict))

    write_report(results, rev, tin, tout, cost)


def write_report(results, rev: str, tin: int, tout: int, cost: float) -> None:
    n = len(results)
    agrees = sum(1 for _, o, v in results if agreement(v.vote, o) == "agree")
    diverges = [(p, o, v) for p, o, v in results if agreement(v.vote, o) == "DIVERGE"]
    flagged = sum(1 for _, _, v in results if v.flags)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out_dir = REPO_ROOT / "backtests"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"backtest-{ts}.md"

    lines = [
        f"# Backtest — {n} proposals, constitution @ {rev}",
        "",
        f"Model: {ANTHROPIC_MODEL} · {datetime.now(timezone.utc).date()} · "
        f"{tin:,} in / {tout:,} out tokens ≈ ${cost:.2f}",
        "",
        f"**Agreement with history: {agrees}/{n}** · divergences: {len(diverges)} · flagged: {flagged}",
        "",
        "Divergence is the interesting column — it's where this constitution would have",
        "voted against what actually happened.",
        "",
        "| Prop | Title | Outcome | Verdict | Conf | Clauses | Flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for p, o, v in results:
        title = (p.get("title") or "").replace("|", "\\|")[:50]
        mark = " **≠**" if agreement(v.vote, o) == "DIVERGE" else ""
        lines.append(
            f"| {p['id']} | {title} | {o} | {v.vote}{mark} | {v.confidence:.2f} "
            f"| {', '.join(v.clauses_cited)} | {', '.join(v.flags)} |"
        )
    lines += ["", "## Divergences in detail", ""]
    for p, o, v in diverges:
        lines += [
            f"### Prop {p['id']} — {p.get('title', '')}",
            f"History: **{o}** · Constitution: **{v.vote}** (conf {v.confidence:.2f}, clauses {', '.join(v.clauses_cited)})",
            "",
            f"> {v.reason}",
            "",
        ]
    path.write_text("\n".join(lines))
    print(f"\nagreement {agrees}/{n}, {len(diverges)} divergences, report → {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
