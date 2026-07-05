"""Minimal Telegram notifier. No-op unless TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set.
Commands (/hold etc.) arrive at M1 — M0 only pushes verdict cards."""

import requests

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_message(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        ).raise_for_status()
    except Exception as exc:  # notification failure must never kill the loop
        print(f"telegram send failed: {exc}")
