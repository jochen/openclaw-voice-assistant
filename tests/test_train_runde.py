"""Vorbereitung einer Trainingsrunde (wakeword_studio/train_runde.py), ohne GPU."""

import os
import tempfile
import unittest
from pathlib import Path

import yaml

from wakeword_studio.train_runde import (KLASSEN, lauf_vorbereiten, rezept_lesen,
                                         rezept_vorbereiten)


def _wav(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF")


class RundeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paket = self.tmp / "paket"
        for n in ("a.wav", "b.wav"):
            _wav(self.paket / "train" / "positive" / n)
        _wav(self.paket / "train" / "negative" / "x.wav")
        self.synth = self.tmp / "synth"
        for k in KLASSEN:
            _wav(self.synth / k / "s1.wav")
        # Rest einer früheren Runde im synthetischen Ordner: darf nicht mit.
        _wav(self.synth / "positive_train" / "real_alt_d0.wav")

    def test_voll_nimmt_alles(self):
        name, listen = rezept_lesen("voll", self.paket)
        self.assertEqual(name, "voll")
        self.assertEqual(listen, {"positive": ["a.wav", "b.wav"], "negative": ["x.wav"]})

    def test_listen_rezept_und_fehlende_datei(self):
        pos, neg = self.tmp / "p.txt", self.tmp / "n.txt"
        pos.write_text("a.wav\n\n")
        neg.write_text("x.wav\n")
        self.assertEqual(rezept_lesen(f"C={pos},{neg}", self.paket),
                         ("C", {"positive": ["a.wav"], "negative": ["x.wav"]}))
        pos.write_text("a.wav\nfehlt.wav\n")
        with self.assertRaises(ValueError):
            rezept_lesen(f"C={pos},{neg}", self.paket)

    def test_unbekanntes_rezept(self):
        with self.assertRaises(ValueError):
            rezept_lesen("halb", self.paket)

    def test_synth_ohne_paket(self):
        self.assertEqual(rezept_lesen("synth", None),
                         ("synth", {"positive": [], "negative": []}))
        with self.assertRaises(ValueError):
            rezept_lesen("voll", None)
        wurzel = self.tmp / "runden" / "r1"
        z = rezept_vorbereiten(wurzel, "synth", "gaston", self.synth, None,
                               {"positive": [], "negative": []}, dupl=10, praefix="real_",
                               basis={"output_dir": "x"})
        self.assertEqual(z["positive_train"], 1)
        self.assertEqual(z["positive_train_echt"], 0)

    def test_vorbereiten_und_lauf(self):
        wurzel = self.tmp / "runden" / "r9"
        _, listen = rezept_lesen("voll", self.paket)
        basis = {"model_name": "gaston", "output_dir": "./train_out", "steps": 10}
        z = rezept_vorbereiten(wurzel, "voll", "gaston", self.synth, self.paket, listen,
                               dupl=3, praefix="real_", basis=basis)
        g = wurzel / "voll" / "gaston"
        self.assertEqual(z["positive_train"], 1)          # real_alt übersprungen
        self.assertEqual(z["positive_train_echt"], 6)      # 2 Clips × 3
        self.assertEqual(z["negative_train_echt"], 3)
        self.assertEqual(len(os.listdir(g / "positive_train")), 7)
        self.assertTrue(os.path.islink(g / "positive_train" / "real_a_d2.wav"))
        cfg = yaml.safe_load((wurzel / "voll" / "config.yaml").read_text())
        self.assertEqual(cfg["output_dir"], str(wurzel / "voll"))
        self.assertEqual(cfg["steps"], 10)

        (g / "positive_features_train.npy").write_bytes(b"x")
        l1 = lauf_vorbereiten(wurzel / "voll", 1, "gaston", basis)
        self.assertTrue((l1 / "gaston" / "positive_features_train.npy").is_symlink())
        self.assertTrue((l1 / "gaston" / "positive_test").exists())
        cfg = yaml.safe_load((l1 / "config.yaml").read_text())
        self.assertEqual(cfg["output_dir"], str(l1))
        self.assertEqual(basis["output_dir"], "./train_out")  # Basis unverändert


if __name__ == "__main__":
    unittest.main()
