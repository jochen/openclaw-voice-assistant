"""Eigene Stimmen des Antwort-Dienstes (services/stimmen.py)."""
import base64
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_tts_strom import FakeLeds, FakeSenke, FakeTts, _ton, _wav_strom  # noqa: E402
from voice_assistant.audio.strom import zu_48k_stereo  # noqa: E402
from voice_assistant.services import tts  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.services.stimmen import AntwortTts, StimmenLager  # noqa: E402
from voice_assistant.services.voice_control import VoiceController  # noqa: E402
from voice_assistant.state import voice_state  # noqa: E402


class _Cosy(http.server.BaseHTTPRequestHandler):
    """Wie cosy_dienst.py: unbekannte Stimme 404, POST /v1/voices legt an."""
    stimmen: set = set()
    anfragen: list = []
    uploads: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/voices":
            _Cosy.uploads.append(body)
            _Cosy.stimmen.add(body["id"])
            return self._antwort(200, b'{"ok": true}', "application/json")
        _Cosy.anfragen.append(body)
        if body["voice"] not in _Cosy.stimmen:
            return self._antwort(404, b'{"error": "unbekannt"}', "application/json")
        self._antwort(200, _wav_strom(_ton(0.1), 48000), "audio/wav")

    def _antwort(self, code, daten, typ):
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def log_message(self, *a):
        pass


def _stimme(ordner, name, text="Hallo.", meta=None, wav=b"RIFF....WAVEfmt "):
    os.makedirs(os.path.join(ordner, name))
    with open(os.path.join(ordner, name, "probe.wav"), "wb") as f:
        f.write(wav)
    with open(os.path.join(ordner, name, "probe.txt"), "w") as f:
        f.write(text)
    if meta is not None:
        with open(os.path.join(ordner, name, "meta.json"), "w") as f:
            json.dump(meta, f)


class _Basis(unittest.TestCase):
    def setUp(self):
        self.ordner = tempfile.mkdtemp()
        _stimme(self.ordner, "gaston2", meta={"beschreibung": "ruhiger Erzähler"})
        _stimme(self.ordner, "thorsten")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Cosy)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        _Cosy.stimmen, _Cosy.anfragen, _Cosy.uploads = set(), [], []
        self.lager = StimmenLager(self.ordner, "haus", f"http://127.0.0.1:{self.srv.server_port}",
                                  "cosyvoice3", "gaston2")
        self.alt = voice_state.get()
        voice_state.set(model="speaches-ai/piper-de_DE-thorsten-medium",
                        voice="de_DE-thorsten-medium", speed=1.0)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.ordner)
        m, v, sp = self.alt
        voice_state.set(model=m, voice=v, speed=sp)


class LagerTest(_Basis):
    def test_namen_nur_vollstaendige_und_freigegebene(self):
        _stimme(self.ordner, "gesperrt", meta={"freigegeben": False})
        os.makedirs(os.path.join(self.ordner, "halb"))
        open(os.path.join(self.ordner, "halb", "probe.wav"), "wb").close()
        self.assertEqual(self.lager.namen(), ["gaston2", "thorsten"])
        self.assertEqual(self.lager.eintraege()[0], {"name": "gaston2", "beschreibung": "ruhiger Erzähler"})

    def test_keine_pfade_als_name(self):
        self.assertFalse(self.lager.hat("../gaston2"))
        self.assertFalse(self.lager.hat(None))

    def test_kennung_wechselt_mit_der_probe(self):
        alt = self.lager.dienst_id("gaston2")
        self.assertTrue(alt.startswith("haus/gaston2-"))
        with open(os.path.join(self.ordner, "gaston2", "probe.txt"), "w") as f:
            f.write("Anderer Text.")
        self.assertNotEqual(self.lager.dienst_id("gaston2"), alt)

    def test_wirksam(self):
        self.assertEqual(self.lager.wirksam("cosyvoice3", "thorsten"), "thorsten")
        self.assertEqual(self.lager.wirksam("cosyvoice3", "gibtsnicht"), "gaston2")
        # Piper-Vorliebe (alte speaker_voices.json) ist keine Wahl im Lager
        self.assertEqual(self.lager.wirksam("speaches-ai/piper-de_DE-thorsten-medium", "thorsten"), "gaston2")

    def test_hochladen(self):
        kennung = self.lager.hochladen("gaston2")
        up = _Cosy.uploads[0]
        self.assertEqual((up["id"], up["text"]), (kennung, "Hallo."))
        self.assertEqual(base64.b64decode(up["wav_b64"]), b"RIFF....WAVEfmt ")


