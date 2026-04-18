"""Wakeword-Engine-Interface."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class WakewordEngine(Protocol):
    def feed(self, audio_16k: np.ndarray) -> float:
        """Füttert einen 16-kHz-Chunk und liefert den aktuellen Score zurück."""
        ...

    def reset(self) -> None: ...
