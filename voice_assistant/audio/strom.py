"""Wiedergabe als Strom: Audio geht zum ESP, während es noch entsteht.

Bis 2026-11-08 wurde jeder Satz erst komplett synthetisiert, als Datei
abgelegt und dann per URL abgespielt. Bei Piper (0,17 s je Satz) merkt man das
nicht, bei CosyVoice schon: der erste Ton käme erst nach dem ganzen ersten Satz
(Median 1,6 s, p90 2,9 s). Ein Strom spielt ab, was schon da ist.

Gemessen am Fablab-ReSpeaker (Strom-Probe 2026-11-08, ESPHome 2026.9.1):

- Der Media-Player spielt eine WAV, die noch wächst: Kopf mit Platzhalter-Länge,
  kein ``Content-Length``, das Schließen der Verbindung ist das Ende.
- **Nur 48 kHz Stereo 16 bit.** Ein Strom mit 24 kHz mono brachte
  „Incompatible stream settings“, der Player hing in einer Fehlerschleife bis
  zum nächsten Befehl.
- Er beginnt erst, wenn ein Startpuffer gefüllt ist: 1 s Audio, höchstens 3/4
  von ``buffer_size`` (Default 1 MB). Mit ``buffer_size: 128000`` sind es 0,5 s.
- Lücken ohne Daten (2 s, 8 s) brechen nichts ab; der Leser gibt erst nach 30 s
  ohne Daten auf.

Was dieser Strom dazu tut:

- **Er füllt Lücken selbst mit Stille**, sobald der Spielkopf das Gesendete
  einzuholen droht. Damit ist die gesendete Länge zugleich die gespielte, und
  das Ende lässt sich ausrechnen wie bei einer Datei (Falle 3 im CLAUDE.md:
  die Länge ist die Hauptgröße, der Player-Zustand nur der Anker).
- **Er schließt sich im Leerlauf selbst** (``leerlauf_s`` ohne neues Audio),
  damit eine lange Werkzeug-Phase des Brains nicht als minutenlange Stille im
  Lautsprecher hängt. Der nächste Satz öffnet einen neuen Strom.
- Solange jemand schreibt (``beschaeftigt``), schließt er sich nicht: die
  Synthese eines Satzes darf länger brauchen als der Leerlauf.

Die Klasse kennt weder ESP noch HTTP-Server: ``ausliefern(wfile)`` schreibt in
irgendeinen Datei-artigen Ausgang. So bleibt sie ohne Gerät testbar.
"""

from __future__ import annotations

import struct
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

RATE = 48000
KANAELE = 2
BYTES_JE_S = RATE * KANAELE * 2

# Stille wird in diesen Stücken nachgeschoben.
_STILLE_STUECK_S = 0.05
# So weit soll das Gesendete dem geschätzten Spielkopf voraus sein, bevor mit
# Stille aufgefüllt wird. Klein halten: jede Stille, die vor einem neuen Satz
# im Puffer liegt, verzögert ihn um genau diese Länge.
_VORSPRUNG_S = 0.15
# Spielbeginn nach dem ersten Audio-Byte: Startpuffer des Players (0,5 s mit
# buffer_size 128000) plus Holen/Decoder. Nur für das Auffüllen gebraucht.
_START_ANNAHME_S = 0.6


def wav_kopf() -> bytes:
    """WAV-Kopf mit Platzhalter-Längen, wie ihn auch Speaches beim Streamen schickt."""
    platz = 0x7FFFFFFF
    return (b"RIFF" + struct.pack("<I", platz) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, KANAELE, RATE, BYTES_JE_S, KANAELE * 2, 16)
            + b"data" + struct.pack("<I", platz))


