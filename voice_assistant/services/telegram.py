"""Telegram-Mirror: schickt Voice-Anfragen und Antworten in einen Chat."""

from __future__ import annotations

import json
import urllib.request


def send(bot_token: str, chat_id: str, text: str, prefix: str = "") -> None:
    if not bot_token or not chat_id:
        return
    payload = json.dumps(
        {"chat_id": chat_id, "text": f"{prefix}{text}" if prefix else text}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"✅ Telegram: '{text[:60]}'")
    except Exception as e:
        print(f"⚠️ Telegram Fehler: {e}")
