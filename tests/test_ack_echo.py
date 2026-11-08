"""Eigenes "Ja?" im Transkript: nur reine Ja-Sätze am Rand fallen weg."""

import unittest

from voice_assistant.ack_echo import ohne_ack_echo
from voice_assistant.wake_pruefung import woerter_nach_wakewort


class OhneAckEchoTest(unittest.TestCase):
    def test_nur_ja_wird_leer(self):
        for t in ("Ja.", "ja", "Ja?", "Ja. Ja.", "Jaja."):
            self.assertEqual(ohne_ack_echo(t), "", t)

    def test_ja_am_anfang_und_ende(self):
        self.assertEqual(ohne_ack_echo("Ja. Ich wollte nur mal kurz testen, ob du funktionierst."),
                         "Ich wollte nur mal kurz testen, ob du funktionierst.")
        self.assertEqual(ohne_ack_echo("Gaston, schalte das Tischlicht ein. Ja."),
                         "Gaston, schalte das Tischlicht ein.")
        self.assertEqual(ohne_ack_echo("Ja. Stop. Stop."), "Stop. Stop.")

    def test_ja_mit_mehr_bleibt(self):
        for t in ("Ja, stopp, stopp.", "Ja bitte mach das Licht an.",
                  "Mach das Licht an. Ja, genau.", "Ich sage ja. Mach an."):
            self.assertEqual(ohne_ack_echo(t), t)

    def test_ja_in_der_mitte_bleibt(self):
        t = "Mach das Licht an. Ja. Und das Radio aus."
        self.assertEqual(ohne_ack_echo(t), t)

    def test_unveraendert_heisst_identisch(self):
        t = "Gaston,  wie  spät ist es?"
        self.assertIs(ohne_ack_echo(t), t)
        self.assertIsNone(ohne_ack_echo(None))
        self.assertEqual(ohne_ack_echo(""), "")


class WoerterNachWakewortTest(unittest.TestCase):
    def test_zaehlt_hinter_dem_wort(self):
        self.assertEqual(woerter_nach_wakewort(
            {"text": "Gastor, mach das Tischlicht an.", "anfang_nach": 0}), 4)
        self.assertEqual(woerter_nach_wakewort(
            {"text": "spiel ein bisschen Gastor.", "anfang_nach": 3}), 0)
        self.assertEqual(woerter_nach_wakewort({"text": "Gasthof.", "anfang_nach": 0}), 0)

    def test_ohne_anfangs_mass_none(self):
        self.assertIsNone(woerter_nach_wakewort({"text": "Gaston, mach an."}))


if __name__ == "__main__":
    unittest.main()