def zu_48k_stereo(pcm: bytes, rate: int, kanaele: int) -> bytes:
    """int16-PCM auf 48 kHz Stereo. Nur ganze Blöcke umrechnen: an jeder
    Blockgrenze entsteht sonst ein kleiner Filter-Einschwinger. Deshalb fordert
    der Aufrufer 48 kHz beim TTS-Dienst an, dies ist der Rückfall."""
    s = np.frombuffer(pcm, dtype=np.int16)
    if kanaele > 1:
        s = s.reshape(-1, kanaele)[:, 0]
    if rate != RATE:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(rate, RATE)
        s = np.clip(resample_poly(s, RATE // g, rate // g), -32768, 32767).astype(np.int16)
    return np.column_stack([s, s]).astype(np.int16).tobytes()


class WiedergabeStrom:
    def __init__(self, leerlauf_s: float = 1.5, uhr: Callable[[], float] = time.monotonic) -> None:
        self._uhr = uhr
        self._leerlauf_s = leerlauf_s
        self._cv = threading.Condition()
        self._puffer: deque[bytes] = deque()
        self._zu = False
        self._abgebrochen = False
        self.beschaeftigt = False
        self.gesendet_bytes = 0
        self.audio_bytes = 0
        # vom Strom selbst nachgeschobene Stille (Spielkopf holte das
        # Geschriebene ein). Der Satz-Planer misst daran echte Aussetzer.
        self.aufgefuellt_bytes = 0
        self.erstes_audio: float | None = None   # Uhrzeit des ersten gesendeten Audio-Bytes
        self.letzte_daten: float = uhr()
        self.abgeholt = threading.Event()        # der Player hat die Adresse geholt
        self.fertig = threading.Event()          # Auslieferung beendet (zu oder abgebrochen)

    # --- Schreiber-Seite -------------------------------------------------
    def schreiben(self, pcm48_stereo: bytes) -> bool:
        """False, wenn der Strom schon zu ist — dann einen neuen öffnen."""
        if not pcm48_stereo:
            return not self._zu
        with self._cv:
            if self._zu:
                return False
            self._puffer.append(pcm48_stereo)
            self.letzte_daten = self._uhr()
            self._cv.notify_all()
            return True

    def beginnen(self) -> bool:
        """Ein Satz wird geschrieben: so lange schliesst kein Leerlauf den
        Strom. False = schon zu, einen neuen oeffnen."""
        with self._cv:
            if self._zu:
                return False
            self.beschaeftigt = True
            return True

    def beenden(self) -> None:
        with self._cv:
            self.beschaeftigt = False
            self.letzte_daten = self._uhr()
            self._cv.notify_all()

    def lebt(self) -> bool:
        with self._cv:
            return not self._zu

    def schliessen(self) -> None:
        with self._cv:
            self._zu = True
            self._cv.notify_all()

    def abbrechen(self) -> None:
        with self._cv:
            self._zu = True
            self._abgebrochen = True
            self._puffer.clear()
            self._cv.notify_all()

    @property
    def abgebrochen(self) -> bool:
        return self._abgebrochen

    @property
    def gesendet_s(self) -> float:
        return self.gesendet_bytes / BYTES_JE_S

    def position_s(self) -> float:
        """Länge alles bisher Geschriebenen (gesendet + noch im Puffer), auch
        nachgeschobene Stille. Der Satz-Planer (services/satz_planer.py) misst
        daran, wie viel Pause seit dem letzten Satz schon im Strom liegt."""
        with self._cv:
            return (self.gesendet_bytes + sum(len(b) for b in self._puffer)) / BYTES_JE_S

    def vorsprung_s(self) -> float:
        """So viel Geschriebenes liegt noch vor dem geschätzten Spielkopf.
        0, solange nichts gesendet ist (der Player hat dann noch nichts)."""
        with self._cv:
            if self.erstes_audio is None:
                return 0.0
            spielkopf = max(0.0, self._uhr() - self.erstes_audio - _START_ANNAHME_S)
            geschrieben = (self.gesendet_bytes + sum(len(b) for b in self._puffer)) / BYTES_JE_S
            return max(0.0, geschrieben - spielkopf)

    @property
    def aufgefuellt_s(self) -> float:
        with self._cv:
            return self.aufgefuellt_bytes / BYTES_JE_S

    def ende_geschaetzt(self) -> float | None:
        """Uhrzeit, zu der der Player das Gesendete fertig gespielt haben sollte."""
        if self.erstes_audio is None:
            return None
        return self.erstes_audio + _START_ANNAHME_S + self.gesendet_s

    # --- Auslieferung ----------------------------------------------------
    def _naechstes(self) -> tuple[bytes, bool] | None:
        """Nächstes Stück für den Ausgang und ob es Audio ist (sonst Stille);
        None = Ende der Auslieferung."""
        with self._cv:
            while True:
                if self._abgebrochen:
                    return None
                if self._puffer:
                    return self._puffer.popleft(), True
                jetzt = self._uhr()
                if self._zu:
                    return None
                if (not self.beschaeftigt and self.erstes_audio is not None
                        and jetzt - self.letzte_daten >= self._leerlauf_s):
                    self._zu = True
                    return None
                if self.erstes_audio is not None:
                    spielkopf = jetzt - self.erstes_audio - _START_ANNAHME_S
                    if self.gesendet_s - spielkopf < _VORSPRUNG_S:
                        return bytes(int(BYTES_JE_S * _STILLE_STUECK_S)), False
                self._cv.wait(_STILLE_STUECK_S / 2)

    def ausliefern(self, ausgang) -> None:
        """Schreibt Kopf und Audio in ``ausgang`` (wfile), bis der Strom endet.

        Läuft im Thread des HTTP-Servers. Ein abgerissener Ausgang (STOP am
        Player, Verbindung weg) bricht den Strom ab."""
        self.abgeholt.set()
        try:
            ausgang.write(wav_kopf())
            ausgang.flush()
            while True:
                naechstes = self._naechstes()
                if naechstes is None:
                    break
                stueck, ist_audio = naechstes
                ausgang.write(stueck)
                ausgang.flush()
                with self._cv:
                    if self.erstes_audio is None:
                        self.erstes_audio = self._uhr()
                    self.gesendet_bytes += len(stueck)
                    if ist_audio:
                        self.audio_bytes += len(stueck)
                    else:
                        self.aufgefuellt_bytes += len(stueck)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.abbrechen()
        finally:
            self.fertig.set()
