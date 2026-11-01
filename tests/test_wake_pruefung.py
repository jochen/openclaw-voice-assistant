"""Prüfung nach dem Trigger (Schatten): Fläche, Text-Regel, Log, nie ein Abbruch."""

import json
import os
import tempfile
import time
import unittest

import numpy as np

from voice_assistant.anrede import anrede_im_text
from voice_assistant.wake_pruefung import ANFANG_MAX_STELLEN, WakePruefung, anfang_mass, flaeche


class _Stt:
    def __init__(self, text=None, fehler=None, sprache=None, anfang=None):
        self.text, self.fehler, self.aufrufe = text, fehler, 0
        self.sprache = sprache
        self.anfang = anfang or {}   # Präfix -> log P
        self.praefixe = []

    def anfang_logprob(self, wav, praefix, anfang):
        self.praefixe.append(praefix)
        if "fehler" in self.anfang:
            raise self.anfang["fehler"]
        return self.anfang.get(praefix, -20.0)

    def _anfrage(self, wav, kontext):
        self.aufrufe += 1
        if self.fehler:
            raise self.fehler
        return self.text


def _lauf(stt, scores, grenze=2.7, anfang=""):
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "p.jsonl")
        p = WakePruefung(stt, log, grenze, anfang=anfang, anfang_grenze=-4.0)
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


class AnfangTest(unittest.TestCase):
    def test_bestes_maximum_ueber_wortgrenzen(self):
        stt = _Stt(anfang={"": -9.0, "spiel ein bisschen": -0.5})
        self.assertEqual(anfang_mass(stt, b"", "spiel ein bisschen Gastor.", "Gast"), (-0.5, 3))
        self.assertEqual(stt.praefixe, ["", "spiel", "spiel ein", "spiel ein bisschen",
                                        "spiel ein bisschen Gastor."])

    def test_leerer_text_eine_stelle(self):
        self.assertEqual(anfang_mass(_Stt(anfang={"": -1.0}), b"", "", "Gast"), (-1.0, 0))

    def test_stellen_begrenzt(self):
        stt = _Stt()
        anfang_mass(stt, b"", " ".join(["wort"] * 40), "Gast")
        self.assertEqual(len(stt.praefixe), ANFANG_MAX_STELLEN)

    def test_ganz_toll_geht_durch_alte_regel_nicht(self):
        z = _lauf(_Stt("Ganz toll. Also.", sprache="German", anfang={"": -2.45}),
                  [0.9, 0.8], anfang="Gast")
        self.assertTrue(z["haette_verworfen"])
        self.assertFalse(z["haette_verworfen_anfang"])
        self.assertEqual((z["anfang"], z["anfang_lp"], z["anfang_nach"]), ("Gast", -2.45, 0))

    def test_unter_grenze_haette_verworfen(self):
        z = _lauf(_Stt("Oh.", sprache="German"), [0.98] * 6, anfang="Gast")
        self.assertFalse(z["haette_verworfen"])
        self.assertTrue(z["haette_verworfen_anfang"])

    def test_aus_ohne_anfang_oder_sprache(self):
        for stt, anfang in ((_Stt("Oh.", sprache="German"), ""), (_Stt("Oh."), "Gast")):
            z = _lauf(stt, [0.9, 0.8], anfang=anfang)
            self.assertNotIn("anfang_lp", z)
            self.assertEqual(stt.praefixe, [])

    def test_fehler_laesst_altes_urteil_stehen(self):
        z = _lauf(_Stt("Oh.", sprache="German", anfang={"fehler": TimeoutError("weg")}),
                  [0.9, 0.8], anfang="Gast")
        self.assertIn("weg", z["anfang_fehler"])
        self.assertTrue(z["haette_verworfen"])
        self.assertNotIn("fehler", z)


if __name__ == "__main__":
    unittest.main()
