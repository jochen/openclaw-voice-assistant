"""Prüfung nach dem Trigger (Schatten): Fläche, Text-Regel, Log, nie ein Abbruch."""

import json
import os
import tempfile
import time
import unittest

import numpy as np

from voice_assistant.anrede import anrede_im_text
from voice_assistant.wake_pruefung import WakePruefung, flaeche


class _Stt:
    def __init__(self, text=None, fehler=None):
        self.text, self.fehler, self.aufrufe = text, fehler, 0

    def _anfrage(self, wav, kontext):
        self.aufrufe += 1
        if self.fehler:
            raise self.fehler
        return self.text


def _lauf(stt, scores, grenze=2.7):
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "p.jsonl")
        p = WakePruefung(stt, log, grenze)
        p.start({"audio": "x_gaston_wake.wav", "bundle": "gaston", "hits": 3, "peak": 0.9,
                 "threshold": 0.35, "scores": scores}, [np.zeros(1280, np.int16)])
        for _ in range(100):
            if os.path.exists(log):
                break
            time.sleep(0.02)
        return json.loads(open(log).read().splitlines()[0])


class FlaecheTest(unittest.TestCase):
    def test_nur_ueber_schwelle(self):
        self.assertEqual(flaeche([0.1, 0.35, 0.5, 0.9], 0.35), 1.4)


class WortImWakeClipTest(unittest.TestCase):
    def test_wort_am_ende_zaehlt_nur_mit_ganzem_text(self):
        text = "und dann haben wir noch gesagt Gaston"
        self.assertFalse(anrede_im_text(text, "gaston"))
        self.assertTrue(anrede_im_text(text, "gaston", woerter=None))


class SchattenTest(unittest.TestCase):
    def test_hohe_flaeche_geht_durch(self):
        z = _lauf(_Stt("Ich habe keine Ahnung."), [0.98] * 6)
        self.assertTrue(z["flaeche_ok"])
        self.assertFalse(z["haette_verworfen"])

    def test_niedrige_flaeche_ohne_wort_haette_verworfen(self):
        z = _lauf(_Stt("Ich habe keine Ahnung."), [0.9, 0.8])
        self.assertTrue(z["haette_verworfen"])
        self.assertEqual(z["text"], "Ich habe keine Ahnung.")
        self.assertEqual(z["status"], "schatten")

    def test_niedrige_flaeche_mit_wort_geht_durch(self):
        z = _lauf(_Stt("Gaston."), [0.9, 0.8])
        self.assertFalse(z["haette_verworfen"])

    def test_stt_fehler_wird_zeile_ohne_urteil(self):
        z = _lauf(_Stt(fehler=TimeoutError("weg")), [0.9, 0.8])
        self.assertIn("weg", z["fehler"])
        self.assertNotIn("haette_verworfen", z)

    def test_ohne_stt_nur_flaeche(self):
        z = _lauf(None, [0.9, 0.8])
        self.assertEqual(z["fehler"], "keine STT")
        self.assertFalse(z["flaeche_ok"])


if __name__ == "__main__":
    unittest.main()
