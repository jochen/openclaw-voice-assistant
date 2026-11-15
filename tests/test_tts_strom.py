"""Wiedergabe als Strom (audio/strom.py) — ohne Geraet und ohne Speaches.

Der ESP wird durch einen HTTP-Leser ersetzt, Speaches durch einen kleinen
Server, der eine WAV in Stuecken schickt. Was das Geraet selbst tut, ist in
der Strom-Probe vom 2026-11-08 gemessen (Docstring von audio/strom.py).
"""

import http.server
import io
import os
import struct
import sys
import threading
import time
import unittest
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio import strom as strom_mod  # noqa: E402
from voice_assistant.audio.respeaker import RespeakerSink, _make_http_handler  # noqa: E402
from voice_assistant.audio.strom import BYTES_JE_S, WiedergabeStrom, wav_kopf, zu_48k_stereo  # noqa: E402
from voice_assistant.config import RespeakerAudio  # noqa: E402
from voice_assistant.services import tts  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.state import tts_lock  # noqa: E402


def _ton(sekunden: float, rate: int = 48000) -> bytes:
    n = int(sekunden * rate)
    s = (3000 * np.sin(np.arange(n) * 2 * np.pi * 440 / rate)).astype(np.int16)
    return s.tobytes()


class Ausgang(io.RawIOBase):
    def __init__(self) -> None:
        self.daten = bytearray()
        self.kaputt = False

    def write(self, b) -> int:
        if self.kaputt:
            raise BrokenPipeError
        self.daten += b
        return len(b)

    def flush(self) -> None:
        pass


def _ausliefern(strom: WiedergabeStrom) -> tuple[Ausgang, threading.Thread]:
    aus = Ausgang()
    t = threading.Thread(target=strom.ausliefern, args=(aus,), daemon=True)
    t.start()
    return aus, t


class StromTest(unittest.TestCase):
    def test_kopf_und_audio_bis_zum_schliessen(self):
        s = WiedergabeStrom(leerlauf_s=5)
        aus, t = _ausliefern(s)
        audio = zu_48k_stereo(_ton(0.2), 48000, 1)
        self.assertTrue(s.beginnen())
        self.assertTrue(s.schreiben(audio))
        s.beenden()
        s.schliessen()
        t.join(2)
        self.assertTrue(s.fertig.is_set())
        self.assertEqual(bytes(aus.daten[:44]), wav_kopf())
        self.assertEqual(bytes(aus.daten[44:44 + len(audio)]), audio)
        self.assertEqual(s.audio_bytes, len(audio))
        self.assertFalse(s.schreiben(audio), "nach dem Schliessen nimmt er nichts mehr")

    def test_kopf_ist_48k_stereo_16bit(self):
        kopf = wav_kopf()
        _, kanaele, rate, _, _, bits = struct.unpack("<HHIIHH", kopf[20:36])
        self.assertEqual((kanaele, rate, bits), (2, 48000, 16))

    def test_leerlauf_schliesst_und_fuellt_vorher_stille(self):
        s = WiedergabeStrom(leerlauf_s=0.3)
        aus, t = _ausliefern(s)
        s.schreiben(zu_48k_stereo(_ton(0.1), 48000, 1))
        t.join(3)
        self.assertTrue(s.fertig.is_set(), "Leerlauf muss den Strom beenden")
        self.assertFalse(s.lebt())
        self.assertGreaterEqual(s.gesendet_bytes, s.audio_bytes)

    def test_beschaeftigt_haelt_offen(self):
        s = WiedergabeStrom(leerlauf_s=0.2)
        aus, t = _ausliefern(s)
        s.schreiben(zu_48k_stereo(_ton(0.05), 48000, 1))
        self.assertTrue(s.beginnen())
        time.sleep(0.6)               # Synthese dauert laenger als der Leerlauf
        self.assertTrue(s.lebt())
        s.beenden()
        t.join(2)
        self.assertFalse(s.lebt())

    def test_luecke_wird_stille_und_zaehlt_zur_laenge(self):
        s = WiedergabeStrom(leerlauf_s=5)
        aus, t = _ausliefern(s)
        s.beginnen()
        s.schreiben(zu_48k_stereo(_ton(0.05), 48000, 1))
        time.sleep(1.2)               # Spielkopf ueberholt die 0,05 s Audio
        s.schreiben(zu_48k_stereo(_ton(0.05), 48000, 1))
        s.beenden()
        s.schliessen()
        t.join(2)
        stille_s = (s.gesendet_bytes - s.audio_bytes) / BYTES_JE_S
        self.assertGreater(stille_s, 0.3, "die Luecke muss mit Stille gefuellt sein")
        # Das Gesendete laeuft dem geschaetzten Spielkopf nur wenig voraus.
        spielkopf = 1.2 - strom_mod._START_ANNAHME_S
        self.assertLess(s.gesendet_s - spielkopf, 0.5)

    def test_abgerissener_ausgang_bricht_ab(self):
        s = WiedergabeStrom(leerlauf_s=5)
        aus, t = _ausliefern(s)
        s.schreiben(b"\x00" * 1000)
        time.sleep(0.1)
        aus.kaputt = True
        s.schreiben(b"\x01" * 1000)
        t.join(2)
        self.assertTrue(s.abgebrochen)
        self.assertTrue(s.fertig.is_set())

    def test_mono_22050_wird_48k_stereo(self):
        pcm = _ton(1.0, 22050)
        aus = zu_48k_stereo(pcm, 22050, 1)
        self.assertEqual(len(aus), BYTES_JE_S)


