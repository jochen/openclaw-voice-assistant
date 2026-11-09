"""Telegram-Mirror: schickt Voice-Anfragen und Antworten in einen Chat."""

from __future__ import annotations

import json
import urllib.request


def send(bot_token: str, chat_id: str, text: str, prefix: str = "",
         leise: bool = False) -> None:
    """leise: ohne Benachrichtigungston (Telegram disable_notification) —
    fuer Berichte, die nachts entstehen und morgens gelesen werden."""
    if not bot_token or not chat_id:
        return
    body = {"chat_id": chat_id, "text": f"{prefix}{text}" if prefix else text}
    if leise:
        body["disable_notification"] = True
    payload = json.dumps(body).encode("utf-8")
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


def send_voice(bot_token: str, chat_id: str, ogg: bytes, caption: str = "") -> bool:
    """Sprachnachricht (OGG/Opus) mit Bildunterschrift — für Hörproben neuer
    Stimmen (services/stimmen_eingang.py). True, wenn Telegram sie annahm."""
    if not bot_token or not chat_id:
        return False
    grenze = "----GastonVoiceBoundary"
    teile = []
    for feld, wert in (("chat_id", chat_id), ("caption", caption[:1000])):
        teile.append(f"--{grenze}\r\nContent-Disposition: form-data; name=\"{feld}\"\r\n\r\n"
                     f"{wert}\r\n".encode())
    teile.append(f"--{grenze}\r\nContent-Disposition: form-data; name=\"voice\"; "
                 f"filename=\"probe.ogg\"\r\nContent-Type: audio/ogg\r\n\r\n".encode()
                 + ogg + b"\r\n")
    teile.append(f"--{grenze}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendVoice",
        data=b"".join(teile),
        headers={"Content-Type": f"multipart/form-data; boundary={grenze}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        print(f"✅ Telegram Sprachnachricht: '{caption[:60]}'")
        return True
    except Exception as e:
        print(f"⚠️ Telegram Sprachnachricht Fehler: {e}")
        return False
