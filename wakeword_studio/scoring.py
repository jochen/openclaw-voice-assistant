"""Scoring von Aufnahmen gegen ein Wakeword-Bundle (openwakeword).

Bildet die Live-Trigger-Semantik aus voice_assistant/assistant.py nach:
ein Trigger ist ein Streak von >= 3 Frames über dem Threshold, wobei die
erste 1-Frame-Lücke im Streak toleriert wird (Commit 384e76d). So sagt der
Score einer Datei direkt voraus, ob der Assistant live auslösen würde.
"""

from __future__ import annotations

import os
import wave

import numpy as np

from voice_assistant.config import RATE_OW
from voice_assistant.wakeword.openwakeword_engine import (
    _DEFAULT_THRESHOLD,
    _OW_FRAME,
    _resolve_bundle,
)

# Wie assistant.py: wake_hits >= 3 löst aus (mit 1-Frame-Gap-Toleranz)
STREAK_TRIGGER = 3
# Stille vor/nach dem Clip: openwakeword-Feature-Puffer aufwärmen bzw. den
# letzten Frame noch durchs Modell schieben
_PAD_SAMPLES = RATE_OW // 2


def load_wav_16k(path: str) -> np.ndarray:
    """Liest ein WAV als 16-kHz-mono-int16-Array (resampelt/mono-mixt bei Bedarf)."""
    with wave.open(path, "rb") as wf:
        n_ch = wf.getnchannels()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"{path}: nur 16-bit PCM unterstützt (hat {width * 8} bit)")
    samples = np.frombuffer(raw, dtype=np.int16)
    if n_ch > 1:
        samples = samples.reshape(-1, n_ch)[:, 0]
    if rate != RATE_OW:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(rate, RATE_OW)
        samples = np.clip(
            resample_poly(samples, RATE_OW // g, rate // g), -32768, 32767
        ).astype(np.int16)
    return samples


class BundleScorer:
    """Lädt das Modell eines Bundles einmal und scored beliebig viele Clips."""

    def __init__(self, bundle: str, threshold: float | None = None) -> None:
        model_arg, manifest = _resolve_bundle(bundle)
        self.bundle = bundle
        self.display = str(manifest.get("display", bundle))
        self.threshold = (
            threshold
            if threshold is not None
            else float(manifest.get("threshold", _DEFAULT_THRESHOLD))
        )
        is_path = os.path.exists(model_arg)
        self._key = (
            os.path.splitext(os.path.basename(model_arg))[0] if is_path else model_arg
        )
        from openwakeword import Model  # type: ignore[import-not-found]

        self._model = Model(wakeword_models=[model_arg])

    def score_pcm(self, samples: np.ndarray) -> dict:
        """16-kHz-mono-int16 → max_score, Frame-Hits, Streak, Live-Trigger-Urteil."""
        self._model.reset()
        pad = np.zeros(_PAD_SAMPLES, dtype=np.int16)
        padded = np.concatenate([pad, samples.astype(np.int16), pad])

        scores: list[float] = []
        for i in range(0, len(padded) - _OW_FRAME + 1, _OW_FRAME):
            result = self._model.predict(padded[i : i + _OW_FRAME])
            scores.append(float(result.get(self._key, 0.0)))

        best_streak = 0
        streak = 0
        gap_used = False
        frames_over = 0
        for s in scores:
            if s > self.threshold:
                streak += 1
                frames_over += 1
            elif streak > 0 and not gap_used:
                gap_used = True
            else:
                best_streak = max(best_streak, streak)
                streak = 0
                gap_used = False
        best_streak = max(best_streak, streak)

        return {
            "max_score": max(scores, default=0.0),
            "frames_over": frames_over,
            "best_streak": best_streak,
            "triggered": best_streak >= STREAK_TRIGGER,
            "threshold": self.threshold,
        }

    def score_wav(self, path: str) -> dict:
        return self.score_pcm(load_wav_16k(path))
