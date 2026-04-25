"""OpenClaw /v1/responses — non-streaming, vollständiger Agentic Loop."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from voice_assistant.config import OPENCLAW_RESPONSES_URL, OPENCLAW_TIMEOUT


def query(text: str, token: str, session: str, voice_instruction: str = "", on_done=None) -> str | None:
    """Send a voice turn to /v1/responses and return the final reply.

    on_done: optional callback invoked before returning (e.g. to stop the thinking worker).
    """
    voice_input = f"🎤 {text}\n\n{voice_instruction}" if voice_instruction else f"🎤 {text}"
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
        print("⚠️  Empty response from /v1/responses")
        return None
    except urllib.error.HTTPError as e:
        print(f"❌ OpenClaw HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")  # noqa: E501
        if on_done:
            on_done()
        return None
    except Exception as e:
        print(f"❌ OpenClaw error: {e}")
        if on_done:
            on_done()
        return None
