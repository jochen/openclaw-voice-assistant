"""Wakeword-Engine für ReSpeaker-Modus: openwakeword läuft auf dem Pi.

Der ESP streamt Audio kontinuierlich; dieser Wrapper leitet jeden Chunk an
OpenWakewordEngine weiter — identisches Interface wie der lokale Modus, inkl.
derselben Multi-Wakeword-Config (siehe openwakeword_engine.py).
"""

from __future__ import annotations

import numpy as np

from voice_assistant.config import RespeakerAudio, WakewordConfig
from voice_assistant.wakeword.base import WakewordHit
from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine


class RespeakerWakeword:
    def __init__(self, cfg: RespeakerAudio, wakewords: list[WakewordConfig]) -> None:
        self._engine = OpenWakewordEngine(wakewords)

    def feed(self, audio_16k: np.ndarray) -> WakewordHit | None:
        return self._engine.feed(audio_16k)

    def reset(self) -> None:
        self._engine.reset()
