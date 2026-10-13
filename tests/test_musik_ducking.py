"""Musik-Ducking (services/musik_ducking.py) — ohne Netz und ohne Bridge.

Festgehalten wird, wann gedaempft und freigegeben wird: nur bei Turns des
Haupt-Mikrofons, ueber den ganzen Turn bis zum Leerlauf, Erneuerung nur fuer
tatsaechlich gedaempfte Musik, und jede Lease wird am Ende freigegeben.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services.musik_ducking import MusikDucker  # noqa: E402
from voice_assistant.state import (  # noqa: E402
    STATE_LISTENING,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_WAITING,
)


class FakeBridge:
    def __init__(self, spielt=True) -> None:
        self.spielt = spielt
        self.aufrufe: list[tuple[str, dict]] = []
        self._n = 0
        self.kaputt = False

    def post(self, url, body):
        if self.kaputt:
            raise OSError("connection refused")
        was = url.rsplit("/", 1)[1]
        self.aufrufe.append((was, body))
        if was == "duck":
            if not self.spielt:
                return {"ducked": False, "reason": "not_playing"}
            self._n += 1
            return {"ducked": True, "lease_id": f"L{self._n}"}
        return {"restored": True}

    def was(self):
        return [(w, b.get("lease_id")) for w, b in self.aufrufe]


def _bauen(**kw):
    bridge = FakeBridge(**kw)
    d = MusikDucker("http://bridge", "t", "wohnzimmer", erneuern_s=15.0, post=bridge.post)
    return d, bridge


def _thread(d):
    """Was der Thread nach dem letzten Wunsch tun wuerde."""
    with d._cv:
        wunsch, erstes = d._wunsch, d._erstes
        d._erstes = False
    d.schritt(wunsch, erstes)


class WannGedaempftWirdTest(unittest.TestCase):
    def test_haupt_turn_daempft_bis_zum_leerlauf(self):
        d, bridge = _bauen()
        d.zustand(STATE_LISTENING, False)
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        for s in (STATE_PROCESSING, STATE_WAITING):
            d.zustand(s, False)
            _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)], "kein zweites duck vor der Frist")
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "L1")])

    def test_satelliten_turn_daempft_nicht(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, True)
        _thread(d)
        d.zustand(STATE_LISTENING, True)
        _thread(d)
        self.assertEqual(bridge.aufrufe, [])

    def test_langer_turn_erneuert_und_gibt_alle_leases_frei(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        d._letztes_duck -= 16        # Frist abgelaufen
        _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(),
                         [("duck", None), ("duck", None), ("unduck", "L1"), ("unduck", "L2")])

    def test_ohne_musik_beim_ruf_wird_nicht_nachgedaempft(self):
        # Gaston startet im Turn selbst Musik — die soll nicht leise werden.
        d, bridge = _bauen(spielt=False)
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        bridge.spielt = True
        d._letztes_duck -= 16
        _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)])

    def test_bridge_weg_stoert_nicht(self):
        d, bridge = _bauen()
        bridge.kaputt = True
        d.zustand(STATE_RECORDING, False)
        with self.assertLogs("voice_assistant.services.musik_ducking", "WARNING") as cm:
            _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(d._leases, [])


class VorabTest(unittest.TestCase):
    """Daempfen schon beim Ueberschreiten der Schwelle, nicht erst beim Turn."""

    def test_vorab_daempft_und_der_turn_uebernimmt(self):
        d, bridge = _bauen()
        d.zustand(STATE_LISTENING, False)
        d.vorab()
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)], "vor dem Turn-Beginn")
        d.zustand(STATE_LISTENING, False)   # Serie laeuft noch
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)], "kein zweites duck beim Turn")
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "L1")])

    def test_vom_pegel_gate_verworfen_gibt_sofort_frei(self):
        d, bridge = _bauen()
        d.vorab()
        _thread(d)
        d.verworfen()
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "L1")])

    def test_ohne_turn_greift_die_frist(self):
        from voice_assistant.services import musik_ducking
        d, bridge = _bauen()
        d.vorab()
        _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)], "innerhalb der Frist warten")
        d._vorab_seit -= musik_ducking._VORAB_FRIST + 1
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "L1")])


class AnsageTest(unittest.TestCase):
    def test_ansage_daempft_waehrend_sie_laeuft(self):
        d, bridge = _bauen()
        with d.ansage():
            _thread(d)
            self.assertEqual(bridge.was(), [("duck", None)])
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "L1")])

    def test_ruf_waehrend_der_ansage_haelt_die_daempfung(self):
        d, bridge = _bauen()
        with d.ansage():
            _thread(d)
            d.vorab()
            d.zustand(STATE_RECORDING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)], "Turn laeuft noch")
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "L1")])


class AnnounceWorkerTest(unittest.TestCase):
    """Timer-Ansagen (text, False, False) daempfen nicht, alle anderen schon."""

    def test_timer_nicht_voice_speak_text_schon(self):
        import contextlib
        import threading
        from voice_assistant import state
        from voice_assistant.services import speak_server

        gesprochen: list[tuple[str, bool]] = []
        gedaempft = [False]
        fertig = threading.Event()

        @contextlib.contextmanager
        def daempfen():
            gedaempft[0] = True
            try:
                yield
            finally:
                gedaempft[0] = False

        class Sprecher:
            class leds:
                @staticmethod
                def set_phase(_):
                    pass

            def speak(self, text):
                gesprochen.append((text, gedaempft[0]))
                if len(gesprochen) == 2:
                    fertig.set()

        state.current_state[0] = state.STATE_LISTENING
        speak_server.start_announce_worker(Sprecher(), daempfen=daempfen)
        state.announce_queue.put(("Der Timer ist abgelaufen.", False, False))
        state.announce_queue.put("Der Bericht ist fertig.")
        self.assertTrue(fertig.wait(5))
        self.assertEqual(gesprochen, [("Der Timer ist abgelaufen.", False),
                                      ("Der Bericht ist fertig.", True)])


if __name__ == "__main__":
    unittest.main()
