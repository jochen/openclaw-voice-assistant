"""Umgebungspegel (voice_assistant/umgebung.py): im Lärm strenger.

Die Fälle stammen aus der groben Messung vom 2026-10-17 (wake_events.log +
Vorlauf-Clips): Fernseh-Fehltrigger in lauter Umgebung mit 2-3 Frames, echte
Rufe mit mehr Frames oder deutlich lauter als der Raum.
"""

import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.umgebung import Umgebungspegel, entscheide  # noqa: E402

REGEL = dict(laut_faktor=4.0, streng_min_hits=4, nah_faktor=6.0)


class RegelTest(unittest.TestCase):
    def test_ruhig_alles_wie_bisher(self):
        self.assertTrue(entscheide(2, 300, gleitend=40, ruhe=30, **REGEL).ok)

    def test_fernseher_laut_kurzer_streak(self):
        # 2026-10-13 22:35: Raum 468, Wakewort 665, 2 Frames
        u = entscheide(2, 665, gleitend=468, ruhe=27, **REGEL)
        self.assertTrue(u.laut)
        self.assertFalse(u.ok)

    def test_laut_langer_streak_kommt_durch(self):
        # 2026-10-04 14:42: Raum 677, Wakewort 1209, 6 Frames (echt)
        self.assertTrue(entscheide(6, 1209, gleitend=677, ruhe=27, **REGEL).ok)

    def test_laut_aber_nah_kommt_durch(self):
        # 2026-10-05 18:54: Raum 280, Wakewort 2296, 3 Frames (echt)
        u = entscheide(3, 2296, gleitend=280, ruhe=27, **REGEL)
        self.assertTrue(u.nah)
        self.assertTrue(u.ok)

    def test_ohne_gelernte_ruhe_nicht_streng(self):
        self.assertTrue(entscheide(2, 665, gleitend=468, ruhe=None, **REGEL).ok)
        self.assertTrue(entscheide(2, 665, gleitend=None, ruhe=27, **REGEL).ok)

    def test_nur_messen_blockt_nie(self):
        u = entscheide(2, 665, gleitend=468, ruhe=27, scharf=False, **REGEL)
        self.assertTrue(u.laut)
        self.assertTrue(u.ok)


def _ton(rms: float, sec: float, rate: int = 16000) -> np.ndarray:
    return np.full(int(rate * sec), rms, dtype=np.int16)


class PegelTest(unittest.TestCase):
    def test_gleitend_ohne_die_letzten_sekunden(self):
        p = Umgebungspegel(gleit_sec=10.0)
        t = 1000.0
        for _ in range(100):           # 10 s Raum bei 200
            p.feed(t, _ton(200, 0.1)); t += 0.1
        for _ in range(20):            # 2 s Wakewort bei 3000
            p.feed(t, _ton(3000, 0.1)); t += 0.1
        self.assertAlmostEqual(p.gleitend(t), 200, delta=1)

    def test_ruhe_erst_nach_gelernten_minuten(self):
        p = Umgebungspegel()
        self.assertIsNone(p.ruhe())
        t = 0.0
        for minute in range(40):
            pegel = 30 if minute % 4 else 400     # meist ruhig, ab und zu laut
            for _ in range(10):
                p.feed(t, _ton(pegel, 6.0)); t += 6.0
        self.assertAlmostEqual(p.ruhe(), 30, delta=1)

    def test_stummes_mikrofon_ist_keine_ruhe(self):
        # 2026-10-18: Stunden exakter Nullen zogen den Ruhepegel auf 0
        p = Umgebungspegel()
        t = 0.0
        for minute in range(60):
            pegel = 0 if minute < 20 else 25
            for _ in range(10):
                p.feed(t, _ton(pegel, 6.0)); t += 6.0
        self.assertAlmostEqual(p.ruhe(), 25, delta=1)

    def test_alte_nullen_werden_beim_laden_verworfen(self):
        with tempfile.TemporaryDirectory() as d:
            datei = os.path.join(d, "umgebung.json")
            with open(datei, "w") as f:
                json.dump({"minuten": [[i * 60, 0.0] for i in range(40)]
                           + [[(40 + i) * 60, 25.0] for i in range(40)]}, f)
            self.assertAlmostEqual(Umgebungspegel(zustand_datei=datei).ruhe(), 25, delta=1)

    def test_ruhe_ueberlebt_neustart(self):
        with tempfile.TemporaryDirectory() as d:
            datei = os.path.join(d, "umgebung.json")
            p = Umgebungspegel(zustand_datei=datei)
            t = 0.0
            for _ in range(40 * 10):
                p.feed(t, _ton(30, 6.0)); t += 6.0
            p._speichern()
            self.assertAlmostEqual(Umgebungspegel(zustand_datei=datei).ruhe(), 30, delta=1)


if __name__ == "__main__":
    unittest.main()
