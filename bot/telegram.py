"""Telegram: verdict cards out, ratification commands in.

Commands (PRD §6.4):
  /status                      all open props: verdict, timer, held/flagged state
  /hold <prop>                 freeze the cast; hold wins at the deadline
  /release <prop>              resume the scheduled cast
  /override <prop> <for|against|abstain> <reason...>   replace verdict (reason mandatory)
  /cast <prop>                 cast immediately (also the explicit ratify for flagged props)
  /revoke c<num>               invalidate a previously published candidate signature

Discover your channel id with: python -m bot.telegram (posts nothing; prints chats it can see)
"""

import json

import requests

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_message(text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return True
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        ).raise_for_status()
        return True
    except Exception as exc:  # notification failure must never kill the loop
        print(f"telegram send failed: {exc}")
        return False


def get_updates(offset: int) -> list[dict]:
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        resp = requests.get(
            f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as exc:
        print(f"telegram poll failed: {exc}")
        return []


def parse_command(update: dict) -> tuple[str, list[str]] | None:
    """Returns (command, args) for messages from the configured chat; else None."""
    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return None  # ignore strangers
    parts = text.split()
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    return cmd, parts[1:]


def main() -> None:
    """Setup helper: prints every chat the bot can currently see."""
    updates = get_updates(0)
    if not updates:
        print("No updates. Post any message in the channel (bot must be admin), then rerun.")
        return
    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat", {})
        if chat:
            seen[chat.get("id")] = f"{chat.get('type')} '{chat.get('title') or chat.get('username')}'"
    for cid, desc in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({desc})")


if __name__ == "__main__":
    main()
