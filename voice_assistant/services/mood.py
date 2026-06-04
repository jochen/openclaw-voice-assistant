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

    def analyze(self, wav_bytes: bytes) -> dict | None:
        """POST WAV an {base}/mood, gibt das komplette Response-Dict zurück (oder None).

        Antwort-JSON: {"prosody": {...}, "mood_proxy": {"label": "...", "hint": "..."},
                       "ser": {"arousal": 0.33, "valence": 0.46, "dominance": 0.33,
                               "label": "...", "infer_ms": 15, "device": "cuda"}}
        (`ser` kann null sein wenn der SER-Dienst nicht verfügbar ist.)
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

        return result


def run_mood(analyzer: MoodAnalyzer, wav_bytes: bytes, out: queue.Queue) -> None:
    try:
        result = analyzer.analyze(wav_bytes)
        if result is None:
            print("⚠️  Mood: keine Antwort vom Dienst")
            out.put(None)
            return
        label = (result.get("mood_proxy") or {}).get("label")
        ser = result.get("ser")
        if ser and all(isinstance(ser.get(k), (int, float)) for k in ("arousal", "valence", "dominance")):
            a = ser["arousal"]
            v = ser["valence"]
            d = ser["dominance"]
            infer_ms = ser.get("infer_ms")
            device = ser.get("device")
            print(f"🫧 [Mood] {label}  arousal={a:.3f} valence={v:.3f} dominance={d:.3f} ({infer_ms}ms/{device})")
            out.put({"arousal": float(a), "valence": float(v), "dominance": float(d)})
        else:
            print(f"🫧 [Mood] {label} (Prosodie-Fallback, keine Dimensionen)")
            out.put(None)
    except Exception as e:
        print(f"⚠️  Mood worker error: {e}")
        out.put(None)
