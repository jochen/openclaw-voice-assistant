"""Schatten-Aufnahme nach Near-Miss — ohne Netz, ohne Speaches, ohne Gemma.

Festgehalten wird, was die Messung sauber hält:
  (1) Mitschnitt = Pre-Roll + TAIL_SEC, Datei neben dem Near-Miss-Clip,
  (2) jede Zeile ist als "vorlaeufig" markiert,
  (3) die Auswertung wartet, solange ein echter Turn laeuft,
  (4) sie fasst den Cooldown-Zustand von Speaches nie an — ein Ausfall hier
      darf den naechsten echten Turn nicht auf die lokale Whisper schicken,
  (5) ein Trigger waehrend des Mitschnitts schliesst mit dem Bisherigen ab.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import RATE_OW  # noqa: E402
from voice_assistant.nearmiss_shadow import NearMissShadow  # noqa: E402


class FakeState:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def stt_ok(self) -> bool:
        return self.ok

    def mark_stt_failed(self) -> None:
        raise AssertionError("Schatten-STT darf den Cooldown nicht setzen")

    def mark_stt_ok(self) -> None:
        raise AssertionError("Schatten-STT darf den Zustand nicht anfassen")


class FakeStt:
    def __init__(self, text="Gaston mach das Licht an", fehler=None, ok=True) -> None:
        self.state = FakeState(ok)
        self.text, self.fehler, self.aufrufe = text, fehler, 0

    def transcribe_raw(self, wav_bytes: bytes) -> dict:
        self.aufrufe += 1
        if self.fehler:
            raise self.fehler
        return {"text": self.text, "segments": [{"no_speech_prob": 0.1}]}


class FakeUrteil:
    def __init__(self) -> None:
        self.ja, self.p_ja, self.ms, self.fehler = True, 0.9, 400.0, None


class FakeActuator:
    def __init__(self) -> None:
        self.saetze = []

    def tor(self, text: str):
        self.saetze.append(text)
        return FakeUrteil()


def _chunk(n: int = 640) -> np.ndarray:
    return np.full(n, 1000, dtype=np.int16)


EVENT = {"result": "nearmiss", "bundle": "gaston", "hits": 1, "peak": 0.8,
         "failed_on": "min_hits", "audio": "20260925_210000_gaston_nearmiss.wav"}


class ShadowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "shadow.jsonl")
        self.turn = [False]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _shadow(self, stt=None, actuator=None, tail=0.2):
        return NearMissShadow(
            stt=stt if stt is not None else FakeStt(), actuator=actuator,
            archive_dir=self.tmp.name, log_path=self.log,
            turn_laeuft=lambda: self.turn[0], tail_sec=tail,
        )

    def _zeilen(self, n: int = 1, timeout: float = 3.0) -> list:
        ende = time.time() + timeout
        while time.time() < ende:
            if os.path.exists(self.log):
                with open(self.log, encoding="utf-8") as f:
                    z = [json.loads(line) for line in f if line.strip()]
                if len(z) >= n:
                    return z
            time.sleep(0.02)
        self.fail(f"keine {n} Zeile(n) im Log")

    def _laufen(self, sh, sekunden: float) -> None:
        for _ in range(int(RATE_OW * sekunden / 640) + 1):
            sh.feed(_chunk())

    def test_mitschnitt_und_vorlaeufige_zeile(self) -> None:
        akt = FakeActuator()
        sh = self._shadow(actuator=akt)
        sh.start(EVENT, [_chunk(), _chunk()])
        self._laufen(sh, 0.3)
        self.assertFalse(sh.aktiv)
        z = self._zeilen()[0]
        self.assertEqual(z["status"], "vorlaeufig")
        self.assertEqual(z["folge_audio"], "20260925_210000_gaston_nearmiss_folge.wav")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, z["folge_audio"])))
        self.assertEqual(z["ende"], "voll")
        self.assertEqual(z["stt_text"], "Gaston mach das Licht an")
        self.assertIs(z["tor_ja"], True)
        self.assertEqual(akt.saetze, ["Gaston mach das Licht an"])

    def test_wartet_auf_laufenden_turn(self) -> None:
        stt = FakeStt()
        sh = self._shadow(stt=stt)
        self.turn[0] = True
        sh.start(EVENT, [_chunk()])
        self._laufen(sh, 0.3)
        time.sleep(0.8)
        self.assertEqual(stt.aufrufe, 0, "STT darf nicht neben einem echten Turn laufen")
        self.turn[0] = False
        z = self._zeilen()[0]
        self.assertEqual(stt.aufrufe, 1)
        self.assertGreater(z["gewartet_sec"], 0.4)

    def test_stt_fehler_fasst_den_zustand_nicht_an(self) -> None:
        # FakeState wirft bei jedem mark_* — ein Aufruf wuerde hier auffallen.
        sh = self._shadow(stt=FakeStt(fehler=OSError("HTTP 500")))
        sh.start(EVENT, [_chunk()])
        self._laufen(sh, 0.3)
        z = self._zeilen()[0]
        self.assertIn("HTTP 500", z["fehler"])
        self.assertNotIn("AssertionError", z["fehler"])

    def test_cooldown_wird_respektiert(self) -> None:
        stt = FakeStt(ok=False)
        sh = self._shadow(stt=stt)
        sh.start(EVENT, [_chunk()])
        self._laufen(sh, 0.3)
        z = self._zeilen()[0]
        self.assertEqual(stt.aufrufe, 0)
        self.assertEqual(z["fehler"], "Speaches im Cooldown")

    def test_trigger_schliesst_mit_dem_bisherigen_ab(self) -> None:
        sh = self._shadow(tail=5.0)
        sh.start(EVENT, [_chunk()])
        self._laufen(sh, 0.2)
        sh.abbrechen("Trigger")
        z = self._zeilen()[0]
        self.assertEqual(z["ende"], "Trigger")
        self.assertLess(z["folge_sec"], 1.0)


if __name__ == "__main__":
    unittest.main()
