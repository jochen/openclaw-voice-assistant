"""Speaker-Diarization via Speaches /v1/audio/diarization.

Läuft parallel zu STT. Bekannte Sprecher liegen als WAVs in SPEAKERS_DIR und
werden bei jedem Aufruf als known_speaker_references mitgeschickt (data: URLs).
"""

from __future__ import annotations

import base64
import json
import os
import queue
import urllib.error
import urllib.request

from voice_assistant.config import (
    DIARIZATION_TIMEOUT,
    SPEAKERS_DIR,
)


def _wav_to_data_url(wav_bytes: bytes) -> str:
    return f"data:audio/wav;base64,{base64.b64encode(wav_bytes).decode()}"


def _list_known_speakers() -> list[tuple[str, bytes]]:
    if not os.path.isdir(SPEAKERS_DIR):
        return []
    out: list[tuple[str, bytes]] = []
    for fname in sorted(os.listdir(SPEAKERS_DIR)):
        if not fname.lower().endswith(".wav"):
            continue
        name = os.path.splitext(fname)[0]
        try:
            with open(os.path.join(SPEAKERS_DIR, fname), "rb") as f:
                out.append((name, f.read()))
        except OSError as e:
            print(f"⚠️  speakers/{fname}: {e}")
    return out


class SpeachesDiarizer:
    def __init__(self, base: str) -> None:
        self.base = base

    def diarize(self, wav_bytes: bytes) -> str | None:
        """Liefert den dominanten Sprechernamen oder None bei 'unbekannt'/Fehler.

        Speaches-Cluster (SPEAKER_00, SPEAKER_01, ...) gelten als unbekannt.
        """
        speakers = _list_known_speakers()
        boundary = "----GastonDiarBoundary"
        parts: list[bytes] = []
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode()
            + wav_bytes
            + b"\r\n"
        )
        # WICHTIG: Form-Feldnamen brauchen [] Suffix — Speaches behandelt das
        # als Multi-Value-Liste. Ohne den Suffix werden die Referenzen still
        # ignoriert und Speaches fällt auf anonymes Clustering (SPEAKER_NN) zurück.
        for name, ref_bytes in speakers:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="known_speaker_names[]"\r\n\r\n'
                    f"{name}\r\n"
                ).encode()
            )
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="known_speaker_references[]"\r\n\r\n'
                    f"{_wav_to_data_url(ref_bytes)}\r\n"
                ).encode()
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            f"{self.base}/v1/audio/diarization",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=DIARIZATION_TIMEOUT) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            print(f"⚠️  Diarization HTTP {e.code}: {err[:120]}")
            return None
        except Exception as e:
            print(f"⚠️  Diarization error: {e}")
            return None

        durations: dict[str, float] = {}
        for seg in result.get("segments", []):
            spk = str(seg.get("speaker", ""))
            dur = float(seg.get("end", 0)) - float(seg.get("start", 0))
            if dur > 0 and spk:
                durations[spk] = durations.get(spk, 0.0) + dur
        if not durations:
            return None
        dominant = max(durations.items(), key=lambda x: x[1])[0]
        if dominant.startswith("SPEAKER_"):
            return None
        return dominant


def run_diarization(
    diarizer: SpeachesDiarizer,
    wav_bytes: bytes,
    out: queue.Queue,
) -> None:
    try:
        spk = diarizer.diarize(wav_bytes)
        if spk:
            print(f"🎙  [Diarization] Sprecher: {spk}")
        else:
            print("🎙  [Diarization] Sprecher: unbekannt")
        out.put(spk)
    except Exception as e:
        print(f"⚠️  Diarization worker error: {e}")
        out.put(None)
