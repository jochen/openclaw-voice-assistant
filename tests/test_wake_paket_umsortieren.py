"""Vorrunden-Val: Clips mit geändertem hartem Label wandern in eigene Gruppen."""

import os
import tempfile
import unittest

from tools.wake_paket import _val_umsortieren


class UmsortierenTest(unittest.TestCase):
    def test_nur_harte_abweichende_labels(self):
        with tempfile.TemporaryDirectory() as d:
            dateien = {
                "negative/20261005_165752_gaston_bargein.wav": None,   # selbst: echt
                "negative/20261005_200000_gaston_wake.wav": None,      # stt: echt -> bleibt
                "negative_auto/auto_20261005_210000_gaston_nearmiss.wav": None,  # ohr: unklar
                "positive/20261006_100000_gaston_wake_b.wav": None,    # ohr: rauschen
                "positive/20261006_110000_gaston_wake.wav": None,      # ohr: echt -> bleibt
            }
            for rel in dateien:
                os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
                open(os.path.join(d, rel), "wb").close()
            labels = {
                "20261005_165752_gaston_bargein.wav": {"klasse": "echter_ruf", "quelle": "selbst"},
                "20261005_200000_gaston_wake.wav": {"klasse": "echter_ruf", "quelle": "stt"},
                "20261005_210000_gaston_nearmiss.wav": {"klasse": "unklar", "quelle": "ohr"},
                "20261006_100000_gaston_wake.wav": {"klasse": "rauschen", "quelle": "ohr"},
                "20261006_110000_gaston_wake.wav": {"klasse": "echter_ruf", "quelle": "ohr"},
            }
            nach = {z["nach"] for z in _val_umsortieren(d, labels)}
            self.assertEqual(nach, {
                "umgelabelt_positiv/20261005_165752_gaston_bargein.wav",
                "umgelabelt_unklar/auto_20261005_210000_gaston_nearmiss.wav",
                "umgelabelt_negativ/20261006_100000_gaston_wake_b.wav",
            })
            self.assertTrue(os.path.exists(os.path.join(d, "negative/20261005_200000_gaston_wake.wav")))


if __name__ == "__main__":
    unittest.main()
