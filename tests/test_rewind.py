"""Rückspul-Puffer — ohne Netz, ohne Mikro, ohne Broker.

Festgehalten wird, was die spätere Auswertung trägt:
  (1) der Puffer hält nur die letzten `seconds`,
  (2) `seconds=` beim Sichern schneidet den Vorlauf richtig zu,
  (3) Scores stehen an ihrer Position IN DER WAV-DATEI — sonst zeigt der
      Verlauf beim Anhören auf die falsche Stelle,
  (4) ein stockender Mikro-Strom erscheint als Lücke (Mikro taub ≠ Modell taub),
  (5) nur ein echter Tastendruck ist ein Marker: retained-Zustand beim
      Verbinden und Status-Meldungen ohne "action" nicht.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import RATE_OW  # noqa: E402
from voice_assistant.rewind import RewindBuffer, auswerten  # noqa: E402

CHUNK = 640  # 40 ms wie ReSpeaker


def _chunk(wert: int = 1000) -> np.ndarray:
    return np.full(CHUNK, wert, dtype=np.int16)


class RewindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.index = os.path.join(self.tmp.name, "rueckspul.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _puffer(self, seconds: float = 2.0) -> RewindBuffer:
        return RewindBuffer(seconds, self.tmp.name, self.index)

    def _fuettern(self, rb, sekunden: float, t0: float = 1000.0, zustand="leerlauf"):
        n = int(sekunden * RATE_OW / CHUNK)
        for i in range(n):
            rb.feed(t0 + i * CHUNK / RATE_OW, zustand, _chunk())
            rb.score(0.01, "leerlauf")
        return t0 + n * CHUNK / RATE_OW

    def _warten(self, stamm: str) -> dict:
        pfad = os.path.join(self.tmp.name, f"{stamm}.json")
        ende = time.time() + 3
        while time.time() < ende:
            if os.path.exists(pfad) and os.path.exists(self.index):
                with open(pfad, encoding="utf-8") as f:
                    return json.load(f)
            time.sleep(0.02)
        self.fail(f"{stamm} nicht geschrieben")

    def test_puffer_haelt_nur_die_letzten_sekunden(self) -> None:
        rb = self._puffer(seconds=2.0)
        self._fuettern(rb, 5.0)
        rb.sichern("marker", "m1")
        meta = self._warten("m1")
        self.assertAlmostEqual(meta["dauer_sec"], 2.0, delta=0.05)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "m1.wav")))

    def test_vorlauf_schneidet_zu(self) -> None:
        rb = self._puffer(seconds=10.0)
        self._fuettern(rb, 8.0)
        rb.sichern("trigger", "t1", seconds=3.0, extra={"wake_audio": "x_wake.wav"})
        meta = self._warten("t1")
        self.assertAlmostEqual(meta["dauer_sec"], 3.0, delta=0.05)
        self.assertEqual(meta["anlass"], "trigger")
        self.assertEqual(meta["status"], "vorlaeufig")
        self.assertEqual(meta["wake_audio"], "x_wake.wav")

    def test_score_steht_an_seiner_wav_position(self) -> None:
        rb = self._puffer(seconds=10.0)
        t = self._fuettern(rb, 4.0)            # alte Scores fallen raus
        rb.feed(t, "leerlauf", _chunk())
        rb.score(0.93, "leerlauf")             # DER Ruf
        t += CHUNK / RATE_OW
        self._fuettern(rb, 1.0, t0=t)
        rb.sichern("marker", "m2", seconds=2.0)
        meta = self._warten("m2")
        ruf = [s for s in meta["scores"] if s[1] == 0.93]
        self.assertEqual(len(ruf), 1)
        # 2 s Datei, danach noch 1 s gefüttert → der Ruf endet bei ~1 s.
        self.assertAlmostEqual(ruf[0][0], 1.0, delta=0.05)
        self.assertEqual(meta["score_max"], 0.93)

    def test_zeitluecke_wird_sichtbar(self) -> None:
        rb = self._puffer(seconds=10.0)
        t = self._fuettern(rb, 1.0)
        self._fuettern(rb, 1.0, t0=t + 3.0)     # 3 s lang kam nichts
        rb.sichern("marker", "m3")
        meta = self._warten("m3")
        self.assertEqual(len(meta["luecken"]), 1)
        self.assertAlmostEqual(meta["luecken"][0][1], 3.0, delta=0.05)
        self.assertAlmostEqual(meta["luecken"][0][0], 1.0, delta=0.05)

    def test_zustaende_als_abschnitte(self) -> None:
        audio = [(i * CHUNK, 1000 + i * 0.04, z, _chunk())
                 for i, z in enumerate(["leerlauf"] * 3 + ["aufnahme"] * 2)]
        meta = auswerten(audio, [])
        self.assertEqual([z for _, z in meta["zustaende"]], ["leerlauf", "aufnahme"])

    def test_nur_ein_tastendruck_ist_ein_marker(self) -> None:
        rb = self._puffer()
        # Den on_message-Callback direkt prüfen, ohne Broker: marker_starten
        # mit einem Fake-paho aufrufen und den registrierten Callback greifen.
        gefangen = {}

        class FakeClient:
            def __init__(self, **kw): pass
            def reconnect_delay_set(self, **kw): pass
            def connect_async(self, *a, **kw): pass
            def loop_start(self): gefangen["cb"] = self.on_message

        fake = SimpleNamespace(Client=FakeClient,
                               CallbackAPIVersion=SimpleNamespace(VERSION2=2))
        sys.modules["paho"] = SimpleNamespace(mqtt=SimpleNamespace(client=fake))
        sys.modules["paho.mqtt"] = SimpleNamespace(client=fake)
        sys.modules["paho.mqtt.client"] = fake
        try:
            rb.marker_starten("h", 1883, "zigbee2mqtt/taster")
        finally:
            for k in ("paho", "paho.mqtt", "paho.mqtt.client"):
                sys.modules.pop(k, None)
        cb = gefangen["cb"]

        def msg(payload, retain=False):
            return SimpleNamespace(payload=json.dumps(payload).encode(),
                                   retain=retain, topic="zigbee2mqtt/taster")

        cb(None, None, msg({"action": "single"}, retain=True))    # alter Zustand
        cb(None, None, msg({"battery": 90, "linkquality": 120}))  # Statusmeldung
        cb(None, None, msg({"action": ""}))                        # z2m-Leerwert
        cb(None, None, SimpleNamespace(payload=b"kein json", retain=False, topic="x"))
        cb(None, None, msg({"action": "single", "battery": 90}))  # echter Druck
        marker = rb.marker_abholen()
        self.assertEqual(marker, [{"action": "single", "topic": "zigbee2mqtt/taster"}])
        self.assertEqual(rb.marker_abholen(), [])


if __name__ == "__main__":
    unittest.main()
