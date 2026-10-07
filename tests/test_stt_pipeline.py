"""SttPipeline mit vorgeschaltetem onnx-asr: wer antwortet wann."""

import queue
import unittest

import numpy as np

from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stt import SttPipeline


class _Engine:
    def __init__(self, antwort, fehler=False):
        self.state = SpeachesState()
        self.antwort, self.fehler, self.aufrufe = antwort, fehler, 0

    def transcribe(self, wav_bytes):
        self.aufrufe += 1
        if self.fehler:
            self.state.mark_stt_failed()
            return None
        self.state.mark_stt_ok()
        return self.antwort


class _Lokal:
    def __init__(self):
        self.aufrufe = 0

    def transcribe(self, chunks):
        self.aufrufe += 1
        return "lokal"


def _lauf(onnx, speaches, lokal):
    q = queue.Queue()
    SttPipeline(speaches, lokal, onnx).run([np.zeros(1600, dtype=np.int16)], q)
    return q.get_nowait()


class SttPipelineOnnxTest(unittest.TestCase):
    def test_onnx_antwortet_speaches_wird_nicht_gefragt(self):
        onnx, sp, lo = _Engine("Gaston, Licht an."), _Engine("speaches"), _Lokal()
        self.assertEqual(_lauf(onnx, sp, lo), "Gaston, Licht an.")
        self.assertEqual((sp.aufrufe, lo.aufrufe), (0, 0))

    def test_keine_sprache_ist_kein_fehler(self):
        # Leerer Text = nichts gesagt. Kein Rückfall, sonst schriebe Speaches
        # auf Stille womöglich etwas hin.
        onnx, sp, lo = _Engine(None), _Engine("speaches"), _Lokal()
        self.assertIsNone(_lauf(onnx, sp, lo))
        self.assertEqual(sp.aufrufe, 0)

    def test_ausfall_faellt_auf_speaches_zurueck(self):
        onnx, sp, lo = _Engine(None, fehler=True), _Engine("speaches"), _Lokal()
        self.assertEqual(_lauf(onnx, sp, lo), "speaches")

    def test_ohne_onnx_wie_bisher(self):
        sp, lo = _Engine("speaches"), _Lokal()
        self.assertEqual(_lauf(None, sp, lo), "speaches")


if __name__ == "__main__":
    unittest.main()
