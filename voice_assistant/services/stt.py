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


def normalize_peak(
    audio: np.ndarray, target: float = 0.7, max_gain: float = 20.0
) -> np.ndarray:
    """Hebt leise Aufnahmen vor der STT an — nur verstärkend, nie leiser.

    Der ReSpeaker liefert konstant niedrigen Pegel (~-19 dBFS gemessen); auf so
    schwachem Signal halluziniert Whisper (kleines Modell driftet sogar ins
    Englische, größeres ins falsche Deutsch). Peak-Normalisierung auf `target`
    (Anteil von Full-Scale ≈ -3 dBFS) behebt genau diese Fehlerklasse und ist
    modell-agnostisch. Der Gain ist gedeckelt, damit reine Rausch-Böden (kein
    Sprach-Peak) nicht auf Vollaussteuerung aufgeblasen werden.
    """
    if audio.size == 0:
        return audio
    peak = float(np.abs(audio).max())
    if peak < 1.0:
        return audio
    gain = min(max_gain, (target * 32767.0) / peak)
    if gain <= 1.0:
        return audio
    print(f"🔊 STT-Normalisierung: Peak {peak:.0f} → Gain {gain:.1f}×")
    return np.clip(audio.astype(np.float32) * gain, -32768.0, 32767.0).astype(np.int16)


def chunks_to_wav_bytes(
    audio_chunks: list[np.ndarray], normalize: bool = False
) -> bytes:
    audio = np.concatenate(audio_chunks)
    if normalize:
        audio = normalize_peak(audio)
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
        # Speaches bedient Parakeet (onnx-asr) nur mit "text"/"json" und lehnt
        # verbose_json ab. Ohne Segmente gibt es kein no_speech_prob — der
        # Halluzinations-Filter unten greift dann nie, das ist bekannt
        # (Messreihe in tools/stt_vergleich.py).
        self.response_format = "json" if "parakeet" in model.lower() else "verbose_json"

    def transcribe(self, wav_bytes: bytes) -> str | None:
        try:
            result = self.transcribe_raw(wav_bytes)

            # Halluzinations-Filter: no_speech_prob über alle Segmente mitteln
            segments = result.get("segments", [])
            avg_no_speech = (
                sum(s.get("no_speech_prob", 0.0) for s in segments) / len(segments)
                if segments else 0.0
            )
            # Schwelle 0.5: Halluzinationen auf Stille liegen bei >= 0.51,
            # echte (auch geschriene Stop-Kommandos) bei <= 0.49
            if avg_no_speech >= 0.5:
                print(f"⚠️  STT discarded (no_speech_prob={avg_no_speech:.2f}) — likely hallucination")
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
            print(f"⚠️  Speaches STT error: {e}")
            self.state.mark_stt_failed()
            return None

    def transcribe_raw(self, wav_bytes: bytes, prompt: str | None = None) -> dict:
        """Nur die Anfrage: verbose_json (bzw. json, s. __init__) roh, Fehler als Exception.

        Ohne Halluzinations-Filter und OHNE Wirkung auf SpeachesState — für
        Nebenmessungen (Schatten-Aufnahme nach Near-Miss), deren Ausfall den
        echten Turn nicht in den 60-s-Cooldown schicken darf.
        """
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
            f"{self.response_format}\r\n"
            f"--{boundary}\r\n"
            + (f'Content-Disposition: form-data; name="prompt"\r\n\r\n'
               f"{prompt}\r\n"
               f"--{boundary}\r\n" if prompt else "")
            + f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{self.base}/v1/audio/transcriptions",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=SPEACHES_TIMEOUT) as resp:
            return json.loads(resp.read())


