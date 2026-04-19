"""Wakeword-Engine für ReSpeaker-Modus: openwakeword läuft auf dem Pi.

Der ESP streamt Audio kontinuierlich; dieser Wrapper leitet jeden Chunk an
OpenWakewordEngine weiter — identisches Interface wie der lokale Modus.
"""

from __future__ import annotations

import numpy as np

from voice_assistant.config import RespeakerAudio
from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine


class RespeakerWakeword:
    def __init__(self, cfg: RespeakerAudio) -> None:
        self._engine = OpenWakewordEngine("hey_jarvis")

    def feed(self, audio_16k: np.ndarray) -> float:
        return self._engine.feed(audio_16k)

    def reset(self) -> None:
        self._engine.reset()
