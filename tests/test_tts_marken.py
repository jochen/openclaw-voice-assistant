"""Klang-Marken (services/tts_marken.py): überleben clean_for_tts, werden für
CosyVoice übersetzt und für jede andere Stimme entfernt."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import tts_marken  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.services.tts import ReplyRecorder, SpeachesTts, clean_for_tts  # noqa: E402


class MarkenTest(unittest.TestCase):
    def test_ueberleben_clean_for_tts(self):
        c = clean_for_tts("Das ist ja lustig [lachen], damit habe ich nicht gerechnet. "
                          "[Atmen] Aber das ist **wirklich** wichtig.")
        self.assertEqual(c, "Das ist ja lustig [lachen], damit habe ich nicht gerechnet. "
                            "[atmen] Aber das ist **wirklich** wichtig.")

    def test_verbalisierung_laeuft_weiter(self):
        c = clean_for_tts("[hm] Um 14:30 Uhr ist der Laser frei.")
        self.assertEqual(c, "[hm] Um vierzehn Uhr dreißig ist der Laser frei.")

    def test_alle_geraeusche_fuer_cosyvoice(self):
        for deutsch, tag in tts_marken.GERAEUSCHE.items():
            c = clean_for_tts(f"Na gut [{deutsch}], weiter.")
            self.assertEqual(tts_marken.fuer_cosyvoice(c), f"Na gut {tag}, weiter.", deutsch)

    def test_betont_und_lachend(self):
        c = clean_for_tts("<lachend>Das glaube ich nicht</lachend>, das ist **echt** stark.")
        self.assertEqual(tts_marken.fuer_cosyvoice(c),
                         "<laughter>Das glaube ich nicht</laughter>, das ist <strong>echt</strong> stark.")

    def test_entfernen_ohne_reste(self):
        for roh, erwartet in (
            ("[hm], mal sehen.", "mal sehen."),
            ("Lustig [lachen], ehrlich.", "Lustig, ehrlich."),
            ("Das ist **wirklich** wichtig [atmen].", "Das ist wirklich wichtig."),
            ("<lachend>Echt jetzt</lachend>!", "Echt jetzt!"),
        ):
            self.assertEqual(tts_marken.entfernen(clean_for_tts(roh)), erwartet, roh)

    def test_unbekannte_klammern_wie_bisher(self):
        # keine Marke: die Klammern verschwinden wie vorher, der Inhalt bleibt
        self.assertEqual(clean_for_tts("Siehe [Anhang] unten."), "Siehe Anhang unten.")

    def test_ohne_marken_unveraendert(self):
        satz = "Normaler Satz mit z. B. drei Dingen."
        self.assertEqual(clean_for_tts(satz), "Normaler Satz mit zum Beispiel drei Dingen.")
        self.assertFalse(tts_marken.hat_marken(clean_for_tts(satz)))

    def test_dienst_mit_und_ohne_marken(self):
        satz = clean_for_tts("Ach [seufzen], das ist **wichtig**.")
        cosy = SpeachesTts(SpeachesState(), "http://x", "cosyvoice3", "s", fest=True, marken=True)
        self.assertEqual(cosy._parameter(satz)[3], "Ach [sigh], das ist <strong>wichtig</strong>.")
        piper = SpeachesTts(SpeachesState(), "http://x", "speaches-ai/piper-de", "s", fest=True)
        self.assertEqual(piper._parameter(satz)[3], "Ach, das ist wichtig.")

    def test_mitschnitt_ohne_marken(self):
        rec = ReplyRecorder(active=True)
        rec.add_pcm("Lustig [lachen], ehrlich.", b"\x00\x00" * 4, (1, 2, 48000))
        self.assertEqual(rec._texts, ["Lustig, ehrlich."])


if __name__ == "__main__":
    unittest.main()
