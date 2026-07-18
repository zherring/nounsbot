"""SQLite persistence. One file, two tables: proposals we've seen, verdicts we've rendered."""

import json
import sqlite3
import subprocess
from datetime import datetime, timezone

from .config import DB_PATH, REPO_ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  title TEXT,
  status TEXT,
  content_hash TEXT,
  outcome TEXT,
  vote_open_notified_hash TEXT,
  raw TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS casts (
  prop_id INTEGER PRIMARY KEY,
  state TEXT DEFAULT 'scheduled',    -- scheduled | held | cast | missed | skipped
  vote TEXT,                          -- what will be / was cast (verdict or override)
  reason TEXT,
  override_by TEXT,                   -- 'human' when /override or /cast forced it
  cast_block_target INTEGER,
  tx_hash TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (       -- telegram update offset etc.
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
  rowid INTEGER PRIMARY KEY AUTOINCREMENT,
  prop_id INTEGER,
  content_hash TEXT,
  constitution_rev TEXT,
  model TEXT,
  vote TEXT,
  confidence REAL,
  clauses TEXT,
  tldr TEXT,
  reason TEXT,
  flags TEXT,
  requires_human_review INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  created_at TEXT,
  UNIQUE(prop_id, content_hash, constitution_rev, model)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    seed = REPO_ROOT / "data" / "seed.db"
    if not DB_PATH.exists() and seed.exists():
        import shutil

        shutil.copy(seed, DB_PATH)  # fresh volume inherits the paper-era history
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # one-time hygiene: rev-less verdicts (pre-fix Railway boots) re-evaluate cleanly
    conn.execute("DELETE FROM verdicts WHERE constitution_rev='unknown'")
    conn.commit()
    migrate(conn)
    return conn


def constitution_fingerprint() -> str:
    """Hash of the constitution's CONTENT — the evaluation cache key. The git rev
    changes on every deploy; the constitution doesn't. Re-judging should only
    happen when the document itself changes."""
    import hashlib

    from .config import CONSTITUTION_PATH

    return hashlib.sha256(CONSTITUTION_PATH.read_bytes()).hexdigest()[:12]


def constitution_rev() -> str:
    """Verdicts cite the constitution version they were evaluated under (Art. VI.2).
    On Railway there is no .git — the platform injects the commit SHA instead."""
    import os

    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
        return sha[:7] if sha else "unknown"


def upsert_proposal(conn: sqlite3.Connection, prop: dict, chash: str, outcome: str) -> None:
    conn.execute(
        """INSERT INTO proposals (id, title, status, content_hash, outcome, raw, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, status=excluded.status, content_hash=excluded.content_hash,
             outcome=excluded.outcome, raw=excluded.raw, updated_at=excluded.updated_at""",
        (
            int(prop["id"]),
            prop.get("title"),
            prop["status"],
            chash,
            outcome,
            json.dumps(prop),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def kv_get(conn, k: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(conn, k: str, v: str) -> None:
    conn.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    conn.commit()


def get_cast(conn, prop_id: int):
    return conn.execute("SELECT * FROM casts WHERE prop_id=?", (prop_id,)).fetchone()


def upsert_cast(conn, prop_id: int, **fields) -> None:
    existing = get_cast(conn, prop_id)
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE casts SET {sets} WHERE prop_id=?", (*fields.values(), prop_id))
    else:
        cols = ", ".join(["prop_id", *fields])
        marks = ", ".join("?" * (len(fields) + 1))
        conn.execute(f"INSERT INTO casts ({cols}) VALUES ({marks})", (prop_id, *fields.values()))
    conn.commit()


def get_verdict(conn: sqlite3.Connection, prop_id: int, chash: str, fp: str, model: str):
    """Cache hit = same prop content judged under the same constitution CONTENT."""
    return conn.execute(
        "SELECT * FROM verdicts WHERE prop_id=? AND content_hash=? AND constitution_fp=? AND model=?",
        (prop_id, chash, fp, model),
    ).fetchone()


def save_verdict(conn, prop_id: int, chash: str, rev: str, model: str, verdict, usage) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO verdicts
           (prop_id, content_hash, constitution_rev, model, vote, confidence, clauses, tldr,
            reason, flags, requires_human_review, input_tokens, output_tokens, created_at,
            suggestions, constitution_fp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            prop_id,
            chash,
            rev,
            model,
            verdict.vote,
            verdict.confidence,
            json.dumps(verdict.clauses_cited),
            getattr(verdict, "tldr", None),
            verdict.reason,
            json.dumps(verdict.flags),
            int(verdict.requires_human_review),
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(getattr(verdict, "suggestions", []) or []),
            constitution_fingerprint(),
        ),
    )
    conn.commit()


CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  num INTEGER PRIMARY KEY AUTOINCREMENT,
  cand_id TEXT UNIQUE,           -- proposer-slug
  logical_id TEXT,               -- proposal-update:<id> for reposted update candidates
  superseded INTEGER DEFAULT 0,
  title TEXT,
  content_hash TEXT,
  sponsor_state TEXT DEFAULT 'none',   -- none | sponsored | stale | revoked | expired
  sig_tx TEXT,
  sig_bytes TEXT,
  sig_expiration INTEGER,
  signed_content_hash TEXT,
  revoke_tx TEXT,
  verdict_json TEXT,             -- latest verdict (vote/conf/clauses/tldr/reason/suggestions/flags)
  constitution_rev TEXT,
  signal_reason TEXT,
  raw TEXT,
  updated_at TEXT
);
"""


def migrate(conn) -> None:
    conn.executescript(CANDIDATE_SCHEMA)
    for ddl in (
        "ALTER TABLE verdicts ADD COLUMN suggestions TEXT",
        "ALTER TABLE verdicts ADD COLUMN tldr TEXT",
        "ALTER TABLE candidates ADD COLUMN signal_tx TEXT",
        "ALTER TABLE candidates ADD COLUMN signal_stance TEXT",
        "ALTER TABLE candidates ADD COLUMN signal_reason TEXT",
        "ALTER TABLE verdicts ADD COLUMN constitution_fp TEXT",
        "ALTER TABLE proposals ADD COLUMN vote_open_notified_hash TEXT",
        "ALTER TABLE candidates ADD COLUMN logical_id TEXT",
        "ALTER TABLE candidates ADD COLUMN superseded INTEGER DEFAULT 0",
        "ALTER TABLE candidates ADD COLUMN sig_bytes TEXT",
        "ALTER TABLE candidates ADD COLUMN sig_expiration INTEGER",
        "ALTER TABLE candidates ADD COLUMN signed_content_hash TEXT",
        "ALTER TABLE candidates ADD COLUMN revoke_tx TEXT",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass  # column already exists
    # backfill: rows predating the fp column key on the current constitution —
    # correct for all current-era rows, irrelevant for finalized history
    conn.execute(
        "UPDATE verdicts SET constitution_fp=? WHERE constitution_fp IS NULL",
        (constitution_fingerprint(),),
    )
    # Older releases keyed candidates only by proposer+slug. Proposal-update
    # candidates are often reposted under fresh slugs, so collapse those rows
    # onto the row with the newest subgraph activity while retaining old rows.
    conn.execute("DROP INDEX IF EXISTS candidates_active_logical_id")
    keepers: dict[str, tuple[tuple[int, int, int], int]] = {}
    for row in conn.execute(
        "SELECT num, cand_id, raw, sponsor_state, sig_tx, sig_bytes, signal_tx FROM candidates"
    ):
        logical_id = row["cand_id"]
        activity = 0
        try:
            cand = json.loads(row["raw"] or "{}")
            content = (cand.get("latestVersion") or {}).get("content") or {}
            proposal_id = int(content.get("proposalIdToUpdate") or 0)
            activity = int(cand.get("lastUpdatedTimestamp") or 0)
            if proposal_id:
                # proposer-scoped: a third party's candidate targeting the same
                # proposal must not collapse onto the genuine proposer's update
                logical_id = f"proposal-update:{proposal_id}:{str(cand.get('proposer') or '').lower()}"
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        conn.execute(
            "UPDATE candidates SET logical_id=?, superseded=0 WHERE num=?",
            (logical_id, row["num"]),
        )
        # A row that acted onchain holds a live signature or published feedback —
        # it must stay the visible/revocable row even if a newer repost exists.
        acted = int(
            row["sponsor_state"] in ("sponsored", "stale")
            or bool(row["sig_tx"] or row["sig_bytes"] or row["signal_tx"])
        )
        rank = (acted, activity, row["num"])
        if logical_id not in keepers or rank > keepers[logical_id][0]:
            keepers[logical_id] = (rank, row["num"])
    for logical_id, (_, keep_num) in keepers.items():
        conn.execute(
            "UPDATE candidates SET superseded=CASE WHEN num=? THEN 0 ELSE 1 END WHERE logical_id=?",
            (keep_num, logical_id),
        )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS candidates_active_logical_id
           ON candidates(logical_id) WHERE superseded=0"""
    )
    # One-time: props already in VOTING at upgrade time were announced by the
    # pre-notification code — don't re-announce every open prop on the first tick.
    if not kv_get(conn, "vote_open_notified_backfill"):
        conn.execute(
            "UPDATE proposals SET vote_open_notified_hash=content_hash "
            "WHERE outcome='VOTING' AND vote_open_notified_hash IS NULL"
        )
        kv_set(conn, "vote_open_notified_backfill", "1")
    conn.commit()


def get_candidate(conn, cand_id: str):
    return conn.execute("SELECT * FROM candidates WHERE cand_id=?", (cand_id,)).fetchone()


def get_candidate_by_logical_id(conn, logical_id: str):
    return conn.execute(
        "SELECT * FROM candidates WHERE logical_id=? AND superseded=0", (logical_id,)
    ).fetchone()


def get_candidate_by_num(conn, num: int):
    return conn.execute("SELECT * FROM candidates WHERE num=?", (num,)).fetchone()


def upsert_candidate(conn, cand_id: str, logical_id: str | None = None, **fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = get_candidate_by_logical_id(conn, logical_id) if logical_id else get_candidate(conn, cand_id)
    if existing:
        # A previously superseded slug can become current again after an edit.
        # Keep raw audit data but free the UNIQUE cand_id for the active row.
        conflict = get_candidate(conn, cand_id)
        if conflict and conflict["num"] != existing["num"]:
            conn.execute(
                "UPDATE candidates SET cand_id=cand_id || '#superseded:' || num WHERE num=?",
                (conflict["num"],),
            )
        fields["cand_id"] = cand_id
        if logical_id:
            # Only the logical_id-keyed path may (re)activate a row — a plain
            # cand_id update (revoke/signal bookkeeping) must not resurrect a
            # superseded row and collide with the active one on the unique index.
            fields["logical_id"] = logical_id
            fields["superseded"] = 0
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE candidates SET {sets} WHERE num=?", (*fields.values(), existing["num"]))
    else:
        if logical_id:
            fields["logical_id"] = logical_id
        cols = ", ".join(["cand_id", *fields])
        marks = ", ".join("?" * (len(fields) + 1))
        conn.execute(f"INSERT INTO candidates ({cols}) VALUES ({marks})", (cand_id, *fields.values()))
    conn.commit()
    return get_candidate(conn, cand_id)


def mark_vote_open_notified(conn, prop_id: int, chash: str) -> None:
    conn.execute(
        "UPDATE proposals SET vote_open_notified_hash=? WHERE id=?", (chash, prop_id)
    )
    conn.commit()
