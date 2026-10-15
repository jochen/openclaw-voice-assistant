"""Englische Stellen (services/englisch.py) und ihre Aussprache (services/aussprache.py).

Braucht die Debian-Wortlisten unter /usr/share/dict; fehlen sie, werden die
Tests uebersprungen (die Regel findet dann nichts, das ist gewollt).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import englisch  # noqa: E402
from voice_assistant.services.aussprache import Aussprache  # noqa: E402

_LISTEN = all(englisch.listen())


def _stellen(text):
    return [text[a:e] for a, e in englisch.englische_stellen(text)]


@unittest.skipUnless(_LISTEN, "Wortlisten fehlen")
class StellenTest(unittest.TestCase):
    def test_titel_im_deutschen_satz(self):
        self.assertEqual(_stellen("Jetzt läuft Wind of Change von den Scorpions."),
                         ["Wind of Change", "Scorpions"])

    def test_deutsche_titel_bleiben_deutsch(self):
        for t in ("Atemlos durch die Nacht", "Hurra die Welt geht unter",
                  "Ein bisschen Frieden", "Das Rollo in der Küche ist offen."):
            self.assertEqual(_stellen(t), [], t)

    def test_funktionswort_mittendrin_gehoert_dazu(self):
        self.assertEqual(_stellen("Ace In The Hole"), ["Ace In The Hole"])

    def test_unbekannter_name_neben_englisch(self):
        self.assertEqual(_stellen("Horns Of Baphomet"), ["Horns Of Baphomet"])
        self.assertEqual(_stellen("Vorm Proberaum (Skit)"), ["Skit"], "D daneben: bleibt")

    def test_apostroph_varianten(self):
        self.assertEqual(_stellen("It´s Hard"), ["It´s Hard"])


@unittest.skipUnless(_LISTEN, "Wortlisten fehlen")
class AusspracheEnglischTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        grund = os.path.join(d, "grund.tsv")
        with open(grund, "w", encoding="utf-8") as f:
            f.write("Change\ttʃˈeːndʒə\n")          # deutscher Grundstock: gilt hier nicht
        self.a = Aussprache(grundstock=grund, ergaenzt=os.path.join(d, "erg.tsv"),
                            eigen=os.path.join(d, "eigen.tsv"),
                            faelle=os.path.join(d, "faelle.jsonl"), englisch=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_englische_stelle_wird_lautschrift(self):
        aus = self.a.anwenden("Jetzt läuft Wind of Change.")
        self.assertTrue(aus.startswith("Jetzt läuft [["), aus)
        self.assertNotIn("tʃˈeːndʒə", aus, "Grundstock liest englisch deutsch")
        self.assertTrue(aus.endswith("]]."), aus)

    def test_eigen_gewinnt_auch_englisch(self):
        self.a.eigen_setzen("Change", "tʃˈeːntʃ")
        self.assertIn("[[tʃˈeːntʃ]]", self.a.anwenden("Wind of Change"))

    def test_satzzeichen_bleiben(self):
        aus = self.a.anwenden("Red, White (Live)")
        self.assertRegex(aus, r"^\[\[[^\]]+\]\], \[\[[^\]]+\]\] \(\[\[[^\]]+\]\]\)$")

    def test_aus_heisst_aus(self):
        self.a.englisch = False
        self.assertEqual(self.a.anwenden("Wind of Change"), "Wind of [[tʃˈeːndʒə]]")


if __name__ == "__main__":
    unittest.main()
