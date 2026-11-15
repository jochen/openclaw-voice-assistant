"""Text-to-Speech: Speaches primär, Piper lokal als Fallback.

Stellt zusätzlich `speak_reply` und den Lebenszeichen-Worker (_thinking) bereit.
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Callable

from voice_assistant.audio.strom import RATE as STROM_RATE, zu_48k_stereo
from voice_assistant.services import aussprache
from voice_assistant.config import (
    ABORT_BEEP_PATH,
    MIKRO_ZU_BEEP_PATH,
    FOLLOWUP_BEEP_PATH,
    LAST_REPLY_TXT,
    LAST_REPLY_WAV,
    PIPER_MODEL,
    PIPER_MODEL_EMO,
    PIPER_OUT,
    SPEACHES_TIMEOUT,
)
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.state import lebenszeichen, tts_lock, turn_stopped

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
        #    Mit Doppelpunkt auch ohne "Uhr" ("Es ist gerade 11:55." wurde sonst
        #    "elf:fünfundfünfzig", 2026-10-24). Nur gültige Uhrzeiten mit zwei
        #    Minutenziffern: "2:1" (Spielstand) und "16:9" bleiben unberührt.
        #    Der Punkt bleibt an "Uhr" gebunden, "12.30" allein ist mehrdeutig.
        time_colon_pat = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
        text = time_colon_pat.sub(_verbalize_time, text)

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


class StreamingSentenceBuffer:
    """Akkumuliert Text-Deltas und gibt vollständige Sätze frei, sobald eine
    Satzgrenze sicher erkannt ist. Nutzt split_into_sentences (das z.B. 'z. B.',
    '30. Mai', 'Dr.' vor Fehlsplits schützt). Verbalisiert NICHT — das macht der
    Speaker pro fertigem Satz."""

    def __init__(self) -> None:
        self._buffer = ""
        self._emitted = 0

    def feed(self, chunk: str) -> list[str]:
        """Fügt ein Delta an. Gibt die Liste der NEU vollständig gewordenen Sätze
        zurück (kann leer sein). Der jeweils letzte Teil bleibt zurückgehalten,
        weil er noch wachsen könnte."""
        self._buffer += chunk
        segs = split_into_sentences(self._buffer)
        # Nur Segmente mit Index >= _emitted und < len(segs)-1 freigeben,
        # weil das letzte Segment noch wachsen könnte.
        if len(segs) <= self._emitted + 1:
            # Kein neues abgeschlossenes Segment vorhanden
            return []
        new = segs[self._emitted : len(segs) - 1]
        self._emitted += len(new)
        return new

    def flush(self) -> list[str]:
        """Am Stream-Ende: gibt alle noch nicht emittierten Sätze zurück."""
        segs = split_into_sentences(self._buffer)
        remaining = segs[self._emitted :]
        self._emitted = len(segs)
        return remaining


# ---------------------------------------------------------------------------
# Speaches TTS
# ---------------------------------------------------------------------------
def _genau(resp, n: int) -> bytes:
    daten = b""
    while len(daten) < n:
        block = resp.read(n - len(daten))
        if not block:
            raise ValueError("TTS-Strom endet im WAV-Kopf")
        daten += block
    return daten


def _wav_kopf_lesen(resp) -> tuple[int, int]:
    """WAV-Kopf aus einem Strom lesen bis zum Beginn der Daten.

    Die Laengen im Kopf sind beim Streamen Platzhalter (siehe synth()), sie
    werden hier nicht benutzt. Gibt (rate, kanaele) zurueck; nur 16 bit PCM."""
    if _genau(resp, 12)[8:12] != b"WAVE":
        raise ValueError("TTS-Antwort ist keine WAV")
    rate = kanaele = None
    while True:
        kennung = _genau(resp, 8)
        name, groesse = kennung[:4], struct.unpack("<I", kennung[4:])[0]
        if name == b"data":
            if rate is None:
                raise ValueError("WAV ohne fmt vor data")
            return rate, kanaele
        inhalt = _genau(resp, groesse + (groesse & 1))
        if name == b"fmt ":
            fmt, kanaele, rate, _, _, bits = struct.unpack("<HHIIHH", inhalt[:16])
            if fmt != 1 or bits != 16:
                raise ValueError(f"WAV-Format {fmt}/{bits} bit nicht unterstuetzt")


# Modelle anderer Dienste (CosyVoice, services/stimmen.py). Steht so eines in
# voice_state, hat jemand eine Stimme des Antwort-Dienstes gewählt; Speaches
# kennt es nicht und spricht dann mit seiner Profil-Stimme (Rückfall,
# Bestätigung), statt mit einem fremden Modellnamen zu scheitern.
FREMDE_MODELLE: set[str] = set()


class SpeachesTts:
    def __init__(self, state: SpeachesState, base: str, model: str, voice: str,
                 fest: bool = False, timeout: float = SPEACHES_TIMEOUT) -> None:
        """fest=True: Modell und Stimme immer aus dem Konstruktor, nicht aus
        voice_state (fuer den Antwort-Dienst, der die Piper-Stimmen nicht
        kennt). timeout: kurz fuer einen Dienst auf einem anderen Rechner —
        ist der weg, soll der Rueckfall nicht 15 s warten."""
        self.state = state
        self.base = base
        self.model = model
        self.voice = voice
        self.fest = fest
        self.timeout = timeout

    def _parameter(self, text: str) -> tuple[str, str, float, str]:
        from voice_assistant.state import voice_state
        vm, vv, vsp = voice_state.get()
        eigen = not self.fest and vm not in FREMDE_MODELLE
        model = (vm or self.model) if eigen else self.model
        voice = (vv or self.voice) if eigen else self.voice
        speed = vsp if vsp else 1.0
        # Problemwoerter als [[Phoneme]] (services/aussprache.py). Erst HIER,
        # nach clean_for_tts — das entfernt eckige Klammern.
        return model, voice, speed, aussprache.fuer_piper(text, model)

    def _anfrage(self, model: str, voice: str, speed: float, text: str,
                 sample_rate: int | None = None) -> urllib.request.Request:
        body = {"model": model, "input": text, "voice": voice,
                "response_format": "wav", "speed": speed}
        if sample_rate:
            body["sample_rate"] = sample_rate
        return urllib.request.Request(
            f"{self.base}/v1/audio/speech",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _modell_laden(self, model: str) -> None:
        print(f"⚠️  Speaches TTS 404 für '{model}' — versuche zu laden …")
        encoded = urllib.parse.quote(model, safe="")
        load_req = urllib.request.Request(f"{self.base}/v1/models/{encoded}", data=b"", method="POST")
        with urllib.request.urlopen(load_req, timeout=30):
            pass

    def synth_strom(self, text: str):
        """Wie synth(), aber als Folge von PCM-Bloecken (48 kHz Stereo int16),
        sobald sie ankommen — fuer audio/strom.py.

        48 kHz wird beim Dienst bestellt (Speaches: ``sample_rate``), dann
        ist hier nur noch Mono → Stereo zu tun. Liefert er eine andere Rate,
        wird der ganze Satz am Ende auf einmal umgerechnet: Block fuer Block
        gaebe an jeder Grenze einen Filter-Einschwinger.

        Fehler werfen; der Aufrufer entscheidet ueber den Rueckfall (nur,
        solange noch nichts geschrieben ist — sonst kaeme der Satzanfang
        doppelt)."""
        model, voice, speed, text = self._parameter(text)
        try:
            try:
                resp = urllib.request.urlopen(
                    self._anfrage(model, voice, speed, text, STROM_RATE), timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                self._modell_laden(model)
                resp = urllib.request.urlopen(
                    self._anfrage(model, voice, speed, text, STROM_RATE), timeout=self.timeout)
            with resp:
                rate, kanaele = _wav_kopf_lesen(resp)
                rahmen = 2 * kanaele
                rest = b""
                gesammelt: list[bytes] = []
                lesen = getattr(resp, "read1", resp.read)
                while True:
                    block = lesen(16384)
                    if not block:
                        break
                    block = rest + block
                    n = len(block) - len(block) % rahmen
                    rest = block[n:]
                    if not n:
                        continue
                    if rate == STROM_RATE:
                        yield zu_48k_stereo(block[:n], rate, kanaele)
                    else:
                        gesammelt.append(block[:n])
                if gesammelt:
                    yield zu_48k_stereo(b"".join(gesammelt), rate, kanaele)
            self.state.mark_tts_ok()
        except GeneratorExit:
            raise
        except Exception:
            self.state.mark_tts_failed()
            raise

    def _do_synth(self, model: str, voice: str, speed: float, text: str) -> bytes | None:
        """Führt einen einzelnen Speaches-Aufruf durch. Gibt Bytes oder None zurück.

        Wirft urllib.error.HTTPError mit code 404 wenn das Modell nicht geladen ist,
        damit synth() einen Retry anstoßen kann.
        """
        payload = json.dumps(
            {
                "model": model,
                "input": text,
                "voice": voice,
                "response_format": "wav",
                "speed": speed,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/v1/audio/speech",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=SPEACHES_TIMEOUT) as resp:
            return resp.read()

    def synth(self, text: str) -> bytes | None:
        """WAV-Bytes der gerenderten Sprache, oder None bei Fehler.

        **Zwei Eigenschaften dieser Bytes, die schon je einen Messlauf gekostet
        haben** (2026-09-20, siehe tools/bargein_echo_test.py):

        1. **Der WAV-Header luegt ueber die Laenge.** Speaches streamt und setzt
           ``nframes`` auf den Platzhalter 2147483647 — bei 22050 Hz sind das
           97391 Sekunden. ``readframes(getnframes())`` ist harmlos (es liest,
           was da ist), aber jede RECHNUNG mit ``getnframes()`` ist falsch:
           Dauer, Fortschritt, "ist die Datei plausibel". Die Laenge muss aus
           der Menge der gelesenen Bytes kommen
           (``len(roh) / (rate * channels * sampwidth)``).
        2. **Dasselbe Wort klingt jedes Mal anders.** Drei Renderings desselben
           Satzes ergaben 137294 / 130638 / 133710 Bytes. Wer gegen TTS-Ausgabe
           misst, braucht deshalb Wiederholungen — ein Durchlauf ist eine
           Stichprobe von eins, und ein Befund, der beim ersten Mal ausbleibt,
           kann beim zweiten da sein.
        """
        model, voice, speed, text = self._parameter(text)

        try:
            data = self._do_synth(model, voice, speed, text)
            if data is not None and len(data) < 100:
                raise ValueError(f"TTS Antwort zu klein ({len(data)} Bytes)")
            self.state.mark_tts_ok()
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Modell nicht geladen → einmal nachladen und nochmal versuchen
                print(f"⚠️  Speaches TTS 404 für '{model}' — versuche zu laden …")
                try:
                    encoded = urllib.parse.quote(model, safe="")
                    load_req = urllib.request.Request(
                        f"{self.base}/v1/models/{encoded}",
                        data=b"",
                        method="POST",
                    )
                    with urllib.request.urlopen(load_req, timeout=30):
                        pass
                    data = self._do_synth(model, voice, speed, text)
                    if data is not None and len(data) < 100:
                        raise ValueError(f"TTS Antwort nach Reload zu klein ({len(data)} Bytes)")
                    self.state.mark_tts_ok()
                    return data
                except Exception as retry_exc:
                    print(f"⚠️  Speaches TTS Retry fehlgeschlagen: {retry_exc}")
                    self.state.mark_tts_failed()
                    return None
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
    text = aussprache.fuer_piper(text, "piper")
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


def prerender_abort_beep() -> None:
    """Zwei kurze FALLENDE Toene als Abbruch-Signal (Barge-in).

    Bewusst fallend und zweitoenig: der Follow-up-Beep ist ein einzelner
    steigender Ton, und die beiden duerfen nicht zu verwechseln sein. Der eine
    sagt "ich hoere jetzt zu", der andere "ich habe mitten im Satz aufgehoert".

    Ein Beep und keine Sprachausgabe, weil es sofort kommen muss: TTS ueber
    Speaches braucht rund eine Sekunde, und in dieser Sekunde wartet der Nutzer
    darauf, ob sein Abbruch ankam.
    """
    import wave as _wave
    import numpy as np

    rate = 16000
    ton_sec = 0.085
    pause_sec = 0.03
    teile = []
    for hz in (740, 466):          # fis'' -> b' , deutlich fallend
        t = np.linspace(0, ton_sec, int(rate * ton_sec), endpoint=False)
        ton = (np.sin(2 * np.pi * hz * t) * 16384).astype(np.int16)
        fade = int(rate * 0.008)
        ton[:fade] = (ton[:fade] * np.linspace(0, 1, fade)).astype(np.int16)
        ton[-fade:] = (ton[-fade:] * np.linspace(1, 0, fade)).astype(np.int16)
        teile.append(ton)
        teile.append(np.zeros(int(rate * pause_sec), dtype=np.int16))
    samples = np.concatenate(teile[:-1])
    try:
        with _wave.open(ABORT_BEEP_PATH, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(samples.tobytes())
        print(f"✅ Abbruch-Beep erzeugt: {ABORT_BEEP_PATH}")
    except Exception as e:
        print(f"⚠️  Abbruch-Beep fehlgeschlagen: {e}")


def prerender_mikro_zu_beep() -> None:
    """Ein kurzer, harter "umgekehrter Plop": "das Mikrofon ist jetzt zu".

    Fuer Satelliten ohne Ring (Ava auf dem Handy, Jochen 2026-11-15: man hoert
    nur, wann zugehoert wird, nicht wann das Mikro wieder zu ist). Ein Plop
    (Blase) springt in der Tonhoehe nach oben; dieser stuerzt in 70 ms
    exponentiell von 1400 auf 300 Hz, mit hartem Einsatz und schnellem
    Abklingen (Jochen: "etwas haerter, schnell, wie ein umgekehrter Plop").
    Gegen die anderen Toene abgesetzt: der Follow-up-Beep ist ein steigender
    Einzelton ("ich hoere zu"), der Abbruch-Beep zwei getrennte fallende Toene
    ("mitten im Satz aufgehoert").
    """
    import wave as _wave
    import numpy as np

    rate = 16000
    n = int(rate * 0.07)
    t = np.arange(n) / rate
    hz = 1400 * (300 / 1400) ** (t / t[-1])          # exponentiell fallend
    phase = 2 * np.pi * np.cumsum(hz) / rate
    huelle = np.minimum(t / 0.002, 1.0) * np.exp(-t / 0.025)
    huelle[-int(rate * 0.005):] *= np.linspace(1, 0, int(rate * 0.005))  # kein Knacks am Ende
    samples = (np.sin(phase) * huelle * 20000).astype(np.int16)
    try:
        with _wave.open(MIKRO_ZU_BEEP_PATH, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(samples.tobytes())
        print(f"✅ Mikro-zu-Ton erzeugt: {MIKRO_ZU_BEEP_PATH}")
    except Exception as e:
        print(f"⚠️  Mikro-zu-Ton fehlgeschlagen: {e}")


def prerender_ja(text: str = "Ja?", out_path: str = PIPER_OUT) -> None:
    """Pre-renders the wakeword acknowledgement with Piper.

    out_path: Default PIPER_OUT (ja.wav) — bei mehreren Wakewords bekommt
    jedes seine eigene Datei (siehe assistant.py._wakeword_ack_path).
    """
    print(f"🎤 Pre-rendering wakeword acknowledgement ('{text}') with Piper...")
    try:
        subprocess.run(
            ["piper", "--model", PIPER_MODEL_EMO, "--output_file", out_path, "--speaker=1", text],
            check=True,
            capture_output=True,
        )
        print(f"✅ Audio file created: {out_path}")
    except Exception as e:
        print(f"⚠️  TTS setup failed: {e}")


def rerender_ja_speaches(speaches: SpeachesTts, text: str = "Ja?") -> bool:
    """Rendert die Wakeword-Quittung (PIPER_OUT) in der aktiven Speaches-Stimme neu.

    True bei Erfolg. Bei Speaches-Fehler False — der bestehende (Piper-)Render
    in PIPER_OUT bleibt dann unangetastet als Fallback liegen.
    """
    audio = speaches.synth(text)
    if not audio:
        print("⚠️  Quittung-Re-Render via Speaches fehlgeschlagen → bestehende Datei bleibt")
        return False
    try:
        with open(PIPER_OUT, "wb") as f:
            f.write(audio)
        print(f"✅ Quittung in aktiver Stimme neu gerendert → {PIPER_OUT}")
        return True
    except Exception as e:
        print(f"⚠️  Quittung schreiben fehlgeschlagen: {e}")
        return False


# ---------------------------------------------------------------------------
# ReplyRecorder — puffert Audio + Text einer vollständigen Antwort
# ---------------------------------------------------------------------------
class ReplyRecorder:
    """Pro-Antwort-Instanz. Sammelt WAV-Frames aller Sätze und schreibt am Ende
    eine kombinierte WAV-Datei sowie den Gesamt-Text in den Workspace.

    active=False → alle Methoden sind No-Ops (Bestätigungs-TTS wird nie gepuffert).
    """

    def __init__(self, active: bool) -> None:
        self.active = active
        self._texts: list[str] = []
        self._frames: list[bytes] = []
        self._params: tuple[int, int, int] | None = None  # (nchannels, sampwidth, framerate)

    def add_wav_bytes(self, text: str, wav_bytes: bytes) -> None:
        if not self.active:
            return
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                params = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
                frames = wf.readframes(wf.getnframes())
        except Exception:
            return
        if self._params is None:
            self._params = params
        elif params != self._params:
            print("⚠️  last-reply buffer: Sample-Rate-Mismatch, Teil verworfen")
            return
        if text and text.strip():
            self._texts.append(text.strip())
        self._frames.append(frames)

    def add_pcm(self, text: str, pcm: bytes, params: tuple[int, int, int]) -> None:
        """Rohes PCM (nchannels, sampwidth, framerate) — fuer den Strom-Weg."""
        if not self.active or not pcm:
            return
        if self._params is None:
            self._params = params
        elif params != self._params:
            print("⚠️  last-reply buffer: Sample-Rate-Mismatch, Teil verworfen")
            return
        if text and text.strip():
            self._texts.append(text.strip())
        self._frames.append(pcm)

    def add_file(self, text: str, path: str) -> None:
        if not self.active:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return
        self.add_wav_bytes(text, data)

    def finalize(self) -> None:
        if not self.active or not self._frames or self._params is None:
            return
        from voice_assistant import state
        nch, sw, fr = self._params
        try:
            with wave.open(LAST_REPLY_WAV, "wb") as wf:
                wf.setnchannels(nch)
                wf.setsampwidth(sw)
                wf.setframerate(fr)
                wf.writeframes(b"".join(self._frames))
            text = " ".join(self._texts)
            with open(LAST_REPLY_TXT, "w") as f:
                f.write(text)
            state.last_spoken.update(text, LAST_REPLY_WAV)
            print(f"💾 last-reply gepuffert: {len(self._frames)} Satz/Sätze → {LAST_REPLY_WAV}")
        except Exception as e:
            print(f"⚠️  last-reply buffer failed: {e}")


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
        strom_oeffnen: Callable | None = None,
        antwort_tts: "SpeachesTts | None" = None,
    ) -> None:
        self.speaches = speaches
        self.play_wav = play_wav
        self.leds = leds
        self.tts_prefix = tts_prefix
        # Senke.strom_oeffnen (audio/strom.py) oder None = Dateien wie bisher.
        # Nur Brain-Antworten (ReplyStreamSession) gehen als Strom.
        self.strom_oeffnen = strom_oeffnen
        # Eigener TTS-Dienst nur fuer Brain-Antworten im Strom (antwort_tts im
        # Profil, z. B. CosyVoice im Fablab). Rueckfall: speaches, dann Piper —
        # eine andere Stimme, und genau daran hoert man, dass er weg ist
        # (Jochen 2026-11-08).
        self.antwort_tts = antwort_tts

    def strom_kandidaten(self) -> list["SpeachesTts"]:
        """TTS-Dienste fuer einen Satz im Strom, in dieser Reihenfolge.

        Hat jemand per Werkzeug eine andere Stimme gewaehlt (voice_state weicht
        vom Profil ab), gilt die: dann nur speaches. Ein Piper-Modell hat genau
        eine Stimme, dort zählt nur das Modell: die gespeicherte Wahl „thorsten“
        und der Profil-Default „de_DE-thorsten-medium“ sind dieselbe Stimme
        (2026-11-08 hat genau das CosyVoice still übersprungen). Eine Stimme
        des Antwort-Dienstes selbst (Modell = antwort_tts.model) ist keine
        Abweichung: welche, entscheidet AntwortTts (services/stimmen.py)."""
        from voice_assistant.state import voice_state
        vm, vv, _ = voice_state.get()
        vm = vm or self.speaches.model
        if self.antwort_tts is not None and vm == self.antwort_tts.model:
            eigene_wahl = False  # eine Stimme des Antwort-Dienstes gewählt
        else:
            eigene_wahl = vm != self.speaches.model or (
                "piper" not in vm.lower() and (vv or self.speaches.voice) != self.speaches.voice)
        kandidaten = []
        if self.antwort_tts is not None and not eigene_wahl:
            kandidaten.append(self.antwort_tts)
        kandidaten.append(self.speaches)
        return [k for k in kandidaten if k.state.tts_ok()]

    def _play_speaches_sentence(self, sentence: str, recorder: ReplyRecorder | None = None) -> bool:
        # Datei-Weg (Satelliten, Senke ohne Strom): erst der Antwort-Dienst,
        # wenn er dran ist, dann speaches — dieselbe Reihenfolge wie im Strom.
        audio_data = None
        for dienst in self.strom_kandidaten():
            audio_data = dienst.synth(sentence)
            if audio_data:
                break
        if not audio_data:
            return False
        if recorder:
            recorder.add_wav_bytes(sentence, audio_data)
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

    def speak(self, text: str, restore_leds: bool = True, turn: int | None = None) -> None:
        """turn: Nummer des Turns aus state.turn_control (None = kein
        abbrechbarer Turn, siehe state.turn_stopped).

        Vor jedem Satz wird geprueft, ob der Turn abgebrochen wurde. Das
        Abwuergen der LAUFENDEN Datei macht die Senke (AudioSink.stop); diese
        Pruefung verhindert, dass danach der naechste Satz anfaengt — sonst
        wuerde ein Abbruch nur eine Luecke in die Antwort schneiden.
        """
        from voice_assistant.services.leds import (
            LED_ANSWER_GLOW, LED_AUDIO_OUT, LED_CONFIRMATION, LED_OPENCLAW,
        )
        if turn_stopped(turn):
            return
        if self.strom_oeffnen is not None and self.antwort_tts is not None:
            # Bestaetigung, Quittungen, Ansagen: derselbe Weg wie die Antworten
            # (Strom, Antwort-Dienst zuerst), seit 2026-11-09. Die Sitzung
            # nimmt tts_lock selbst und prueft den Abbruch im Lock; kann die
            # Senke keinen Strom (Satellit), spielt sie Dateien.
            clean = self.tts_prefix + clean_for_tts(text)
            if not clean.strip():
                return
            print(f"🔊 Speaking: '{clean}'")
            sitz = self.stream_session(restore_leds=restore_leds, turn=turn)
            for satz in split_into_sentences(clean):
                if turn_stopped(turn):
                    break
                sitz.feed(satz, bereinigt=True)
            sitz.end()
            return
        with tts_lock:
            # Zweite Pruefung INNERHALB des Locks, und die ist der eigentliche
            # Punkt: auf tts_lock wird gewartet, oft Sekunden (die Bestaetigung
            # haelt ihn, waehrend sie Satz fuer Satz spricht). Ein Abbruch in
            # dieser Wartezeit wuerde von der Pruefung oben nicht gesehen — der
            # Aufrufer haette sie passiert, als der Turn noch lebte. Live
            # beobachtet am 2026-09-20 15:45:49: der Abbruch kam 0,5 s bevor
            # die Bestaetigung den Lock freigab, und der erste Satz der
            # abgebrochenen Antwort ("Alles klar, Jochen.") wurde trotzdem
            # gesprochen.
            if turn_stopped(turn):
                return
            clean = self.tts_prefix + clean_for_tts(text)
            if not clean.strip():
                return
            print(f"🔊 Speaking: '{clean}'")
            self.leds.set_phase(LED_CONFIRMATION if not restore_leds else LED_ANSWER_GLOW)

            sentences = split_into_sentences(clean)
            print(f"🔊 {len(sentences)} sentence(s)")

            rec = ReplyRecorder(active=restore_leds)
            played = False
            if self.speaches.state.tts_ok():
                print("🔄 TTS: Speaches (sentence by sentence)...")
                for i, sentence in enumerate(sentences):
                    if turn_stopped(turn):
                        print(f"🛑 Abbruch — Rest der Ansage entfaellt ({len(sentences) - i} Satz/Sätze)")
                        break
                    print(f"🔊 Sentence {i + 1}/{len(sentences)}: '{sentence}'")
                    if restore_leds:
                        self.leds.set_phase(LED_AUDIO_OUT)
                    ok = self._play_speaches_sentence(sentence, recorder=rec)
                    if not ok:
                        print(f"⚠️  Speaches failed at sentence {i + 1} → Piper fallback")
                        remaining = " ".join(sentences[i:])
                        tmp_wav = piper_synth(remaining)
                        if tmp_wav:
                            rec.add_file(remaining, tmp_wav)
                            self.play_wav(tmp_wav)
                            os.unlink(tmp_wav)
                        break
                played = True

            if not played and not turn_stopped(turn):
                print("🔄 TTS: Piper (local)...")
                if restore_leds:
                    self.leds.set_phase(LED_AUDIO_OUT)
                tmp_wav = piper_synth(clean)
                if tmp_wav:
                    rec.add_file(clean, tmp_wav)
                    self.play_wav(tmp_wav)
                    os.unlink(tmp_wav)
                else:
                    print("❌ TTS completely failed")

            rec.finalize()

            # Confirmation fertig → OpenClaw wartet noch; Antwort fertig → assistant.py übernimmt
            if not restore_leds:
                self.leds.set_phase(LED_OPENCLAW)

    def stream_session(self, restore_leds: bool = True, turn: int | None = None) -> "ReplyStreamSession":
        """Liefert eine Sitzung, die Antwort-Sätze abspielt, sobald sie aus dem
        OpenClaw-Stream eintreffen (statt erst nach Volltext wie speak())."""
        return ReplyStreamSession(self, restore_leds, turn)


class ReplyStreamSession:
    """Spielt Antwort-Sätze inkrementell ab. Spiegelt ReplySpeaker.speak(),
    aber satzweise getrieben durch den OpenClaw-Stream. feed() pro fertigem Satz,
    end() einmal am Schluss. Jeder Satz wird hier verbalisiert (clean_for_tts) —
    der Stream-Buffer liefert Rohsätze."""

    def __init__(self, sp: "ReplySpeaker", restore_leds: bool, turn: int | None = None) -> None:
        self.sp = sp
        self.restore_leds = restore_leds
        self.turn = turn
        self.spoke = False
        self._led_set = False
        self._first = True
        self.rec = ReplyRecorder(active=restore_leds)
        # Strom-Weg (ReplySpeaker.strom_oeffnen): EIN Strom traegt die Saetze
        # hintereinander und haelt tts_lock, bis der Player fertig ist (die
        # Senke ruft dann bei_ende, aus ihrem Thread). Schliesst er sich im
        # Leerlauf (Brain denkt zwischen zwei Saetzen), oeffnet der naechste
        # Satz einen neuen.
        self._strom = None
        self._strom_ende: threading.Event | None = None

    def _strom_abwarten(self) -> None:
        if self._strom_ende is not None:
            self._strom_ende.wait()
        self._strom = None
        self._strom_ende = None

    def _strom_holen(self):
        """Laufender oder neuer Strom, belegt fuer einen Satz (beginnen()).
        None = Datei-Weg; "stop" = Turn abgebrochen."""
        if self._strom is not None and self._strom.beginnen():
            return self._strom
        self._strom_abwarten()
        if self.sp.strom_oeffnen is None:
            return None
        tts_lock.acquire()
        # Zweite Pruefung im Lock — siehe ReplySpeaker.speak().
        if turn_stopped(self.turn):
            tts_lock.release()
            return "stop"
        ende = threading.Event()

        def bei_ende() -> None:
            tts_lock.release()
            ende.set()

        strom = self.sp.strom_oeffnen(bei_ende)
        if strom is None or not strom.beginnen():
            if strom is None:
                tts_lock.release()
            else:
                strom.abbrechen()
                ende.wait()
            return None
        self._strom, self._strom_ende = strom, ende
        return strom

    def _satz_in_strom(self, clean: str, strom) -> None:
        geschrieben: list[bytes] = []
        for dienst in self.sp.strom_kandidaten():
            try:
                try:
                    for block in dienst.synth_strom(clean):
                        if turn_stopped(self.turn) or not strom.schreiben(block):
                            return
                        geschrieben.append(block)
                    print(f"   ↳ {dienst.model}/{dienst.voice}")
                    return
                finally:
                    # Ein Eintrag je Satz im Recorder (finalize zaehlt Eintraege).
                    if geschrieben:
                        self.rec.add_pcm(clean, b"".join(geschrieben), (2, 2, STROM_RATE))
            except Exception as e:
                print(f"⚠️  TTS-Strom ({dienst.model}) fehlgeschlagen: {e}")
                if geschrieben:
                    return
        print("⚠️  → Piper fallback (in den Strom)")
        tmp_wav = piper_synth(clean)
        if not tmp_wav:
            return
        try:
            with wave.open(tmp_wav, "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
                block = zu_48k_stereo(pcm, wf.getframerate(), wf.getnchannels())
            if not turn_stopped(self.turn) and strom.schreiben(block):
                self.rec.add_pcm(clean, block, (2, 2, STROM_RATE))
        finally:
            os.unlink(tmp_wav)

    def feed(self, sentence: str, bereinigt: bool = False) -> None:
        """bereinigt: Satz kommt aus speak(), schon durch clean_for_tts und mit
        tts_prefix — nicht noch einmal."""
        from voice_assistant.services.leds import (
            LED_ANSWER_GLOW, LED_AUDIO_OUT, LED_CONFIRMATION,
        )
        if turn_stopped(self.turn):
            return
        # tts_prefix nur dem ersten Satz voranstellen (wie speak(): prefix ungecleant)
        if bereinigt:
            clean = sentence
        else:
            clean = (self.sp.tts_prefix if self._first else "") + clean_for_tts(sentence)
        self._first = False
        if not clean.strip():
            return
        strom = self._strom_holen()
        if strom == "stop":
            return
        if strom is not None:
            try:
                if not self._led_set:
                    self.sp.leds.set_phase(
                        LED_CONFIRMATION if not self.restore_leds else LED_ANSWER_GLOW
                    )
                    self._led_set = True
                if self.restore_leds:
                    self.sp.leds.set_phase(LED_AUDIO_OUT)
                print(f"🔊 Sentence (Strom): '{clean}'")
                self._satz_in_strom(clean, strom)
                self.spoke = True
            finally:
                strom.beenden()
            return
        with tts_lock:
            # Zweite Pruefung im Lock — siehe ReplySpeaker.speak(). Genau hier
            # ist ein abgebrochener Satz durchgekommen: feed() hatte die
            # Pruefung oben schon passiert und hing dann am Lock, den die
            # Bestaetigung hielt.
            if turn_stopped(self.turn):
                return
            if not self._led_set:
                self.sp.leds.set_phase(
                    LED_CONFIRMATION if not self.restore_leds else LED_ANSWER_GLOW
                )
                self._led_set = True
            print(f"🔊 Sentence (stream): '{clean}'")
            played = False
            if self.sp.speaches.state.tts_ok():
                if self.restore_leds:
                    self.sp.leds.set_phase(LED_AUDIO_OUT)
                ok = self.sp._play_speaches_sentence(clean, recorder=self.rec)
                if not ok:
                    print("⚠️  Speaches failed (stream) → Piper fallback")
                    tmp_wav = piper_synth(clean)
                    if tmp_wav:
                        self.rec.add_file(clean, tmp_wav)
                        self.sp.play_wav(tmp_wav)
                        os.unlink(tmp_wav)
                played = True
            if not played:
                if self.restore_leds:
                    self.sp.leds.set_phase(LED_AUDIO_OUT)
                tmp_wav = piper_synth(clean)
                if tmp_wav:
                    self.rec.add_file(clean, tmp_wav)
                    self.sp.play_wav(tmp_wav)
                    os.unlink(tmp_wav)
            self.spoke = True

    def end(self) -> bool:
        """Gibt True zurück, wenn mindestens ein Satz gesprochen wurde."""
        from voice_assistant.services.leds import LED_OPENCLAW
        if self._strom is not None:
            self._strom.schliessen()
        self._strom_abwarten()
        self.rec.finalize()
        if not self.restore_leds:
            self.sp.leds.set_phase(LED_OPENCLAW)
        return self.spoke


# ---------------------------------------------------------------------------
# Heartbeat phrases while OpenClaw is thinking
# ---------------------------------------------------------------------------

class ThinkingWorker:
    def __init__(
        self,
        play_wav: PlayWav,
        phrases: list,
        speaches: SpeachesTts | None = None,
        quiet_factor: float = 1.5,
        min_quiet: float = 10.0,
        max_quiet: float = 30.0,
        interval: float = 25.0,
        fest=None,  # Callable[[str], str | None]: gespeicherte Fassung (services/stimmen.py)
    ) -> None:
        self.play_wav = play_wav
        self._phrases = phrases
        # Feste Sätze in der Stimme des Antwort-Dienstes (Pfad im Cache, nicht
        # löschen). None/kein Treffer → wie bisher Speaches, dann Piper.
        self.fest = fest
        # Optionaler Speaches-TTS: Heartbeat-Phrasen folgen der aktiven Stimme.
        # Fällt auf lokalen Piper zurück, wenn None oder Speaches gerade fehlschlägt.
        self.speaches = speaches
        # Wartezeit vor dem ERSTEN Warte-Satz ist an die zuvor gesprochene Länge
        # (Echo/Ansage) gekoppelt: kurze Ansage → früher Lebenszeichen, lange
        # Ansage → mehr Luft. gap = clamp(ansage_dauer * factor, min, max).
        self._quiet_factor = quiet_factor
        self._min_quiet = min_quiet
        self._max_quiet = max_quiet
        # Abstand zwischen weiteren Warte-Sätzen — bewusst weiter auseinander.
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _render(self, phrase: str) -> str | None:
        """Rendert die Heartbeat-Phrase als WAV-Datei und gibt den Pfad zurück.

        Bevorzugt die gespeicherte Fassung des Antwort-Dienstes, dann Speaches
        (aktive Stimme); bei Fehler/None → Piper-Fallback.
        """
        if self.fest is not None:
            gespeichert = self.fest(phrase)
            if gespeichert:
                import shutil
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    pass
                shutil.copyfile(gespeichert, f.name)
                return f.name
        if self.speaches is not None and self.speaches.state.tts_ok():
            audio = self.speaches.synth(phrase)
            if audio:
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        f.write(audio)
                        return f.name
                except Exception as e:
                    print(f"⚠️  Heartbeat Speaches-WAV schreiben fehlgeschlagen: {e}")
        return piper_synth(phrase)

    def _loop(self) -> None:
        phrases = iter(self._phrases)
        fallback = self._phrases[-1] if self._phrases else "..."
        # tts_lock wird durchgehend gehalten, solange das Echo bzw. die Antwort
        # spricht. Wir messen diese gehaltene Zeit = die "vorher gesprochene Länge"
        # und koppeln die Wartezeit vor dem ersten Warte-Satz daran.
        speak_start = 0.0     # Beginn der laufenden Ansage
        spoken_len = 0.0      # Dauer der zuletzt von außen gesprochenen Ansage
        quiet_since = time.time()
        was_locked = False
        fired_count = 0       # Warte-Sätze seit der letzten Ansage
        while not self._stop.is_set():
            now = time.time()
            locked = tts_lock.locked()
            if locked and not was_locked:
                speak_start = now                # Ansage beginnt
            elif was_locked and not locked:
                spoken_len = now - speak_start    # Ansage endete → Länge merken
                quiet_since = now
                fired_count = 0                   # nach neuer Ansage wieder erlauben
            was_locked = locked

            if not locked:
                # Erster Warte-Satz: Wartezeit ∝ vorher gesprochener Länge,
                # geklemmt auf [min_quiet, max_quiet]. Danach wachsender Abstand
                # (×1.5 pro Wiederholung, max 120 s) — bei langen Agentic-Turns
                # nervt ein starrer 25-s-Takt mehr als er beruhigt.
                if fired_count:
                    gap = min(self._interval * (1.5 ** (fired_count - 1)), 120.0)
                else:
                    gap = max(self._min_quiet,
                              min(self._max_quiet, spoken_len * self._quiet_factor))
                if now - quiet_since >= gap:
                    phrase = next(phrases, fallback)
                    print(
                        f"💭 Heartbeat: '{phrase}' "
                        f"(Ansage {spoken_len:.1f}s → Wartezeit {gap:.1f}s)"
                    )
                    tmp_wav = self._render(phrase)
                    if tmp_wav:
                        lebenszeichen.set()
                        try:
                            with tts_lock:
                                if not self._stop.is_set():
                                    self.play_wav(tmp_wav)
                        finally:
                            lebenszeichen.clear()
                        os.unlink(tmp_wav)
                    quiet_since = time.time()
                    fired_count += 1
                    was_locked = False            # eigene Ausgabe nicht als Ansage zählen
            if self._stop.wait(timeout=1.0):
                return

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
