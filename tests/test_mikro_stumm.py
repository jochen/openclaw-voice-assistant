"""Mikro stumm schalten (services/mikro_stumm.py), ohne Gerät und ohne Netz."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from voice_assistant.services import mikro_stumm
from voice_assistant.services.mikro_stumm import MikroSchalter


class _Client:
    """Wie RespeakerClient: mute_setzen schaltet, mute ist der gemeldete Zustand."""

    def __init__(self, mute=False, verbunden=True):
        self.mute = mute
        self.verbunden = verbunden
        self.befehle = []

    def mute_setzen(self, stumm):
        if not self.verbunden:
            return False
        self.befehle.append(stumm)
        self.mute = stumm
        return True


class MikroSchalterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.zustand = os.path.join(self.dir, "mikro.json")
        self.log = os.path.join(self.dir, "mikro.log")
        self.ansagen = []
        # Der Hintergrund-Thread soll in den Tests nichts tun.
        p = mock.patch.object(MikroSchalter, "_lauf", lambda self: None)
        p.start()
        self.addCleanup(p.stop)

    def _schalter(self, client):
        return MikroSchalter(client, 120, self.zustand, self.log, ansagen=self.ansagen.append)

    def test_stumm_mit_standard_zeitgrenze(self):
        c = _Client()
        s = self._schalter(c)
        erg = s.stumm(quelle="sprache")
        self.assertTrue(erg.ok)
        self.assertEqual(c.befehle, [True])
        self.assertAlmostEqual(erg.bis, time.time() + 7200, delta=5)
        self.assertIn("Uhr", erg.text)
        with open(self.zustand) as fh:
            self.assertTrue(json.load(fh)["stumm_von_uns"])

    def test_unbegrenzt(self):
        erg = self._schalter(_Client()).stumm(unbegrenzt=True)
        self.assertIsNone(erg.bis)
        self.assertIn("bis mich jemand", erg.text)

    def test_zeitgrenze_laeuft_ab_und_sagt_es(self):
        c = _Client()
        s = self._schalter(c)
        s.stumm(sekunden=60)
        s._takt()                       # Abgleich
        s._bis = time.time() - 1
        s._takt()
        self.assertEqual(c.befehle, [True, False])
        self.assertEqual(self.ansagen, ["Ich höre wieder zu."])

    def test_neustart_mitten_im_mute_haelt_die_zeitgrenze(self):
        c = _Client()
        self._schalter(c).stumm(sekunden=60)
        neu = self._schalter(c)         # Dienst neu gestartet, Gerät noch stumm
        neu._takt()
        self.assertEqual(c.befehle, [True])
        neu._bis = time.time() - 1      # ... und während des Neustarts abgelaufen
        neu._takt()
        self.assertEqual(c.befehle, [True, False])

    def test_knopf_am_geraet_beendet_die_zeitgrenze(self):
        c = _Client()
        s = self._schalter(c)
        s.stumm(sekunden=60)
        s._takt()
        s._eigen_ts -= 10               # eigener Befehl ist lange her
        c.mute = False                  # jemand drückt den Knopf
        s._takt()
        self.assertIsNone(s._bis)
        self.assertEqual(c.befehle, [True])

    def test_knopf_stumm_bekommt_keine_zeitgrenze(self):
        c = _Client(mute=True)
        s = self._schalter(c)
        s._takt()
        self.assertIsNone(s._bis)
        s._takt()
        self.assertEqual(c.befehle, [])

    def test_nicht_verbunden(self):
        erg = self._schalter(_Client(verbunden=False)).stumm()
        self.assertFalse(erg.ok)
        self.assertIn("nicht schalten", erg.text)

    def test_mqtt_knopf_schaltet_um_und_sagt_an(self):
        c = _Client()
        s = self._schalter(c)
        s.nachricht({"action": "single"})
        s.nachricht({"action": "single"})
        self.assertEqual(c.befehle, [True, False])
        self.assertEqual(len(self.ansagen), 2)
        self.assertIsNone(s.nachricht({"battery": 90}))      # Statusmeldung

    def test_mqtt_gezielt_und_still(self):
        c = _Client()
        s = self._schalter(c)
        erg = s.nachricht({"stumm": True, "minuten": 1, "ansage": False})
        self.assertAlmostEqual(erg.bis, time.time() + 60, delta=5)
        self.assertEqual(self.ansagen, [])

    def test_http_spricht_nur_auf_wunsch(self):
        c = _Client()
        s = self._schalter(c)
        s.nachricht({"stumm": True}, quelle="brain", ansage=False)
        self.assertEqual(self.ansagen, [])
        with open(self.log) as fh:
            self.assertEqual(json.loads(fh.readline())["quelle"], "brain")

    def test_uhrzeit(self):
        with mock.patch.object(mikro_stumm, "datetime") as dt:
            dt.fromtimestamp.return_value = mock.Mock(hour=22, minute=0)
            self.assertEqual(mikro_stumm._uhrzeit(0), "22 Uhr")
            dt.fromtimestamp.return_value = mock.Mock(hour=7, minute=5)
            self.assertEqual(mikro_stumm._uhrzeit(0), "7 Uhr 5")


if __name__ == "__main__":
    unittest.main()