class OnnxAsrStt:
    """STT im eigenen Prozess über onnx-asr, auf der CPU (seit 2026-10-05).

    Für Parakeet-TDT-0.6B-v3: auf der CPU ~0,2–0,3 s je Aufnahme, schneller
    als medium live auf der GPU (0,36 s). Auf die GPU passt es nicht: Speaches
    lädt nur die fp32-Fassung (~2,4 GB Gewichte), neben Laya, ser und
    Speaches selbst bleiben auf der 3060 Ti keine 3 GB — gemessen, HTTP 500
    aus der ONNX-Arena. Vergleich mit medium: tools/stt_vergleich.py.

    Gleiche Schnittstelle wie SpeachesStt (state, transcribe(wav_bytes)), damit
    SttPipeline beide gleich behandelt. Kein Halluzinations-Filter: Parakeet
    liefert kein no_speech_prob. Fehltrigger gehen zum Brain, der einen
    Hinweis bekommt, wenn das Wakewort fehlt (voice_assistant/anrede.py).
    """

    def __init__(self, model: str, threads: int = 8) -> None:
        import onnx_asr  # type: ignore[import-not-found]
        import onnxruntime as ort  # type: ignore[import-not-found]

        print(f"🔧 Loading {model} (onnx-asr, CPU, {threads} Threads)...")
        so = ort.SessionOptions()
        # Nicht alle Kerne: Wakeword und Audio laufen im selben Prozess weiter.
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.model_name = model
        self.model = onnx_asr.load_model(model, providers=["CPUExecutionProvider"], sess_options=so)
        self.state = SpeachesState()        # nur für stt_ok()/Cooldown-Semantik
        print(f"✅ {model} ready")

    def transcribe(self, wav_bytes: bytes) -> str | None:
        try:
            with wave.open(io.BytesIO(wav_bytes)) as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            text = self.model.recognize(audio.astype(np.float32) / 32768.0, sample_rate=16000)
            text = (text or "").strip()
            self.state.mark_stt_ok()
            if text:
                print(f"🗣  [{self.model_name.split('/')[-1]}] Erkannt: '{text}'")
            return text or None
        except Exception as e:
            print(f"⚠️  onnx-asr STT error: {e}")
            self.state.mark_stt_failed()
            return None


class LocalWhisperStt:
    """Fallback-STT mit faster-whisper (CPU)."""

    def __init__(self) -> None:
        print("🔧 Loading faster-whisper (local fallback)...")
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print(f"✅ faster-whisper '{WHISPER_MODEL}' ready")

    def transcribe(self, audio_chunks: list[np.ndarray]) -> str:
        audio = normalize_peak(np.concatenate(audio_chunks))
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
    """Kapselt [onnx-asr →] Speaches → Whisper-Fallback in einem Aufruf."""

    def __init__(self, speaches_stt: SpeachesStt, local_stt: LocalWhisperStt,
                 onnx_stt: OnnxAsrStt | None = None) -> None:
        self.speaches = speaches_stt
        self.local = local_stt
        self.onnx = onnx_stt

    def run(self, audio_chunks: list[np.ndarray], out: queue.Queue) -> None:
        if self.onnx is not None and self.onnx.state.stt_ok():
            text = self.onnx.transcribe(chunks_to_wav_bytes(audio_chunks, normalize=True))
            if text is not None or self.onnx.state.stt_ok():
                out.put(text)               # None = keine Sprache erkannt, kein Fehler
                return
            print("⚠️  onnx-asr STT failed → falling back to Speaches")
        if self.speaches.state.stt_ok():
            print("🔄 STT: trying Speaches...")
            wav_bytes = chunks_to_wav_bytes(audio_chunks, normalize=True)
            text = self.speaches.transcribe(wav_bytes)
            if text is not None:
                out.put(text)
                return
            if self.speaches.state.stt_ok():
                # stt_ok() noch True → Halluzination verworfen, kein Fallback nötig
                out.put(None)
                return
            print("⚠️  Speaches STT failed → falling back to faster-whisper")
        print("🔄 STT: using faster-whisper (local)...")
        out.put(self.local.transcribe(audio_chunks))
