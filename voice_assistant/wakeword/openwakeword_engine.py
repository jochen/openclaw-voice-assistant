"""Lokale Wakeword-Engine basierend auf openwakeword (TFLite/ONNX)."""

from __future__ import annotations

import os

import numpy as np

from voice_assistant.config import OW_MODEL_PATH


class OpenWakewordEngine:
    def __init__(self, phrase: str = "hey_jarvis") -> None:
        self.phrase = phrase
        self._key = phrase.replace(" ", "_")
        os.environ["OPENWAKEWORD_MODEL_PATH"] = OW_MODEL_PATH
        from openwakeword import Model  # type: ignore[import-not-found]

        print(f"🔧 Lade openwakeword '{phrase}'...")
        self._model = Model(wakeword_models=[phrase])
        print("✅ Modelle:", list(self._model.models.keys()))

    def feed(self, audio_16k: np.ndarray) -> float:
        result = self._model.predict(audio_16k)
        return float(result.get(self._key, result.get(self.phrase, 0.0)))

    def reset(self) -> None:
        self._model.reset()
