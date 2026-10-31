"""Lokale Wakeword-Engine basierend auf openwakeword (TFLite/ONNX).

Lädt N Wakewords gleichzeitig (`Model(wakeword_models=[...])`). Jeder Eintrag
ist entweder ein Bundle-Verzeichnis `models/wakewords/<name>/` (manifest.yaml
+ .tflite, Wakeword-Studio Teil 1) oder ein eingebauter openwakeword-
Modellname ('hey_jarvis', 'alexa', …). feed() liefert je Aufruf den best-
scorenden Kandidaten samt seinem konfigurierten Schwellwert zurück — die
Hauptschleife (assistant.py) entscheidet anhand mehrerer Frames (Debounce),
ob/wann tatsächlich getriggert wird.

Ensemble (seit 2026-10-31): trägt das Manifest unter ``model`` eine LISTE von
Dateien, laufen alle im selben ``Model`` (Mel-Spektrum und Embedding nur einmal
gerechnet, je Datei nur der kleine Kopf) und der Score des Wakeworts ist der
Mittelwert über die Köpfe. Gewichte lassen sich nicht mitteln: Läufe ohne
gemeinsamen Start haben keine gemeinsame Parametrisierung. Gemessen in
WAKEWORD_PROCESS.md, "Ensemble: Score-Mittel statt Glückslauf".
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
# Peak-Bedingung (siehe WakewordHit.min_peak) — 0.0 = aus. Eigene Bundles
# setzen ihren Wert im manifest.yaml.
_DEFAULT_MIN_PEAK = 0.0


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


def _resolve_modelle(bundle: str) -> tuple[list[str], dict]:
    """Wie ``_resolve_bundle``, aber immer eine Liste von Modellen: bei einem
    Ensemble-Manifest (``model: [a.tflite, b.tflite]``) alle, sonst eines."""
    bundle_dir = os.path.join(WAKEWORDS_DIR, bundle)
    manifest_path = os.path.join(bundle_dir, "manifest.yaml")
    if os.path.isfile(manifest_path):
        import yaml  # type: ignore[import-not-found]

        with open(manifest_path, "r") as f:
            manifest = yaml.safe_load(f) or {}
        modell = manifest.get("model")
        if isinstance(modell, list):
            if not modell:
                raise ValueError(f"{bundle}: model ist eine leere Liste")
            return [os.path.join(bundle_dir, m) for m in modell], manifest
    model_arg, manifest = _resolve_bundle(bundle)
    return [model_arg], manifest


def _modell_key(model_arg: str) -> str:
    """Bundle-Pfad → Dateiname ohne Endung (so vergibt openwakeword.Model die
    Keys für predict()); eingebauter Name → der Name selbst."""
    if os.path.exists(model_arg):
        return os.path.splitext(os.path.basename(model_arg))[0]
    return model_arg


class OpenWakewordEngine:
    def __init__(self, wakewords: list[WakewordConfig]) -> None:
        if not wakewords:
            raise ValueError("OpenWakewordEngine braucht mindestens ein Wakeword")

        model_args: list[str] = []
        model_keys: list[str] = []
        self._entries: list[dict] = []  # [{key, name, threshold}, ...]
        print(f"🔧 Lade openwakeword-Modelle: {[w.bundle for w in wakewords]}...")
        for w in wakewords:
            modelle, manifest = _resolve_modelle(w.bundle)
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
            min_peak = (
                w.min_peak
                if w.min_peak is not None
                else float(manifest.get("min_peak", _DEFAULT_MIN_PEAK))
            )
            min_peak_short = (
                w.min_peak_short
                if w.min_peak_short is not None
                else float(manifest.get("min_peak_short", min_peak))
            )
            # 0.0 = aus. Kein Fallback auf min_peak — ein 1-Frame-Trigger ist
            # eine bewusste Entscheidung und darf nicht aus einem anderen Wert
            # herausfallen.
            min_peak_single = (
                w.min_peak_single
                if w.min_peak_single is not None
                else float(manifest.get("min_peak_single", 0.0))
            )
            keys = [_modell_key(m) for m in modelle]
            doppelt = set(keys) & set(model_keys) or len(set(keys)) != len(keys)
            if doppelt:
                # predict() liefert je Key EINEN Score; zwei gleiche Dateinamen
                # würden still zu einem Modell zusammenfallen.
                raise ValueError(f"{w.bundle}: Modelle brauchen verschiedene Dateinamen ({keys})")
            model_keys.extend(keys)
            model_args.extend(modelle)
            if len(modelle) > 1:
                print(f"   {w.bundle}: Ensemble aus {len(modelle)} Modellen (Score-Mittel)")
            self._entries.append(
                {
                    "keys": keys,
                    "name": w.bundle,
                    "threshold": threshold,
                    "min_hits": min_hits,
                    "min_peak": min_peak,
                    "min_peak_short": min_peak_short,
                    "min_peak_single": min_peak_single,
                }
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
            keys = entry["keys"]
            score = float(sum(result.get(k, 0.0) for k in keys) / len(keys))
            if score > best_score:
                best, best_score = entry, score
        return WakewordHit(
            name=best["name"] if best else "",
            score=best_score,
            threshold=best["threshold"] if best else _DEFAULT_THRESHOLD,
            min_hits=best["min_hits"] if best else _DEFAULT_MIN_HITS,
            min_peak=best["min_peak"] if best else _DEFAULT_MIN_PEAK,
            min_peak_short=best["min_peak_short"] if best else _DEFAULT_MIN_PEAK,
            min_peak_single=best["min_peak_single"] if best else 0.0,
        )

    def reset(self) -> None:
        self._model.reset()
        self._buf = np.empty(0, dtype=np.int16)
