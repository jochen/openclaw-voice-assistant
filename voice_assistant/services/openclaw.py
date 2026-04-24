"""OpenClaw /v1/responses — non-streaming, vollständiger Agentic Loop."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from voice_assistant.config import OPENCLAW_RESPONSES_URL, OPENCLAW_TIMEOUT


def query(text: str, token: str, session: str, on_done=None) -> str | None:
    """Sendet einen Voice-Turn an /v1/responses und gibt die finale Antwort zurück.

    on_done: optionaler Callback, der vor dem Rückgabezeitpunkt aufgerufen wird
             (z.B. um den Thinking-Worker zu stoppen).
    """
    voice_input = (
        f"🎤 {text}\n\n"
        f"[VOICE: Ruf zuerst alle nötigen Tools auf, dann antworte in max 2-3 "
        f"gesprochenen Sätzen auf Deutsch. Kein Markdown, keine Listen, keine Abkürzungen. "
        f"Niemals etwas erfinden — entweder Tool aufrufen oder sagen was du nicht weißt.]"
    )
    payload = json.dumps(
        {
            "model": "openclaw/main",
            "input": voice_input,
            "user": session,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENCLAW_RESPONSES_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "x-openclaw-session-key": session,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENCLAW_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if on_done:
            on_done()
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    text_out = part.get("text", "").strip()
                    if text_out:
                        return text_out
        print("⚠️  Leere Antwort von /v1/responses")
        return None
    except urllib.error.HTTPError as e:
        print(f"❌ OpenClaw HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        if on_done:
            on_done()
        return None
    except Exception as e:
        print(f"❌ OpenClaw Fehler: {e}")
        if on_done:
            on_done()
        return None
