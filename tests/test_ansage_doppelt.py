"""Schlussantwort wiederholt die Ansage im Turn nicht (2026-10-30).

Der Brain sagte „Welche Rollos meinst du …?“ per voice_speak_text an und gab
denselben Satz danach als Schlussantwort zurück: zweimal gesprochen, zweimal
im Chat, und die Eingabe 🎤 stand erst hinter der Ansage. 3 von 3 Ansagen im
Turn seit 2026-10-21 liefen so, trotz der Regel in AGENTS.md.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant import state, workers  # noqa: E402

FRAGE = "Welche Rollos meinst du: alle Rollos oder die Rollos im ganzen Haus?"


class Sitzung:
    def __init__(self, gesprochen):
        self.gesprochen = gesprochen

    def feed(self, satz):
        self.gesprochen.append(satz)

    def end(self):
        return bool(self.gesprochen)


def _arbeiter(gesprochen, use_stream=True):
    sprecher = types.SimpleNamespace(
        stream_session=lambda **kw: Sitzung(gesprochen),
        speak=lambda text, turn=None: gesprochen.append(text),
        leds=types.SimpleNamespace(set_phase=lambda p: None),
    )
    return types.SimpleNamespace(
        use_stream=use_stream, speaker=sprecher, openclaw_token="t",
        openclaw_session="s", voice_instruction="", instructions_feld=True,
        voice_erinnerung="", telegram_bot_token="b", telegram_chat_id="c",
        thinking=types.SimpleNamespace(stop=lambda: None),
        no_reply_fallback="?", abort_notify_brain=False,
        _finish_cancelled=lambda key: None,
    )


class AnsageDoppeltTest(unittest.TestCase):
    def setUp(self):
        state.current_state[0] = state.STATE_WAITING
        self.chat = []

    def tearDown(self):
        state.current_state[0] = state.STATE_LISTENING

    def _turn(self, ansage, antwort, use_stream=True):
        """Ansage kommt mitten im Stream (wie voice_speak_text), dann die Antwort."""
        nr = state.turn_control.begin()
        gesprochen = []

        def stream(text, on_sentence=None, **kw):
            # Was der announce-worker vor dem Sprechen tut
            state.turn_ansagen.angesagt(nr, ansage)
            self.chat.append("🔊 " + ansage)
            for satz in state_split(antwort):
                on_sentence(satz)
            return antwort, False

        def anfrage(text, **kw):
            state.turn_ansagen.angesagt(nr, ansage)
            self.chat.append("🔊 " + ansage)
            return antwort

        with mock.patch.object(workers.openclaw, "query_stream", stream), \
                mock.patch.object(workers.openclaw, "query", anfrage), \
                mock.patch.object(workers.telegram, "send",
                                  lambda tok, chat, text, prefix="": self.chat.append(prefix + text)):
            workers.Workers._run_openclaw_turn(
                _arbeiter(gesprochen, use_stream), "Die Rollos aufmachen.",
                speaker=None, turn=nr)
        return gesprochen

    def test_ganze_antwort_schon_angesagt(self):
        gesprochen = self._turn(FRAGE, FRAGE)
        self.assertEqual(gesprochen, [])
        self.assertEqual(self.chat, ["🎤 [unbekannt] Die Rollos aufmachen.", "🔊 " + FRAGE])
        # Follow-up bleibt offen: die Frage will eine Antwort
        self.assertEqual(state.pending_reply_text[0], FRAGE)

    def test_nur_neues_wird_gesagt(self):
        antwort = "Alles klar, die Pastinaken sind gesetzt. Kurz vor Schluss dazugeben."
        gesprochen = self._turn("Alles klar, die Pastinaken sind gesetzt!", antwort)
        self.assertEqual(gesprochen, ["Kurz vor Schluss dazugeben."])
        self.assertEqual(self.chat[-1], "🔊 Kurz vor Schluss dazugeben.")

    def test_ohne_stream(self):
        gesprochen = self._turn(FRAGE, FRAGE, use_stream=False)
        self.assertEqual(gesprochen, [])
        self.assertEqual(self.chat[0], "🎤 [unbekannt] Die Rollos aufmachen.")

    def test_andere_antwort_bleibt(self):
        gesprochen = self._turn("Einen Moment, ich schaue nach.", "Alle Rollos sind offen.")
        self.assertEqual(gesprochen, ["Alle Rollos sind offen."])

    def test_alter_turn_zaehlt_nicht(self):
        alt = state.turn_control.begin()
        state.turn_ansagen.angesagt(alt, "Alle Rollos werden geöffnet.")
        neu = state.turn_control.begin()
        self.assertFalse(state.turn_ansagen.schon_gesagt(neu, "Alle Rollos werden geöffnet."))
        self.assertFalse(state.turn_ansagen.schon_gesagt(None, "Alle Rollos werden geöffnet."))


def state_split(text):
    from voice_assistant.services.tts import split_into_sentences
    return split_into_sentences(text)


if __name__ == "__main__":
    unittest.main()
