"""Abstrakte Interfaces für Audio-Quelle und -Senke.

AudioSource liefert 16-kHz-mono-int16-Chunks (shape: (CHUNK_SIZE,)).
AudioSink spielt WAV-Dateien ab (Pfad).
"""

from __future__ import annotations

from typing import Iterator, Protocol

import numpy as np


class AudioSource(Protocol):
    def start(self) -> None: ...

    def read_chunk(self) -> np.ndarray:
        """Liefert einen Audio-Chunk (16 kHz mono int16)."""
        ...

    def flush(self) -> None:
        """Puffer leeren (nach langer Unterbrechung, z.B. während TTS)."""
        ...

    def close(self) -> None: ...


class AudioSink(Protocol):
    def play_wav(self, path: str) -> None: ...
