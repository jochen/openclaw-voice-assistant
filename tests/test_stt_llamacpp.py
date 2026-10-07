"""LlamaCppAsrStt: Antwortformat, Kontext-Leck mit Wiederholung, Ausfall."""

import http.server
import json
import queue
import threading
import unittest

import numpy as np

from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stt import LlamaCppAsrStt, SttPipeline, ist_kontext_leck

KONTEXT = "Gaston, Küchenlicht, Wohnzimmerrollo, Heizung oben, alle Rollos, Flurlicht."


class _Server(http.server.BaseHTTPRequestHandler):
    antworten: list = []
    anfragen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).anfragen.append(body)
        text = type(self).antworten.pop(0)
        out = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class LlamaCppAsrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Server.anfragen.clear()

    def test_praefix_wird_abgeschnitten_kontext_geht_mit(self):
        _Server.antworten[:] = ["language German<asr_text>Gaston, mach das Flurlicht an."]
        stt = LlamaCppAsrStt(self.url, kontext=lambda: KONTEXT)
        self.assertEqual(stt.transcribe(b"RIFF"), "Gaston, mach das Flurlicht an.")
        self.assertEqual(_Server.anfragen[0]["messages"][0], {"role": "system", "content": KONTEXT})

    def test_leck_wird_ohne_kontext_wiederholt(self):
        _Server.antworten[:] = ["language German<asr_text>" + KONTEXT,
                                "language German<asr_text>Gaston, Tischlicht an."]
        stt = LlamaCppAsrStt(self.url, kontext=lambda: KONTEXT)
        self.assertEqual(stt.transcribe(b"RIFF"), "Gaston, Tischlicht an.")
        self.assertEqual(len(_Server.anfragen), 2)
        self.assertEqual(_Server.anfragen[1]["messages"][0]["role"], "user")   # ohne System

    def test_leer_ist_keine_sprache_kein_fehler(self):
        _Server.antworten[:] = ["language None<asr_text>"]
        stt = LlamaCppAsrStt(self.url)
        self.assertIsNone(stt.transcribe(b"RIFF"))
        self.assertTrue(stt.state.stt_ok())

    def test_ausfall_faellt_auf_naechste_stufe(self):
        stt = LlamaCppAsrStt("http://127.0.0.1:9", timeout=1)   # niemand hört zu

        class _Naechste:
            state = SpeachesState()

            def transcribe(self, wav):
                return "parakeet"
        q = queue.Queue()
        SttPipeline(None, None, _Naechste(), stt).run([np.zeros(1600, dtype=np.int16)], q)
        self.assertEqual(q.get_nowait(), "parakeet")
        self.assertFalse(stt.state.stt_ok())


class KontextLeckTest(unittest.TestCase):
    def test_echter_satz_mit_wakewort_ist_kein_leck(self):
        self.assertFalse(ist_kontext_leck("Gaston, Küchenlicht an.", KONTEXT))

    def test_abgeschriebener_kontext_ist_leck(self):
        self.assertTrue(ist_kontext_leck(KONTEXT, KONTEXT))

    def test_ohne_kontext_nie(self):
        self.assertFalse(ist_kontext_leck("Gaston, Küchenlicht an.", None))


if __name__ == "__main__":
    unittest.main()
