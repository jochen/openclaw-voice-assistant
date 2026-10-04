"""Transkript nur aus dem Wakewort → "Ja?" nachholen statt an den Brain.

Anlass 2026-10-04 13:47: "Gaston" gesagt, auf das "Ja?" gewartet — die
Aufnahme wurde vom Hintergrund in den Kommando-Modus gezogen, Transkript
"Gaston.", Ansage "Ich habe verstanden: Gaston.".
Ohne Netz, ohne Modelle: nur das Muster aus voice_assistant/assistant.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.assistant import _ist_nur_wakewort  # noqa: E402


class NurWakewortTest(unittest.TestCase):
    def test_echt(self):
        # endpoint.log 2026-10-03 10:45 und 2026-10-04 13:47
        self.assertTrue(_ist_nur_wakewort("Gaston.", "gaston"))

    def test_verhoerer(self):
        for satz in ("Gaston", "gaston!", "Gastón?", "Gastro.", "Gastau", " Gaston … "):
            with self.subTest(satz=satz):
                self.assertTrue(_ist_nur_wakewort(satz, "gaston"))

    def test_mehr_als_der_name(self):
        for satz in ("", "  ", ".", "Gastostop.",  # endpoint.log 2026-09-25
                     "Gaston, kannst du mir sagen, was heute auf der Essensliste steht?",
                     "Gaston Licht an", "Gastgeber", "Danke."):
            with self.subTest(satz=satz):
                self.assertFalse(_ist_nur_wakewort(satz, "gaston"))

    def test_anderes_bundle(self):
        self.assertTrue(_ist_nur_wakewort("Hey Jarvis.", "hey_jarvis"))
        self.assertFalse(_ist_nur_wakewort("Gaston.", "hey_jarvis"))
        self.assertFalse(_ist_nur_wakewort("Hey Jarvis, Licht an", "hey_jarvis"))


if __name__ == "__main__":
    unittest.main()