# --- Speaches als Strom -------------------------------------------------------

def _wav_strom(pcm: bytes, rate: int, mit_list: bool = False) -> bytes:
    platz = 0x7FFFFFFF
    kopf = b"RIFF" + struct.pack("<I", platz) + b"WAVE"
    kopf += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    if mit_list:
        kopf += b"LIST" + struct.pack("<I", 5) + b"abcde\x00"
    return kopf + b"data" + struct.pack("<I", platz) + pcm


class _Speaches(http.server.BaseHTTPRequestHandler):
    antwort = b""
    anfragen: list = []

    def do_POST(self):
        import json
        laenge = int(self.headers["Content-Length"])
        _Speaches.anfragen.append(json.loads(self.rfile.read(laenge)))
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.end_headers()
        for i in range(0, len(self.antwort), 7001):   # krumme Stuecke
            self.wfile.write(self.antwort[i:i + 7001])
            self.wfile.flush()

    def log_message(self, *a):
        pass


class SynthStromTest(unittest.TestCase):
    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Speaches)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tts = tts.SpeachesTts(SpeachesState(), f"http://127.0.0.1:{self.srv.server_port}",
                                   "piper", "thorsten")
        _Speaches.anfragen = []

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_48k_kommt_blockweise_und_vollstaendig(self):
        pcm = _ton(0.5)
        _Speaches.antwort = _wav_strom(pcm, 48000, mit_list=True)
        bloecke = list(self.tts.synth_strom("Hallo."))
        self.assertGreater(len(bloecke), 1)
        self.assertEqual(b"".join(bloecke), zu_48k_stereo(pcm, 48000, 1))
        self.assertEqual(_Speaches.anfragen[0]["sample_rate"], 48000)
        self.assertTrue(self.tts.state.tts_available)

    def test_andere_rate_wird_am_stueck_umgerechnet(self):
        pcm = _ton(0.5, 22050)
        _Speaches.antwort = _wav_strom(pcm, 22050)
        bloecke = list(self.tts.synth_strom("Hallo."))
        self.assertEqual(len(bloecke), 1)
        self.assertEqual(bloecke[0], zu_48k_stereo(pcm, 22050, 1))

    def test_kaputte_antwort_wirft_und_markiert(self):
        _Speaches.antwort = b"kein wav"
        with self.assertRaises(ValueError):
            list(self.tts.synth_strom("Hallo."))
        self.assertFalse(self.tts.state.tts_available)


# --- Antwort-Sitzung ------------------------------------------------------------

