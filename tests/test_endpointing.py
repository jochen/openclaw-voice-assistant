"""Offline-Tests fuer das Kommando-Endpointing (Ein-Satz-Turns).

Laeuft OHNE Mikrofon/Hardware, startet den Assistant NICHT.

Ausfuehren:
    ow-venv/bin/python tests/test_endpointing.py

Der Zuschnitt der Parameter (1,0 s Nachlauf / 8 s Deckel / 0,5 s Sperre) ist
NICHT hier begruendet, sondern an echten Aufnahmen gemessen — siehe
tools/endpoint_replay.py. Diese Tests sichern nur die Mechanik dahinter:
dass die Werte ueberhaupt ankommen und dass die Chunk-Kennzahlen stimmen,
auf denen die Sperre aufsetzt.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webrtcvad  # noqa: E402

from voice_assistant.assistant import (  # noqa: E402
    _COMMAND_MIN_SPEECH_SEC,
    _chunk_speech_stats,
    _signal_stats,
)
import voice_assistant.config as cfg  # noqa: E402
from voice_assistant.config import CHUNK_SIZE, load_profile  # noqa: E402


def _ton(n: int, amplitude: int, hz: float = 220.0) -> np.ndarray:
    t = np.arange(n) / 16000.0
    return (np.sin(2 * np.pi * hz * t) * amplitude).astype(np.int16)


class TestChunkSpeechStats(unittest.TestCase):
    def setUp(self) -> None:
        self.vad = webrtcvad.Vad(3)

    def test_stille_ist_keine_sprache(self) -> None:
        ist_sprache, rms, sf, tf = _chunk_speech_stats(
            self.vad, np.zeros(CHUNK_SIZE, dtype=np.int16)
        )
        self.assertFalse(ist_sprache)
        self.assertEqual(rms, 0.0)
        self.assertEqual(sf, 0)
        self.assertEqual(tf, 4)  # 80-ms-Chunk = 4 VAD-Frames a 20 ms

    def test_rms_wird_auch_ohne_gate_gemeldet(self) -> None:
        """Pegel und Frame-Zahl kommen IMMER heraus, nicht nur wenn gefiltert
        wird — genau dafuer sind sie da: sie sollen im endpoint.log landen,
        damit spaeter ueberhaupt entscheidbar ist, ob eine Pegelschwelle
        traegt."""
        _, rms, _, tf = _chunk_speech_stats(
            self.vad, _ton(CHUNK_SIZE, 3000), min_rms=0.0
        )
        self.assertGreater(rms, 1000)
        self.assertEqual(tf, 4)

    def test_pegelschwelle_verwirft_leise_chunks(self) -> None:
        leise = _ton(CHUNK_SIZE, 200)
        _, rms, _, _ = _chunk_speech_stats(self.vad, leise)
        ist_sprache, _, _, _ = _chunk_speech_stats(self.vad, leise, min_rms=rms + 100)
        self.assertFalse(ist_sprache)

    def test_teilchunk_zaehlt_nicht_mit(self) -> None:
        """Ein angebrochener 20-ms-Frame am Chunk-Ende wird verworfen — sonst
        wuerde die Frame-Quote je nach Quelle unterschiedlich ausfallen."""
        _, _, _, tf = _chunk_speech_stats(self.vad, np.zeros(CHUNK_SIZE + 100, dtype=np.int16))
        self.assertEqual(tf, 4)


class TestSignalStats(unittest.TestCase):
    def test_leere_aufnahme(self) -> None:
        self.assertEqual(_signal_stats([], 0, 0), {})

    def test_kennzahlen(self) -> None:
        s = _signal_stats([10.0] * 9 + [1000.0], 3, 12)
        self.assertEqual(s["rms_max"], 1000.0)
        self.assertEqual(s["rms_p10"], 10.0)
        self.assertEqual(s["vad_frame_ratio"], 0.25)

    def test_ohne_frames_keine_quote(self) -> None:
        self.assertIsNone(_signal_stats([5.0], 0, 0)["vad_frame_ratio"])


class TestKommandoProfil(unittest.TestCase):
    def test_defaults(self) -> None:
        p = load_profile()
        self.assertEqual(p.command_silence_seconds, 1.0)
        self.assertEqual(p.command_max_seconds, 8.0)
        # Der Kommando-Nachlauf muss unter dem Dialog-Nachlauf liegen, sonst
        # ist der ganze Modus wirkungslos.
        self.assertLess(p.command_silence_seconds, p.silence_seconds)

    def test_ueberschreibbar_per_yaml(self) -> None:
        orig_pfad, orig_env = cfg.CONFIG_PATH, os.environ.get("GASTON_PROFILE")
        tmp = tempfile.TemporaryDirectory()
        try:
            pfad = os.path.join(tmp.name, "config.yaml")
            with open(pfad, "w") as f:
                f.write(
                    "profiles:\n"
                    "  testprofile:\n"
                    "    mode: local\n"
                    "    command_silence_seconds: 0.7\n"
                    "    command_max_seconds: 6.0\n"
                )
            cfg.CONFIG_PATH = pfad
            os.environ["GASTON_PROFILE"] = "testprofile"
            p = cfg.load_profile()
            self.assertEqual(p.command_silence_seconds, 0.7)
            self.assertEqual(p.command_max_seconds, 6.0)
        finally:
            cfg.CONFIG_PATH = orig_pfad
            if orig_env is None:
                os.environ.pop("GASTON_PROFILE", None)
            else:
                os.environ["GASTON_PROFILE"] = orig_env
            tmp.cleanup()

    def test_sperre_liegt_ueber_dem_wakeword_ausklang(self) -> None:
        """0,5 s ist kein runder Wunschwert: der Ausklang des Wakewords nach
        dem Trigger misst in den Archivaufnahmen 1-2 Chunks (~0,16 s). Faellt
        die Sperre darunter, macht der Ausklang allein den Kommando-Modus
        scharf — genau der Fehler, der 20260730_181211 zerschnitten haette."""
        self.assertGreater(_COMMAND_MIN_SPEECH_SEC, 2 * CHUNK_SIZE / 16000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
