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

VERDICTS_PATH = REPO_ROOT / "docs" / "verdicts.json"


def export(conn) -> bool:
    """Write docs/verdicts.json. Returns True if content changed."""
    rows = conn.execute(
        """SELECT v.prop_id, v.vote, v.confidence, v.clauses, v.reason, v.flags,
                  v.constitution_rev, v.created_at,
                  p.title, p.outcome,
                  c.state AS cast_state, c.tx_hash, c.vote AS cast_vote, c.override_by
           FROM verdicts v
           JOIN proposals p ON p.id = v.prop_id
           LEFT JOIN casts c ON c.prop_id = v.prop_id
           WHERE v.rowid IN (SELECT MAX(rowid) FROM verdicts GROUP BY prop_id)
           ORDER BY v.prop_id DESC"""
    ).fetchall()

    verdicts = []
    for r in rows:
        verdicts.append(
            {
                "prop_id": r["prop_id"],
                "title": r["title"],
                "vote": r["cast_vote"] or r["vote"],
                "confidence": r["confidence"],
                "clauses": json.loads(r["clauses"]),
                "reason": r["reason"],
                "flags": json.loads(r["flags"]),
                "constitution_rev": r["constitution_rev"],
                "outcome": r["outcome"],
                "status": r["cast_state"] or "paper",  # paper | scheduled | held | cast | missed | skipped
                "tx_hash": r["tx_hash"],
                "overridden": bool(r["override_by"]),
                "evaluated_at": r["created_at"],
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdicts": verdicts,
    }
    new_body = json.dumps(payload, indent=1)
    old_body = VERDICTS_PATH.read_text() if VERDICTS_PATH.exists() else ""
    # ignore the timestamp line when deciding whether anything real changed
    if old_body.split("\n", 2)[-1:] == new_body.split("\n", 2)[-1:]:
        return False
    VERDICTS_PATH.write_text(new_body)
    return True


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)


def push() -> bool:
    """Commit docs/verdicts.json and push. Returns True on success."""
    git("add", str(VERDICTS_PATH))
    if not git("diff", "--cached", "--quiet").returncode:
        return False  # nothing staged
    git("-c", "user.name=nounsbot", "-c", "user.email=bot@zachherring.com",
        "commit", "-m", "record: update verdicts.json")
    token = os.environ.get("GIT_PUSH_TOKEN")
    if token:
        remote = f"https://x-access-token:{token}@github.com/zherring/nounsbot.git"
        result = git("push", remote, "HEAD:main")
    else:
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
