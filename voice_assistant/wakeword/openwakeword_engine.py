"""Lokale Wakeword-Engine basierend auf openwakeword (TFLite/ONNX).

Lädt N Wakewords gleichzeitig (`Model(wakeword_models=[...])`). Jeder Eintrag
ist entweder ein Bundle-Verzeichnis `models/wakewords/<name>/` (manifest.yaml
+ .tflite, Wakeword-Studio Teil 1) oder ein eingebauter openwakeword-
Modellname ('hey_jarvis', 'alexa', …). feed() liefert je Aufruf den best-
scorenden Kandidaten samt seinem konfigurierten Schwellwert zurück — die
Hauptschleife (assistant.py) entscheidet anhand mehrerer Frames (Debounce),
ob/wann tatsächlich getriggert wird.
"""

from __future__ import annotations

import os

import numpy as np

from voice_assistant.config import WAKEWORDS_DIR, WakewordConfig
from voice_assistant.wakeword.base import WakewordHit

_OW_FRAME = 1280  # openwakeword verarbeitet intern 1280 Samples (80ms @ 16kHz)
# Entspricht dem früher hart in assistant.py codierten Trigger (score > 0.65) —
# das produktiv eingestellte Verhalten der eingebauten Modelle bleibt erhalten.
# Eigene Bundles setzen ihren Wert im manifest.yaml (aus der Validierung).
_DEFAULT_THRESHOLD = 0.65
# Streak-Länge bis zum Trigger (siehe WakewordHit.min_hits) — kurze Wakewords
# setzen im manifest.yaml einen kleineren Wert.
_DEFAULT_MIN_HITS = 3


def _resolve_bundle(bundle: str) -> tuple[str, dict]:
    """Löst einen Wakeword-Namen auf: Bundle-Verzeichnis oder eingebauter Name.

    models/wakewords/<bundle>/manifest.yaml vorhanden → liefert den Pfad zur
    .tflite (manifest['model'], Default '<bundle>.tflite') + das Manifest-Dict.
    Sonst: 'bundle' unverändert als eingebauter openwakeword-Modellname
    zurückgeben (z.B. 'hey_jarvis', 'alexa'), leeres Manifest.
    """
    bundle_dir = os.path.join(WAKEWORDS_DIR, bundle)
    manifest_path = os.path.join(bundle_dir, "manifest.yaml")
    if os.path.isdir(bundle_dir) and os.path.exists(manifest_path):
        import yaml  # type: ignore[import-not-found]

        with open(manifest_path, "r") as f:
            manifest = yaml.safe_load(f) or {}
        model_file = manifest.get("model", f"{bundle}.tflite")
        model_path = os.path.join(bundle_dir, model_file)
        return model_path, manifest
    return bundle, {}


class OpenWakewordEngine:
    def __init__(self, wakewords: list[WakewordConfig]) -> None:
        if not wakewords:
            raise ValueError("OpenWakewordEngine braucht mindestens ein Wakeword")

        model_args: list[str] = []
        self._entries: list[dict] = []  # [{key, name, threshold}, ...]
        print(f"🔧 Lade openwakeword-Modelle: {[w.bundle for w in wakewords]}...")
        for w in wakewords:
            model_arg, manifest = _resolve_bundle(w.bundle)
            threshold = (
                w.threshold
                if w.threshold is not None
                else float(manifest.get("threshold", _DEFAULT_THRESHOLD))
            )
            min_hits = (
                w.min_hits
                if w.min_hits is not None
                else int(manifest.get("min_hits", _DEFAULT_MIN_HITS))
            )
            # Bundle-Pfad → Key ist der Dateiname ohne Endung (so vergibt
            # openwakeword.Model die Keys für predict()); eingebauter Name →
            # Key ist der Name selbst (unverändert durchgereicht).
            is_path = os.path.exists(model_arg)
            key = os.path.splitext(os.path.basename(model_arg))[0] if is_path else model_arg
            model_args.append(model_arg)
            self._entries.append(
                {"key": key, "name": w.bundle, "threshold": threshold, "min_hits": min_hits}
            )

        from openwakeword import Model  # type: ignore[import-not-found]

        self._model = Model(wakeword_models=model_args)
        print("✅ Modelle:", list(self._model.models.keys()))
        self._buf = np.empty(0, dtype=np.int16)

    def feed(self, audio_16k: np.ndarray) -> WakewordHit | None:
        """Gibt den best-scorenden Kandidaten zurück sobald 1280 Samples
        akkumuliert sind, sonst None."""
        self._buf = np.concatenate([self._buf, audio_16k])
        if len(self._buf) < _OW_FRAME:
            return None
        chunk, self._buf = self._buf[:_OW_FRAME], self._buf[_OW_FRAME:]
        result = self._model.predict(chunk)

        best: dict | None = None
        best_score = -1.0
        for entry in self._entries:
            score = float(result.get(entry["key"], 0.0))
            if score > best_score:
                best, best_score = entry, score
        return WakewordHit(
            name=best["name"] if best else "",
            score=best_score,
            threshold=best["threshold"] if best else _DEFAULT_THRESHOLD,
            min_hits=best["min_hits"] if best else _DEFAULT_MIN_HITS,
        )

    def reset(self) -> None:
        self._model.reset()
        self._buf = np.empty(0, dtype=np.int16)
