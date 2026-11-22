"""Satz-Planer für Antworten als Strom (``satz_planer``, seit 2026-11-22).

Anlass: ein Antwort-Dienst, der einzelne Sätze langsamer erzeugt, als sie
gesprochen werden (CosyVoice auf älteren GPUs: RTF ~1,2 je Satz). Im Strom
heißt das: der Spielkopf holt das Erzeugte mitten im Satz ein, der Strom füllt
mit Stille auf — hörbar als Stottern mitten im Wort.

Der Planer verschiebt diese Lücken an die Satzgrenzen:

- Jeder Satz wird angefragt, sobald er da ist und der vorige erstes Audio hat
  (höchstens einer voraus). Ein Dienst, der gleichzeitige Anfragen als Pipeline
  rechnet, ist so über die ganze Antwort schneller als Echtzeit, auch wenn es
  einzelne Sätze nicht sind.
- Ein Satz geht erst in den Strom, wenn er ohne Lücke durchlaufen kann
  (``freigeben``). Bis dahin füllt der Strom Stille auf — eine Pause vor dem
  Satz statt eines Aussetzers darin.
- Zwischen zwei Sätzen liegt mindestens ``satz_pause_s``. Hörprobe 2026-11-22
  (Jochen): mit Pausen „sogar besser als durchgesprochen … wirkt fast wie ein
  rhetorisch geübter Sprecher“. Bei einem schnellen Dienst bleibt nur diese
  Mindestpause übrig.

Die Klassen hier kennen weder Strom noch HTTP; die Verdrahtung steht in
``ReplyStreamSession`` (services/tts.py).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# Gesprochene Sekunden je Zeichen, bevor die erste Messung da ist. Deutsch,
# CosyVoice gemessen 0,09–0,11 s; lieber zu lang schätzen (dann wartet der
# Planer etwas länger) als zu kurz (dann stottert es).
STANDARD_S_JE_ZEICHEN = 0.10
# Ab so viel erzeugtem Audio gilt das gemessene Tempo des laufenden Satzes.
_TEMPO_AB_S = 1.0
# Sicherheitsabstand für Schwankungen (Netz, GPU nebenan beschäftigt).
RESERVE_S = 0.2


def freigeben(gepuffert_s: float, vorsprung_s: float, dauer_s: float, tempo: float,
              reserve_s: float = RESERVE_S) -> bool:
    """Darf ein Satz jetzt in den Strom, ohne mitten drin leerzulaufen?

    gepuffert_s: von diesem Satz schon erzeugtes Audio.
    vorsprung_s: was vor ihm noch abgespielt wird (Rest im Strom + Pause).
    dauer_s: geschätzte Gesamtlänge des Satzes.
    tempo: erzeugte Audio-Sekunden je Sekunde (1/RTF).

    Bei konstantem Tempo ist die knappste Stelle das Satzende: bis dahin
    vergehen vorsprung + dauer Sekunden, in denen tempo·(…) dazukommt. Also
    gepuffert + tempo·(vorsprung + dauer) ≥ dauer, umgestellt wie unten.
    """
    if gepuffert_s <= 0:
        return False
    if tempo >= 1.0:
        return True
    return gepuffert_s + tempo * vorsprung_s >= (1.0 - tempo) * dauer_s + reserve_s


class Schaetzer:
    """Gleitende Mittel über die Sätze hinweg: Tempo des Dienstes und
    Sekunden je Zeichen der Stimme. Lebt im ReplySpeaker, also über Antworten
    hinweg; der erste Satz nach dem Start nimmt die Startwerte."""

    def __init__(self, tempo_start: float = 1.0, s_je_zeichen: float = STANDARD_S_JE_ZEICHEN,
                 gewicht: float = 0.3) -> None:
        self._lock = threading.Lock()
        self._tempo = tempo_start
        self._s_je_zeichen = s_je_zeichen
        self._gewicht = gewicht

    @property
    def tempo(self) -> float:
        with self._lock:
            return self._tempo

    @property
    def s_je_zeichen(self) -> float:
        with self._lock:
            return self._s_je_zeichen

    def dauer(self, text: str) -> float:
        return len(text) * self.s_je_zeichen

    def lernen(self, zeichen: int, audio_s: float, tempo: float | None) -> None:
        g = self._gewicht
        with self._lock:
            if zeichen >= 15 and audio_s > 0.5:
                self._s_je_zeichen = (1 - g) * self._s_je_zeichen + g * (audio_s / zeichen)
            if tempo is not None and tempo > 0:
                self._tempo = (1 - g) * self._tempo + g * tempo


class SatzPuffer:
    """Audio eines Satzes, wie es vom Dienst kommt (48 kHz Stereo int16),
    mit den Zeiten, die der Planer braucht. Ein Erzeuger schreibt, ein
    Schreiber liest."""

    def __init__(self, text: str, bytes_je_s: int, uhr: Callable[[], float] = time.monotonic) -> None:
        self.text = text
        self._bytes_je_s = bytes_je_s
        self._uhr = uhr
        self._cv = threading.Condition()
        self.bloecke: list[bytes] = []
        self._bytes = 0
        self._erster_block_s = 0.0
        self.erstes_audio: float | None = None   # Uhrzeit des ersten Blocks
        self.letztes_audio: float | None = None
        self.fertig = False
        self.abgebrochen = False
        self.dienst: str | None = None           # "modell/stimme" für das Journal
        self.unterwegs = threading.Event()        # erstes Audio da oder fertig: Nachfolger darf anfragen

    # --- Erzeuger ----------------------------------------------------------
    def dazu(self, block: bytes) -> None:
        if not block:
            return
        with self._cv:
            jetzt = self._uhr()
            if self.erstes_audio is None:
                self.erstes_audio = jetzt
                self._erster_block_s = len(block) / self._bytes_je_s
            self.letztes_audio = jetzt
            self.bloecke.append(block)
            self._bytes += len(block)
            self._cv.notify_all()
        self.unterwegs.set()

    def ende(self) -> None:
        with self._cv:
            self.fertig = True
            self._cv.notify_all()
        self.unterwegs.set()

    def abbrechen(self) -> None:
        with self._cv:
            self.abgebrochen = True
            self.fertig = True
            self._cv.notify_all()
        self.unterwegs.set()

    # --- Schreiber ---------------------------------------------------------
    @property
    def gepuffert_s(self) -> float:
        with self._cv:
            return self._bytes / self._bytes_je_s

    def tempo_gemessen(self) -> float | None:
        """Erzeugungstempo dieses Satzes seit dem ersten Block, sobald genug da ist."""
        with self._cv:
            if self.erstes_audio is None:
                return None
            erzeugt = self._bytes / self._bytes_je_s - self._erster_block_s
            dauer = (self.letztes_audio if self.fertig else self._uhr()) - self.erstes_audio
            if erzeugt < _TEMPO_AB_S or dauer <= 0:
                return None
            return erzeugt / dauer

    def warten(self, timeout: float) -> None:
        with self._cv:
            self._cv.wait(timeout)

    def ab(self, index: int) -> tuple[list[bytes], bool]:
        """Blöcke ab index und ob der Satz vollständig ist."""
        with self._cv:
            return self.bloecke[index:], self.fertig
