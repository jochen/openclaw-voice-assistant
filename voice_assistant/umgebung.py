"""Umgebungspegel: im Lärm triggert das Wakewort strenger (Experiment).

Jochens Idee (2026-10-17): Fernseher und volles Fablab lösen Fehltrigger aus.
Ist es laut, soll das Wakewort strenger sein. Wer Gaston dann rufen will,
schaltet den Fernseher stumm oder bittet um Ruhe. Wer direkt am Mikrofon
steht, soll trotzdem durchkommen.

Zwei Pegel, beide aus dem RMS von 300-ms-Fenstern im Leerlauf:

- gleitend: Median der letzten ``gleit_sec`` (die letzten 3 s vor dem
  Trigger ausgenommen, dort steckt das Wakewort selbst).
- ruhe: unteres Zehntel der Minuten-Mediane der letzten ``ruhe_stunden``.
  Ein Wert je Raum und Mikrofon, gelernt statt eingetragen, deshalb passt er
  auch zur Verstärkung im Fablab. Überlebt Neustarts (``zustand_datei``).

Laut ist es, wenn gleitend >= ``laut_faktor`` x ruhe. Dann braucht ein
Trigger mindestens ``streng_min_hits`` Frames, AUSSER das Wakewort ist
mindestens ``nah_faktor`` x gleitend laut (der Rufer am Mikrofon).

Warum Frames und nicht der Score: grob gemessen am 2026-10-17 über 345
Trigger mit 30 s Vorlauf (Messung im Chat, kein Werkzeug): in lauter
Umgebung (Grundpegel >= 120) peakten Fernseh-Fehltrigger genauso hoch wie
echte Rufe (0,94-0,98), hatten aber meist nur 2-3 Frames, echte Rufe 3-12.
Mit dieser Regel wären 6 von 9 lauten Fernseh-Fehltriggern weggefallen und
5 von 62 echten Rufen in lauter Umgebung verloren gegangen. Die Labels waren
behelfsmäßig (Fernseher = Anrede-Hinweis, echt = Aktuator hat geschaltet),
und die Regel ist an denselben Daten gefunden. Deshalb ein Experiment, das
jede Entscheidung mitloggt (``wake_events.log``, Feld ``umgebung``).

``scharf: false`` misst und loggt nur, ohne zu blocken.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

_FENSTER_SEC = 0.3
# Die letzten Sekunden vor dem Trigger enthalten das Wakewort selbst.
_OHNE_LETZTE_SEC = 3.0
_MINUTE = 60.0
# Erst ab so vielen Minuten-Werten gilt der Ruhepegel als gelernt.
_RUHE_MIN_WERTE = 30


@dataclass(frozen=True)
class Urteil:
    """Was die Regel zu einem Streak sagt, der das Score-Gate bestanden hat."""
    ok: bool
    laut: bool
    nah: bool
    gleitend: float | None
    ruhe: float | None
    wake_rms: float
    hits: int

    def als_log(self) -> dict:
        return {
            "ok": self.ok,
            "laut": self.laut,
            "nah": self.nah,
            "gleitend": None if self.gleitend is None else round(self.gleitend, 1),
            "ruhe": None if self.ruhe is None else round(self.ruhe, 1),
            "wake_rms": round(self.wake_rms, 1),
        }


def entscheide(
    hits: int,
    wake_rms: float,
    gleitend: float | None,
    ruhe: float | None,
    laut_faktor: float,
    streng_min_hits: int,
    nah_faktor: float,
    scharf: bool = True,
) -> Urteil:
    """Die Regel ohne Zustand, damit sie sich testen und nachspielen lässt."""
    laut = bool(gleitend and ruhe and gleitend >= laut_faktor * ruhe)
    nah = bool(gleitend and wake_rms >= nah_faktor * gleitend)
    ok = (not scharf) or (not laut) or nah or hits >= streng_min_hits
    return Urteil(ok, laut, nah, gleitend, ruhe, wake_rms, hits)


class Umgebungspegel:
    def __init__(
        self,
        gleit_sec: float = 30.0,
        ruhe_stunden: float = 24.0,
        zustand_datei: str | None = None,
        rate: int = 16000,
    ) -> None:
        self._rate = rate
        self._fenster_n = int(rate * _FENSTER_SEC)
        self._rest = np.zeros(0, dtype=np.float64)
        # (Zeit, RMS) je 300-ms-Fenster
        self._fenster: deque[tuple[float, float]] = deque(
            maxlen=int((gleit_sec + _OHNE_LETZTE_SEC) / _FENSTER_SEC) + 1)
        self._gleit_sec = gleit_sec
        self._minute: list[float] = []
        self._minute_start: float | None = None
        self._minuten: deque[tuple[float, float]] = deque(maxlen=int(ruhe_stunden * 60))
        self._datei = zustand_datei
        self._gespeichert = 0.0
        self._laden()

    # --- füttern -------------------------------------------------------
    def feed(self, now: float, chunk: np.ndarray) -> None:
        """Nur im Leerlauf aufrufen: Turns und eigene Ansagen sind kein Raum."""
        a = np.concatenate([self._rest, chunk.astype(np.float64)])
        n = len(a) // self._fenster_n
        for i in range(n):
            f = a[i * self._fenster_n:(i + 1) * self._fenster_n]
            rms = float(np.sqrt(np.mean(f * f)))
            self._fenster.append((now, rms))
            self._minute_dazu(now, rms)
        self._rest = a[n * self._fenster_n:]

    def luecke(self) -> None:
        """Ein Turn lag dazwischen: der Rest des alten Fensters gilt nicht mehr."""
        self._rest = np.zeros(0, dtype=np.float64)

    def _minute_dazu(self, now: float, rms: float) -> None:
        if self._minute_start is None:
            self._minute_start = now
        self._minute.append(rms)
        if now - self._minute_start >= _MINUTE:
            self._minuten.append((now, float(np.median(self._minute))))
            self._minute = []
            self._minute_start = now
            if now - self._gespeichert >= 600:
                self._speichern()
                self._gespeichert = now

    # --- lesen ---------------------------------------------------------
    def gleitend(self, now: float | None = None) -> float | None:
        now = time.time() if now is None else now
        werte = [r for t, r in self._fenster
                 if now - _OHNE_LETZTE_SEC - self._gleit_sec <= t <= now - _OHNE_LETZTE_SEC]
        # Mindestens ein Drittel des Fensters, sonst ist es keine Messung.
        if len(werte) < self._gleit_sec / _FENSTER_SEC / 3:
            return None
        return float(np.median(werte))

    def ruhe(self) -> float | None:
        if len(self._minuten) < _RUHE_MIN_WERTE:
            return None
        return float(np.percentile([r for _, r in self._minuten], 10))

    # --- Zustand -------------------------------------------------------
    def _laden(self) -> None:
        if not self._datei:
            return
        try:
            with open(self._datei) as f:
                for t, r in json.load(f).get("minuten", []):
                    self._minuten.append((float(t), float(r)))
        except (OSError, ValueError, TypeError):
            pass

    def _speichern(self) -> None:
        if not self._datei:
            return
        try:
            d = os.path.dirname(self._datei) or "."
            with tempfile.NamedTemporaryFile("w", dir=d, delete=False, suffix=".tmp") as f:
                json.dump({"minuten": [[round(t), round(r, 1)] for t, r in self._minuten]}, f)
            os.replace(f.name, self._datei)
        except OSError:
            pass