class AntwortTtsTest(_Basis):
    def test_unbekannt_beim_dienst_wird_hochgeladen_und_gesprochen(self):
        t = AntwortTts(self.lager, SpeachesState())
        bloecke = list(t.synth_strom("Hallo."))
        self.assertEqual(b"".join(bloecke), zu_48k_stereo(_ton(0.1), 48000, 1))
        self.assertEqual(len(_Cosy.uploads), 1)
        self.assertEqual([a["voice"] for a in _Cosy.anfragen], [self.lager.dienst_id("gaston2")] * 2)
        # zweiter Satz: kein Upload mehr
        list(t.synth_strom("Noch einer."))
        self.assertEqual(len(_Cosy.uploads), 1)

    def test_gewaehlte_stimme_aus_voice_state(self):
        voice_state.set(model="cosyvoice3", voice="thorsten", speed=1.0)
        t = AntwortTts(self.lager, SpeachesState())
        list(t.synth_strom("Hallo."))
        self.assertTrue(_Cosy.anfragen[-1]["voice"].startswith("haus/thorsten-"))
        self.assertEqual(t.voice, "thorsten")

    def test_speaches_spricht_bei_cosy_wahl_mit_profilstimme(self):
        AntwortTts(self.lager, SpeachesState())   # meldet cosyvoice3 als fremd an
        voice_state.set(model="cosyvoice3", voice="thorsten", speed=1.0)
        s = tts.SpeachesTts(SpeachesState(), "http://x", "speaches-ai/piper-de_DE-thorsten-medium",
                            "de_DE-thorsten-medium")
        self.assertEqual(s._parameter("Hallo.")[:2],
                         ("speaches-ai/piper-de_DE-thorsten-medium", "de_DE-thorsten-medium"))

    def test_cosy_wahl_bleibt_im_strom_beim_antwort_dienst(self):
        voice_state.set(model="cosyvoice3", voice="thorsten", speed=1.0)
        cosy = FakeTts(model="cosyvoice3")
        lokal = FakeTts(model="speaches-ai/piper-de_DE-thorsten-medium", voice="de_DE-thorsten-medium")
        senke = FakeSenke()
        sp = tts.ReplySpeaker(lokal, senke.play_wav, FakeLeds(),
                              strom_oeffnen=senke.strom_oeffnen, antwort_tts=cosy)
        self.assertEqual(sp.strom_kandidaten(), [cosy, lokal])


class VoiceControllerTest(_Basis):
    def _vc(self):
        return VoiceController("http://127.0.0.1:1", "speaches-ai/piper-de_DE-thorsten-medium",
                               "de_DE-thorsten-medium", stimmen=self.lager)

    def test_liste_sind_die_eigenen_stimmen(self):
        liste = self._vc().list_available()
        self.assertEqual([(e["model"], e["voice"]) for e in liste],
                         [("cosyvoice3", "gaston2"), ("cosyvoice3", "thorsten")])
        self.assertEqual(liste[0]["beschreibung"], "ruhiger Erzähler")

    def test_wahl_setzt_voice_state_und_laedt_hoch(self):
        vc = self._vc()
        self.assertTrue(vc.set_active("cosyvoice3", "thorsten"))
        self.assertEqual(voice_state.get()[:2], ("cosyvoice3", "thorsten"))
        for _ in range(50):           # Upload laeuft im Hintergrund
            if _Cosy.uploads:
                break
            threading.Event().wait(0.02)
        self.assertTrue(_Cosy.uploads[0]["id"].startswith("haus/thorsten-"))

    def test_unbekannte_stimme_abgelehnt(self):
        self.assertFalse(self._vc().set_active("cosyvoice3", "gibtsnicht"))

    def test_wirksam_meldet_die_sprechende_stimme(self):
        self.assertEqual(self._vc().wirksam()[:2], ("cosyvoice3", "gaston2"))


if __name__ == "__main__":
    unittest.main()
