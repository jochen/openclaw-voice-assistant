"""timer_parser.parse — Vorlagen (data/timer/de_saetze.jsonl) plus die
Bruchstellen, die erst die eingesprochenen Sätze vom 2026-10-10 gezeigt haben
(Messung: tools/timer_parser_test.py). Die Transkripte unten sind wörtlich
aus diesen Aufnahmen (Qwen bzw. Speaches/medium)."""

import json
import os
import unittest

from voice_assistant.services.timer_parser import (
    ABFRAGEN, ALLE, KLINGELN, LOESCHEN, NOCH, STELLEN, VERLAENGERN, parse, schluessel,
)

_VORLAGEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "timer", "de_saetze.jsonl")


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
                self.assertEqual((b.aktion, schluessel(b.name), b.dauer_s, b.klingeln),
                                 (e["aktion"], schluessel(e["name"]), e["dauer_s"], e["klingeln"]),
                                 v["satz"])


class TranskriptTest(unittest.TestCase):
    def _gleich(self, text, aktion, name=None, dauer=None, klingeln=None):
        b = parse(text)
        self.assertIsNotNone(b, text)
        self.assertEqual((b.aktion, schluessel(b.name), b.dauer_s, b.klingeln),
                         (aktion, schluessel(name), dauer, klingeln), text)

    def test_anrede_verschmilzt_wird_kein_name(self):
        self._gleich("Das Duschstellentimer auf dreißig Sekunden.", STELLEN, None, 30)
        self._gleich("Gastrostellen Timer auf fünfundvierzig Minuten.", STELLEN, None, 2700)
        self._gleich("Gastrostimer auf fünf Minuten.", STELLEN, None, 300)
        self._gleich("Gasthausstelle, aber im Timer auch sieben Minuten.", STELLEN, None, 420)

    def test_namen(self):
        self._gleich("Gastrost stellt einen Nudeltimer auf acht Minuten.", STELLEN, "nudel", 480)
        self._gleich("Gaston, Timer für Nudeln auf acht Minuten.", STELLEN, "nudel", 480)
        self._gleich("Gaston Eier Timer sechs Minuten.", STELLEN, "ei", 360)
        self._gleich("Gaston, alle Timer löschen.", LOESCHEN, ALLE)

    def test_zeitangaben_qwen_und_medium(self):
        self._gleich("Gastau Timer vierte Stunde.", STELLEN, None, 900)
        self._gleich("Gasthaus stellt einen Timer für drei Viertelstunde.", STELLEN, None, 2700)
        self._gleich("Gaston stellt Timer auf eineinhalb Stunden.", STELLEN, None, 5400)
        self._gleich("Gaston stellt den Timer auf 1,5 Stunden.", STELLEN, None, 5400)
        self._gleich("Gaston Timer, 3 Minuten 20 Sekunden.", STELLEN, None, 200)
        self._gleich("Gaston, Timer zwei Minuten dreißig.", STELLEN, None, 150)

    def test_klingen_statt_klingeln(self):
        self._gleich("Gaston, der Timer soll zehn Mal klingen.", KLINGELN, None, None, 10)
        self._gleich("Gaston, stelle den Timer auf fünf Minuten und lass ihn zehnmal klingen.",
                     STELLEN, None, 300, 10)

    def test_aktionen(self):
        self._gleich("Gaston, verlängert den Timer um zwei Minuten.", VERLAENGERN, None, 120)
        self._gleich("Gaston, Nudeltimer noch drei Minuten.", NOCH, "nudel", 180)
        self._gleich("Gastor, welche Timer laufen?", ABFRAGEN)

    def test_nicht_eindeutig_geht_an_den_brain(self):
        for t in ("Gaston, Timer für die Stunde.",              # Viertelstunde verhört
                  "Gaste aufstellen, timer für 3-5 Stunden.",    # medium
                  "Gastraumstellentimer für drei für eine Stunde.",  # Dreiviertelstunde verstümmelt
                  "Gastrostellen Timer auf acht Uhr.",           # Uhrzeit
                  "Gastrop stellt einen Timer.",                 # keine Dauer
                  "Gaston, kannst du eigentlich auch ein Timer stellen?",
                  "Gaston, erinnere mich in fünf Minuten an die Wäsche.",
                  "Gaston, in zehn Minuten ist Küchenlicht aus.",
                  "Gaston, der Timer gestern hat nicht geklingelt.",
                  "Gaston Thalma zwei Minuten dreißig."):        # "Timer" verhört
            self.assertIsNone(parse(t), t)


if __name__ == "__main__":
    unittest.main()
