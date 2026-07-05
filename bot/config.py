"""Runtime configuration. Everything env-driven; nothing hardcoded that the DAO can change."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

SUBGRAPH_URL = os.environ.get("SUBGRAPH_URL", "https://www.nouns.camp/subgraphs/nouns")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")  # the judge
CONDENSER_MODEL = os.environ.get("CONDENSER_MODEL", "claude-sonnet-5")  # crunches long prose
CONDENSE_THRESHOLD_CHARS = int(os.environ.get("CONDENSE_THRESHOLD_CHARS", "6000"))
DB_PATH = Path(os.environ.get("DB_PATH", REPO_ROOT / "data" / "nounsbot.db"))
CONSTITUTION_PATH = Path(os.environ.get("CONSTITUTION_PATH", REPO_ROOT / "constitution.md"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# fast lane: telegram commands + cast schedule + publish (cheap, no LLM calls)
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
# slow lane: subgraph ingest + evaluation of new/edited props (the LLM spend)
INGEST_INTERVAL_SECONDS = int(os.environ.get("INGEST_INTERVAL_SECONDS", "86400"))
