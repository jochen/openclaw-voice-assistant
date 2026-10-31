"""Score-Mittel mehrerer Modelle in BundleScorer (wakeword_studio/scoring.py)."""

import os
import shutil
import tempfile
import unittest

import numpy as np

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
            self.assertAlmostEqual(ens.score_pcm(x)["max_score"], einzeln.score_pcm(x)["max_score"], places=6)
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


if __name__ == "__main__":
    unittest.main()
