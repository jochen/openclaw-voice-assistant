"""„Gaston, stell dich taub“ (services/mikro_parser.py)."""

import unittest

from voice_assistant.services.mikro_parser import MikroBefehl, parse


class MikroParserTest(unittest.TestCase):
    def test_stumm_mit_standarddauer(self):
        for satz in ("Gaston, stell dich taub.",
                     "Gaston, stell dich bitte mal taub",
                     "Gaston, sei taub.",
                     "Gaston, Mikro aus.",
                     "Gaston, schalte dein Mikrofon aus.",
                     "Gaston, mach das Mikro stumm.",
                     "Gaston, hör weg.",
                     "Gaston, hör nicht mehr zu."):
            self.assertEqual(parse(satz), MikroBefehl(stumm=True), satz)

    def test_dauer(self):
        self.assertEqual(parse("Gaston, stell dich für eine halbe Stunde taub."),
                         MikroBefehl(stumm=True, sekunden=1800))
        self.assertEqual(parse("Gaston, stell dich taub für 10 Minuten"),
                         MikroBefehl(stumm=True, sekunden=600))
        self.assertEqual(parse("Gaston, Mikro aus für zwei Stunden."),
                         MikroBefehl(stumm=True, sekunden=7200))

    def test_unbegrenzt(self):
        for satz in ("Gaston, stell dich taub, bis ich dich wieder anmache.",
                     "Gaston, Mikro aus bis auf Weiteres.",
                     "Gaston, stell dich dauerhaft taub."):
            self.assertEqual(parse(satz), MikroBefehl(stumm=True, unbegrenzt=True), satz)

    def test_wieder_an(self):
        for satz in ("Gaston, hör wieder zu.",
                     "Gaston, hör mir bitte wieder zu.",
                     "Gaston, Mikro wieder an.",
                     "Gaston, schalte dein Mikrofon wieder ein.",
                     "Gaston, sei nicht mehr taub."):
            self.assertEqual(parse(satz), MikroBefehl(stumm=False), satz)

    def test_kein_befehl(self):
        for satz in ("Gaston, bist du taub?",
                     "Warum ist das Mikro aus?",
                     "Gaston, stell dich nicht taub.",
                     "Gaston, stell dich taub bis 22 Uhr.",
                     "Gaston, mach das Küchenlicht aus.",
                     "Der Opa ist ein bisschen taub geworden.",
                     "Gaston, du hörst mir nie zu.",
                     ""):
            self.assertIsNone(parse(satz), satz)


if __name__ == "__main__":
    unittest.main()
