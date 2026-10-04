"""tools/wiedergabe_pruefen.analysiere gegen kuenstliche Fehler — ohne Netz.

"Sprache" ist hier ein vokal-artiges Obertonsignal in Silben; der Mitschnitt ist
dieselbe Folge, verzoegert, leiser und mit Grundrauschen — wie die
Wiedergabe-Referenz des XVF3800. Einmal sauber, einmal mit einem Aussetzer
und einem verwuerfelten Stueck.
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.wiedergabe_pruefen import _RATE, analysiere  # noqa: E402


def _sprache(sekunden: float, rng: np.random.Generator) -> np.ndarray:
    """Vokal-artig: Grundton 110-190 Hz mit Obertoenen unter einer Formant-
    Huelle, ~6 Silben/s mit Pausen. Weisses Rauschen taugt fuer den
    Spektral-Vergleich nicht — es hat in 100 ms kein stabiles Spektrum."""
    n = int(sekunden * _RATE)
    t = np.arange(n) / _RATE
    f0 = 150 + 40 * np.sin(2 * np.pi * 0.7 * t)                  # Satzmelodie
    phase = 2 * np.pi * np.cumsum(f0) / _RATE
    x = np.zeros(n)
    for k in range(1, 30):
        f = k * f0
        gewicht = np.exp(-((f - 700) / 400) ** 2) + 0.6 * np.exp(-((f - 1800) / 500) ** 2) + 0.05
        x += gewicht * np.sin(k * phase) * (f < 7500)
    huelle = np.clip(np.sin(2 * np.pi * 3.0 * t), 0, None) ** 0.5
    x = x * huelle + 0.02 * rng.standard_normal(n)
    return (x / np.abs(x).max() * 8000).astype(np.float32)


def _mitschnitt(g: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    vorlauf = np.zeros(int(0.4 * _RATE), dtype=np.float32)
    m = np.concatenate([vorlauf, 0.3 * g, np.zeros(int(0.3 * _RATE), dtype=np.float32)])
    return m + rng.standard_normal(len(m)).astype(np.float32) * 5


def _in(intervalle, t: float) -> bool:
    return any(a - 0.05 <= t <= b + 0.05 for a, b in intervalle)


class AnalyseTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(1)
        self.g = _sprache(4.0, self.rng)

    def test_sauber(self):
        r = analysiere(self.g, _mitschnitt(self.g, self.rng))
        self.assertAlmostEqual(r["versatz_s"], 0.4, places=2)
        self.assertGreater(r["huelle_korr"], 0.95)
        self.assertEqual(r["luecken"], [])
        self.assertEqual(r["verzerrt"], [])
        self.assertEqual(r["spruenge"], [])

    def test_taktdrift_ist_kein_befund(self):
        # Gemessen 2026-10-03: der Mitschnitt laeuft ~0,4 % schneller als die
        # Datei. Eine glatte Drift ist hoerbar nichts und darf nicht anschlagen.
        from scipy.signal import resample
        g = _sprache(7.0, self.rng)
        m = _mitschnitt(g, self.rng)
        m = resample(m, int(len(m) * 0.996)).astype(np.float32)
        r = analysiere(g, m)
        # Synthetisch schaetzt das Verfahren hier ~-2,6 statt -4 ‰ (die streng
        # periodische Silbenhuelle macht benachbarte Lagen mehrdeutig); an den
        # echten Mitschnitten vom 2026-10-03 kamen -3,9/-4,0 ‰ heraus. Wichtig
        # ist: Drift erkannt, und sie loest keinen Befund aus.
        self.assertTrue(-6.0 < r["drift_promille"] < -2.0, r["drift_promille"])
        self.assertEqual((r["luecken"], r["verzerrt"], r["spruenge"]), ([], [], []))

    def test_fehlendes_stueck_ist_ein_sprung(self):
        g = _sprache(7.0, self.rng)
        m = _mitschnitt(g, self.rng)
        off = int(0.4 * _RATE)
        a = off + int(3.0 * _RATE)
        m = np.concatenate([m[:a], m[a + int(0.06 * _RATE):]])   # 60 ms fehlen
        r = analysiere(g, m)
        self.assertTrue(r["spruenge"], r)
        self.assertTrue(all(abs(ms) >= 40 for _, ms in r["spruenge"]), r["spruenge"])

    def test_aussetzer_und_verwuerfelt(self):
        m = _mitschnitt(self.g, self.rng)
        off = int(0.4 * _RATE)
        # Aussetzer 1,05–1,25 s (mitten in einer Silbe)
        m[off + int(1.05 * _RATE):off + int(1.25 * _RATE)] = 0
        # verwuerfeltes Stueck 2,05–2,25 s: gleiche Energie, falscher Inhalt
        a, b = off + int(2.05 * _RATE), off + int(2.25 * _RATE)
        m[a:b] = self.rng.permutation(m[a:b])
        r = analysiere(self.g, m)
        self.assertTrue(_in(r["luecken"], 1.15), r["luecken"])
        self.assertTrue(_in(r["verzerrt"], 2.15), r["verzerrt"])
        self.assertFalse(_in(r["luecken"], 3.15))

    def test_leer(self):
        self.assertIn("fehler", analysiere(self.g, np.zeros(0, dtype=np.float32)))


if __name__ == "__main__":
    unittest.main()
