"""tools/wiedergabe_pruefen.analysiere gegen kuenstliche Fehler — ohne Netz.

"Sprache" ist hier silbenartig moduliertes Rauschen; der Mitschnitt ist
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
    t = np.arange(int(sekunden * _RATE)) / _RATE
    huelle = np.clip(np.sin(2 * np.pi * 3.0 * t), 0, None) ** 0.5   # ~6 Silben/s, Pausen dazwischen
    return (rng.standard_normal(len(t)) * huelle * 3000).astype(np.float32)


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
        self.assertGreater(r["spitzenkorrelation"], 0.9)
        self.assertEqual(r["luecken"], [])
        self.assertEqual(r["verzerrt"], [])

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
