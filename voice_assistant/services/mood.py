"""Stimmungsanalyse von Nutzer-Audio via voice-analysis /mood (parallel zur STT)."""

from __future__ import annotations

import json
import queue
import urllib.error
import urllib.request

from voice_assistant.config import DIARIZATION_TIMEOUT


class MoodAnalyzer:
    def __init__(self, base: str) -> None:
        self.base = base

    def analyze(self, wav_bytes: bytes) -> str | None:
        """POST WAV an {base}/mood, gibt mood_proxy.label zurück (oder None).

        Antwort-JSON: {"prosody": {...}, "mood_proxy": {"label": "...", "hint": "..."}}
        """
        boundary = "----GastonMoodBoundary"
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode()
            + wav_bytes
            + b"\r\n"
            + f"--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            f"{self.base}/mood",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=DIARIZATION_TIMEOUT) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            print(f"⚠️  Mood HTTP {e.code}: {err[:120]}")
            return None
        except Exception as e:
            print(f"⚠️  Mood error: {e}")
            return None

        mood_proxy = result.get("mood_proxy", {})
        label = mood_proxy.get("label")
        return label if label else None


def run_mood(analyzer: MoodAnalyzer, wav_bytes: bytes, out: queue.Queue) -> None:
    try:
        label = analyzer.analyze(wav_bytes)
        print(f"🫧 [Mood] {label or 'neutral/unbekannt'}")
        out.put(label)
    except Exception as e:
        print(f"⚠️  Mood worker error: {e}")
        out.put(None)
