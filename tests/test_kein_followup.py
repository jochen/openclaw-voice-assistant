"""NO_FOLLOWUP (services/openclaw.ohne_followup): Gaston laesst nichts offen.

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
        self.assertEqual(ohne_followup("NO_FOLLOWUP"), ("", True))

    def test_angehaengt(self):
        self.assertEqual(ohne_followup("Hab ich mir gemerkt. NO_FOLLOWUP"),
                         ("Hab ich mir gemerkt.", True))

    def test_varianten(self):
        for t in ("Die Musik läuft. [NO_FOLLOWUP]", "Die Musik läuft. no_follow_up.",
                  "Die Musik läuft. KEIN_FOLLOWUP", "Die Musik läuft. kein followup.",
                  "Die Musik läuft. KEIN-FOLLOW-UP"):
            self.assertEqual(ohne_followup(t), ("Die Musik läuft.", True), t)

    def test_ohne_schluesselwort_unveraendert(self):
        self.assertEqual(ohne_followup("Soll ich noch etwas spielen?"),
                         ("Soll ich noch etwas spielen?", False))
        self.assertEqual(ohne_followup(None), (None, False))

    def test_followup_im_normalen_wort_bleibt(self):
        # "Follow-up" als Wort in einem Satz ist kein Schluesselwort.
        self.assertFalse(ohne_followup("Das Follow-up-Fenster ist zu.")[1])
        self.assertFalse(ohne_followup("There is no follow-up needed.")[1])

    def test_no_reply_bleibt_erkennbar(self):
        text, _ = ohne_followup("NO_REPLY NO_FOLLOWUP")
        self.assertTrue(is_no_reply(text))

    def test_kein_reply_ist_no_reply(self):
        # 2026-10-16: der Brain schrieb KEIN_REPLY statt NO_REPLY (Fernseher).
        for t in ("KEIN_REPLY", "kein reply.", "KEIN-REPLY", "KEIN_REPLY KEIN_FOLLOWUP"):
            text, _ = ohne_followup(t)
            self.assertTrue(is_no_reply(text), t)
        self.assertFalse(is_no_reply("Kein Reply nötig, das Licht ist an."))


class BeepBlipTest(unittest.TestCase):
    """Der eigene Beep im Mikrofon schloss das Fenster nach 2,2 s (2026-10-15)."""

    def test_werte_aus_dem_endpoint_log(self):
        from voice_assistant.assistant import _followup_nur_blip
        # 09:49:25: 7 von 57 Chunks "Sprache", nur der Beep -> weiter zuhoeren
        self.assertTrue(_followup_nur_blip(7, 57))
        # 2026-10-09 "ja" als Handshake-Antwort (VAD 0,28) -> normal beenden
        self.assertFalse(_followup_nur_blip(16, 57))
        self.assertFalse(_followup_nur_blip(0, 0))


class AnsageWartetTest(unittest.TestCase):
    """Steht eine Ansage aus, oeffnet Gaston kein Follow-up (2026-10-21).

    Die Falle: der announce-worker nimmt die erste Ansage sofort aus der
    Queue und wartet dann auf LISTENING. qsize() waere dann 0 — mit nur
    einer ausstehenden Ansage oeffnete das Fenster trotzdem.
    """

    def test_vom_worker_gehaltene_ansage_zaehlt(self):
        import threading
        import time

        from voice_assistant import state
        from voice_assistant.services import speak_server

        gesprochen = threading.Event()

        class Sprecher:
            class leds:
                @staticmethod
                def set_phase(_):
                    pass

            def speak(self, text):
                gesprochen.set()

        state.current_state[0] = state.STATE_PAUSE
        speak_server.start_announce_worker(Sprecher())
        self.assertFalse(state.ansage_wartet())
        state.announce_queue.put("Das Ergebnis: der Neustart hat geklappt.")
        time.sleep(0.3)
        self.assertEqual(state.announce_queue.qsize(), 0)  # Worker haelt sie
        self.assertTrue(state.ansage_wartet())
        state.current_state[0] = state.STATE_LISTENING
        self.assertTrue(gesprochen.wait(5))
        time.sleep(0.1)
        self.assertFalse(state.ansage_wartet())


class FollowupLeiseFremdTest(unittest.TestCase):
    """Leise UND fremd → verwerfen (Messung 2026-10-21, _FOLLOWUP_LEISE_SPITZE).

    Die Zahlen sind echte Faelle aus dem Journal: Spitze vor der
    STT-Normalisierung und Status der Sprecher-Verifikation.
    """

    def test_gemessene_faelle(self):
        from voice_assistant.assistant import _followup_leise_fremd
        hintergrund = [622, 1306, 1420, 1462, 1594, 1859, 2121]
        for spitze in hintergrund:
            self.assertTrue(_followup_leise_fremd(spitze, "unbekannt"), spitze)
        # Die leisesten echten Antworten waren alle erkannt.
        for spitze in (2341, 2469, 2484):
            self.assertFalse(_followup_leise_fremd(spitze, "bekannt"), spitze)
        # Kurze echte Antworten sind oft unbekannt, aber laut.
        self.assertFalse(_followup_leise_fremd(9893, "unbekannt"))  # "Dem Monitor ein"
        self.assertFalse(_followup_leise_fremd(3013, "unbekannt"))  # "ja"

    def test_ausfall_verschluckt_nichts(self):
        from voice_assistant.assistant import _followup_leise_fremd
        self.assertFalse(_followup_leise_fremd(622, "ausgefallen"))
        self.assertFalse(_followup_leise_fremd(622, "nicht_eingerichtet"))

    def test_spitze_wie_stt_normalisierung(self):
        import numpy as np

        from voice_assistant.assistant import _spitze
        chunks = [np.array([10, -622, 300], dtype=np.int16),
                  np.array([], dtype=np.int16),
                  np.array([-32768], dtype=np.int16)]
        self.assertEqual(_spitze(chunks[:2]), 622)
        self.assertEqual(_spitze(chunks), 32768)  # kein int16-Ueberlauf
        self.assertEqual(_spitze([]), 0)


if __name__ == "__main__":
    unittest.main()
