"""anrede.anrede_im_text — die Fälle stammen aus der Messung vom 2026-10-05
(169 archivierte Aufnahmen, siehe tools/stt_vergleich.py)."""

import unittest

from voice_assistant.anrede import anrede_im_text


class AnredeTest(unittest.TestCase):
    def test_echte_rufe_und_verhoerer(self):
        for t in ("Gaston, mach alle Rollos zu.",
                  "Gastau, schalte das Tischlicht ein.",
                  "Gastraum schalte das Flurlicht ein.",
                  "Kastronen. Am Sonntag wurden wir essen",
                  "Gerstor macht alle Rollos zu.",
                  "Gas doch abendlich aus!",
                  "Ach so, Gaston! Stopp, stopp!"):
            self.assertTrue(anrede_im_text(t, "gaston"), t)

    def test_fernsehen_und_raum(self):
        # In keinem dieser Live-Transkripte stand das Wakewort.
        for t in ("Was sagst du?",
                  "Das ist schon ganz verkehrt, glaube ich.",
                  "Hast du denn noch?",
                  "Andrew Jackson. Nein, das ist der Titel",
                  "Guck, sofort 300.000 Likes! Glückwunsch",
                  "Der Dollar war stark.",
                  ""):
            self.assertFalse(anrede_im_text(t, "gaston"), t)

    def test_alltagswoerter_mit_aehnlichkeit_zaehlen_nicht(self):
        # "ganz" (4 Buchstaben), "hast" (anderer Anlaut): unscharf ähnlich,
        # aber keine Anrede — beide kamen in Fernseh-Transkripten vor.
        self.assertFalse(anrede_im_text("Ganz schön laut hier", "gaston"))
        self.assertFalse(anrede_im_text("Hast du das gesehen", "gaston"))

    def test_nur_die_ersten_woerter(self):
        self.assertFalse(anrede_im_text("Wir waren am Sonntag bei Gaston essen", "gaston"))

    def test_bundle_aus_mehreren_woertern(self):
        self.assertTrue(anrede_im_text("Hey Jarvis, wie spät ist es?", "hey_jarvis"))
        self.assertFalse(anrede_im_text("Wie spät ist es?", "hey_jarvis"))

    def test_ohne_brauchbaren_namen_kein_hinweis(self):
        # Kein Name → "Anrede vorhanden" → es wird kein Hinweis erzeugt.
        self.assertTrue(anrede_im_text("Irgendwas", ""))
        self.assertTrue(anrede_im_text("Irgendwas", None))


if __name__ == "__main__":
    unittest.main()
