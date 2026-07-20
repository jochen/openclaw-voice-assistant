"""Speaker-Diarization via Speaches /v1/audio/diarization.

Läuft parallel zu STT. Bekannte Sprecher liegen als WAVs in SPEAKERS_DIR und
werden bei jedem Aufruf als known_speaker_references mitgeschickt (data: URLs).
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import queue
import urllib.error
import urllib.request
import wave

from voice_assistant.config import (
    DIARIZATION_TIMEOUT,
    RATE_OW,
    SPEAKERS_DIR,
)


# Speaches' Diarization-Modell (resnet34) braucht für lange Audios mehr GPU-RAM
# als verfügbar — bei ~20 s @ 16 kHz mono kommt schon ein OOM (Conv-Node kann
# Buffer ~180 MB nicht allokieren). 8 s Mono-Audio reichen sowohl für Sprecher-
# Identifikation als auch für Enrolment-Referenzen aus.
DIARIZATION_MAX_SEC = 8.0


def _wav_to_data_url(wav_bytes: bytes) -> str:
    return f"data:audio/wav;base64,{base64.b64encode(wav_bytes).decode()}"


def _truncate_wav(wav_bytes: bytes, max_sec: float) -> bytes:
    """Schneidet eine WAV auf max_sec — vermeidet GPU-OOM bei Speaker-Embedding."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            sr = src.getframerate()
            n_total = src.getnframes()
            max_frames = int(sr * max_sec)
            if n_total <= max_frames:
                return wav_bytes
            sw = src.getsampwidth()
            ch = src.getnchannels()
            data = src.readframes(max_frames)
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setnchannels(ch)
            dst.setsampwidth(sw)
            dst.setframerate(sr)
            dst.writeframes(data)
        return out.getvalue()
    except Exception as e:
        print(f"⚠️  _truncate_wav failed: {e} — using full audio")
        return wav_bytes


def _repeat_to_fill(wav_bytes: bytes, target_sec: float) -> bytes:
    """Tilt eine Aufnahme an sich selbst bis ~target_sec (Deckel, frame-aligned).

    Speaches koppelt < ~2-3 s Sprache nicht an eine Referenz (anonymer
    SPEAKER_NN); das Signal zu wiederholen hebt kurze — auch mit Endpoint-Stille
    gepolsterte — Clips über die Schwelle und liefert den KORREKTEN Sprecher
    (verifiziert an jochen-/petra-Slices 2026-07-21: nie eine Falsch-Zuordnung,
    nur richtig oder unbekannt). Gibt das Original zurück, wenn schon >= target.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            sr = src.getframerate()
            n = src.getnframes()
            if n == 0:
                return wav_bytes
            dur = n / sr
            if dur >= target_sec:
                return wav_bytes
            sw = src.getsampwidth()
            ch = src.getnchannels()
            frames = src.readframes(n)
        reps = math.ceil(target_sec / dur)
        data = frames * reps
        max_bytes = int(sr * target_sec) * sw * ch  # frame-aligned
        if len(data) > max_bytes:
            data = data[:max_bytes]
        print(f"🔁 Diarization-Retry: {dur:.1f}s Input → {reps}× getilt")
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setnchannels(ch)
            dst.setsampwidth(sw)
            dst.setframerate(sr)
            dst.writeframes(data)
        return out.getvalue()
    except Exception as e:
        print(f"⚠️  _repeat_to_fill failed: {e} — using original audio")
        return wav_bytes


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

        Retry-on-Miss: Bleibt der erste Durchlauf 'unbekannt' UND war der Input
        kürzer als DIARIZATION_MAX_SEC, wird das Signal einmal auf volle Länge
        getilt und erneut gesendet. Speaches koppelt < ~2-3 s Sprache nicht an
        eine Referenz — Wiederholung hebt kurze/still-gepolsterte Aufnahmen über
        die Schwelle, ohne bei genuin Unbekannten falsch zuzuordnen (der zweite
        Aufruf fällt nur an, wenn der erste nichts fand).
        """
        speakers = _list_known_speakers()
        # Referenzen kürzen (GPU-OOM-Schutz beim Speaker-Embedding)
        speakers = [(name, _truncate_wav(b, DIARIZATION_MAX_SEC)) for name, b in speakers]
        inp = _truncate_wav(wav_bytes, DIARIZATION_MAX_SEC)

        spk = self._diarize_pass(inp, speakers)
        if spk is not None:
            return spk

        boosted = _repeat_to_fill(inp, DIARIZATION_MAX_SEC)
        if boosted is inp:
            return None  # Input war schon volle Länge → Retry brächte nichts
        return self._diarize_pass(boosted, speakers)

    def _diarize_pass(
        self, input_bytes: bytes, speakers: list[tuple[str, bytes]]
    ) -> str | None:
        """Ein Diarization-Request; dominanter bekannter Sprecher oder None."""
        boundary = "----GastonDiarBoundary"
        parts: list[bytes] = []
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode()
            + input_bytes
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
