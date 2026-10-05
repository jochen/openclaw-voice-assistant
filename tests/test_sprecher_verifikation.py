"""SprecherVerifikation: Urteil, Fehlerwege, Anlernen ohne Neustart — ohne Modell.

Der Fingerabdruck wird durch eine Fake-Funktion ersetzt (Vektor aus dem
ersten Sample), damit die Tests ohne ONNX-Modell und in Millisekunden laufen.
Die Rechnung selbst ist gegen die Messung geprüft (Abweichung 0,000000, siehe
tools/sprecher_verifikation_test.py).
"""

import io
import os
import tempfile
import time
import unittest
import wave

import numpy as np

from voice_assistant.services.diarization import (
    STATUS_AUSGEFALLEN,
    STATUS_BEKANNT,
    STATUS_NICHT_EINGERICHTET,
    STATUS_UNBEKANNT,
)
from voice_assistant.services.sprecher_verifikation import SprecherVerifikation


def _wav(wert: float, sek: float = 1.0) -> bytes:
    a = np.full(int(16000 * sek), int(wert * 32767), dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(a.tobytes())
    return buf.getvalue()


def _fake_abdruck(audio):
    # Winkel aus dem Pegel: gleicher Pegel -> gleiche Richtung -> Ähnlichkeit 1
    phi = float(audio[0]) * np.pi
    return np.array([np.cos(phi), np.sin(phi)])


def _pruefer(verz, schwelle=0.40, abstand=0.15):
    v = SprecherVerifikation.__new__(SprecherVerifikation)
    v.schwelle, v.abstand, v.sprecher_dir = schwelle, abstand, verz
    v._refs = {}
    import threading
    v._lock = threading.Lock()
    v.fingerabdruck = _fake_abdruck
    return v


class UrteilTest(unittest.TestCase):
    def setUp(self):
        self.v = _pruefer("/nicht/da")

    def test_ueber_schwelle_mit_abstand_ist_bekannt(self):
        u = self.v.urteil({"jochen": 0.53, "petra": 0.09})
        self.assertEqual((u.name, u.status), ("jochen", STATUS_BEKANNT))

    def test_unter_schwelle_ist_unbekannt(self):
        # "Gaston." allein: 0,39 (gemessen 2026-10-05)
        u = self.v.urteil({"jochen": 0.39, "petra": 0.22})
        self.assertEqual((u.name, u.status), (None, STATUS_UNBEKANNT))

    def test_zu_wenig_abstand_ist_unbekannt(self):
        u = self.v.urteil({"jochen": 0.45, "petra": 0.40})
        self.assertEqual(u.status, STATUS_UNBEKANNT)

    def test_ohne_referenzen_nicht_eingerichtet(self):
        self.assertEqual(self.v.urteil({}).status, STATUS_NICHT_EINGERICHTET)


class DiarizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.verz = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _ref(self, name, wert):
        with open(os.path.join(self.verz, f"{name}.wav"), "wb") as f:
            f.write(_wav(wert))

    def test_ohne_referenzen_nicht_eingerichtet(self):
        self.assertEqual(_pruefer(self.verz).diarize(_wav(0.1)).status, STATUS_NICHT_EINGERICHTET)

    def test_kaputtes_audio_ist_ausfall_nicht_fremder(self):
        self._ref("jochen", 0.1)
        self.assertEqual(_pruefer(self.verz).diarize(b"kein wav").status, STATUS_AUSGEFALLEN)

    def test_absturz_im_fingerabdruck_ist_ausfall(self):
        self._ref("jochen", 0.1)
        v = _pruefer(self.verz)
        v.fingerabdruck = lambda a: (_ for _ in ()).throw(RuntimeError("onnx"))
        self.assertEqual(v.diarize(_wav(0.1)).status, STATUS_AUSGEFALLEN)

    def test_bekannte_stimme_wird_erkannt(self):
        self._ref("jochen", 0.1)
        self._ref("petra", 0.6)
        u = _pruefer(self.verz).diarize(_wav(0.1))
        self.assertEqual((u.name, u.status), ("jochen", STATUS_BEKANNT))

    def test_neu_angelernt_wirkt_ohne_neustart(self):
        self._ref("jochen", 0.1)
        v = _pruefer(self.verz)
        self.assertEqual(v.diarize(_wav(0.6)).status, STATUS_UNBEKANNT)
        time.sleep(0.01)
        self._ref("maja", 0.6)                 # Anlernen über den Brain
        u = v.diarize(_wav(0.6))
        self.assertEqual((u.name, u.status), ("maja", STATUS_BEKANNT))

    def test_nachgelernt_ersetzt_alten_fingerabdruck(self):
        self._ref("petra", 0.9)                # weit weg von 0,3 -> Ähnlichkeit < 0
        v = _pruefer(self.verz)
        self.assertEqual(v.diarize(_wav(0.3)).status, STATUS_UNBEKANNT)
        time.sleep(0.01)
        self._ref("petra", 0.3)                # Nachlernen
        os.utime(os.path.join(self.verz, "petra.wav"))
        self.assertEqual(v.diarize(_wav(0.3)).name, "petra")


if __name__ == "__main__":
    unittest.main()
