"""Mitschnitt des zweiten Kanals bei der Wiedergabe — ohne Netz und ohne Geraet.

Prueft die Bausteine, die tools/wiedergabe_pruefen.py voraussetzt: der
Client sammelt data2 nur waehrend eines Mitschnitts, und die Senke legt
gesendete Datei, Mitschnitt und Zeiten nebeneinander ab.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio.respeaker import RespeakerClient, RespeakerSink  # noqa: E402
from voice_assistant.config import RespeakerAudio  # noqa: E402


def _client() -> RespeakerClient:
    c = RespeakerClient.__new__(RespeakerClient)   # ohne Verbindungs-Thread
    c._mitschnitt = None
    c._mitschnitt_lock = threading.Lock()
    return c


class MitschnittTest(unittest.TestCase):
    def test_nur_waehrend_mitschnitt(self):
        c = _client()
        self.assertEqual(c.mitschnitt_stop(), b"")      # nie gestartet
        c.mitschnitt_start()
        with c._mitschnitt_lock:
            c._mitschnitt.extend([b"\x01\x00", b"\x02\x00"])
        self.assertEqual(c.mitschnitt_stop(), b"\x01\x00\x02\x00")
        self.assertIsNone(c._mitschnitt)                  # danach wieder aus

    def test_ablage(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = RespeakerSink.__new__(RespeakerSink)
            sink._cfg = RespeakerAudio(mitschnitt=True, kanal2_quelle="referenz")
            sink._MITSCHNITT_DIR = tmp
            gesendet = os.path.join(tmp, "quelle.wav")
            with wave.open(gesendet, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(np.zeros(9600, dtype=np.int16).tobytes())
            pcm = np.arange(1600, dtype=np.int16).tobytes()    # 0,1 s
            sink._mitschnitt_sichern(gesendet, pcm, {"folge": 7, "dauer_s": 0.1})

            namen = sorted(n for n in os.listdir(tmp) if n != "quelle.wav")
            self.assertEqual(len(namen), 3)
            json_name = next(n for n in namen if n.endswith(".json"))
            self.assertIn("_0007", json_name)
            basis = os.path.join(tmp, json_name[: -len(".json")])
            with wave.open(basis + "_kanal2.wav") as wf:
                self.assertEqual((wf.getframerate(), wf.getnchannels()), (16000, 1))
                self.assertEqual(wf.readframes(wf.getnframes()), pcm)
            meta = json.load(open(basis + ".json"))
            self.assertEqual(meta["kanal2_quelle"], "referenz")
            self.assertEqual(meta["kanal2_sekunden"], 0.05 * 2)
            self.assertTrue(os.path.exists(basis + "_gesendet.wav"))

    def test_leerer_mitschnitt_wird_vermerkt(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = RespeakerSink.__new__(RespeakerSink)
            sink._cfg = RespeakerAudio(mitschnitt=True)
            sink._MITSCHNITT_DIR = tmp
            gesendet = os.path.join(tmp, "quelle.wav")
            with wave.open(gesendet, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(b"")
            with self.assertLogs("voice_assistant.audio.respeaker", level="WARNING"):
                sink._mitschnitt_sichern(gesendet, b"", {"folge": 1})
            meta = json.load(open(next(os.path.join(tmp, n) for n in os.listdir(tmp)
                                        if n.endswith(".json"))))
            self.assertEqual(meta["kanal2_sekunden"], 0.0)
            self.assertEqual(meta["kanal2_quelle"], "unveraendert")


if __name__ == "__main__":
    unittest.main()
