"""Aussprache-Liste — ohne Netz, ohne Speaches."""

import gzip
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import aussprache  # noqa: E402
from voice_assistant.services.aussprache import Aussprache, piper_phoneme  # noqa: E402


class PiperPhonemeTest(unittest.TestCase):
    """Wiktionary-IPA in den Lautvorrat, den die Stimme aus espeak kennt."""

    def test_blindtest_woerter(self) -> None:
        # Die Fassungen, die Jochen am 2026-10-06 blind gewaehlt hat.
        self.assertEqual(piper_phoneme("[ˈzoːsə]"), "zˈoːsə")
        self.assertEqual(piper_phoneme("[kɔfeˈʔiːn]"), "kɔfeˈiːn")

    def test_r_wie_espeak(self) -> None:
        # espeak selbst: maɾkˈiːrən — r vor Vokal, sonst ɾ. ʁ kennt die Stimme nicht.
        self.assertEqual(piper_phoneme("[maʁˈkiːʁən]"), "maɾkˈiːrən")

    def test_nichts_ausserhalb_des_espeak_vorrats(self) -> None:
        for ipa in ("[ˈaʊ̯fˌstaɪ̯ləndəs]", "[ˈbaːɐ̯ˌhɔpɪŋ]", "[ˈʃlʏsl̩ˌbʊnt]", "[ˈkœʁiˌvʊʁst]"):
            ph = piper_phoneme(ipa)
            self.assertTrue(set(ph) <= aussprache._ESPEAK_DE, (ipa, ph))


class _Lexikon(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.grund = os.path.join(d, "grund.tsv")
        with open(self.grund, "w", encoding="utf-8") as f:
            f.write("# Kommentar\nSauce\tzˈoːsə\t0.6\nKoffein\tkɔfeˈiːn\t0.33\nHeadset\thˈɛdsˌɛt\t0.57\n")
        self.bekannt = os.path.join(d, "bekannt.txt.gz")
        with gzip.open(self.bekannt, "wt", encoding="utf-8") as f:
            f.write("\n".join(["Küchen", "Rollo", "Arbeit", "Platten", "Licht", "Präsident", "hieß",
                               "Der", "der", "und", "mochte", "Wohnzimmer"]) + "\n")
        self.a = Aussprache(grundstock=self.grund, ergaenzt=os.path.join(d, "erg.tsv"),
                            eigen=os.path.join(d, "eigen.tsv"), faelle=os.path.join(d, "faelle.jsonl"),
                            bekannt=self.bekannt)

    def tearDown(self) -> None:
        aussprache.einrichten(None)
        self.tmp.cleanup()


class AnwendenTest(_Lexikon):
    def test_ersetzt_und_laesst_den_rest(self) -> None:
        self.assertEqual(self.a.anwenden("Eine Sauce, bitte."), "Eine [[zˈoːsə]], bitte.")

    def test_satzanfang_klein_geschrieben_im_lexikon(self) -> None:
        self.assertEqual(self.a.anwenden("sauce"), "[[zˈoːsə]]")

    def test_bindestrich_teile(self) -> None:
        self.assertEqual(self.a.anwenden("Der Koffein-Gehalt"), "Der [[kɔfeˈiːn]]-Gehalt")

    def test_eigen_gewinnt_und_wirkt_sofort(self) -> None:
        self.a.eigen_setzen("Headset", "hˈɛtsɛt")
        self.assertEqual(self.a.anwenden("Headset"), "[[hˈɛtsɛt]]")
        self.assertEqual(self.a.auskunft("Headset")["quelle"], "eigen")
        self.assertTrue(self.a.eigen_loeschen("Headset"))
        self.assertEqual(self.a.anwenden("Headset"), "[[hˈɛdsˌɛt]]")

    def test_nur_fuer_piper(self) -> None:
        # Eine andere Engine laese [[…]] woertlich vor.
        aussprache.einrichten(self.a)
        self.assertEqual(aussprache.fuer_piper("Sauce", "kokoro-82m"), "Sauce")
        self.assertEqual(aussprache.fuer_piper("Sauce", "speaches-ai/piper-de_DE-thorsten-medium"),
                         "[[zˈoːsə]]")


class FaelleTest(_Lexikon):
    def _faelle(self) -> list[str]:
        try:
            return [json.loads(z)["wort"] for z in open(self.a.faelle_pfad, encoding="utf-8")]
        except FileNotFoundError:
            return []

    def test_unbekanntes_wird_gesammelt_bekanntes_nicht(self) -> None:
        self.a.fall_sammeln("Der Präsident hieß Andrew und mochte Küchenarbeitsplattenlicht und Sauce.")
        # Andrew: unbekannt. Kuechen·arbeit·s·platten·licht: aus bekannten Teilen.
        # Sauce: steht schon in der Liste.
        self.assertEqual(self._faelle(), ["Andrew"])

    def test_bindestrich_nur_der_unbekannte_teil(self) -> None:
        self.a.fall_sammeln("Wohnzimmer-Timer")
        self.assertEqual(self._faelle(), ["Timer"])

    def test_einmal_je_wort(self) -> None:
        self.a.fall_sammeln("Andrew")
        self.a.fall_sammeln("Andrew")
        self.assertEqual(self._faelle(), ["Andrew"])

    def test_bestaetigung_sammelt_nicht(self) -> None:
        # "Ich habe verstanden: …" wiederholt das rohe Transkript mit Verhoerern.
        aussprache.ohne_sammeln(self.a.fall_sammeln)("Ich habe verstanden: Callsender")
        self.assertEqual(self._faelle(), [])
        self.a.fall_sammeln("Callsender")       # danach wieder normal
        self.assertEqual(self._faelle(), ["Callsender"])


if __name__ == "__main__":
    unittest.main()
