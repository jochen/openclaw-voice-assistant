"""Barge-in: Wakeword-Erkennung, waehrend der Assistent selbst dran ist.

Wozu
----
Bis hierher gab es genau ein Zeitfenster, in dem ein Turn abgebrochen werden
konnte: das Stopp-Wort in der laufenden Aufnahme (``_is_stop_command`` in
``assistant.py``, geprueft auf dem Transkript in STATE_PROCESSING). War die
Aufnahme vorbei, lief der Turn zu Ende — Bestaetigung, Denk-Phrasen, Antwort,
und was der Brain dabei tut.

Genau das wurde zum Problem, als das „Ja?" nach dem Wakewort bei
durchgesprochenen Ein-Satz-Kommandos wegfiel (siehe ``_ACK_DELAY_SEC``): ohne
diese hoerbare Quittung merkt der Nutzer einen Fehltrigger oft erst, wenn der
Assistent schon antwortet — und dann gibt es keinen Weg mehr raus.

Wie
---
Das Fenster ist STATE_WAITING, also Bestaetigung + Denken + Vorlesen. Dort
lief das Mikrofon bisher ins Leere (die Chunks wurden gelesen und verworfen).
Hier bekommt es dieselbe Wakeword-Erkennung wie im Leerlauf, mit denselben
Gates (``wake_gate.gate_passed``, ``wake_rms.loudest_window_rms``) — der
Abbruch soll weder schwerer noch leichter ausloesbar sein als der Ruf selbst.

Das Wort „Stopp" pruefen wir NICHT hier, sondern spaeter am Transkript: der
Trigger feuert auf „Gaston", und das davor gesprochene „Stopp" steckt im
Pre-Roll (``_PRE_ROLL_SEC``, dieselbe Begruendung wie beim durchgesprochenen
Kommando). Deshalb funktionieren „Stopp Gaston" und „Gaston, Stopp"
gleichermassen, und deshalb ist ein Barge-in ohne Stopp-Wort einfach ein
neuer Auftrag statt eines Abbruchs.

Ein eigenes, nachtrainiertes Bundle („stopp_gaston") ist hier kein Sonderfall,
sondern ein Eintrag unter ``barge_in.wakewords`` — der Code kennt keinen
Bundle-Namen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from voice_assistant.config import RATE_OW
from voice_assistant.wake_gate import gate_passed, required_peak
from voice_assistant.wake_rms import loudest_window_rms


@dataclass
class BargeInHit:
    """Ein Streak, der beide Gates bestanden hat."""

    bundle: str
    hits: int
    peak: float
    threshold: float
    min_hits: int
    rms: float
    scores: list = field(default_factory=list)


@dataclass
class BargeInMiss:
    """Ein Streak, der ein Gate NICHT bestanden hat.

    Wird archiviert statt verworfen, aus demselben Grund wie der Near-Miss im
    Leerlauf: ein Abbruch, der nicht ankam, ist der Fall, den man messen muss
    — nicht der, der klappte.
    """

    bundle: str
    hits: int
    peak: float
    threshold: float
    min_hits: int
    rms: float
    failed_on: str  # "min_hits" | "min_peak" | "min_rms"
    scores: list = field(default_factory=list)


class BargeInDetector:
    """Wakeword-Streaks im eigenen Sprech-/Denkfenster.

    Bewusst eine EIGENE Engine-Instanz und ein eigener Ringpuffer, nicht die
    des Leerlaufs: sonst traegt der Streak-Zaehler Zustand ueber den Wechsel
    der Betriebsart hinweg, und ein halber Streak von vor der Antwort wuerde
    mitten in der Antwort fertig gezaehlt.
    """

    # Ringpuffer-Laenge. Muss den Pre-Roll (1,5 s) und das 300-ms-Pegelfenster
    # bequem fassen; 3 s ist dieselbe Groesse wie der wake_ring im Leerlauf.
    RING_SECONDS = 3.0

    # Sicherheits-Timeout wie im Leerlauf: ein Streak, der nicht enden will,
    # wird nach ~2 s entschieden, statt bis zum Ende der Antwort zu warten.
    MAX_STREAK = 25

    def __init__(self, engine, rms_min: float = 0.0) -> None:
        self._engine = engine
        self._rms_min = rms_min
        self._ring: deque = deque()
        self._ring_samples = 0
        self._ring_max = int(RATE_OW * self.RING_SECONDS)
        self._scores: deque = deque(maxlen=20)
        # Hoechster Score, den die Engine seit dem letzten reset() gesehen hat —
        # unabhaengig davon, ob ein Streak zustande kam. Ohne diesen Wert kann
        # ein Messwerkzeug "nichts gehoert" nicht von "gehoert, aber unter der
        # Schwelle" unterscheiden, und ein Urteil ueber Selbst-Trigger waere
        # dann eine Behauptung (siehe tools/bargein_echo_test.py).
        self.max_score = 0.0
        self.reset()

    @property
    def ring(self) -> deque:
        """Ringpuffer fuer Pre-Roll und Archiv (16 kHz mono int16)."""
        return self._ring

    def reset(self) -> None:
        """Streak-Zaehler und Ringpuffer leeren.

        Gehoert an jeden Wechsel in das und aus dem Barge-in-Fenster: was vor
        dem Fenster im Puffer lag, ist die eigene letzte Ansage bzw. die
        Aufnahme des Nutzers und hat im Pre-Roll eines Abbruchs nichts zu
        suchen.
        """
        self._engine.reset()
        self._ring.clear()
        self._ring_samples = 0
        self._scores.clear()
        self._hits = 0
        self._peak = 0.0
        self.max_score = 0.0
        self._gap_used = False
        self._bundle = ""
        self._threshold = 0.0
        self._min_hits = 3
        self._min_peak = 0.0
        self._min_peak_short = 0.0
        self._min_peak_single = 0.0

    def level_rms(self) -> float:
        """RMS des lautesten 300-ms-Fensters im Ring — wie im Leerlauf."""
        if not self._ring:
            return 0.0
        samples = (
            np.concatenate(list(self._ring)) if len(self._ring) > 1 else self._ring[0]
        )
        return loudest_window_rms(samples, rate=RATE_OW, window_ms=300)

    def feed(self, chunk: np.ndarray):
        """Einen 16-kHz-Chunk verarbeiten.

        Liefert BargeInHit (beide Gates bestanden), BargeInMiss (ein Gate
        nicht bestanden) oder None (nichts entschieden).
        """
        if len(chunk) > 0:
            self._ring.append(chunk.copy())
            self._ring_samples += len(chunk)
            while self._ring_samples > self._ring_max and len(self._ring) > 1:
                self._ring_samples -= len(self._ring.popleft())

        hit = self._engine.feed(chunk)
        if hit is None:
            return None  # noch nicht genug Samples fuer eine Prediction

        self._scores.append(hit.score)
        self.max_score = max(self.max_score, hit.score)
        if hit.score > hit.threshold:
            self._hits += 1
            self._peak = max(self._peak, hit.score)
            self._bundle = hit.name
            self._threshold = hit.threshold
            self._min_hits = hit.min_hits
            self._min_peak = hit.min_peak
            self._min_peak_short = hit.min_peak_short
            self._min_peak_single = hit.min_peak_single
            # Sicherheits-Timeout: Streak entscheiden, ohne auf sein Ende zu
            # warten. Peak-Bedingung wie im Leerlauf — echte Rufe peaken frueh.
            if self._hits >= self.MAX_STREAK and self._peak >= self._min_peak:
                return self._entscheiden()
            return None

        if self._hits >= self._min_hits and not self._gap_used:
            # Erste Luecke im Streak tolerieren (wie im Leerlauf, und erst ab
            # min_hits — sonst naeht die Toleranz zwei Rausch-Bursts zusammen).
            self._gap_used = True
            return None

        if self._hits == 0:
            return None
        return self._entscheiden()

    def _entscheiden(self):
        hits, peak = self._hits, self._peak
        bundle, threshold = self._bundle, self._threshold
        min_hits = self._min_hits
        scores = [round(s, 3) for s in self._scores]

        score_ok = gate_passed(
            hits, peak, min_hits, self._min_peak,
            self._min_peak_short, self._min_peak_single,
        )
        rms = self.level_rms() if self._rms_min > 0.0 else 0.0

        # Zaehler zuruecksetzen, Ring NICHT: folgt gleich der richtige Ruf,
        # behaelt er seinen vollen Pre-Roll (gleiche Ueberlegung wie beim
        # Near-Miss im Leerlauf).
        self._hits = 0
        self._peak = 0.0
        self._gap_used = False

        if not score_ok:
            return BargeInMiss(
                bundle=bundle, hits=hits, peak=peak, threshold=threshold,
                min_hits=min_hits, rms=rms, scores=scores,
                failed_on="min_hits" if hits < min_hits else "min_peak",
            )
        if self._rms_min > 0.0 and rms < self._rms_min:
            return BargeInMiss(
                bundle=bundle, hits=hits, peak=peak, threshold=threshold,
                min_hits=min_hits, rms=rms, scores=scores, failed_on="min_rms",
            )
        return BargeInHit(
            bundle=bundle, hits=hits, peak=peak, threshold=threshold,
            min_hits=min_hits, rms=rms, scores=scores,
        )


def format_scores(hit) -> str:
    """Kurze Score-Zeile fuers Log (letzte fuenf Frames)."""
    return " ".join(f"{s:.2f}" for s in hit.scores[-5:])


__all__ = [
    "BargeInDetector",
    "BargeInHit",
    "BargeInMiss",
    "format_scores",
    "required_peak",
]
