"""Ansagen des Brains waehrend seines Turns (2026-10-21).

Ein OpenClaw-Turn endet erst, wenn das Modell ohne Tool-Aufruf antwortet;
voice_speak_text-Zwischenmeldungen kamen deshalb bis zu fuenf Minuten zu
spaet. Jetzt laufen Ansagen des Brains (reiner Text) schon in STATE_WAITING,
an die Turn-Nummer gebunden. System-Ansagen (Tupel: Timer, Mikro) warten
weiter auf LISTENING. Und solange eine Ansage aussteht, oeffnet sich kein
Follow-up (state.ansage_wartet).
"""

import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant import state  # noqa: E402
from voice_assistant.services import speak_server  # noqa: E402
from voice_assistant.services.leds import LED_IDLE, LED_OPENCLAW  # noqa: E402


class Sprecher:
    def __init__(self):
        self.gesprochen = []
        self.phasen = []
        self.neu = threading.Event()
        sp = self

        class leds:
            @staticmethod
            def set_phase(p):
                sp.phasen.append(p)
        self.leds = leds

    def speak(self, text, turn=None, **kw):
        self.gesprochen.append((text, turn))
        self.neu.set()


def _worker(daempfen=None):
    q = queue.Queue()
    sp = Sprecher()
    speak_server.start_announce_worker(sp, daempfen=daempfen, warteschlange=q)
    return q, sp


class AnsageImTurnTest(unittest.TestCase):
    def tearDown(self):
        state.current_state[0] = state.STATE_LISTENING

    def test_brain_spricht_waehrend_er_denkt(self):
        state.current_state[0] = state.STATE_WAITING
        nr = state.turn_control.begin()
        q, sp = _worker()
        q.put("Kurze Zwischenmeldung: das Badrollo faehrt nicht.")
        self.assertTrue(sp.neu.wait(3))
        self.assertEqual(sp.gesprochen, [("Kurze Zwischenmeldung: das Badrollo faehrt nicht.", nr)])
        time.sleep(0.05)
        self.assertEqual(sp.phasen[-1], LED_OPENCLAW)  # zurueck auf "denkt"

    def test_systemansage_wartet_auf_leerlauf(self):
        state.current_state[0] = state.STATE_WAITING
        q, sp = _worker()
        q.put(("Der Timer ist abgelaufen.", False, False))
        self.assertFalse(sp.neu.wait(0.6))
        self.assertTrue(state.ansage_wartet(q))
        state.current_state[0] = state.STATE_LISTENING
        self.assertTrue(sp.neu.wait(3))
        self.assertEqual(sp.gesprochen, [("Der Timer ist abgelaufen.", None)])
        time.sleep(0.05)
        self.assertEqual(sp.phasen[-1], LED_IDLE)
        self.assertFalse(state.ansage_wartet(q))

    def test_im_turn_nicht_extra_daempfen(self):
        # Im Turn daempft die Turn-Logik (tts_lock); daempfen() wuerde auch
        # Musik leiser machen, die Gaston in diesem Turn gestartet hat.
        aufrufe = []

        def daempfen():
            aufrufe.append(1)
            return None
        state.current_state[0] = state.STATE_WAITING
        q, sp = _worker(daempfen=daempfen)
        q.put("Zwischenstand.")
        self.assertTrue(sp.neu.wait(3))
        self.assertEqual(aufrufe, [])

    def test_vom_worker_gehaltene_ansage_zaehlt(self):
        # Der Worker nimmt die Ansage sofort aus der Queue: qsize() waere 0,
        # mit nur einer ausstehenden Ansage oeffnete das Follow-up trotzdem.
        state.current_state[0] = state.STATE_PAUSE
        q, sp = _worker()
        self.assertFalse(state.ansage_wartet(q))
        q.put("Das Ergebnis: der Neustart hat geklappt.")
        time.sleep(0.3)
        self.assertEqual(q.qsize(), 0)
        self.assertTrue(state.ansage_wartet(q))
        state.current_state[0] = state.STATE_LISTENING
        self.assertTrue(sp.neu.wait(3))
        time.sleep(0.1)
        self.assertFalse(state.ansage_wartet(q))


if __name__ == "__main__":
    unittest.main()