class FakeLeds:
    def set_phase(self, *_):
        pass


class FakeTts:
    def __init__(self, fehler: bool = False, model: str = "piper", voice: str = "thorsten") -> None:
        self.state = SpeachesState()
        self.fehler = fehler
        self.model = model
        self.voice = voice
        self.saetze: list[str] = []

    def synth_strom(self, text):
        self.saetze.append(text)
        if self.fehler:
            raise RuntimeError("weg")
        yield zu_48k_stereo(_ton(0.05), 48000, 1)
        yield zu_48k_stereo(_ton(0.05), 48000, 1)


class FakeSenke:
    """Nimmt Stroeme an, liest sie wie der Player und meldet das Ende."""

    def __init__(self, geht: bool = True) -> None:
        self.geht = geht
        self.stroeme: list[tuple[WiedergabeStrom, Ausgang]] = []
        self.dateien: list[str] = []

    def strom_oeffnen(self, bei_ende):
        if not self.geht:
            return None
        s = WiedergabeStrom(leerlauf_s=0.3)
        aus = Ausgang()

        def lauf():
            s.ausliefern(aus)
            bei_ende()

        threading.Thread(target=lauf, daemon=True).start()
        self.stroeme.append((s, aus))
        return s

    def play_wav(self, path):
        self.dateien.append(path)


