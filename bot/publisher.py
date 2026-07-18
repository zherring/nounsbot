"""Publish the verdict record to the static site.

The site is GitHub Pages; git is the deploy pipeline. Each publish writes
docs/verdicts.json and pushes a commit — Pages redeploys, the record table
renders client-side. No servers on the public surface.

On Railway set GIT_PUSH_TOKEN (a GitHub fine-grained PAT with contents:write
on this repo) so the loop can push. Locally your normal git auth is used.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone

from .config import CONSTITUTION_PATH, REPO_ROOT
from .evaluator import first_sentence, split_posted_reason

VERDICTS_PATH = REPO_ROOT / "docs" / "verdicts.json"
AMENDMENTS_PATH = REPO_ROOT / "docs" / "amendments.json"


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


def constitution_version() -> str | None:
    """The version label from the constitution's own heading — the document is
    the source of truth; git SHAs are just deploy artifacts."""
    try:
        heading = CONSTITUTION_PATH.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    m = re.search(r"\bv(\d+\.\d+)\b", heading)
    return f"v{m.group(1)}" if m else None


def register_constitution_rev() -> bool:
    """Self-maintain the amendments rev→version map. Every deploy mints a new
    SHA and verdicts cite whatever SHA was live at eval time, so hand-curating
    the map desyncs on the first untracked deploy. Instead, map the current
    rev to the current heading version the first time we publish under this
    deploy. Returns True if the map changed."""
    from . import db

    version = constitution_version()
    rev = db.constitution_rev()
    if not version or not rev or rev == "unknown":
        return False
    try:
        data = json.loads(AMENDMENTS_PATH.read_text())
    except (OSError, ValueError):
        return False
    if data.setdefault("revs", {}).get(rev) == version:
        return False
    data["revs"][rev] = version
    AMENDMENTS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def export(conn) -> bool:
    """Write the site data files. Returns True if anything changed."""
    payload = build_payload(conn)
    new_body = json.dumps(payload, indent=1)
    old_body = VERDICTS_PATH.read_text() if VERDICTS_PATH.exists() else ""
    # ignore the timestamp line when deciding whether anything real changed
    changed = old_body.split("\n", 2)[-1:] != new_body.split("\n", 2)[-1:]
    if changed:
        VERDICTS_PATH.write_text(new_body)
    return register_constitution_rev() or changed


GITHUB_REPO = os.environ.get("GITHUB_REPO", "zherring/nounsbot")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)


PUBLISHED_FILES = (VERDICTS_PATH, AMENDMENTS_PATH)


def push_via_api(token: str) -> bool:
    """Commit the site data files through the GitHub Contents API — works on
    Railway, where the deployed filesystem has no .git directory. Each file is
    compared against the remote copy first, so retries and no-op files skip
    the write."""
    import base64

    import requests

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    ok = True
    for path in PUBLISHED_FILES:
        rel = path.relative_to(REPO_ROOT).as_posix()
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{rel}"
        current = requests.get(url, headers=headers, timeout=20)
        sha = None
        local = path.read_bytes()
        if current.status_code == 200:
            remote = current.json()
            sha = remote.get("sha")
            try:
                if base64.b64decode(remote.get("content") or "") == local:
                    continue  # remote already matches
            except (ValueError, TypeError):
                pass
        body = {
            "message": f"record: update {path.name}",
            "content": base64.b64encode(local).decode(),
            "committer": {"name": "nounsbot", "email": "bot@zachherring.com"},
        }
        if sha:
            body["sha"] = sha
        resp = requests.put(url, headers=headers, json=body, timeout=20)
        if resp.status_code not in (200, 201):
            print(f"publish API push failed for {rel}: {resp.status_code} {resp.text[:200]}")
            ok = False
    return ok


def push() -> bool:
    """Publish the site data files. GitHub API when GIT_PUSH_TOKEN is set
    (Railway); plain git commit+push locally."""
    token = os.environ.get("GIT_PUSH_TOKEN")
    if token:
        return push_via_api(token)

    git("add", *(str(p) for p in PUBLISHED_FILES))
    if not git("diff", "--cached", "--quiet").returncode:
        return False  # nothing staged
    git("-c", "user.name=nounsbot", "-c", "user.email=bot@zachherring.com",
        "commit", "-m", "record: update site data")
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
