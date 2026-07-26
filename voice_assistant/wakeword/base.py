"""Wakeword-Engine-Interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass
class WakewordHit:
    """Best-scorender Kandidat eines feed()-Aufrufs, wenn mehrere Wakewords
    gleichzeitig geladen sind.

    name: Bundle-/Modellname (entspricht WakewordConfig.bundle) des
        best-scorenden Kandidaten in diesem Frame.
    score: dessen aktueller Score.
    threshold: dessen konfigurierter Schwellwert (Config > manifest.yaml >
        Default 0.65). score > threshold heißt: dieses Wakeword hat in diesem
        Frame seinen Schwellwert überschritten — der eigentliche Trigger-
        Entscheid (Debounce über mehrere Frames) bleibt beim Aufrufer.
    min_hits: benötigte Streak-Länge (Frames über Threshold, 1-Gap-toleriert)
        bis zum Trigger. Kurze Wakewords ("Gaston" ≈ 0.5 s ≈ 6 Frames)
        erreichen strukturell kürzere Streaks als lange ("hey Jarvis") und
        dürfen deshalb per manifest.yaml/Config auf 2 heruntergehen.
    min_peak: zusätzlich zum Streak muss der beste Score im Streak diesen
        Wert erreichen. Echte Rufe peaken deutlich über dem Threshold
        (gaston: 0.92-0.99), False Positives aus Gesprächsfetzen bleiben
        flach (FP 2026-07-07: Peak 0.68). 0.0 = Bedingung aus.
    min_peak_short: strengere Peak-Anforderung für Kurz-Streaks unter
        3 Frames (greift nur bei min_hits 2). Live-Logs 2026-07-08..13:
        alle vier 2-Frame-Trigger waren False Positives (Peaks 0.70-0.92),
        der einzige echte 2-Frame-Ruf peakte 0.93. Fällt ohne Konfiguration
        auf min_peak zurück (Engine löst das auf).
    min_peak_single: erlaubt einen Trigger aus einem EINZIGEN Frame, wenn
        dessen Peak diesen Wert erreicht — zusätzlich zur min_hits-Bedingung,
        nicht an ihrer Stelle. 0.0 = aus (Default, Verhalten wie vorher).
        Datenbasis (gaston, Triage vom 2026-07-26, tools/wake_triage.py):
        von 16 Near-Misses waren 6 echte Rufe und 9 Rauschen. Die Peaks des
        Rauschens endeten bei 0.58, die wiedergewinnbaren echten Rufe begannen
        bei 0.82 — dazwischen eine leere Lücke. Vier der sechs verlorenen Rufe
        liegen darüber, kein einziges Rauschen. Vier weitere Merkmale
        (zweithöchster Score, Summe Top 3, Frames über 0.1 bzw. 0.2) trennten
        NICHT besser als der Peak. Die zwei Rufe bei 0.37/0.38 sind durch keine
        Schwelle zu retten (Rauschen liegt dort gleichauf) — das ist ein
        Modellproblem, kein Schwellenproblem.
    """

    name: str
    score: float
    threshold: float
    min_hits: int = 3
    min_peak: float = 0.0
    min_peak_short: float = 0.0
    min_peak_single: float = 0.0


class WakewordEngine(Protocol):
    def feed(self, audio_16k: np.ndarray) -> WakewordHit | None:
        """Füttert einen 16-kHz-Chunk und liefert den best-scorenden
        Kandidaten zurück, sobald genug Samples akkumuliert sind — sonst
        None (Frame-Puffer noch nicht voll)."""
        ...

    def reset(self) -> None: ...
