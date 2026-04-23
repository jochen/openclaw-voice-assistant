"""Speech-to-Text: Speaches primär, faster-whisper als Fallback."""

from __future__ import annotations

import io
import json
import queue
import urllib.error
import urllib.request
import wave

import numpy as np

from voice_assistant.config import (
    SPEACHES_TIMEOUT,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)
from voice_assistant.services.speaches import SpeachesState


def chunks_to_wav_bytes(audio_chunks: list[np.ndarray]) -> bytes:
    audio = np.concatenate(audio_chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


class SpeachesStt:
    def __init__(self, state: SpeachesState, base: str, model: str) -> None:
        self.state = state
        self.base = base
        self.model = model

    def transcribe(self, wav_bytes: bytes) -> str | None:
        boundary = "----GastonSTTBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{self.model}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\n'
            f"de\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            f"verbose_json\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{self.base}/v1/audio/transcriptions",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=SPEACHES_TIMEOUT) as resp:
                result = json.loads(resp.read())

            # Halluzinations-Filter: no_speech_prob über alle Segmente mitteln
            segments = result.get("segments", [])
            avg_no_speech = (
                sum(s.get("no_speech_prob", 0.0) for s in segments) / len(segments)
                if segments else 0.0
            )
            if avg_no_speech > 0.6:
                print(f"⚠️  STT verworfen (no_speech_prob={avg_no_speech:.2f}) — wahrscheinlich Halluzination")
                self.state.mark_stt_ok()
                return None

            text = result.get("text", "").strip()
            self.state.mark_stt_ok()
            if text:
                nsp_str = f" (no_speech_prob={avg_no_speech:.2f})" if avg_no_speech > 0.0 else ""
                print(f"🗣  [Speaches STT] Erkannt: '{text}'{nsp_str}")
            return text if text else None
        except urllib.error.HTTPError as e:
            body_err = e.read().decode(errors="replace")
            print(f"⚠️  Speaches STT HTTP {e.code}: {body_err[:120]}")
            self.state.mark_stt_failed()
            return None
        except Exception as e:
            print(f"⚠️  Speaches STT Fehler: {e}")
            self.state.mark_stt_failed()
            return None


class LocalWhisperStt:
    """Fallback-STT mit faster-whisper (CPU)."""

    def __init__(self) -> None:
        print("🔧 Lade faster-whisper (lokaler Fallback)...")
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print(f"✅ faster-whisper '{WHISPER_MODEL}' bereit")

    def transcribe(self, audio_chunks: list[np.ndarray]) -> str:
        audio = np.concatenate(audio_chunks)
        audio_float = audio.astype(np.float32) / 32768.0
        segments, info = self.model.transcribe(
            audio_float,
            language=WHISPER_LANGUAGE,
            beam_size=3,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            no_speech_threshold=0.5,
            log_prob_threshold=-1.0,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        print(f"🗣  [faster-whisper] Erkannt: '{text}' ({info.language}, {info.duration:.1f}s)")
        return text


class SttPipeline:
    """Kapselt Speaches + Whisper-Fallback in einem Aufruf."""

    def __init__(self, speaches_stt: SpeachesStt, local_stt: LocalWhisperStt) -> None:
        self.speaches = speaches_stt
        self.local = local_stt

    def run(self, audio_chunks: list[np.ndarray], out: queue.Queue) -> None:
        if self.speaches.state.stt_ok():
            print("🔄 STT: Versuche Speaches...")
            wav_bytes = chunks_to_wav_bytes(audio_chunks)
            text = self.speaches.transcribe(wav_bytes)
            if text is not None:
                out.put(text)
                return
            if self.speaches.state.stt_ok():
                # stt_ok() noch True → Halluzination verworfen, kein Fallback nötig
                out.put(None)
                return
            print("⚠️  Speaches STT fehlgeschlagen → Fallback auf faster-whisper")
        print("🔄 STT: Verwende faster-whisper (lokal)...")
        out.put(self.local.transcribe(audio_chunks))
