"""Silero als Einspruch gegen WebRTC-"Sprache" im Raumpegel (voice_assistant/sprache_vad.py).

Anlass Fablab 2026-11-04..07: Lasercutter-Lüfter hielten Aufnahmen bis zum
30-s-Deckel offen. Festgehalten wird hier die Regel, nicht das Modell:
Silero darf nur widersprechen, wenn der Chunk leiser als Faktor × Raumpegel
ist — lautes, nahes Sprechen überstimmt es nie (Silero allein schnitt
daheim ein "Stopp, Stopp" ab).
"""
import unittest

import numpy as np

from voice_assistant.sprache_vad import SileroVad, SprachEntscheider


class FalscherSilero:
    def __init__(self):
        self.p = 0.0
        self.resets = 0

    def reset(self):
        self.resets += 1

    def prob(self, chunk):
        return self.p


def stats_mit(werte):
    """stats-Funktion, die (ist_sprache, rms) der Reihe nach liefert"""
    it = iter(werte)

    def stats(vad, audio, rms_min):
        s, r = next(it)
        return s, r, 4 if s else 0, 4
    return stats


CHUNK = np.zeros(1280, np.int16)


class EinspruchTest(unittest.TestCase):
    def setUp(self):
        self.sil = FalscherSilero()

    def entscheider(self, werte, raum=1000.0, aktiv=True):
        e = SprachEntscheider(stats_mit(werte), None, 900, self.sil, schwelle=0.3, faktor=2.0)
        e.neuer_turn(raum, aktiv=aktiv)
        return e

    def test_raumpegel_ohne_silero_ist_keine_sprache(self):
        e = self.entscheider([(True, 1100.0)])
        self.sil.p = 0.1
        self.assertFalse(e.chunk(CHUNK, 25)[0])
        self.assertEqual(e.ueberstimmt, 1)

    def test_laut_ueberstimmt_silero_nie(self):
        e = self.entscheider([(True, 2500.0)])
        self.sil.p = 0.0
        self.assertTrue(e.chunk(CHUNK, 25)[0])

    def test_silero_bestaetigt_sprache_im_raumpegel(self):
        e = self.entscheider([(True, 1100.0)])
        self.sil.p = 0.8
        self.assertTrue(e.chunk(CHUNK, 25)[0])

    def test_webrtc_still_bleibt_still(self):
        e = self.entscheider([(False, 3000.0)])
        self.sil.p = 1.0
        self.assertFalse(e.chunk(CHUNK, 25)[0])

    def test_ohne_raumpegel_alte_regel(self):
        e = self.entscheider([(True, 1100.0)], raum=None)
        self.assertFalse(e.aktiv)
        self.assertTrue(e.chunk(CHUNK, 25)[0])
        self.assertEqual(e.als_log(0.08), {})

    def test_satellit_alte_regel(self):
        e = self.entscheider([(True, 1100.0)], aktiv=False)
        self.assertTrue(e.chunk(CHUNK, 25)[0])

    def test_neuer_turn_setzt_silero_zurueck(self):
        self.entscheider([])
        self.assertEqual(self.sil.resets, 2)   # Konstruktor + neuer_turn

    def test_log_sagt_wann_die_alte_regel_geendet_haette(self):
        # Sprache, dann zwei Chunks Stille nach alter Regel: Ende bei Chunk 3
        e = self.entscheider([(True, 2500.0), (False, 50.0), (False, 50.0), (True, 1100.0)])
        self.sil.p = 0.0
        for _ in range(4):
            e.chunk(CHUNK, 2)
        log = e.als_log(0.08)
        self.assertEqual(log["vad_alt_ende_s"], 0.24)
        self.assertEqual(log["silero_ueberstimmt_s"], 0.08)
        self.assertEqual(log["vad_raum"], 1000.0)


class SileroModellTest(unittest.TestCase):
    """Das echte Modell aus models/silero_vad: läuft auf beliebigen Chunk-Größen."""

    def test_stille_und_rauschen_sind_keine_sprache(self):
        s = SileroVad()
        for laenge in (640, 1280, 300):
            p = s.prob(np.zeros(laenge, np.int16))
            self.assertLess(p, 0.3)
        rng = np.random.default_rng(1)
        s.reset()
        ps = [s.prob((rng.standard_normal(1280) * 1000).astype(np.int16)) for _ in range(25)]
        self.assertLess(max(ps), 0.3)


if __name__ == "__main__":
    unittest.main()
