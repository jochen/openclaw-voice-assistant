"""OpenClaw /v1/responses — non-streaming und streaming Agentic Loop."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

from voice_assistant.config import OPENCLAW_RESPONSES_URL, OPENCLAW_TIMEOUT

# Sentinel, das OpenClaw zurückgibt, wenn keine Ausgabe erfolgen soll — z.B.
# wenn die Spracheingabe nicht an den Assistenten gerichtet war (Fernseher/
# Hintergrundgespräch). In diesem Fall bricht der Assistent komplett ab: keine
# Audioausgabe, kein Telegram-Post, kein Follow-up-Zuhören.
#
# Der Vertrag wird NICHT hier definiert, sondern OpenClaw-seitig im Workspace-
# Kontext, der in den Agent-Prompt geladen wird — kanonische Quelle:
#   /home/pi/.openclaw/workspace/SYSTEMNOTIZEN.md  → "NO_REPLY-Regel (2026-05-08)"
#   "Wenn keine Ausgabe erfolgen soll, muss die Antwort exakt `NO_REPLY` sein."
# (Die Regel ist bewusst generisch; die TV-Erkennung ist eine Generalisierung
# des Modells, kein eigener Trigger.) Hier nur die Erkennung/der Abbruch.
NO_REPLY_SENTINEL = "NO_REPLY"
_NO_REPLY_RE = re.compile(r"^\W*NO[ _]?REPLY\W*$", re.IGNORECASE)


def is_no_reply(text: str | None) -> bool:
    """True, wenn die (komplette) LLM-Antwort nur das NO_REPLY-Sentinel ist.

    Tolerant gegenüber Groß/Klein, Leer-/Unterstrich und umgebender
    Interpunktion ('no reply.', 'NO_REPLY' …), aber nur als Voll-Match — eine
    mehrsätzige echte Antwort, die das Wort beiläufig enthält, wird NICHT
    unterdrückt.
    """
    return bool(text) and bool(_NO_REPLY_RE.match(text.strip()))


def query(
    text: str,
    token: str,
    session: str,
    voice_instruction: str = "",
    speaker: str | None = None,
    mood: dict | None = None,
    on_done=None,
) -> str | None:
    """Send a voice turn to /v1/responses and return the final reply.

    speaker: erkannter Sprecher-Name (oder None für unbekannt). Wird im
        Wrapper-Prefix mitgegeben, damit das LLM weiß, wer spricht und
        ggf. ein Enrolment-Tool aufrufen kann.
    mood: akustische Stimmungsdimensionen als dict {"arousal", "valence", "dominance"}
        (floats 0–1), oder None wenn keine SER-Messung verfügbar.
    on_done: optional callback invoked before returning (e.g. to stop the thinking worker).
    """
    speaker_label = speaker if speaker else "unbekannt"
    voice_input = f"🎤 [Sprecher: {speaker_label}] {text}"
    if mood and all(isinstance(mood.get(k), (int, float)) for k in ("arousal", "valence", "dominance")):
        a, v, d = mood["arousal"], mood["valence"], mood["dominance"]
        voice_input += (
            f"\n\n[Akustische Stimmungsmessung deiner Sprachaufnahme (weiches Signal, grob, "
            f"im Kontext deuten): Erregung/arousal={a:.2f}, Wertung/valence={v:.2f}, "
            f"Dominanz/dominance={d:.2f}. Skala 0–1, ~0.5 ist neutral. Beziehe das in dein "
            f"Verständnis und dein Vorgehen ein, ohne es zu überinterpretieren oder explizit zu benennen.]"
        )
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
    mood: dict | None = None,
    on_sentence: Callable[[str], None] | None = None,
    on_first_text: Callable[[], None] | None = None,
) -> str | None:
    """Streaming-Variante von query(): liest SSE-Events und liefert fertige Sätze
    via on_sentence-Callback, sobald split_into_sentences eine Satzgrenze erkennt.

    speaker: erkannter Sprecher-Name (oder None für unbekannt).
    mood: akustische Stimmungsdimensionen als dict {"arousal", "valence", "dominance"}
        (floats 0–1), oder None wenn keine SER-Messung verfügbar.
    on_sentence: wird für jeden abgeschlossenen Satz aufgerufen (kann parallel sprechen).
    on_first_text: wird einmalig beim ersten Delta aufgerufen (z.B. ThinkingWorker stoppen).
    Gibt den vollständigen akkumulierten Text zurück (für Telegram-Spiegelung),
    oder None bei Fehler.
    """
    from voice_assistant.services.tts import StreamingSentenceBuffer

    speaker_label = speaker if speaker else "unbekannt"
    voice_input = f"🎤 [Sprecher: {speaker_label}] {text}"
    if mood and all(isinstance(mood.get(k), (int, float)) for k in ("arousal", "valence", "dominance")):
        a, v, d = mood["arousal"], mood["valence"], mood["dominance"]
        voice_input += (
            f"\n\n[Akustische Stimmungsmessung deiner Sprachaufnahme (weiches Signal, grob, "
            f"im Kontext deuten): Erregung/arousal={a:.2f}, Wertung/valence={v:.2f}, "
            f"Dominanz/dominance={d:.2f}. Skala 0–1, ~0.5 ist neutral. Beziehe das in dein "
            f"Verständnis und dein Vorgehen ein, ohne es zu überinterpretieren oder explizit zu benennen.]"
        )
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
