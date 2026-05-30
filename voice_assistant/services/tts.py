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
    FOLLOWUP_BEEP_PATH,
    PIPER_MODEL,
    PIPER_MODEL_EMO,
    PIPER_OUT,
    SPEACHES_TIMEOUT,
)
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.state import tts_lock

PlayWav = Callable[[str], None]

# ---------------------------------------------------------------------------
# num2words — optionale Abhängigkeit (defensiver Import)
# ---------------------------------------------------------------------------
try:
    from num2words import num2words as _num2words
    _HAS_NUM2WORDS = True
except ImportError:
    _HAS_NUM2WORDS = False


# ---------------------------------------------------------------------------
# Verbalisierungs-Hilfsfunktionen
# ---------------------------------------------------------------------------

# Abkürzungen: (Muster, Ersatz) — Reihenfolge wichtig (längste zuerst)
_ABBREV_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bz\.\s*B\."),        "zum Beispiel"),
    (re.compile(r"\bd\.\s*h\."),        "das heißt"),
    (re.compile(r"\bu\.\s*a\."),        "unter anderem"),
    (re.compile(r"\bo\.\s*ä\."),        "oder ähnliches"),
    (re.compile(r"\bDr\."),             "Doktor"),
    (re.compile(r"\bProf\."),           "Professor"),
    (re.compile(r"\bca\."),             "circa"),
    (re.compile(r"\bggf\."),            "gegebenenfalls"),
    (re.compile(r"\busw\."),            "und so weiter"),
    (re.compile(r"\betc\."),            "et cetera"),
    (re.compile(r"\bbzw\."),            "beziehungsweise"),
    (re.compile(r"\bevtl\."),           "eventuell"),
    (re.compile(r"\binkl\."),           "inklusive"),
    (re.compile(r"\bmax\."),            "maximal"),
    (re.compile(r"\bmin\."),            "minimal"),
    (re.compile(r"\bvs\."),             "versus"),
    (re.compile(r"\bNr\."),             "Nummer"),
    (re.compile(r"\bTel\."),            "Telefon"),
    (re.compile(r"\bStr\."),            "Straße"),
    (re.compile(r"\bMio\."),            "Millionen"),
    (re.compile(r"\bMrd\."),            "Milliarden"),
]

# Monatsnamen für Ordinal-Erkennung
_MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
    "Jan", "Feb", "Mär", "Apr", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
)
_MONTH_PATTERN = "|".join(_MONTH_NAMES)


def _ordinal_de(n: int) -> str:
    """Gibt die Ordinalzahl auf Deutsch zurück (Akkusativ/Genitiv '-sten' als Default)."""
    if not _HAS_NUM2WORDS:
        return str(n) + "."
    base = _num2words(n, lang="de", to="ordinal")
    # num2words liefert z.B. "dreißigste" — wir brauchen "dreißigsten"
    if base.endswith("e"):
        return base + "n"
    return base


def _number_de(n: int | float) -> str:
    """Gibt eine Zahl als deutsches Zahlwort zurück."""
    if not _HAS_NUM2WORDS:
        return str(n)
    if isinstance(n, float):
        return _num2words(n, lang="de")
    return _num2words(int(n), lang="de")


def _verbalize_time(m: re.Match) -> str:
    """Wandelt Uhrzeitmuster in gesprochenes Deutsch um."""
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not _HAS_NUM2WORDS:
        return m.group(0)
    hour_word = _num2words(hour, lang="de")
    if minute == 0:
        return f"{hour_word} Uhr"
    minute_word = _num2words(minute, lang="de")
    return f"{hour_word} Uhr {minute_word}"


def _verbalize_ordinal_date(m: re.Match) -> str:
    """Wandelt '30. Mai' in 'dreißigsten Mai' um."""
    n = int(m.group(1))
    month = m.group(2)
    return f"{_ordinal_de(n)} {month}"


def _verbalize_ordinal_generic(m: re.Match) -> str:
    """Wandelt '1. Stock' o.ä. in 'ersten Stock' um."""
    n = int(m.group(1))
    rest = m.group(2)
    return f"{_ordinal_de(n)} {rest}"


