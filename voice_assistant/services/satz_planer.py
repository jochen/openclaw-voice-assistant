"""Satz-Planer für Antworten als Strom (``satz_planer``, seit 2026-11-22).

Anlass: ein Antwort-Dienst, der einzelne Sätze langsamer erzeugt, als sie
gesprochen werden (CosyVoice auf älteren GPUs: RTF ~1,2 je Satz). Im Strom
heißt das: der Spielkopf holt das Erzeugte mitten im Satz ein, der Strom füllt
mit Stille auf — hörbar als Aussetzer mitten im Wort.

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

**Schübe statt gleichmäßigem Fluss** (nach dem ersten Live-Test, Jochen hörte
kurze Aussetzer): CosyVoice liefert einen Satz in wachsenden Schüben — 0,5 s,
1 s, 2 s, 4 s Audio auf einmal (``token_hop_len`` verdoppelt sich je Abschnitt).
Zwischen zwei Schüben kommt sekundenlang nichts. Ein Planer, der nur mit dem
mittleren Tempo rechnet, gibt zu früh frei und läuft vor dem letzten großen
Schub leer. Deshalb fasst ``SatzPuffer`` dicht nacheinander ankommende Blöcke zu
Schüben zusammen, ``Schaetzer`` lernt, wie sie wachsen, und ``freigeben`` spielt
die noch fehlenden Schübe durch: vor jedem muss genug Gepuffertes liegen.

Die Klassen hier kennen weder Strom noch HTTP; die Verdrahtung steht in
``ReplyStreamSession`` (services/tts.py).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# Gesprochene Sekunden je Zeichen, bevor die erste Messung da ist. Deutsch,
# CosyVoice gemessen 0,06–0,11 s je nach Stimme; lieber zu lang schätzen
# (dann wartet der Planer etwas länger) als zu kurz (dann setzt es aus).
STANDARD_S_JE_ZEICHEN = 0.10
# Abstand, unter dem ankommende Blöcke als ein Schub gelten (ein Dienst-Abschnitt
# kommt als viele HTTP-Lesestücke kurz nacheinander an).
SCHUB_LUECKE_S = 0.15
# Startwerte für die Schübe: Verdopplung je Abschnitt, höchstens 4 s.
STANDARD_WACHSTUM = 2.0
STANDARD_SCHUB_MAX_S = 4.0
# So viel muss vor jedem erwarteten Schub noch gepuffert liegen. Der Strom
# schiebt schon unter 0,15 s Vorsprung Stille ein (audio/strom.py), dazu
# Netz-Schwankung.
RESERVE_S = 0.3


def freigeben(jetzt: float, vorsprung_s: float, gepuffert_s: float, letzter_schub_t: float | None,
              letzter_schub_s: float, dauer_s: float, tempo: float, wachstum: float = STANDARD_WACHSTUM,
              schub_max_s: float = STANDARD_SCHUB_MAX_S, reserve_s: float = RESERVE_S) -> bool:
    """Darf ein Satz jetzt in den Strom, ohne mitten drin leerzulaufen?

    jetzt: Uhrzeit. vorsprung_s: was vor dem Satz noch abgespielt wird (Rest
    im Strom + noch zu schreibende Pause) — der Satz beginnt also um
    jetzt + vorsprung_s. gepuffert_s: von ihm schon erzeugt. letzter_schub_t/_s:
    Ankunft und Größe des letzten Schubs. dauer_s: geschätzte Gesamtlänge.
    tempo: erzeugte Audio-Sekunden je Sekunde (1/RTF).

    Die fehlenden Schübe werden durchgespielt: der nächste ist
    min(wachstum × letzter, schub_max) groß und kommt Größe/tempo nach dem
    vorigen. Vor jedem muss das bis dahin Gepufferte noch reserve_s länger
    reichen, als bis zu seiner Ankunft vergeht.
    """
    if gepuffert_s <= 0 or letzter_schub_t is None:
        return False
    tempo = max(tempo, 0.05)
    start = jetzt + vorsprung_s
    rest = dauer_s - gepuffert_s
    vorrat = gepuffert_s
    ankunft = letzter_schub_t
    schub = max(letzter_schub_s, 0.05)
    while rest > 1e-6:
        schub = min(max(schub * wachstum, schub), schub_max_s, rest)
        ankunft += schub / tempo
        if start + vorrat < ankunft + reserve_s:
            return False
        vorrat += schub
        rest -= schub
    return True


class Schaetzer:
    """Gleitende Mittel über die Sätze hinweg: Tempo des Dienstes, Sekunden
    je Zeichen der Stimme und wie seine Schübe wachsen. Lebt im ReplySpeaker,
    also über Antworten hinweg; der erste Satz nach dem Start nimmt die
    Startwerte."""

    def __init__(self, tempo_start: float = 1.0, s_je_zeichen: float = STANDARD_S_JE_ZEICHEN,
                 gewicht: float = 0.3) -> None:
        self._lock = threading.Lock()
        self._tempo = tempo_start
        self._s_je_zeichen = s_je_zeichen
        self._wachstum = STANDARD_WACHSTUM
        self._schub_max = STANDARD_SCHUB_MAX_S
        self._gewicht = gewicht

    @property
    def tempo(self) -> float:
        with self._lock:
            return self._tempo

    @property
    def s_je_zeichen(self) -> float:
        with self._lock:
            return self._s_je_zeichen

    @property
    def wachstum(self) -> float:
        with self._lock:
            return self._wachstum

    @property
    def schub_max(self) -> float:
        with self._lock:
            return self._schub_max

    def dauer(self, text: str) -> float:
        return len(text) * self.s_je_zeichen

    def lernen(self, zeichen: int, audio_s: float, tempo: float | None,
               wachstum: float | None = None, schub_max: float | None = None) -> None:
        g = self._gewicht
        with self._lock:
            if zeichen >= 15 and audio_s > 0.5:
                self._s_je_zeichen = (1 - g) * self._s_je_zeichen + g * (audio_s / zeichen)
            if tempo is not None and tempo > 0:
                self._tempo = (1 - g) * self._tempo + g * tempo
            if wachstum is not None and wachstum > 0:
                self._wachstum = (1 - g) * self._wachstum + g * min(max(wachstum, 1.0), 4.0)
            if schub_max is not None and schub_max > 0:
                # das Maximum nicht unterschätzen: ein großer Schub zählt sofort voll
                self._schub_max = max(schub_max, (1 - g) * self._schub_max + g * schub_max)


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
        self.schuebe: list[list[float]] = []      # [ankunft_ende, audio_s] je Schub
        self.erstes_audio: float | None = None
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
            dauer = len(block) / self._bytes_je_s
            if self.erstes_audio is None:
                self.erstes_audio = jetzt
            if self.schuebe and jetzt - self.schuebe[-1][0] < SCHUB_LUECKE_S:
                self.schuebe[-1][0] = jetzt
                self.schuebe[-1][1] += dauer
            else:
                self.schuebe.append([jetzt, dauer])
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

    def letzter_schub(self) -> tuple[float | None, float]:
        with self._cv:
            if not self.schuebe:
                return None, 0.0
            return self.schuebe[-1][0], self.schuebe[-1][1]

    def tempo_gemessen(self) -> float | None:
        """Erzeugungstempo dieses Satzes über die Schübe nach dem ersten
        (ab dem zweiten Schub; der erste trägt die Anlaufzeit nicht)."""
        with self._cv:
            if len(self.schuebe) < 2:
                return None
            erzeugt = sum(s for _, s in self.schuebe[1:])
            dauer = self.schuebe[-1][0] - self.schuebe[0][0]
            return erzeugt / dauer if dauer > 0 else None

    def wachstum_gemessen(self) -> float | None:
        """Mittleres Verhältnis aufeinanderfolgender Schübe (ohne den letzten,
        der ist oft nur der Rest des Satzes)."""
        with self._cv:
            groessen = [s for _, s in self.schuebe[:-1]]
        if len(groessen) < 2:
            return None
        verh = [b / a for a, b in zip(groessen, groessen[1:]) if a > 0]
        return sum(verh) / len(verh) if verh else None

    def schub_max(self) -> float | None:
        with self._cv:
            return max((s for _, s in self.schuebe), default=None)

    def warten(self, timeout: float) -> None:
        with self._cv:
            self._cv.wait(timeout)

    def ab(self, index: int) -> tuple[list[bytes], bool]:
        """Blöcke ab index und ob der Satz vollständig ist."""
        with self._cv:
            return self.bloecke[index:], self.fertig
