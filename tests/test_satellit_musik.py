"""Musik am Handy (audio/satellit_musik.py) — ohne Netz und ohne Handy.

Festgehalten: Gaston gibt Ava den Stream, wenn ihr Raum spielt, laesst ihn
bei Pause (Daempfen!) stehen und nimmt ihn erst nach einer Frist weg. Und ein
Abbruch am Satelliten trifft den Medien-Player nicht.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio import satellit_musik as sm  # noqa: E402
from voice_assistant.audio.satellit_musik import SatellitMusik  # noqa: E402

PLAYING = 1   # Hoerer am Stream
IDLE = 0
URL = "http://musik.example:8770/handy"


class FakeClient:
    def __init__(self):
        self.verbunden_ = True
        self.zustand = IDLE
        self.befehle: list[str] = []

    def verbunden(self):
        return self.verbunden_

    def musik_url(self, url):
        self.befehle.append(f"url {url}")
        return True

    def stop_playback(self):
        self.befehle.append("stop")


class Uhr:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SchrittTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.uhr = Uhr()
        self.raum = "play"
        self.m = SatellitMusik("jochen", self.client, "handy_jochen", URL,
                               lambda r: self.raum, hoerer=lambda: self.client.zustand,
                               uhr=self.uhr)

    def test_raum_spielt_ava_still_startet_stream(self):
        self.assertEqual(self.m.schritt(), "start")
        self.assertEqual(self.client.befehle, [f"url {URL}"])

    def test_nicht_nochmal_senden_waehrend_ava_puffert(self):
        self.m.schritt()
        self.uhr.t += 4
        self.assertIsNone(self.m.schritt())
        self.uhr.t += sm._NEU_SENDEN_S
        self.assertEqual(self.m.schritt(), "start")   # kam nie an: neuer Versuch

    def test_ava_spielt_schon(self):
        self.client.zustand = PLAYING
        self.assertIsNone(self.m.schritt())
        self.assertEqual(self.client.befehle, [])

    def test_pause_laesst_stream_stehen_bis_zur_frist(self):
        self.client.zustand = PLAYING
        self.raum = "pause"
        self.assertIsNone(self.m.schritt())
        self.uhr.t += sm._PAUSE_STOP_S - 1
        self.assertIsNone(self.m.schritt())
        self.uhr.t += 2
        self.assertEqual(self.m.schritt(), "stop")

    def test_stop_nimmt_stream_nach_frist_weg(self):
        self.client.zustand = PLAYING
        self.raum = "stop"
        self.assertIsNone(self.m.schritt())
        self.uhr.t += sm._STOP_NACH_S
        self.assertEqual(self.m.schritt(), "stop")
        self.assertEqual(self.client.befehle, ["stop"])

    def test_kurzer_stop_zwischen_titeln_bleibt(self):
        self.client.zustand = PLAYING
        self.raum = "stop"
        self.m.schritt()
        self.uhr.t += 5
        self.raum = "play"
        self.m.schritt()
        self.raum = "stop"
        self.uhr.t += sm._STOP_NACH_S - 1
        self.assertIsNone(self.m.schritt())   # Frist beginnt neu

    def test_pause_dann_stop_zaehlt_ab_stop(self):
        self.client.zustand = PLAYING
        self.raum = "pause"
        self.m.schritt()
        self.uhr.t += 100
        self.raum = "stop"
        self.assertIsNone(self.m.schritt())
        self.uhr.t += sm._STOP_NACH_S
        self.assertEqual(self.m.schritt(), "stop")

    def test_bridge_weg_aendert_nichts(self):
        self.client.zustand = PLAYING
        self.raum = None
        self.uhr.t += 10_000
        self.assertIsNone(self.m.schritt())
        self.assertEqual(self.client.befehle, [])

    def test_stream_server_weg_aendert_nichts(self):
        self.client.zustand = None
        self.assertIsNone(self.m.schritt())
        self.raum = "stop"
        self.uhr.t += 10_000
        self.assertIsNone(self.m.schritt())
        self.assertEqual(self.client.befehle, [])

    def test_nach_neuverbindung_sofort_wieder(self):
        self.m.schritt()
        self.client.verbunden_ = False
        self.assertIsNone(self.m.schritt())
        self.client.verbunden_ = True
        self.uhr.t += 1
        self.assertEqual(self.m.schritt(), "start")


class SatellitStopTest(unittest.TestCase):
    """Ein Barge-in stoppt alle Senken — am Satelliten nie den Medien-Player."""

    def _senke(self, satellit):
        from voice_assistant.audio.respeaker import RespeakerSink
        senke = RespeakerSink.__new__(RespeakerSink)
        import threading

        class Cfg:
            pass
        senke._cfg = Cfg()
        senke._cfg.satellit = satellit
        senke._lock = threading.Lock()
        senke._stroeme = {}
        senke._stopped = False
        self.client = FakeClient()
        senke._client = self.client
        return senke

    def test_satellit_ohne_media_stop(self):
        self._senke(True).stop()
        self.assertEqual(self.client.befehle, [])

    def test_haupt_geraet_mit_media_stop(self):
        self._senke(False).stop()
        self.assertEqual(self.client.befehle, ["stop"])


if __name__ == "__main__":
    unittest.main()
