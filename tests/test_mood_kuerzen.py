"""Stimmungsanalyse bekommt höchstens die ersten _MOOD_MAX_SEC der Aufnahme."""

import io
import queue
import unittest
import wave

import numpy as np

from voice_assistant import workers as w


class _Analyzer:
    def __init__(self):
        self.sekunden = None

    def analyze(self, wav_bytes):
        with wave.open(io.BytesIO(wav_bytes)) as f:
            self.sekunden = f.getnframes() / f.getframerate()
        return None


def _lauf(sekunden):
    a = _Analyzer()
    ws = w.Workers.__new__(w.Workers)          # nur der Mood-Pfad, ohne Dienste
    ws.mood_analyzer = a
    chunk = np.zeros(1280, dtype=np.int16)     # 80 ms bei 16 kHz
    ws._mood_worker([chunk] * int(sekunden / 0.08), queue.Queue())
    return a.sekunden


class MoodKuerzenTest(unittest.TestCase):
    def test_lange_aufnahme_wird_gekuerzt(self):
        self.assertAlmostEqual(_lauf(31.6), w._MOOD_MAX_SEC, places=2)

    def test_kurze_aufnahme_bleibt_ganz(self):
        self.assertAlmostEqual(_lauf(4.8), 4.8, places=2)


if __name__ == "__main__":
    unittest.main()
