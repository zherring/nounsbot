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


def get_verdict(conn: sqlite3.Connection, prop_id: int, chash: str, rev: str, model: str):
    return conn.execute(
        "SELECT * FROM verdicts WHERE prop_id=? AND content_hash=? AND constitution_rev=? AND model=?",
        (prop_id, chash, rev, model),
    ).fetchone()


def save_verdict(conn, prop_id: int, chash: str, rev: str, model: str, verdict, usage) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO verdicts
           (prop_id, content_hash, constitution_rev, model, vote, confidence, clauses, reason,
            flags, requires_human_review, input_tokens, output_tokens, created_at, suggestions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            prop_id,
            chash,
            rev,
            model,
            verdict.vote,
            verdict.confidence,
            json.dumps(verdict.clauses_cited),
            verdict.reason,
            json.dumps(verdict.flags),
            int(verdict.requires_human_review),
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(getattr(verdict, "suggestions", []) or []),
        ),
    )
    conn.commit()


CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  num INTEGER PRIMARY KEY AUTOINCREMENT,
  cand_id TEXT UNIQUE,           -- proposer-slug
  title TEXT,
  content_hash TEXT,
  sponsor_state TEXT DEFAULT 'none',   -- none | sponsored | declined
  sig_tx TEXT,
  verdict_json TEXT,             -- latest verdict (vote/conf/clauses/reason/suggestions/flags)
  constitution_rev TEXT,
  raw TEXT,
  updated_at TEXT
);
"""


def migrate(conn) -> None:
    conn.executescript(CANDIDATE_SCHEMA)
    try:
        conn.execute("ALTER TABLE verdicts ADD COLUMN suggestions TEXT")
    except Exception:
        pass  # column already exists
    conn.commit()


def get_candidate(conn, cand_id: str):
    return conn.execute("SELECT * FROM candidates WHERE cand_id=?", (cand_id,)).fetchone()


def get_candidate_by_num(conn, num: int):
    return conn.execute("SELECT * FROM candidates WHERE num=?", (num,)).fetchone()


def upsert_candidate(conn, cand_id: str, **fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = get_candidate(conn, cand_id)
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE candidates SET {sets} WHERE cand_id=?", (*fields.values(), cand_id))
    else:
        cols = ", ".join(["cand_id", *fields])
        marks = ", ".join("?" * (len(fields) + 1))
        conn.execute(f"INSERT INTO candidates ({cols}) VALUES ({marks})", (cand_id, *fields.values()))
    conn.commit()
    return get_candidate(conn, cand_id)
