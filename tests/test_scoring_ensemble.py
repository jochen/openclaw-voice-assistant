"""Score-Mittel mehrerer Modelle: BundleScorer (wakeword_studio/scoring.py)
und die Live-Engine (voice_assistant/wakeword/openwakeword_engine.py)."""

import os
import shutil
import tempfile
import unittest

import numpy as np

from voice_assistant.config import WakewordConfig
from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine
from wakeword_studio.scoring import BundleScorer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GASTON = os.path.join(REPO, "models", "wakewords", "gaston")


class EnsembleTest(unittest.TestCase):
    def test_modell_mit_sich_selbst_wie_einzeln(self):
        d = tempfile.mkdtemp()
        try:
            for n in ("a", "b"):
                os.symlink(os.path.join(GASTON, "gaston.tflite"), os.path.join(d, f"{n}.tflite"))
            with open(os.path.join(d, "manifest.yaml"), "w") as f:
                f.write("name: gaston\nmodel: [a.tflite, b.tflite]\nthreshold: 0.35\n"
                        "min_hits: 2\nmin_peak: 0.7\n")
            ens = BundleScorer(d)
            einzeln = BundleScorer("gaston")
            self.assertEqual(ens._keys, ["a", "b"])
            x = (np.random.default_rng(1).standard_normal(16000 * 2) * 3000).astype(np.int16)
            # Toleranz: zwei Instanzen weichen um bis ~1,5e-4 ab (siehe unten).
            self.assertAlmostEqual(ens.score_pcm(x)["max_score"], einzeln.score_pcm(x)["max_score"], delta=5e-4)
        finally:
            shutil.rmtree(d)

    def test_gleiche_dateinamen_abgelehnt(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "x"))
            for p in ("gaston.tflite", os.path.join("x", "gaston.tflite")):
                os.symlink(os.path.join(GASTON, "gaston.tflite"), os.path.join(d, p))
            with open(os.path.join(d, "manifest.yaml"), "w") as f:
                f.write("name: gaston\nmodel: [gaston.tflite, x/gaston.tflite]\n")
            with self.assertRaises(ValueError):
                BundleScorer(d)
        finally:
            shutil.rmtree(d)


def _ensemble_bundle(d: str, namen=("a", "b")) -> None:
    for n in namen:
        os.symlink(os.path.join(GASTON, "gaston.tflite"), os.path.join(d, f"{n}.tflite"))
    liste = ", ".join(f"{n}.tflite" for n in namen)
    with open(os.path.join(d, "manifest.yaml"), "w") as f:
        f.write(f"name: gaston\nmodel: [{liste}]\nthreshold: 0.35\nmin_hits: 2\nmin_peak: 0.7\n")


def _scores(engine, x):
    out = []
    for i in range(0, len(x), 320):
        hit = engine.feed(x[i:i + 320])
        if hit is not None:
            out.append(hit.score)
    return out


class EngineEnsembleTest(unittest.TestCase):
    def test_engine_modell_mit_sich_selbst_wie_einzeln(self):
        d = tempfile.mkdtemp()
        try:
            _ensemble_bundle(d)
            ens = OpenWakewordEngine([WakewordConfig(bundle=d)])
            einzeln = OpenWakewordEngine([WakewordConfig(bundle="gaston")])
            self.assertEqual(ens._entries[0]["keys"], ["a", "b"])
            self.assertEqual(ens._entries[0]["threshold"], 0.35)
            x = (np.random.default_rng(2).standard_normal(16000 * 3) * 3000).astype(np.int16)
            a, b = _scores(ens, x), _scores(einzeln, x)
            self.assertEqual(len(a), len(b))
            # Zwei frisch geladene Instanzen weichen in den Aufwärm-Frames um
            # bis ~1,5e-4 voneinander ab (gemessen 2026-10-31, auch ohne
            # Ensemble); die Köpfe a und b sind untereinander bitgleich.
            np.testing.assert_allclose(a, b, atol=5e-4)
            r = ens._model.predict(x[:1280])
            self.assertEqual(r["a"], r["b"])
        finally:
            shutil.rmtree(d)

    def test_engine_mittelt(self):
        """feed() liefert den Mittelwert der Köpfe, nicht das Maximum."""
        d = tempfile.mkdtemp()
        try:
            _ensemble_bundle(d, ("a", "b", "c"))
            eng = OpenWakewordEngine([WakewordConfig(bundle=d)])
            eng._model.predict = lambda chunk: {"a": 0.9, "b": 0.3, "c": 0.0}
            hit = eng.feed(np.zeros(1280, dtype=np.int16))
            self.assertAlmostEqual(hit.score, 0.4)
            self.assertEqual(hit.name, d)
        finally:
            shutil.rmtree(d)

    def test_engine_doppelte_keys_abgelehnt(self):
        d = tempfile.mkdtemp()
        try:
            _ensemble_bundle(d, ("gaston", "b"))
            with self.assertRaises(ValueError):
                OpenWakewordEngine([WakewordConfig(bundle=d), WakewordConfig(bundle="gaston")])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
