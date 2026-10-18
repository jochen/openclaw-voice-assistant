"""musik_parser.parse — Vorlagen (data/musik/de_saetze.jsonl) plus echte
Transkripte, wörtlich aus dem OpenClaw-Verlauf (Messung:
tools/musik_parser_test.py)."""

import json
import os
import unittest

from voice_assistant.services.musik_parser import NEXT, PAUSE, RESUME, STOP, parse

_VORLAGEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "musik", "de_saetze.jsonl")


class VorlagenTest(unittest.TestCase):
    def test_alle_vorlagen(self):
        with open(_VORLAGEN, encoding="utf-8") as f:
            for zeile in f:
                v = json.loads(zeile)
                e, b = v["erwartet"], parse(v["satz"])
                if e["aktion"] is None:
                    self.assertIsNone(b, v["satz"])
                    continue
                self.assertIsNotNone(b, v["satz"])
                self.assertEqual((b.aktion, (b.raum or "").lower()),
                                 (e["aktion"], (e["raum"] or "").lower()), v["satz"])


class TranskriptTest(unittest.TestCase):
    def test_echte_befehle(self):
        for text, aktion, raum in (
            ("Kastor, stopp die Musik bitte in der Küche.", STOP, "Küche"),
            ("Was da stoppt die Musik?", STOP, None),     # Anrede als "Was da" verhört
            ("Stop, bitte die Musik.", STOP, None),
            ("Gaston, spielt die Musik weiter.", RESUME, None),
            ("Nächster. Nächstes Lied.", NEXT, None),
        ):
            b = parse(text)
            self.assertIsNotNone(b, text)
            self.assertEqual((b.aktion, b.raum), (aktion, raum), text)

    def test_echte_gegenbeispiele(self):
        for text in (
            "Stopp alle Rollos!",
            "Stop. Nächstes Licht.",
            "Das nächste Licht bitte.",        # Lied verhört: verpassen, nie schalten
            "Du tust doch sowieso, wenn du die Musik nicht ausmachst. Okay.",
            "Kannst du bitte die Musik in der Küche weiterspielen?",
            "Gastor, spielt mal Musik im ganzen Wohnzimmer.",
            "Gaston, spiel nach dem Lied bitte die gleichen die die Lieder von der gleichen CD ab.",
            "Okay. Machen wir weiter. Gross. Stopp.",
            "Gaston, konntest du das Lied schon herunterladen?",
        ):
            self.assertIsNone(parse(text), text)

    def test_pause_allein(self):
        self.assertEqual(parse("Kastor, Pause.").aktion, PAUSE)
        self.assertIsNone(parse("Wir machen jetzt mal eine Pause."))


if __name__ == "__main__":
    unittest.main()
