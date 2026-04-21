"""Lokale Wakeword-Engine basierend auf openwakeword (TFLite/ONNX)."""

from __future__ import annotations

import os

import numpy as np

from voice_assistant.config import OW_MODEL_PATH


_OW_FRAME = 1280  # openwakeword verarbeitet intern 1280 Samples (80ms @ 16kHz)


class OpenWakewordEngine:
    def __init__(self, phrase: str = "hey_jarvis") -> None:
        self.phrase = phrase
        self._key = phrase.replace(" ", "_")
        os.environ["OPENWAKEWORD_MODEL_PATH"] = OW_MODEL_PATH
        from openwakeword import Model  # type: ignore[import-not-found]

        print(f"🔧 Lade openwakeword '{phrase}'...")
        self._model = Model(wakeword_models=[phrase])
        print("✅ Modelle:", list(self._model.models.keys()))
        self._buf = np.empty(0, dtype=np.int16)

    def feed(self, audio_16k: np.ndarray) -> float | None:
        """Gibt Score zurück sobald 1280 Samples akkumuliert sind, sonst None."""
        self._buf = np.concatenate([self._buf, audio_16k])
        if len(self._buf) < _OW_FRAME:
            return None
        chunk, self._buf = self._buf[:_OW_FRAME], self._buf[_OW_FRAME:]
        result = self._model.predict(chunk)
        return float(result.get(self._key, result.get(self.phrase, 0.0)))

    def reset(self) -> None:
        self._model.reset()
        self._buf = np.empty(0, dtype=np.int16)
