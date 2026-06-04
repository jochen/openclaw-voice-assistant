"""OpenClaw /v1/responses — non-streaming und streaming Agentic Loop."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from voice_assistant.config import OPENCLAW_RESPONSES_URL, OPENCLAW_TIMEOUT


def query(
    text: str,
    token: str,
    session: str,
    voice_instruction: str = "",
    speaker: str | None = None,
    mood: str | None = None,
    on_done=None,
) -> str | None:
    """Send a voice turn to /v1/responses and return the final reply.

    speaker: erkannter Sprecher-Name (oder None für unbekannt). Wird im
        Wrapper-Prefix mitgegeben, damit das LLM weiß, wer spricht und
        ggf. ein Enrolment-Tool aufrufen kann.
    mood: geschätzte Stimmung (oder None/neutral für kein Signal). Nur
        deutliche Signale (nicht neutral/None) werden injiziert.
    on_done: optional callback invoked before returning (e.g. to stop the thinking worker).
    """
    speaker_label = speaker if speaker else "unbekannt"
    tags = f"Sprecher: {speaker_label}"
    if mood and mood != "neutral":
        tags += f" | Stimmung: {mood} (grob geschätzt, nur als weiches Signal nutzen)"
    voice_input = f"🎤 [{tags}] {text}"
    if voice_instruction:
        voice_input = f"{voice_input}\n\n{voice_instruction}"
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


def query_stream(
    text: str,
    token: str,
    session: str,
    voice_instruction: str = "",
    speaker: str | None = None,
    mood: str | None = None,
    on_sentence: Callable[[str], None] | None = None,
    on_first_text: Callable[[], None] | None = None,
) -> str | None:
    """Streaming-Variante von query(): liest SSE-Events und liefert fertige Sätze
    via on_sentence-Callback, sobald split_into_sentences eine Satzgrenze erkennt.

    speaker: erkannter Sprecher-Name (oder None für unbekannt).
    mood: geschätzte Stimmung (oder None/neutral für kein Signal). Nur
        deutliche Signale (nicht neutral/None) werden injiziert.
    on_sentence: wird für jeden abgeschlossenen Satz aufgerufen (kann parallel sprechen).
    on_first_text: wird einmalig beim ersten Delta aufgerufen (z.B. ThinkingWorker stoppen).
    Gibt den vollständigen akkumulierten Text zurück (für Telegram-Spiegelung),
    oder None bei Fehler.
    """
    from voice_assistant.services.tts import StreamingSentenceBuffer

    speaker_label = speaker if speaker else "unbekannt"
    tags = f"Sprecher: {speaker_label}"
    if mood and mood != "neutral":
        tags += f" | Stimmung: {mood} (grob geschätzt, nur als weiches Signal nutzen)"
    voice_input = f"🎤 [{tags}] {text}"
    if voice_instruction:
        voice_input = f"{voice_input}\n\n{voice_instruction}"

    payload = json.dumps(
        {
            "model": "openclaw/main",
            "input": voice_input,
            "user": session,
            "stream": True,
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

    full_text = ""
    buf = StreamingSentenceBuffer()
    first_text_fired = False

    try:
        with urllib.request.urlopen(req, timeout=OPENCLAW_TIMEOUT) as resp:
            current_event: str | None = None
            for raw_line in resp:
                line = raw_line.decode("utf-8").rstrip("\r\n")

                if line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                    continue

                if line.startswith("data:"):
                    raw_data = line[len("data:"):].strip()
                    if not raw_data:
                        continue
                    try:
                        evt = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue

                    evt_type = evt.get("type", "")

                    if evt_type == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        if not isinstance(delta, str):
                            delta = str(delta)
                        if delta:
                            if not first_text_fired:
                                first_text_fired = True
                                if on_first_text:
                                    on_first_text()
                            full_text += delta
                            sentences = buf.feed(delta)
                            if on_sentence:
                                for s in sentences:
                                    on_sentence(s)

                    elif evt_type == "response.failed":
                        print("❌ OpenClaw stream: response.failed")
                        return None

                    elif evt_type == "response.completed":
                        # flush verbleibende Sätze
                        remaining = buf.flush()
                        if on_sentence:
                            for s in remaining:
                                on_sentence(s)
                        return full_text or None

                elif line == "":
                    # SSE-Block-Trenner — kein State nötig, current_event zurücksetzen
                    current_event = None

        # Stream sauber zu Ende ohne response.completed → trotzdem flushen
        remaining = buf.flush()
        if on_sentence:
            for s in remaining:
                on_sentence(s)
        return full_text or None

    except urllib.error.HTTPError as e:
        print(f"❌ OpenClaw stream HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        return None
    except Exception as e:
        print(f"❌ OpenClaw stream error: {e}")
        return None