def _verbalize_number(m: re.Match) -> str:
    """Wandelt eine isolierte Zahl in das deutsche Zahlwort um."""
    raw = m.group(0)
    try:
        # Dezimalzahl?
        if "," in raw:
            val = float(raw.replace(".", "").replace(",", "."))
        else:
            val = int(raw.replace(".", ""))
        return _number_de(val)
    except (ValueError, OverflowError):
        return raw


def _apply_verbalization(text: str) -> str:
    """Verbalisiert Abkürzungen, Uhrzeiten, Ordinalzahlen und Zahlen."""
    # a) Abkürzungen
    for pattern, replacement in _ABBREV_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    if _HAS_NUM2WORDS:
        # b) Uhrzeiten: "12.30 Uhr" oder "14:30 Uhr" → Zahlwörter
        #    Muss VOR der allgemeinen Zahlregel laufen!
        time_pat = re.compile(r"\b(\d{1,2})[.:](\d{2})\s*Uhr\b")
        text = time_pat.sub(_verbalize_time, text)

        # c1) Ordinalzahlen vor Monatsnamen: "30. Mai"
        ord_date_pat = re.compile(
            r"\b(\d{1,2})\.\s+(" + _MONTH_PATTERN + r")\b"
        )
        text = ord_date_pat.sub(_verbalize_ordinal_date, text)

        # c2) Sonstige Ordinalzahlen: "<Zahl>. <Wort>" — nur wenn danach ein
        #     großgeschriebenes Wort folgt (typisch für "1. Stock", "3. Platz")
        ord_generic_pat = re.compile(r"\b(\d{1,2})\.\s+([A-ZÄÖÜ][a-zäöüß]+)")
        text = ord_generic_pat.sub(_verbalize_ordinal_generic, text)

        # d) Übrige Zahlen (inkl. Tausenderpunkt, Komma-Dezimal)
        num_pat = re.compile(r"\b\d{1,3}(?:\.\d{3})*(?:,\d+)?\b|\b\d+\b")
        text = num_pat.sub(_verbalize_number, text)

    return text


# ---------------------------------------------------------------------------
# Text-Aufbereitung
# ---------------------------------------------------------------------------
def clean_for_tts(text: str) -> str:
    # 1. Markdown entfernen
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"[-*•]\s+", "", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"[^\w\s\.,!?;:\-äöüÄÖÜß]", "", text)
    text = re.sub(r"\n+", " ", text)
    # 2. Verbalisierung (Abkürzungen, Zahlen, Zeiten, Ordinalia)
    text = _apply_verbalization(text)
    return text.strip()


# ---------------------------------------------------------------------------
# Splitter-Hilfskonstanten
# ---------------------------------------------------------------------------
_ABBREVS_SPLIT = re.compile(
    r"(?:"
    r"z\.\s*B\.|d\.\s*h\.|u\.\s*a\.|o\.\s*ä\.|"
    r"bzw\.|ca\.|usw\.|etc\.|ggf\.|evtl\.|inkl\.|"
    r"max\.|min\.|Nr\.|Dr\.|Prof\.|Mio\.|Mrd\.|Tel\.|Str\.|vs\.|Abs\."
    r")\s",
    re.IGNORECASE,
)
_PLACEHOLDER = "\x00ABBR\x00"


def split_into_sentences(text: str) -> list[str]:
    # Abkürzungen schützen: ALLE Whitespaces im Match (inkl. innere bei "z. B.")
    # durch den Platzhalter ersetzen — dann kann kein Satzende mitten in "z. B." entstehen
    def _protect_abbrev(m: re.Match) -> str:
        return re.sub(r"\s", _PLACEHOLDER, m.group(0))

    protected = _ABBREVS_SPLIT.sub(_protect_abbrev, text)
    # Ordinalzahlen/Datumsangaben schützen: Ziffer gefolgt von Punkt+Leerzeichen
    protected = re.sub(r"(\d+)\.\s", lambda m: m.group(1) + "." + _PLACEHOLDER, protected)
    # An echten Satzenden aufteilen
    parts = re.split(r"(?<=[.!?])\s+", protected)
    # Platzhalter wieder zu Leerzeichen zurückwandeln
    return [p.replace(_PLACEHOLDER, " ").strip() for p in parts if p.strip()]


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
        print(f"⚠️  Piper TTS failed: {e}")
        return None