class SitzungTest(unittest.TestCase):
    def _sitzung(self, senke, tts_=None):
        sp = tts.ReplySpeaker(tts_ or FakeTts(), senke.play_wav, FakeLeds(),
                              strom_oeffnen=senke.strom_oeffnen if senke.geht else None)
        return sp.stream_session(restore_leds=False, turn=None)

    def test_saetze_laufen_in_einen_strom_und_lock_bis_zum_ende(self):
        senke = FakeSenke()
        sitz = self._sitzung(senke)
        sitz.feed("Erster Satz.")
        self.assertTrue(tts_lock.locked(), "der Strom haelt den Lock ueber die Saetze")
        sitz.feed("Zweiter Satz.")
        self.assertTrue(sitz.end())
        self.assertFalse(tts_lock.locked())
        self.assertEqual(len(senke.stroeme), 1)
        self.assertEqual(senke.stroeme[0][0].audio_bytes, 4 * len(zu_48k_stereo(_ton(0.05), 48000, 1)))

    def test_nach_leerlauf_neuer_strom(self):
        senke = FakeSenke()
        sitz = self._sitzung(senke)
        sitz.feed("Erster Satz.")
        senke.stroeme[0][0].fertig.wait(3)   # Brain denkt laenger als der Leerlauf
        sitz.feed("Viel spaeter.")
        sitz.end()
        self.assertEqual(len(senke.stroeme), 2)
        self.assertFalse(tts_lock.locked())

    def test_ohne_strom_dateien_wie_bisher(self):
        senke = FakeSenke(geht=False)
        sitz = self._sitzung(senke)
        orig = tts.ReplySpeaker._play_speaches_sentence
        tts.ReplySpeaker._play_speaches_sentence = lambda self, s, recorder=None: True
        try:
            sitz.feed("Satz.")
        finally:
            tts.ReplySpeaker._play_speaches_sentence = orig
        sitz.end()
        self.assertFalse(tts_lock.locked())

    def test_speaches_weg_piper_in_den_strom(self):
        senke = FakeSenke()
        sitz = self._sitzung(senke, FakeTts(fehler=True))
        pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_piper_ersatz.wav")
        import wave
        with wave.open(pfad, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
            wf.writeframes(_ton(0.2, 22050))
        orig = tts.piper_synth
        tts.piper_synth = lambda text, model=None: pfad
        try:
            sitz.feed("Satz.")
            sitz.end()
        finally:
            tts.piper_synth = orig
            if os.path.exists(pfad):
                os.unlink(pfad)
        self.assertEqual(senke.stroeme[0][0].audio_bytes, len(zu_48k_stereo(_ton(0.2, 22050), 22050, 1)))
        self.assertFalse(tts_lock.locked())


class BestaetigungAmStueckTest(unittest.TestCase):
    """Am Handy (Datei-Weg) lief die Bestaetigung im Wechsel mit der Antwort,
    weil jeder Satz tts_lock freigab; ihr letzter Satz kam nach dem Turn-Ende
    (2026-11-15). speak() haelt den Lock jetzt ueber alle Saetze."""

    def test_bestaetigung_wird_nicht_von_der_antwort_unterbrochen(self):
        senke = FakeSenke(geht=False)
        # Satellit: strom_oeffnen gibt es, liefert aber keinen Strom.
        sp = tts.ReplySpeaker(FakeTts(), senke.play_wav, FakeLeds(),
                              strom_oeffnen=lambda bei_ende: None,
                              antwort_tts=FakeTts(model="cosyvoice3"))
        gespielt: list[str] = []

        def spielen(self_, satz, recorder=None):
            gespielt.append(satz)
            time.sleep(0.03)
            return True

        orig = tts.ReplySpeaker._play_speaches_sentence
        tts.ReplySpeaker._play_speaches_sentence = spielen
        try:
            a = threading.Thread(target=sp.speak, args=("Eins. Zwei. Drei.",),
                                 kwargs={"restore_leds": False})
            a.start()
            time.sleep(0.01)
            sitz = sp.stream_session(restore_leds=True, turn=None)
            sitz.feed("Antwort A.")
            sitz.feed("Antwort B.")
            sitz.end()
            a.join(3)
        finally:
            tts.ReplySpeaker._play_speaches_sentence = orig
        bestaetigung = [i for i, x in enumerate(gespielt) if not x.startswith("Antwort")]
        self.assertEqual(bestaetigung, [0, 1, 2], gespielt)
        self.assertFalse(tts_lock.locked())


class AntwortDienstTest(unittest.TestCase):
    """antwort_tts (CosyVoice im Fablab) zuerst, Rueckfall speaches."""

    PIPER = "speaches-ai/piper-de_DE-thorsten-medium"

    def setUp(self):
        from voice_assistant.state import voice_state
        self.voice_state = voice_state
        self.alt = voice_state.get()
        voice_state.set(model="piper", voice="thorsten", speed=1.0)

    def tearDown(self):
        m, v, sp = self.alt
        self.voice_state.set(model=m, voice=v, speed=sp)

    def _lauf(self, cosy, lokal):
        senke = FakeSenke()
        sp = tts.ReplySpeaker(lokal, senke.play_wav, FakeLeds(),
                              strom_oeffnen=senke.strom_oeffnen, antwort_tts=cosy)
        sitz = sp.stream_session(restore_leds=False, turn=None)
        sitz.feed("Satz.")
        sitz.end()
        return senke

    def test_antwort_dienst_zuerst(self):
        cosy, lokal = FakeTts(model="cosyvoice3"), FakeTts()
        self._lauf(cosy, lokal)
        self.assertEqual((len(cosy.saetze), len(lokal.saetze)), (1, 0))

    def test_antwort_dienst_weg_dann_speaches(self):
        cosy, lokal = FakeTts(fehler=True, model="cosyvoice3"), FakeTts()
        senke = self._lauf(cosy, lokal)
        self.assertEqual((len(cosy.saetze), len(lokal.saetze)), (1, 1))
        self.assertGreater(senke.stroeme[0][0].audio_bytes, 0)

    def test_eigene_stimmwahl_ohne_antwort_dienst(self):
        self.voice_state.set(model="speaches-ai/piper-de_DE-eva_k-x_low", voice="eva", speed=1.0)
        cosy, lokal = FakeTts(model="cosyvoice3"), FakeTts(model=self.PIPER, voice="de_DE-thorsten-medium")
        self._lauf(cosy, lokal)
        self.assertEqual((len(cosy.saetze), len(lokal.saetze)), (0, 1))

    def test_gespeicherte_wahl_gleiches_piper_modell(self):
        # Live 2026-11-08: Regel=gespeichert setzt voice='thorsten', das Profil
        # sagt 'de_DE-thorsten-medium' -- dieselbe Stimme, CosyVoice muss bleiben.
        self.voice_state.set(model=self.PIPER, voice="thorsten", speed=1.0)
        cosy, lokal = FakeTts(model="cosyvoice3"), FakeTts(model=self.PIPER, voice="de_DE-thorsten-medium")
        self._lauf(cosy, lokal)
        self.assertEqual((len(cosy.saetze), len(lokal.saetze)), (1, 0))

    def test_andere_stimme_im_mehrstimmen_modell(self):
        self.voice_state.set(model="kokoro", voice="af_bella", speed=1.0)
        cosy, lokal = FakeTts(model="cosyvoice3"), FakeTts(model="kokoro", voice="af_sky")
        self._lauf(cosy, lokal)
        self.assertEqual((len(cosy.saetze), len(lokal.saetze)), (0, 1))

    def test_fest_ignoriert_voice_state(self):
        self.voice_state.set(model="piper", voice="eva", speed=1.0)
        d = tts.SpeachesTts(SpeachesState(), "http://x", "cosyvoice3", "thorsten", fest=True)
        self.assertEqual(d._parameter("Hallo.")[:2], ("cosyvoice3", "thorsten"))


# --- ReSpeaker-Senke mit echtem HTTP ------------------------------------------

class FakeClient:
    def __init__(self) -> None:
        self._loop = object()
        self._api = object()
        self.empfangen = bytearray()
        self.gestoppt = False
        self.abholen = True

    def play_url(self, url: str) -> bool:
        if self.abholen:
            def lesen():
                with urllib.request.urlopen(url, timeout=10) as r:
                    while True:
                        b = r.read(4096)
                        if not b:
                            break
                        self.empfangen += b
            threading.Thread(target=lesen, daemon=True).start()
        return True

    def wait_player(self, busy: bool, timeout: float) -> bool:
        return True

    def in_session(self) -> bool:
        return True

    def stop_playback(self) -> None:
        self.gestoppt = True


class SenkeTest(unittest.TestCase):
    def setUp(self):
        sink = RespeakerSink.__new__(RespeakerSink)
        sink._cfg = RespeakerAudio()
        sink._client = FakeClient()
        sink._serve_dir = "/nonexistent"
        sink._pi_ip = "127.0.0.1"
        sink._lock = threading.Lock()
        sink._stopped = False
        sink._folge = 0
        sink._stroeme = {}
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                   _make_http_handler(sink._serve_dir, sink._stroeme))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        sink._cfg.http_port = self.srv.server_port
        self.sink = sink

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_player_bekommt_kopf_und_audio(self):
        ende = threading.Event()
        s = self.sink.strom_oeffnen(ende.set)
        audio = zu_48k_stereo(_ton(0.1), 48000, 1)
        s.beginnen(); s.schreiben(audio); s.beenden(); s.schliessen()
        self.assertTrue(ende.wait(5))
        self.assertTrue(bytes(self.sink._client.empfangen).startswith(wav_kopf() + audio))
        self.assertEqual(self.sink._stroeme, {})

    def test_nicht_abgeholt_endet_trotzdem(self):
        self.sink._client.abholen = False
        self.sink._STROM_ABHOLEN_S = 0.3
        ende = threading.Event()
        s = self.sink.strom_oeffnen(ende.set)
        self.assertTrue(ende.wait(3))
        self.assertTrue(s.abgebrochen)

    def test_stop_bricht_strom_ab(self):
        ende = threading.Event()
        s = self.sink.strom_oeffnen(ende.set)
        s.beginnen(); s.schreiben(zu_48k_stereo(_ton(0.1), 48000, 1))
        self.sink.stop()
        self.assertTrue(ende.wait(3))
        self.assertTrue(s.abgebrochen)
        self.assertTrue(self.sink._client.gestoppt)
        self.assertIsNone(self.sink.strom_oeffnen(lambda: None), "nach Abbruch kein neuer Strom")


if __name__ == "__main__":
    unittest.main()
