"""Doppelbefehle mit „und“ (services/aktuator_und.py), ohne Netz."""

import threading
import unittest
from unittest import mock

from voice_assistant.services import aktuator_schatten, aktuator_und
from voice_assistant.services.actuator import VERDICT_AUSFUEHRBAR, VERDICT_KEIN_KOMMANDO
from voice_assistant.services.aktuator_schatten import Entscheidung
from voice_assistant.services.aktuator_und import zerlegen


class ZerlegenTest(unittest.TestCase):
    def test_zwei_verben(self):
        self.assertEqual(
            zerlegen("Gaston, schalte bitte das Abendlicht ein und mach den Wohnzimmerrollo zu."),
            ["schalte bitte das Abendlicht ein", "mach den Wohnzimmerrollo zu"])

    def test_aktion_wird_nach_vorne_geliehen(self):
        self.assertEqual(zerlegen("Gaston, schalt Tischlicht und Sofalicht aus."),
                         ["schalt Tischlicht aus", "Sofalicht aus"])
        self.assertEqual(zerlegen("Stell die Badheizung und die Kloheizung auf 22 Grad."),
                         ["Stell die Badheizung auf 22 Grad", "die Kloheizung auf 22 Grad"])

    def test_nicht_leihen_ueber_eigenes_verb(self):
        # „raus“ ist ein Aktionswort (verhoertes „aus“); ohne es haette der
        # erste Teil „auf 22 Grad“ bekommen.
        teile = zerlegen("Schalte das Tischlicht raus. Und stell die Gästeheizung auf 22 Grad")
        self.assertEqual(teile, ["Schalte das Tischlicht raus", "stell die Gästeheizung auf 22 Grad"])
        teile = zerlegen("Gaston, mach mal das, das Tischlicht aus und das Sofalicht an.")
        self.assertEqual(teile[0], "mach mal das")

    def test_punkt_und_fuellwoerter(self):
        self.assertEqual(zerlegen("Gaston, mach das Küchenlicht aus. Und, äh, das Flurlicht an."),
                         ["mach das Küchenlicht aus", "das Flurlicht an"])

    def test_drei_teile_mit_komma(self):
        self.assertEqual(zerlegen("Gaston, Küchenlicht aus, Flurlicht an, Wohnzimmerrollo zu."),
                         ["Küchenlicht aus", "Flurlicht an", "Wohnzimmerrollo zu"])

    def test_kein_doppelbefehl(self):
        for satz in ("Gaston, mach die Rollos in der Küche zu.",
                     "Okay, mach das Licht an.",
                     "Gaston, mach das Licht an und danke.",
                     "Ich habe das Licht ausgemacht und den Rollo zugemacht.",
                     ""):
            self.assertIsNone(zerlegen(satz), satz)

    def test_name_mit_und_bleibt_ganz(self):
        self.assertIsNone(zerlegen("Mach Ess- und Wohnbereich an", ["Ess- und Wohnbereich"]))


def _e(intent, verdict=VERDICT_AUSFUEHRBAR):
    return Entscheidung("laya", intent, verdict, None, 80.0)


class _Akt:
    def __init__(self):
        self._lock = threading.Lock()
        self.digest = {}


class EntscheidenTest(unittest.TestCase):
    def _mit(self, antworten, satz):
        with mock.patch.object(aktuator_schatten, "entscheiden",
                               side_effect=lambda akt, t: antworten[t]):
            return aktuator_und.entscheiden(_Akt(), satz)

    def test_alle_ausfuehrbar(self):
        z = self._mit({"mach das Abendlicht an": _e({"ziel": "a", "aktion": "ein"}),
                       "den Rollo zu": _e({"ziel": "r", "aktion": "zu"})},
                      "Gaston, mach das Abendlicht an und den Rollo zu.")
        self.assertTrue(z.ausfuehrbar)
        self.assertEqual([i["ziel"] for i in z.intents], ["a", "r"])

    def test_ein_teil_kein_befehl_heisst_brain(self):
        z = self._mit({"Mach das Abendlicht an": _e({"ziel": "a", "aktion": "ein"}),
                       "erzähl mir einen Witz": _e(None, VERDICT_KEIN_KOMMANDO)},
                      "Mach das Abendlicht an und erzähl mir einen Witz.")
        self.assertFalse(z.ausfuehrbar)

    def test_ziel_doppelt_heisst_brain(self):
        z = self._mit({"Mach das Tischlicht an": _e({"ziel": "t", "aktion": "ein"}),
                       "das Tischlicht aus": _e({"ziel": "t", "aktion": "aus"})},
                      "Mach das Tischlicht an und das Tischlicht aus.")
        self.assertFalse(z.ausfuehrbar)

    def test_bedingung_fragt_niemanden(self):
        with mock.patch.object(aktuator_schatten, "entscheiden") as ent:
            z = aktuator_und.entscheiden(
                _Akt(), "Wenn es dunkel wird, mach das Abendlicht an und die Rollos zu.")
        self.assertFalse(z.ausfuehrbar)
        ent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
