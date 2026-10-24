"""Uhrzeiten in der TTS-Aufbereitung (clean_for_tts).

Anlass 2026-10-24: "Es ist gerade 11:55." wurde als "elf:fünfundfünfzig"
gesprochen, weil Uhrzeiten nur mit nachfolgendem "Uhr" erkannt wurden.
"""
import unittest

from voice_assistant.services import tts


@unittest.skipUnless(tts._HAS_NUM2WORDS, "num2words fehlt")
class UhrzeitTest(unittest.TestCase):
    def test_doppelpunkt_ohne_uhr(self):
        self.assertEqual(tts.clean_for_tts("Es ist gerade 11:55."),
                         "Es ist gerade elf Uhr fünfundfünfzig.")

    def test_volle_stunde_ohne_uhr(self):
        self.assertEqual(tts.clean_for_tts("Um 9:00 geht es los"),
                         "Um neun Uhr geht es los")

    def test_mit_uhr_unveraendert(self):
        self.assertEqual(tts.clean_for_tts("Es ist 14:30 Uhr."),
                         "Es ist vierzehn Uhr dreißig.")

    def test_keine_uhrzeit(self):
        # Spielstand, Seitenverhältnis, unmögliche Uhrzeit: keine "Uhr"
        for text in ("Das Spiel endete 2:1", "Format 16:9", "Stand 25:30"):
            self.assertNotIn("Uhr", tts.clean_for_tts(text), text)


if __name__ == "__main__":
    unittest.main()
