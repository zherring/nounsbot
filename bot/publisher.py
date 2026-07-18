"""Publish the verdict record to the static site.

The site is GitHub Pages; git is the deploy pipeline. Each publish writes
docs/verdicts.json and pushes a commit — Pages redeploys, the record table
renders client-side. No servers on the public surface.

On Railway set GIT_PUSH_TOKEN (a GitHub fine-grained PAT with contents:write
on this repo) so the loop can push. Locally your normal git auth is used.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

from .config import REPO_ROOT
from .evaluator import first_sentence, split_posted_reason

VERDICTS_PATH = REPO_ROOT / "docs" / "verdicts.json"


def build_payload(conn) -> dict:
    """The record as a dict — used by both the file exporter and the live web endpoint."""
    rows = conn.execute(
        """SELECT v.prop_id, v.vote, v.confidence, v.clauses, v.tldr, v.reason, v.flags,
                  v.suggestions, v.constitution_rev, v.created_at,
                  p.title, p.outcome,
                  c.state AS cast_state, c.tx_hash, c.vote AS cast_vote, c.reason AS cast_reason,
                  c.override_by
           FROM verdicts v
           JOIN proposals p ON p.id = v.prop_id
           LEFT JOIN casts c ON c.prop_id = v.prop_id
           WHERE v.rowid IN (SELECT MAX(rowid) FROM verdicts GROUP BY prop_id)
           ORDER BY v.prop_id DESC"""
    ).fetchall()

    # full re-evaluation history per prop: the flips ARE the product working,
    # so they publish too (prior verdicts under earlier constitution revs)
    history: dict[int, list[dict]] = {}
    for h in conn.execute(
        "SELECT prop_id, vote, confidence, clauses, tldr, reason, constitution_rev, created_at "
        "FROM verdicts ORDER BY prop_id, created_at"
    ):
        history.setdefault(h["prop_id"], []).append(
            {
                "vote": h["vote"],
                "confidence": h["confidence"],
                "clauses": json.loads(h["clauses"]),
                "tldr": first_sentence(h["tldr"] or h["reason"]),
                "reason": h["reason"],
                "constitution_rev": h["constitution_rev"],
                "evaluated_at": h["created_at"],
            }
        )

    def display_reason(row) -> str:
        reason = row["reason"] or ""
        try:
            suggestions = json.loads(row["suggestions"] or "[]")
        except (KeyError, IndexError, TypeError, ValueError):
            suggestions = []
        if suggestions:
            reason += "\n\n[ suggestions ]\n" + "\n".join(f"- {s}" for s in suggestions)
        return reason

    verdicts = []
    for r in rows:
        if r["prop_id"] < 0:
            continue  # synthetic candidate budget rows
        tldr = first_sentence(r["tldr"] or r["reason"])
        reason = display_reason(r)
        if r["override_by"] and r["cast_reason"]:
            tldr, reason = split_posted_reason(r["cast_reason"])
        verdicts.append(
            {
                "prop_id": r["prop_id"],
                "title": r["title"],
                "vote": r["cast_vote"] or r["vote"],
                "confidence": r["confidence"],
                "clauses": json.loads(r["clauses"]),
                "tldr": tldr,
                "reason": reason,
                "flags": json.loads(r["flags"]),
                "constitution_rev": r["constitution_rev"],
                "outcome": r["outcome"],
                "status": r["cast_state"] or "paper",  # paper | scheduled | held | cast | missed | skipped
                "tx_hash": r["tx_hash"],
                "overridden": bool(r["override_by"]),
                "evaluated_at": r["created_at"],
                "history": history.get(r["prop_id"], [])[:-1],  # prior eras only
            }
        )

    # candidates: proposals-in-waiting the agent has judged. FOR = sponsor-worthy;
    # the Action column shows what actually happened onchain (sponsor/signal txs).
    candidates = []
    for r in conn.execute(
        """SELECT num, cand_id, title, sponsor_state, sig_tx, sig_bytes, revoke_tx,
                  signal_tx, signal_stance, signal_reason, verdict_json, updated_at
           FROM candidates WHERE superseded=0 ORDER BY num DESC"""
    ):
        try:
            v = json.loads(r["verdict_json"] or "{}")
        except ValueError:
            v = {}
        reason = v.get("reason") or ""
        if v.get("suggestions"):
            reason += "\n\n[ suggestions ]\n" + "\n".join(f"- {s}" for s in v["suggestions"])
        tldr = first_sentence(v.get("tldr") or reason)
        if r["signal_reason"]:
            tldr, reason = split_posted_reason(r["signal_reason"])
        candidates.append(
            {
                "num": r["num"],
                "cand_id": r["cand_id"],
                "title": r["title"],
                "vote": v.get("vote"),
                "confidence": v.get("confidence"),
                "clauses": v.get("clauses", []),
                "tldr": tldr,
                "reason": reason,
                "change_summary": v.get("change_summary"),
                "change_materiality": v.get("change_materiality"),
                "flags": v.get("flags", []),
                "sponsor_state": r["sponsor_state"],
                "sponsor_tx": r["sig_tx"],
                "revoke_tx": r["revoke_tx"],
                "revoke_available": bool(r["sig_bytes"] or r["sig_tx"]),
                "signal_stance": r["signal_stance"],
                "signal_tx": r["signal_tx"],
                "evaluated_at": r["updated_at"],
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdicts": verdicts,
        "candidates": candidates,
    }


def export(conn) -> bool:
    """Write docs/verdicts.json. Returns True if content changed."""
    payload = build_payload(conn)
    new_body = json.dumps(payload, indent=1)
    old_body = VERDICTS_PATH.read_text() if VERDICTS_PATH.exists() else ""
    # ignore the timestamp line when deciding whether anything real changed
    if old_body.split("\n", 2)[-1:] == new_body.split("\n", 2)[-1:]:
        return False
    VERDICTS_PATH.write_text(new_body)
    return True


GITHUB_REPO = os.environ.get("GITHUB_REPO", "zherring/nounsbot")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)


def push_via_api(token: str) -> bool:
    """Commit docs/verdicts.json through the GitHub Contents API — works on
    Railway, where the deployed filesystem has no .git directory."""
    import base64

    import requests

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/docs/verdicts.json"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    current = requests.get(url, headers=headers, timeout=20)
    sha = current.json().get("sha") if current.status_code == 200 else None
    body = {
        "message": "record: update verdicts.json",
        "content": base64.b64encode(VERDICTS_PATH.read_bytes()).decode(),
        "committer": {"name": "nounsbot", "email": "bot@zachherring.com"},
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(url, headers=headers, json=body, timeout=20)
    if resp.status_code not in (200, 201):
        print(f"publish API push failed: {resp.status_code} {resp.text[:200]}")
        return False
    return True


def push() -> bool:
    """Publish docs/verdicts.json. GitHub API when GIT_PUSH_TOKEN is set
    (Railway); plain git commit+push locally."""
    token = os.environ.get("GIT_PUSH_TOKEN")
    if token:
        return push_via_api(token)

    git("add", str(VERDICTS_PATH))
    if not git("diff", "--cached", "--quiet").returncode:
        return False  # nothing staged
    git("-c", "user.name=nounsbot", "-c", "user.email=bot@zachherring.com",
        "commit", "-m", "record: update verdicts.json")
    result = git("push")
    if result.returncode != 0:
        print(f"publish push failed: {result.stderr.strip()[:300]}")
        return False
    return True


def publish(conn) -> None:
    try:
        if export(conn):
            if push():
                print("record published to site")
    except Exception as exc:
        print(f"publisher error: {exc}")  # never kill the loop over the website
