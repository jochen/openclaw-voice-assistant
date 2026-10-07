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


class _Gegenstelle:
    """voice-analysis /fingerabdruck im Kleinen: merkt sich die Samples und
    antwortet mit dem Fake-Abdruck — oder mit dem, was der Test vorgibt."""

    def __init__(self, antwort=None, status=200):
        import http.server
        import threading

        self.empfangen = []
        stelle = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                roh = self.rfile.read(int(self.headers["Content-Length"]))
                audio = np.frombuffer(roh, dtype="<f4")
                stelle.empfangen.append(audio)
                body = antwort if antwort is not None else {"vektor": list(map(float, _fake_abdruck(audio)))}
                daten = body if isinstance(body, bytes) else __import__("json").dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(daten)))
                self.end_headers()
                self.wfile.write(daten)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class EntferntTest(unittest.TestCase):
    """sprecher_verifikation_url: nur der Fingerabdruck wird woanders gerechnet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.verz = self.tmp.name
        with open(os.path.join(self.verz, "jochen.wav"), "wb") as f:
            f.write(_wav(0.1))
        with open(os.path.join(self.verz, "petra.wav"), "wb") as f:
            f.write(_wav(0.6))

    def tearDown(self):
        self.tmp.cleanup()

    def _v(self, url):
        # Echter Konstruktor: mit url darf er kein ONNX-Modell laden
        return SprecherVerifikation(sprecher_dir=self.verz, url=url, timeout=2.0)

    def test_bekannte_stimme_ueber_die_gegenstelle(self):
        g = _Gegenstelle()
        try:
            u = self._v(g.url).diarize(_wav(0.1))
        finally:
            g.stop()
        self.assertEqual((u.name, u.status), ("jochen", STATUS_BEKANNT))

    def test_samples_kommen_unveraendert_an(self):
        # Kein WAV unterwegs: genau das float32-Array, das lokal gerechnet wuerde
        g = _Gegenstelle()
        try:
            v = self._v(g.url)
            audio = np.linspace(-0.5, 0.5, 1601, dtype=np.float32)
            v.fingerabdruck(audio)
        finally:
            g.stop()
        np.testing.assert_array_equal(g.empfangen[-1], audio)

    def test_fehler_der_gegenstelle_ist_ausfall(self):
        g = _Gegenstelle(antwort={"error": "onnx"}, status=500)
        try:
            u = self._v(g.url).diarize(_wav(0.1))
        finally:
            g.stop()
        self.assertEqual(u.status, STATUS_AUSGEFALLEN)

    def test_gegenstelle_nicht_erreichbar_ist_ausfall(self):
        g = _Gegenstelle()
        url = g.url
        g.stop()                                # Port ist jetzt zu
        self.assertEqual(self._v(url).diarize(_wav(0.1)).status, STATUS_AUSGEFALLEN)

    def test_unbrauchbarer_vektor_ist_ausfall(self):
        for kaputt in ({"vektor": []}, {"vektor": [float("nan"), 1.0]}, {"nix": 1}, b"kein json"):
            g = _Gegenstelle(antwort=kaputt)
            try:
                u = self._v(g.url).diarize(_wav(0.1))
            finally:
                g.stop()
            self.assertEqual(u.status, STATUS_AUSGEFALLEN, kaputt)


if __name__ == "__main__":
    unittest.main()
