"""Wakewort-Erkennung im STT-Text aus dem Bundle-Manifest (tools/wake_wort.py)."""

import os
import tempfile
import unittest
from unittest import mock

from tools import wake_wort


class WakeWortTest(unittest.TestCase):
    def test_gaston_wie_vorher(self):
        # Die Werte standen bis 2026-10-31 fest im Code; das Manifest muss
        # dasselbe liefern, sonst ändern sich alte Labels still.
        w = wake_wort.laden("gaston")
        self.assertEqual(w.muster.pattern, "gast|kast|gasch|aston|caston")
        self.assertEqual(w.wortanfaenge, ("gast", "gas", "gest", "gust", "kast",
                                          "gerst", "erstaun", "herztau"))
        self.assertEqual(w.sprache, "de")
        self.assertTrue(w.im_text("Gasthof, mach das Licht an"))
        self.assertTrue(w.im_text("Ich bin Kastor."))
        self.assertFalse(w.im_text("Mach das Licht an."))
        self.assertTrue(w.gehoert("„Gaston“, Licht an"))
        self.assertTrue(w.gehoert("Erstaunlich."))
        self.assertFalse(w.gehoert("Pasta"))
        self.assertTrue(w.wort_beginnt(" Gastó,"))

    def test_ohne_block_bundle_name(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "jarvis"))
        with open(os.path.join(d, "jarvis", "manifest.yaml"), "w") as f:
            f.write("name: jarvis\nmodel: jarvis.tflite\n")
        with mock.patch.object(wake_wort, "BUNDLE_DIR", d):
            w = wake_wort.laden("jarvis")
        self.assertEqual(w.wortanfaenge, ("jarv",))
        self.assertTrue(w.im_text("Hey Jarvis!"))
        self.assertFalse(w.gehoert("Gaston"))

    def test_ohne_bundle_verzeichnis(self):
        w = wake_wort.laden("gibtsnicht")
        self.assertEqual(w.bundle, "gibtsnicht")
        self.assertEqual(w.wortanfaenge, ("gibt",))

    def test_setzen_und_aktiv(self):
        wake_wort.setzen("gaston")
        self.assertEqual(wake_wort.aktiv().bundle, "gaston")


if __name__ == "__main__":
    unittest.main()
