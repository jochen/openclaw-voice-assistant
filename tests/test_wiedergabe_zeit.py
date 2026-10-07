"""Wie lange RespeakerSink.play_wav blockiert — ohne Netz und ohne Geraet.

Anlass 2026-10-06 18:02: nach einem Barge-in meldete der Player fuer den
0,3-s-Abbruch-Beep keinen Abspiel-Zustand (er war vom STOP her noch im
Abspiel-Zustand, ESPHome meldet nur Aenderungen). play_wav wartete dann 2,0 s
auf den Anker und legte die Annahme fuer Datei-Holen obendrauf: 2,9 s
blockierte Hauptschleife fuer 0,3 s Ton.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio.respeaker import RespeakerSink  # noqa: E402
from voice_assistant.config import RespeakerAudio  # noqa: E402


class FakeClient:
    def __init__(self, anker: bool) -> None:
        self._loop = object()
        self._api = object()
        self.anker = anker

    def play_url(self, url: str) -> bool:
        return True

    def wait_player(self, busy: bool, timeout: float) -> bool:
        if busy and not self.anker:
            time.sleep(timeout)         # Zustand kommt nie
            return False
        return True

    def in_session(self) -> bool:
        return True


def _sink(tmp: str, anker: bool) -> RespeakerSink:
    sink = RespeakerSink.__new__(RespeakerSink)
    sink._cfg = RespeakerAudio()
    sink._client = FakeClient(anker)
    sink._serve_dir = tmp
    sink._pi_ip = "127.0.0.1"
    sink._lock = threading.Lock()
    sink._stopped = False
    sink._folge = 0
    return sink


def _wav(tmp: str, sekunden: float) -> str:
    pfad = os.path.join(tmp, "quelle.wav")
    with wave.open(pfad, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(np.zeros(int(16000 * sekunden), dtype=np.int16).tobytes())
    return pfad


def _dauer(sink: RespeakerSink, pfad: str) -> float:
    t0 = time.monotonic()
    sink.play_wav(pfad)
    return time.monotonic() - t0


class OhneAnkerTest(unittest.TestCase):
    def test_kurzer_beep_blockiert_nicht_zwei_sekunden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = _sink(tmp, anker=False)
            dauer = _dauer(sink, _wav(tmp, 0.3))
            # Datei (0,3 s + 2x 30 ms Stille) + Hol-Annahme 0,6 s, nicht 2,9 s.
            self.assertLess(dauer, 1.3)
            self.assertGreater(dauer, 0.8)

    def test_langer_satz_bekommt_die_volle_laenge(self) -> None:
        # Gegenprobe: die Kappung darf einen Satz nicht abschneiden.
        with tempfile.TemporaryDirectory() as tmp:
            dauer = _dauer(_sink(tmp, anker=False), _wav(tmp, 3.0))
            self.assertGreater(dauer, 3.0 + 0.6 - 0.1)
            self.assertLess(dauer, 3.0 + 0.6 + 0.4)


class MitAnkerTest(unittest.TestCase):
    def test_mit_anker_zaehlt_nur_die_datei(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dauer = _dauer(_sink(tmp, anker=True), _wav(tmp, 0.5))
            self.assertGreater(dauer, 0.5)
            self.assertLess(dauer, 0.9)


if __name__ == "__main__":
    unittest.main()
