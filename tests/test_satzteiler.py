"""Satzteiler (services/tts.split_into_sentences): Abkuerzungen sind kein Satzende.

Am Handy ist jeder Satz eine eigene Datei mit Luecke dazwischen; „Mr. President“
kam am 2026-11-15 als „…von Mr.“ + „President.“.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services.tts import split_into_sentences  # noqa: E402


class SatzteilerTest(unittest.TestCase):
    def test_englischer_titel_im_namen(self):
        self.assertEqual(
            split_into_sentences("Läuft, Up'n Away von Mr. President. Kleiner Hinweis: so."),
            ["Läuft, Up'n Away von Mr. President.", "Kleiner Hinweis: so."])

    def test_weitere_titel(self):
        for text in ("Mrs. Robinson läuft.", "Dr. Alban läuft.", "St. Elmo läuft."):
            self.assertEqual(split_into_sentences(text), [text])

    def test_normale_saetze_bleiben_getrennt(self):
        self.assertEqual(split_into_sentences("Okay. Läuft."), ["Okay.", "Läuft."])

    def test_deutsche_abkuerzung_wie_bisher(self):
        self.assertEqual(split_into_sentences("Zum Beispiel z. B. Licht. Fertig."),
                         ["Zum Beispiel z. B. Licht.", "Fertig."])


if __name__ == "__main__":
    unittest.main()