def prerender_followup_beep() -> None:
    """Pre-renders a short 880 Hz beep as the follow-up entry signal."""
    import wave as _wave
    import numpy as np

    rate = 16000
    t = np.linspace(0, 0.25, int(rate * 0.25), endpoint=False)
    samples = (np.sin(2 * np.pi * 880 * t) * 16384).astype(np.int16)
    fade = int(rate * 0.010)
    samples[:fade] = (samples[:fade] * np.linspace(0, 1, fade)).astype(np.int16)
    samples[-fade:] = (samples[-fade:] * np.linspace(1, 0, fade)).astype(np.int16)
    try:
        with _wave.open(FOLLOWUP_BEEP_PATH, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(samples.tobytes())
        print(f"✅ Follow-up beep created: {FOLLOWUP_BEEP_PATH}")
    except Exception as e:
        print(f"⚠️  Follow-up beep failed: {e}")


def prerender_ja(text: str = "Ja?") -> None:
    """Pre-renders the wakeword acknowledgement with Piper."""
    print(f"🎤 Pre-rendering wakeword acknowledgement ('{text}') with Piper...")
    try:
        subprocess.run(
            ["piper", "--model", PIPER_MODEL_EMO, "--output_file", PIPER_OUT, "--speaker=1", text],
            check=True,
            capture_output=True,
        )
        print(f"✅ Audio file created: {PIPER_OUT}")
    except Exception as e:
        print(f"⚠️  TTS setup failed: {e}")


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
        from voice_assistant.services.leds import (
            LED_ANSWER_GLOW, LED_AUDIO_OUT, LED_CONFIRMATION, LED_OPENCLAW,
        )
        with tts_lock:
            clean = self.tts_prefix + clean_for_tts(text)
            if not clean.strip():
                return
            print(f"🔊 Speaking: '{clean}'")
            self.leds.set_phase(LED_CONFIRMATION if not restore_leds else LED_ANSWER_GLOW)

            sentences = split_into_sentences(clean)
            print(f"🔊 {len(sentences)} sentence(s)")

            played = False
            if self.speaches.state.tts_ok():
                print("🔄 TTS: Speaches (sentence by sentence)...")
                for i, sentence in enumerate(sentences):
                    print(f"🔊 Sentence {i + 1}/{len(sentences)}: '{sentence}'")
                    if restore_leds:
                        self.leds.set_phase(LED_AUDIO_OUT)
                    ok = self._play_speaches_sentence(sentence)
                    if not ok:
                        print(f"⚠️  Speaches failed at sentence {i + 1} → Piper fallback")
                        remaining = " ".join(sentences[i:])
                        tmp_wav = piper_synth(remaining)
                        if tmp_wav:
                            self.play_wav(tmp_wav)
                            os.unlink(tmp_wav)
                        break
                played = True

            if not played:
                print("🔄 TTS: Piper (local)...")
                if restore_leds:
                    self.leds.set_phase(LED_AUDIO_OUT)
                tmp_wav = piper_synth(clean)
                if tmp_wav:
                    self.play_wav(tmp_wav)
                    os.unlink(tmp_wav)
                else:
                    print("❌ TTS completely failed")

            # Confirmation fertig → OpenClaw wartet noch; Antwort fertig → assistant.py übernimmt
            if not restore_leds:
                self.leds.set_phase(LED_OPENCLAW)


# ---------------------------------------------------------------------------
# Heartbeat phrases while OpenClaw is thinking
# ---------------------------------------------------------------------------

class ThinkingWorker:
    def __init__(self, play_wav: PlayWav, phrases: list) -> None:
        self.play_wav = play_wav
        self._phrases = phrases
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        phrases = iter(self._phrases)
        fallback = self._phrases[-1] if self._phrases else "..."
        if self._stop.wait(timeout=15):
            return
        while not self._stop.is_set():
            phrase = next(phrases, fallback)
            print(f"💭 Heartbeat: '{phrase}'")
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
