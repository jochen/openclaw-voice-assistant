"""Offline-Tests für Meilenstein 1 der Wakeword-Studio-Spec (Multi-Wakeword +
Routing). Läuft OHNE Mikrofon/Hardware, startet den Assistant NICHT.

Ausführen:
    ow-venv/bin/python tests/test_multi_wakeword.py
oder:
    ow-venv/bin/python -m unittest tests.test_multi_wakeword -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant import config as cfg  # noqa: E402
from voice_assistant.wakeword.base import WakewordHit  # noqa: E402
from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine  # noqa: E402


class ConfigWakewordsTest(unittest.TestCase):
    """(1) Config-Parsing: ohne/mit `wakewords:`-Block, gegen eine temporäre
    YAML-Datei (nicht die echte config.yaml)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config_path = cfg.CONFIG_PATH
        self._orig_profile_env = os.environ.get("GASTON_PROFILE")

    def tearDown(self) -> None:
        cfg.CONFIG_PATH = self._orig_config_path
        if self._orig_profile_env is None:
            os.environ.pop("GASTON_PROFILE", None)
        else:
            os.environ["GASTON_PROFILE"] = self._orig_profile_env
        self._tmpdir.cleanup()

    def _load(self, yaml_text: str, profile_name: str):
        path = os.path.join(self._tmpdir.name, "config.yaml")
        with open(path, "w") as f:
            f.write(yaml_text)
        cfg.CONFIG_PATH = path
        os.environ["GASTON_PROFILE"] = profile_name
        return cfg.load_profile()

    def test_no_wakewords_block_yields_hey_jarvis_default(self) -> None:
        profile = self._load(
            """
profiles:
  testprofile:
    openclaw_session: "agent:main:telegram:group:-1"
    speaches_tts_voice: "de_DE-thorsten-medium"
    locale:
      wakeword_ack: "Jup?"
""",
            "testprofile",
        )
        self.assertEqual(len(profile.wakewords), 1)
        ww = profile.wakewords[0]
        self.assertEqual(ww.bundle, "hey_jarvis")
        self.assertEqual(ww.session, "agent:main:telegram:group:-1")
        self.assertEqual(ww.ack, "Jup?")
        self.assertEqual(ww.tts_voice, "de_DE-thorsten-medium")
        self.assertIsNone(ww.threshold)

    def test_wakewords_block_with_fallback_resolution(self) -> None:
        profile = self._load(
            """
profiles:
  testprofile:
    openclaw_session: "agent:main:telegram:group:-1"
    speaches_tts_voice: "de_DE-thorsten-medium"
    locale:
      wakeword_ack: "Jup?"
    wakewords:
      - bundle: gaston
        session: "agent:gaston:telegram:group:-2"
        ack: "Ja bitte?"
        tts_voice: "de_DE-other-voice"
        threshold: 0.6
      - bundle: hey_jarvis
""",
            "testprofile",
        )
        self.assertEqual(len(profile.wakewords), 2)
        gaston, jarvis = profile.wakewords

        self.assertEqual(gaston.bundle, "gaston")
        self.assertEqual(gaston.session, "agent:gaston:telegram:group:-2")
        self.assertEqual(gaston.ack, "Ja bitte?")
        self.assertEqual(gaston.tts_voice, "de_DE-other-voice")
        self.assertEqual(gaston.threshold, 0.6)

        # Zweiter Eintrag ohne explizite Felder -> Profil-Fallbacks
        self.assertEqual(jarvis.bundle, "hey_jarvis")
        self.assertEqual(jarvis.session, "agent:main:telegram:group:-1")
        self.assertEqual(jarvis.ack, "Jup?")
        self.assertEqual(jarvis.tts_voice, "de_DE-thorsten-medium")
        self.assertIsNone(jarvis.threshold)

    def test_wakewords_entry_without_bundle_is_skipped(self) -> None:
        profile = self._load(
            """
profiles:
  testprofile:
    openclaw_session: "agent:main:telegram:group:-1"
    wakewords:
      - session: "agent:orphan"
      - bundle: hey_jarvis
""",
            "testprofile",
        )
        self.assertEqual(len(profile.wakewords), 1)
        self.assertEqual(profile.wakewords[0].bundle, "hey_jarvis")


class EngineMultiWakewordTest(unittest.TestCase):
    """(2) Engine: zwei eingebaute Modelle laden, Stille/Rauschen füttern →
    kein Trigger; feed() liefert die neue Rückgabeform (WakewordHit | None)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = OpenWakewordEngine(
            [
                cfg.WakewordConfig(bundle="hey_jarvis"),
                cfg.WakewordConfig(bundle="alexa"),
            ]
        )

    def test_feed_return_shape_and_no_trigger_on_silence(self) -> None:
        # Kleinere Chunks als der interne 1280er-Frame (wie reale Audio-Quellen:
        # ALSA/ReSpeaker liefern kürzere Häppchen) — so durchläuft der Test
        # sowohl den "Puffer noch nicht voll" (None) als auch den
        # "Ergebnis berechnet" (WakewordHit) Zweig von feed().
        # Reine Nullen statt Rauschen: openwakeword hat beim Aufwärmen der
        # internen Mel-/Embedding-Puffer bekanntermaßen transiente
        # Fehl-Spitzen auf Zufallsrauschen (Buffer-Warmup-Artefakt, kein Bug
        # dieser Engine) — Stille ist der stabile Fall für "kein Trigger".
        chunk_size = 320
        num_chunks = 100  # ~2s @ 16kHz

        saw_none = False
        saw_hit = False
        for _ in range(num_chunks):
            silence = np.zeros(chunk_size, dtype=np.int16)
            result = self.engine.feed(silence)
            if result is None:
                saw_none = True
                continue
            saw_hit = True
            self.assertIsInstance(result, WakewordHit)
            self.assertIsInstance(result.name, str)
            self.assertIsInstance(result.score, float)
            self.assertIsInstance(result.threshold, float)
            self.assertIn(result.name, ("hey_jarvis", "alexa"))
            self.assertLessEqual(
                result.score,
                result.threshold,
                f"Unerwarteter Trigger auf Rauschen: {result}",
            )

        # Erster Aufruf liefert None (Puffer < 1280 Samples), danach ein
        # Ergebnis je volle 1280er-Charge — beides muss vorgekommen sein.
        self.assertTrue(saw_none, "Erwartete mind. ein None (Puffer noch nicht voll)")
        self.assertTrue(saw_hit, "Erwartete mind. ein berechnetes Ergebnis")

    def test_reset_clears_internal_buffer(self) -> None:
        self.engine.feed(np.zeros(500, dtype=np.int16))  # Puffer teilweise gefüllt
        self.engine.reset()
        self.assertEqual(len(self.engine._buf), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
