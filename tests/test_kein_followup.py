"""KEIN_FOLLOWUP (services/openclaw.ohne_followup): Gaston laesst nichts offen.

Das Schluesselwort darf nie vorgelesen werden, und es muss in den Formen
erkannt werden, in denen ein LLM es schreibt.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services.openclaw import is_no_reply, ohne_followup  # noqa: E402


class OhneFollowupTest(unittest.TestCase):
    def test_eigener_satz_am_ende(self):
        self.assertEqual(ohne_followup("KEIN_FOLLOWUP"), ("", True))

    def test_angehaengt(self):
        self.assertEqual(ohne_followup("Hab ich mir gemerkt. KEIN_FOLLOWUP"),
                         ("Hab ich mir gemerkt.", True))

    def test_varianten(self):
        for t in ("Die Musik läuft. [KEIN_FOLLOWUP]", "Die Musik läuft. kein followup.",
                  "Die Musik läuft. KEIN-FOLLOW-UP"):
            self.assertEqual(ohne_followup(t), ("Die Musik läuft.", True), t)

    def test_ohne_schluesselwort_unveraendert(self):
        self.assertEqual(ohne_followup("Soll ich noch etwas spielen?"),
                         ("Soll ich noch etwas spielen?", False))
        self.assertEqual(ohne_followup(None), (None, False))

    def test_followup_im_normalen_wort_bleibt(self):
        # "Follow-up" als Wort in einem Satz ist kein Schluesselwort.
        self.assertFalse(ohne_followup("Das Follow-up-Fenster ist zu.")[1])

    def test_no_reply_bleibt_erkennbar(self):
        text, _ = ohne_followup("NO_REPLY KEIN_FOLLOWUP")
        self.assertTrue(is_no_reply(text))


if __name__ == "__main__":
    unittest.main()
