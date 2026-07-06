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
    """

    name: str
    score: float
    threshold: float
    min_hits: int = 3


class WakewordEngine(Protocol):
    def feed(self, audio_16k: np.ndarray) -> WakewordHit | None:
        """Füttert einen 16-kHz-Chunk und liefert den best-scorenden
        Kandidaten zurück, sobald genug Samples akkumuliert sind — sonst
        None (Frame-Puffer noch nicht voll)."""
        ...

    def reset(self) -> None: ...
