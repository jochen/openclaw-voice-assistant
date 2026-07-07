"""Scoring von Aufnahmen gegen ein Wakeword-Bundle (openwakeword).

Bildet die Live-Trigger-Semantik aus voice_assistant/assistant.py nach:
ein Trigger ist ein Streak von >= min_hits Frames über dem Threshold, wobei
die erste 1-Frame-Lücke im Streak toleriert wird (Commit 384e76d) UND der
beste Score im Streak >= min_peak liegt (Peak-Bedingung gegen flache FPs,
2026-07-07). So sagt der Score einer Datei direkt voraus, ob der Assistant
live auslösen würde.
"""

from __future__ import annotations

import os
import wave

import numpy as np

from voice_assistant.config import RATE_OW
from voice_assistant.wakeword.openwakeword_engine import (
    _DEFAULT_MIN_PEAK,
    _DEFAULT_THRESHOLD,
    _OW_FRAME,
    _resolve_bundle,
)

# Default-Streak-Länge wie assistant.py; pro Bundle via manifest.yaml
# `min_hits` überschrieben (kurze Wakewords → 2). Immer mit 1-Frame-Gap-Toleranz.
DEFAULT_MIN_HITS = 3
# Stille vor/nach dem Clip: openwakeword-Feature-Puffer aufwärmen bzw. den
# letzten Frame noch durchs Modell schieben
_PAD_SAMPLES = RATE_OW // 2
# Live ist die Frame-Ausrichtung relativ zum Wortanfang zufällig — ein Clip
# wird deshalb an mehreren Offsets ausgewertet. `triggered` = irgendein
# Offset löst aus; `robust` = Anteil der Offsets, die auslösen (Maß dafür,
# wie sicher der Trigger im Alltag kommt).
_OFFSETS = (0, _OW_FRAME // 4, _OW_FRAME // 2, 3 * _OW_FRAME // 4)


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
        self.min_hits = int(manifest.get("min_hits", DEFAULT_MIN_HITS))
        self.min_peak = float(manifest.get("min_peak", _DEFAULT_MIN_PEAK))
        is_path = os.path.exists(model_arg)
        self._key = (
            os.path.splitext(os.path.basename(model_arg))[0] if is_path else model_arg
        )
        from openwakeword import Model  # type: ignore[import-not-found]

        self._model = Model(wakeword_models=[model_arg])

    def _score_offset(self, samples: np.ndarray, offset: int) -> tuple[float, int, bool]:
        """Ein Durchlauf mit fester Frame-Ausrichtung →
        (max_score, best_streak, triggered).

        triggered = irgendein Streak erfüllt BEIDE Live-Bedingungen:
        Länge >= min_hits und Streak-Peak >= min_peak.
        """
        self._model.reset()
        pad_front = np.zeros(_PAD_SAMPLES + offset, dtype=np.int16)
        pad_back = np.zeros(_PAD_SAMPLES, dtype=np.int16)
        padded = np.concatenate([pad_front, samples.astype(np.int16), pad_back])

        max_score = 0.0
        best_streak = 0
        triggered = False
        streak = 0
        streak_peak = 0.0
        gap_used = False
        for i in range(0, len(padded) - _OW_FRAME + 1, _OW_FRAME):
            result = self._model.predict(padded[i : i + _OW_FRAME])
            s = float(result.get(self._key, 0.0))
            max_score = max(max_score, s)
            if s > self.threshold:
                streak += 1
                streak_peak = max(streak_peak, s)
            elif streak > 0 and not gap_used:
                gap_used = True
            else:
                best_streak = max(best_streak, streak)
                triggered = triggered or (streak >= self.min_hits and streak_peak >= self.min_peak)
                streak = 0
                streak_peak = 0.0
                gap_used = False
        triggered = triggered or (streak >= self.min_hits and streak_peak >= self.min_peak)
        return max_score, max(best_streak, streak), triggered

    def score_pcm(self, samples: np.ndarray) -> dict:
        """16-kHz-mono-int16 → max_score, Streak, Live-Trigger-Urteil.

        Wertet den Clip an mehreren Frame-Offsets aus (live ist die
        Ausrichtung zufällig) und aggregiert: triggered = bester Offset,
        robust = wie viele der Offsets auslösen würden.
        """
        max_score = 0.0
        best_streak = 0
        hits = 0
        for offset in _OFFSETS:
            score, streak, triggered = self._score_offset(samples, offset)
            max_score = max(max_score, score)
            best_streak = max(best_streak, streak)
            hits += triggered

        return {
            "max_score": max_score,
            "best_streak": best_streak,
            "triggered": hits > 0,
            "robust": f"{hits}/{len(_OFFSETS)}",
            "threshold": self.threshold,
        }

    def score_wav(self, path: str) -> dict:
        return self.score_pcm(load_wav_16k(path))
