"""Backtest the constitution against historical proposals.

Usage:
  python -m bot.backtest --last 20              # most recent 20 finalized props
  python -m bot.backtest --from-id 940 --to-id 970
  python -m bot.backtest --last 20 --dry-run    # no API calls: fetch + cost estimate
  python -m bot.backtest --rerun-record          # re-judge every recorded verdict + candidate,
                                                  # diff the new verdicts against docs/verdicts.json
  python -m bot.backtest --rerun-record --dry-run

Agreement definition: the constitution "agrees" with history when it voted FOR a prop
that passed, or AGAINST a prop that failed. Disagreement is not error — the whole
thesis is that recent history is wrong — but the table shows exactly where and why
this constitution diverges from what the DAO actually did.

--rerun-record measures something different: not "did the constitution agree with the
DAO" but "did re-judging change our own verdict" — the effect of a calldata-decoding fix
or a constitution amendment on the published record. See run_rerun_record().
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from . import db, subgraph
from .config import ANTHROPIC_MODEL, REPO_ROOT
from .evaluator import CANDIDATE_PREAMBLE, Verdict, build_system_prompt, build_user_prompt, evaluate, first_sentence

FINAL_OUTCOMES = {"EXECUTED", "DEFEATED", "VETOED", "QUEUED", "SUCCEEDED_NOT_QUEUED"}
PASSED = {"EXECUTED", "QUEUED", "SUCCEEDED_NOT_QUEUED"}
FAILED = {"DEFEATED", "VETOED"}

# dry-run estimate only; real runs price per-model via UsageAgg
PRICE_IN, PRICE_OUT = 5.00, 25.00

RECORD_PATH = REPO_ROOT / "docs" / "verdicts.json"


def agreement(vote: str, outcome: str) -> str:
    """Vote vs DAO outcome — used only by the historical-backtest mode, never for
    --rerun-record (candidates have no historical outcome, and that mode compares
    new verdict vs OLD VERDICT, not vs what the DAO did)."""
    if vote == "FOR" and outcome in PASSED:
        return "agree"
    if vote == "AGAINST" and outcome in FAILED:
        return "agree"
    if vote == "ABSTAIN":
        return "abstain"
    return "DIVERGE"


def estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough; real counts come from the API


def verdict_from_cache_row(cached) -> Verdict:
    keys = cached.keys()
    tldr = (cached["tldr"] if "tldr" in keys else None) or first_sentence(cached["reason"])
    suggestions = json.loads(cached["suggestions"] or "[]") if "suggestions" in keys else []
    return Verdict(
        vote=cached["vote"], confidence=cached["confidence"],
        clauses_cited=json.loads(cached["clauses"]),
        tldr=tldr, reason=cached["reason"],
        flags=json.loads(cached["flags"]),
        requires_human_review=bool(cached["requires_human_review"]),
        suggestions=suggestions,
    )


def evaluate_and_cache(client, conn, rev: str, fp: str, cache_prop_id: int, chash: str,
                        prop: dict, candidate: bool = False):
    """Shared cache-or-evaluate step, used by every mode. Cache is keyed on constitution
    CONTENT (fp = db.constitution_fingerprint()), matching bot/poller.py — rev (the git
    SHA) is only ever a display label, never the cache key, so a constitution edit always
    misses even across deploys of the same commit.

    Returns (verdict, usage_or_None, cached_bool). usage is None on a cache hit.
    """
    cached = db.get_verdict(conn, cache_prop_id, chash, fp, ANTHROPIC_MODEL)
    if cached:
        return verdict_from_cache_row(cached), None, True
    verdict, usage = evaluate(client, prop, candidate=candidate)
    db.save_verdict(conn, cache_prop_id, chash, rev, ANTHROPIC_MODEL, verdict, usage)
    return verdict, usage, False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=20, help="evaluate the N most recent finalized props")
    ap.add_argument("--from-id", type=int)
    ap.add_argument("--to-id", type=int)
    ap.add_argument("--dry-run", action="store_true", help="fetch and estimate cost; no API calls")
    ap.add_argument("--include-cancelled", action="store_true")
    ap.add_argument("--rerun-record", action="store_true",
                     help="re-evaluate every proposal + candidate in docs/verdicts.json and "
                          "diff the new verdicts against the recorded ones")
    args = ap.parse_args()

    if args.rerun_record:
        run_rerun_record(args)
        return

    run_backtest(args)


def run_backtest(args) -> None:
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
    fp = db.constitution_fingerprint()

    results = []
    tin = tout = 0
    cost = 0.0
    for p, outcome in rows:
        chash = subgraph.content_hash(p)
        db.upsert_proposal(conn, p, chash, outcome)
        verdict, usage, cached = evaluate_and_cache(client, conn, rev, fp, int(p["id"]), chash, p)
        if cached:
            print(f"prop {p['id']:>4}  [cached]  {verdict.vote:<7} vs {outcome}")
        else:
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


# ---------------------------------------------------------------------------
# --rerun-record: re-judge every recorded proposal + candidate against the
# current constitution / current calldata decoding, and diff vs the record.
# ---------------------------------------------------------------------------


@dataclass
class RecordItem:
    kind: str          # "proposal" | "candidate"
    key: str            # display id: "983" (proposal) or "c9" (candidate)
    title: str
    old_vote: str
    old_clauses: list = field(default_factory=list)
    old_flags: list = field(default_factory=list)
    old_reason: str = ""
    prop: dict = field(default_factory=dict)   # judge-shaped dict for evaluate()
    chash: str = ""
    cache_prop_id: int = 0                      # key used against the verdicts table
    candidate: bool = False


def load_record() -> dict:
    return json.loads(RECORD_PATH.read_text())


def collect_rerun_items(record: dict):
    """Fetch live onchain content for every recorded proposal + candidate. Returns
    (items, skipped) — skipped is a list of (kind, key, title, reason) for anything
    that could no longer be found live (candidate canceled/promoted, etc)."""
    items: list[RecordItem] = []
    skipped: list[tuple[str, str, str, str]] = []

    prop_ids = sorted({v["prop_id"] for v in record.get("verdicts", []) if v.get("prop_id") is not None})
    live_props = {}
    if prop_ids:
        fetched = subgraph.fetch_proposal_range(min(prop_ids), max(prop_ids))
        live_props = {int(p["id"]): p for p in fetched}

    for v in record.get("verdicts", []):
        pid = v["prop_id"]
        live = live_props.get(pid)
        title = v.get("title", "")
        if live is None:
            skipped.append(("proposal", str(pid), title, "not found in current subgraph fetch"))
            continue
        chash = subgraph.content_hash(live)
        items.append(RecordItem(
            kind="proposal", key=str(pid), title=title,
            old_vote=v.get("vote", ""), old_clauses=v.get("clauses") or [],
            old_flags=v.get("flags") or [], old_reason=v.get("reason", ""),
            prop=live, chash=chash, cache_prop_id=pid, candidate=False,
        ))

    # fetch_candidates() default (first=10) only covers the newest activity; the
    # record spans everything ever judged, so ask for a wide window and match by
    # cand_id (== the subgraph candidate `id`, "<proposer>-<slug>").
    live_cands = {c["id"]: c for c in subgraph.fetch_candidates(first=250)}
    for c in record.get("candidates", []):
        cand_id = c.get("cand_id")
        title = c.get("title", "")
        live = live_cands.get(cand_id)
        if live is None:
            skipped.append(("candidate", f"c{c.get('num')}", title,
                             "canceled or promoted to a proposal — no longer open"))
            continue
        chash = subgraph.candidate_content_hash(live)
        prop = subgraph.candidate_as_prop(live)  # carries is_candidate — passed through untouched
        items.append(RecordItem(
            kind="candidate", key=f"c{c.get('num')}", title=title,
            old_vote=c.get("vote", ""), old_clauses=c.get("clauses") or [],
            old_flags=c.get("flags") or [], old_reason=c.get("reason", ""),
            prop=prop, chash=chash, cache_prop_id=-1, candidate=True,
        ))

    return items, skipped


def classify_vote_change(old_vote: str, new_vote: str) -> str:
    if old_vote == new_vote:
        return "unchanged"
    if old_vote == "AGAINST" and new_vote == "FOR":
        return "AGAINST→FOR"
    if old_vote == "FOR" and new_vote == "AGAINST":
        return "FOR→AGAINST"
    if new_vote == "ABSTAIN":
        return "→ABSTAIN"
    return "other"


def bucket_results(results):
    buckets = {"AGAINST→FOR": [], "FOR→AGAINST": [], "→ABSTAIN": [], "other": [], "unchanged": []}
    for it, v in results:
        buckets[classify_vote_change(it.old_vote, v.vote)].append((it, v))
    return buckets


def run_rerun_record(args) -> None:
    record = load_record()
    items, skipped = collect_rerun_items(record)

    n_props = sum(1 for i in items if i.kind == "proposal")
    n_cands = sum(1 for i in items if i.kind == "candidate")
    rev = db.constitution_rev()
    print(f"rerun-record: {len(items)} items ({n_props} proposals, {n_cands} candidates), "
          f"{len(skipped)} skipped, constitution @ {rev} with {ANTHROPIC_MODEL}\n")
    for kind, key, title, reason in skipped:
        print(f"  SKIPPED {kind:<9} {key:<6} {reason} — {title[:60]}")
    if skipped:
        print()

    if not items:
        print("nothing left to rerun — everything in the record was skipped", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        sys_tokens = estimate_tokens(build_system_prompt())
        total_in = 0
        for it in items:
            t = estimate_tokens(build_user_prompt(it.prop))
            if it.candidate:
                t += estimate_tokens(CANDIDATE_PREAMBLE)
            total_in += t
            print(f"  {it.key:>6} [{it.kind:<9}] old={it.old_vote:<7} ~{t:>6} input tokens  {it.title[:55]}")
        total_in += sys_tokens * len(items)
        est_out = 900 * len(items)
        cost = total_in / 1e6 * PRICE_IN + est_out / 1e6 * PRICE_OUT
        print(f"\nestimated: ~{total_in:,} input + ~{est_out:,} output tokens ≈ ${cost:.2f} "
              f"(before prompt-cache savings on the constitution)")
        return

    client = anthropic.Anthropic()
    conn = db.connect()
    fp = db.constitution_fingerprint()
    head = subgraph.current_block()

    results = []   # (RecordItem, Verdict)
    failed = []    # (RecordItem, Exception)
    tin = tout = 0
    cost = 0.0
    for it in items:
        try:
            if it.kind == "proposal":
                outcome = subgraph.derive_outcome(it.prop, head)
                db.upsert_proposal(conn, it.prop, it.chash, outcome)
            verdict, usage, cached = evaluate_and_cache(
                client, conn, rev, fp, it.cache_prop_id, it.chash, it.prop, candidate=it.candidate,
            )
        except Exception as exc:
            print(f"{it.key:>6} [{it.kind:<9}]  FAILED: {exc}")
            failed.append((it, exc))
            continue
        if usage:
            tin += usage.input_tokens
            tout += usage.output_tokens
            cost += usage.cost_usd
        tag = "[cached]" if cached else "        "
        changed = "  CHANGED" if verdict.vote != it.old_vote else ""
        print(f"{it.key:>6} [{it.kind:<9}] {tag} old={it.old_vote:<7} new={verdict.vote:<7}{changed}")
        results.append((it, verdict))

    buckets = bucket_results(results)
    path = write_rerun_report(results, buckets, skipped, failed, rev, tin, tout, cost)
    print_rerun_summary(results, buckets, skipped, failed, path)


def item_detail_block(it: RecordItem, v: Verdict) -> list[str]:
    return [
        f"### {it.key} — {it.title}",
        f"Old: **{it.old_vote}** (clauses {', '.join(it.old_clauses) or '—'}; "
        f"flags {', '.join(it.old_flags) or '—'})",
        f"New: **{v.vote}** (conf {v.confidence:.2f}; clauses {', '.join(v.clauses_cited) or '—'}; "
        f"flags {', '.join(v.flags) or '—'})",
        "",
        f"> {v.reason}",
        "",
    ]


def write_rerun_report(results, buckets, skipped, failed, rev: str, tin: int, tout: int, cost: float) -> Path:
    n = len(results)
    changed_n = n - len(buckets["unchanged"])

    calldata_removed, calldata_added, other_flag_or_clause_changes = [], [], []
    for it, v in results:
        had, has = "calldata_mismatch" in it.old_flags, "calldata_mismatch" in v.flags
        if had and not has:
            calldata_removed.append((it, v))
        elif has and not had:
            calldata_added.append((it, v))
        if v.vote == it.old_vote:
            flags_diff = set(v.flags) != set(it.old_flags)
            clauses_diff = set(v.clauses_cited) != set(it.old_clauses)
            if flags_diff or clauses_diff:
                other_flag_or_clause_changes.append((it, v))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = REPO_ROOT / "backtests"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"rerun-{ts}.md"

    lines = [
        f"# Rerun vs record — constitution @ {rev}",
        "",
        f"Model: {ANTHROPIC_MODEL} · {datetime.now(timezone.utc).isoformat()} · "
        f"{tin:,} in / {tout:,} out tokens ≈ ${cost:.2f}",
        "",
        f"**{n} items re-evaluated, {changed_n} changed vote.** "
        f"AGAINST→FOR: {len(buckets['AGAINST→FOR'])} · FOR→AGAINST: {len(buckets['FOR→AGAINST'])} · "
        f"→ABSTAIN: {len(buckets['→ABSTAIN'])} · other: {len(buckets['other'])} · "
        f"unchanged: {len(buckets['unchanged'])} · skipped: {len(skipped)} · failed: {len(failed)}",
        "",
    ]

    if skipped:
        lines += ["## Skipped", ""]
        for kind, key, title, reason in skipped:
            lines.append(f"- {kind} {key} — {title[:60]} — {reason}")
        lines.append("")

    if failed:
        lines += ["## Failed", ""]
        for it, exc in failed:
            lines.append(f"- {it.kind} {it.key} — {it.title[:60]} — `{exc}`")
        lines.append("")

    lines += ["## AGAINST → FOR", "",
               "The primary thing this rerun checks: proposals/candidates that would have "
               "flipped to FOR once the calldata bug was fixed or the constitution amended.", ""]
    if buckets["AGAINST→FOR"]:
        for it, v in buckets["AGAINST→FOR"]:
            lines += item_detail_block(it, v)
    else:
        lines += ["*(none)*", ""]

    lines += ["## Other vote changes", ""]
    others = buckets["FOR→AGAINST"] + buckets["→ABSTAIN"] + buckets["other"]
    if others:
        for it, v in others:
            lines += item_detail_block(it, v)
    else:
        lines += ["*(none)*", ""]

    lines += [
        "## Flag changes",
        "",
        f"calldata_mismatch removed: {len(calldata_removed)} · "
        f"calldata_mismatch added: {len(calldata_added)} · "
        f"other flag/clause changes with unchanged vote: {len(other_flag_or_clause_changes)}",
        "",
    ]
    if calldata_removed:
        lines += ["### calldata_mismatch removed", ""]
        for it, v in calldata_removed:
            lines += item_detail_block(it, v)
    if calldata_added:
        lines += ["### calldata_mismatch added", ""]
        for it, v in calldata_added:
            lines += item_detail_block(it, v)
    if other_flag_or_clause_changes:
        lines += ["### other flag/clause changes, vote unchanged", ""]
        for it, v in other_flag_or_clause_changes:
            lines += item_detail_block(it, v)
    if not (calldata_removed or calldata_added or other_flag_or_clause_changes):
        lines += ["*(none)*", ""]

    lines += [
        "## All items",
        "",
        "| ID | Title | Old vote | New vote | Old clauses | New clauses | Changed? |",
        "|---|---|---|---|---|---|---|",
    ]
    for it, v in results:
        title = it.title.replace("|", "\\|")[:50]
        changed = "✓" if v.vote != it.old_vote else ""
        lines.append(
            f"| {it.key} | {title} | {it.old_vote} | {v.vote} | "
            f"{', '.join(it.old_clauses)} | {', '.join(v.clauses_cited)} | {changed} |"
        )

    path.write_text("\n".join(lines))
    return path


def print_rerun_summary(results, buckets, skipped, failed, path: Path) -> None:
    n = len(results)
    changed_n = n - len(buckets["unchanged"])
    print(f"\nrerun vs record: {n} items, {changed_n} changed vote "
          f"(AGAINST→FOR {len(buckets['AGAINST→FOR'])}, FOR→AGAINST {len(buckets['FOR→AGAINST'])}, "
          f"→ABSTAIN {len(buckets['→ABSTAIN'])}, other {len(buckets['other'])}, "
          f"unchanged {len(buckets['unchanged'])}) · {len(skipped)} skipped · {len(failed)} failed"
          f"\nreport → {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
