"""Text-to-Speech: Speaches primär, Piper lokal als Fallback.

Stellt zusätzlich `speak_reply` und den Lebenszeichen-Worker (_thinking) bereit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Callable

from voice_assistant.config import (
    PIPER_MODEL,
    PIPER_MODEL_EMO,
    PIPER_OUT,
    SPEACHES_TIMEOUT,
)
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.state import tts_lock

PlayWav = Callable[[str], None]


# ---------------------------------------------------------------------------
# Text-Aufbereitung
# ---------------------------------------------------------------------------
def clean_for_tts(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"[-*•]\s+", "", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"[^\w\s\.,!?;:\-äöüÄÖÜß]", "", text)
    text = re.sub(r"\n+", " ", text)
    return text.strip()


def split_into_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Speaches TTS
# ---------------------------------------------------------------------------
class SpeachesTts:
    def __init__(self, state: SpeachesState, base: str, model: str, voice: str) -> None:
        self.state = state
        self.base = base
        self.model = model
        self.voice = voice

    def synth(self, text: str) -> bytes | None:
        payload = json.dumps(
            {
                "model": self.model,
                "input": text,
                "voice": self.voice,
                "response_format": "wav",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/v1/audio/speech",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=SPEACHES_TIMEOUT) as resp:
                data = resp.read()
                if len(data) < 100:
                    raise ValueError(f"TTS Antwort zu klein ({len(data)} Bytes)")
                self.state.mark_tts_ok()
                return data
        except urllib.error.HTTPError as e:
            body_err = e.read().decode(errors="replace")
            print(f"⚠️  Speaches TTS HTTP {e.code}: {body_err[:120]}")
            self.state.mark_tts_failed()
            return None
        except Exception as e:
            print(f"⚠️  Speaches TTS Fehler: {e}")
            self.state.mark_tts_failed()
            return None


# ---------------------------------------------------------------------------
# Piper (lokaler Fallback)
# ---------------------------------------------------------------------------
def piper_synth(text: str, model: str = PIPER_MODEL) -> str | None:
    """Rendert Text in eine WAV-Datei mit Piper und gibt den Pfad zurück."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        subprocess.run(
            ["piper", "--model", model, "--output_file", tmp_wav, "--speaker=1", text],
            check=True,
            capture_output=True,
        )
        return tmp_wav
    except Exception as e:
        print(f"⚠️  Piper TTS fehlgeschlagen: {e}")
        return None


def prerender_ja() -> None:
    """Erzeugt die vorkonfektionierte 'Ja?'-Antwort."""
    print("🎤 Erstelle 'Ja?' Antwort mit Piper...")
    try:
        subprocess.run(
            [
                "piper",
                "--model",
                PIPER_MODEL_EMO,
                "--output_file",
                PIPER_OUT,
                "--speaker=1",
                "Ja?",
            ],
            check=True,
            capture_output=True,
        )
        print(f"✅ Audiodatei erstellt: {PIPER_OUT}")
    except Exception as e:
        print(f"⚠️ TTS-Setup fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# speak_reply — satzweises Vorlesen mit LED-Feedback
# ---------------------------------------------------------------------------
class ReplySpeaker:
    def __init__(
        self,
        speaches: SpeachesTts,
        play_wav: PlayWav,
        leds,  # LedDirector
        tts_prefix: str = "",
    ) -> None:
        self.speaches = speaches
        self.play_wav = play_wav
        self.leds = leds
        self.tts_prefix = tts_prefix

    def _play_speaches_sentence(self, sentence: str) -> bool:
        audio_data = self.speaches.synth(sentence)
        if not audio_data:
            return False
        tmp_wav: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_data)
                tmp_wav = f.name
            self.play_wav(tmp_wav)
            return True
        except Exception as e:
            print(f"⚠️  Wiedergabe fehlgeschlagen: {e}")
            return False
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                os.unlink(tmp_wav)

    def speak(self, text: str, restore_leds: bool = True) -> None:
        with tts_lock:
            clean = self.tts_prefix + clean_for_tts(text)
            if not clean.strip():
                return
            print(f"🔊 Lese vor: '{clean}'")
            self.leds.clear()
            self.leds.single(5, 0, 255, 255)

            sentences = split_into_sentences(clean)
            print(f"🔊 {len(sentences)} Satz/Sätze")

            played = False
            if self.speaches.state.tts_ok():
                print("🔄 TTS: Speaches (satzweise)...")
                for i, sentence in enumerate(sentences):
                    print(f"🔊 Satz {i + 1}/{len(sentences)}: '{sentence}'")
                    ok = self._play_speaches_sentence(sentence)
                    if not ok:
                        print(
                            f"⚠️  Speaches fehlgeschlagen bei Satz {i + 1} → Fallback Piper"
                        )
                        remaining = " ".join(sentences[i:])
                        tmp_wav = piper_synth(remaining)
                        if tmp_wav:
                            self.play_wav(tmp_wav)
                            os.unlink(tmp_wav)
                        break
                played = True

            if not played:
                print("🔄 TTS: Piper (lokal)...")
                tmp_wav = piper_synth(clean)
                if tmp_wav:
                    self.play_wav(tmp_wav)
                    os.unlink(tmp_wav)
                else:
                    print("❌ TTS vollständig fehlgeschlagen")

            self.leds.clear()
            if restore_leds:
                self.leds.single(0, 0, 0, 255)
            else:
                self.leds.single(3, 255, 0, 0)
                self.leds.single(4, 128, 0, 255)


# ---------------------------------------------------------------------------
# Lebenszeichen während OpenClaw denkt
# ---------------------------------------------------------------------------
THINKING_PHRASES = [
    "Einen Moment bitte.",
    "Ich schaue kurz nach.",
    "Ich bin noch dabei.",
    "Fast fertig.",
    "Noch einen Augenblick.",
]


class ThinkingWorker:
    def __init__(self, play_wav: PlayWav) -> None:
        self.play_wav = play_wav
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        phrases = iter(THINKING_PHRASES)
        if self._stop.wait(timeout=15):
            return
        while not self._stop.is_set():
            phrase = next(phrases, "Ich bin noch dabei.")
            print(f"💭 Lebenszeichen: '{phrase}'")
            tmp_wav = piper_synth(phrase)
            if tmp_wav:
                with tts_lock:
                    if not self._stop.is_set():
                        self.play_wav(tmp_wav)
                os.unlink(tmp_wav)
            self._stop.wait(timeout=20)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
