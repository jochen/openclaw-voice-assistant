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
    STATE_PAUSE,
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
            if body.get("lease_id"):
                return {"ducked": True, "lease_id": body["lease_id"], "renewed": True}
            self._n += 1
            return {"ducked": True, "lease_id": f"L{self._n}"}
        return {"restored": True}

    def was(self):
        return [(w, b.get("lease_id") or b.get("source") if w == "unduck" else b.get("lease_id"))
                for w, b in self.aufrufe]


def _bauen(**kw):
    bridge = FakeBridge(**kw)
    d = MusikDucker("http://bridge", "t", "wohnzimmer", erneuern_s=15.0, post=bridge.post,
                    get=lambda url, timeout: [])
    return d, bridge


def _thread(d):
    """Was der Thread nach dem letzten Wunsch tun wuerde."""
    with d._cv:
        lage = d._lage()
    d.schritt(*lage)


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
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])

    def test_satelliten_turn_daempft_nicht(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, True)
        _thread(d)
        d.zustand(STATE_LISTENING, True)
        _thread(d)
        self.assertEqual(bridge.aufrufe, [])

    def test_langer_turn_erneuert_mit_der_eigenen_lease(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        d._letztes_duck -= 16        # Frist abgelaufen
        _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(),
                         [("duck", None), ("duck", "L1"), ("unduck", "gaston")])

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


class DenkpauseTest(unittest.TestCase):
    """Waehrend der Brain denkt, ist die Musik laut (Jochen 2026-10-18)."""

    def _denken(self, d):
        from voice_assistant.services import musik_ducking
        d._ausgabe_zuletzt -= musik_ducking._HALTEN + 1
        d.zustand(STATE_WAITING, False, False)

    def test_denkpause_gibt_frei_und_der_naechste_satz_daempft(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        d.zustand(STATE_WAITING, False, True)      # Bestaetigung
        d.zustand(STATE_WAITING, False, False)     # kurz still: noch halten
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)])
        self._denken(d)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])
        d.zustand(STATE_WAITING, False, True)      # Antwort kommt
        _thread(d)
        d.zustand(STATE_PAUSE, False)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston"),
                                        ("duck", None), ("unduck", "gaston")])

    def test_selbst_gestartete_musik_bleibt_nach_der_denkpause_laut(self):
        d, bridge = _bauen(spielt=False)
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        bridge.spielt = True                       # "spiel Queen"
        self._denken(d)
        _thread(d)
        d.zustand(STATE_WAITING, False, True)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None)])

    def test_denkpause_behaelt_den_stand_beim_ruf(self):
        d, bridge = _bauen()
        d._get = lambda url, timeout: [{"room": "wohnzimmer", "state": "play"}]
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        self._denken(d)
        _thread(d)
        self.assertEqual(d._lief_beim_ruf, {"wohnzimmer"})


class LebenszeichenTest(unittest.TestCase):
    """"Einen Moment bitte." haelt tts_lock, soll aber nicht daempfen."""

    def test_lebenszeichen_ist_waehrend_der_wiedergabe_gesetzt(self):
        import tempfile
        import threading
        from voice_assistant import state
        from voice_assistant.services.tts import ThinkingWorker

        gesehen: list[tuple[bool, bool]] = []
        gespielt = threading.Event()

        def play_wav(_):
            gesehen.append((state.lebenszeichen.is_set(), state.tts_lock.locked()))
            gespielt.set()

        w = ThinkingWorker(play_wav, ["Einen Moment bitte."], min_quiet=0.0, max_quiet=0.0)
        w._render = lambda phrase: tempfile.mkstemp(suffix=".wav")[1]
        w.start()
        self.assertTrue(gespielt.wait(5))
        w.stop()
        self.assertEqual(gesehen[0], (True, True))
        self.assertFalse(state.lebenszeichen.is_set())


class VerloreneLeaseTest(unittest.TestCase):
    """2026-10-18 22:06:51: duck im Timeout, die Bridge hatte es angenommen."""

    def test_erneuerung_ohne_antwort_bleibt_eine_lease(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        echt = bridge.post

        def timeout(url, body):
            echt(url, body)
            raise TimeoutError("timed out")
        bridge.post = timeout
        d._post = timeout
        d._letztes_duck -= 16
        _thread(d)
        d._post = echt
        d._letztes_duck -= 16
        _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(),
                         [("duck", None), ("duck", "L1"), ("duck", "L1"), ("unduck", "gaston")])
        self.assertFalse(d._duck_fehler)

    def test_erstes_duck_ohne_antwort_wird_trotzdem_freigegeben(self):
        d, bridge = _bauen()
        echt = bridge.post

        def timeout(url, body):
            echt(url, body)
            raise TimeoutError("timed out")
        d._post = timeout
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        d._post = echt
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])

    def test_ende_gibt_nach_source_frei(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        d.zustand(STATE_LISTENING, False)
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])


class BridgeNeustartTest(unittest.TestCase):
    """Die Bridge vergisst ihre Leases: sichtbar machen, nicht verdecken."""

    def test_erneuerung_als_neues_duck_und_unbekannte_lease_melden(self):
        d, bridge = _bauen()
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        echt = bridge.post

        def vergesslich(url, body):
            antwort = echt(url, body)
            if url.endswith("/unduck"):
                return {"restored": False, "reason": "not_ducked"}
            return {"ducked": True, "lease_id": "L9"}   # weder renewed noch already_ducked
        d._post = vergesslich
        d._letztes_duck -= 16
        with self.assertLogs("voice_assistant.services.musik_ducking", "WARNING") as cm:
            _thread(d)
            d.zustand(STATE_LISTENING, False)
            _thread(d)
        self.assertEqual(len(cm.output), 2, cm.output)

    def test_normale_erneuerung_meldet_nichts(self):
        d, bridge = _bauen()
        echt = bridge.post

        def wie_die_bridge(url, body):
            antwort = echt(url, body)
            return antwort        # Erneuerung: renewed
        d._post = wie_die_bridge
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        d._letztes_duck -= 16
        with self.assertNoLogs("voice_assistant.services.musik_ducking", "WARNING"):
            _thread(d)


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
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])

    def test_vom_pegel_gate_verworfen_gibt_sofort_frei(self):
        d, bridge = _bauen()
        d.vorab()
        _thread(d)
        d.verworfen()
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])

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
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])


class AnsageTest(unittest.TestCase):
    def test_ansage_daempft_waehrend_sie_laeuft(self):
        d, bridge = _bauen()
        with d.ansage():
            _thread(d)
            self.assertEqual(bridge.was(), [("duck", None)])
        _thread(d)
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])

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
        self.assertEqual(bridge.was(), [("duck", None), ("unduck", "gaston")])


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

            def speak(self, text, **kw):
                gesprochen.append((text, gedaempft[0]))
                if len(gesprochen) == 2:
                    fertig.set()

        import queue
        q = queue.Queue()
        state.current_state[0] = state.STATE_LISTENING
        speak_server.start_announce_worker(Sprecher(), daempfen=daempfen, warteschlange=q)
        q.put(("Der Timer ist abgelaufen.", False, False))
        q.put("Der Bericht ist fertig.")
        self.assertTrue(fertig.wait(5))
        self.assertEqual(gesprochen, [("Der Timer ist abgelaufen.", False),
                                      ("Der Bericht ist fertig.", True)])


class MusikGestartetTest(unittest.TestCase):
    """Hat der Turn Musik gestartet, entfaellt der Follow-up (Jochen 2026-10-15)."""

    def _bauen(self, raeume):
        bridge = FakeBridge(spielt=False)
        stand = {"r": raeume}
        d = MusikDucker("http://bridge", "t", "wohnzimmer", post=bridge.post,
                        get=lambda url, timeout: stand["r"])
        return d, stand

    def test_spiel_queen_ohne_musik_beim_ruf(self):
        d, stand = self._bauen([{"room": "kachelofen", "state": "stop"}])
        d.vorab()
        _thread(d)
        d.zustand(STATE_RECORDING, False)
        stand["r"] = [{"room": "kachelofen", "state": "play"}]
        self.assertTrue(d.musik_gestartet())

    def test_echo_schaltet_sich_noch_ein(self):
        d, stand = self._bauen([{"room": "kachelofen", "state": "stop"}])
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        stand["r"] = [{"room": "kachelofen", "state": "starting"}]
        self.assertTrue(d.musik_gestartet())

    def test_musik_lief_schon_follow_up_bleibt(self):
        d, stand = self._bauen([{"room": "kachelofen", "state": "play"}])
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        self.assertFalse(d.musik_gestartet(), "naechstes Lied, lauter …")

    def test_rueckfrage_ohne_musik_follow_up_bleibt(self):
        d, _ = self._bauen([{"room": "kachelofen", "state": "stop"}])
        d.zustand(STATE_RECORDING, False)
        _thread(d)
        self.assertFalse(d.musik_gestartet())

    def test_bridge_weg_follow_up_bleibt(self):
        d, stand = self._bauen([{"room": "kachelofen", "state": "stop"}])
        d.zustand(STATE_RECORDING, False)
        _thread(d)

        def kaputt(url, timeout):
            raise OSError("weg")
        d._get = kaputt
        self.assertFalse(d.musik_gestartet())

    def test_satelliten_turn_kennt_keinen_vorher_stand(self):
        d, stand = self._bauen([{"room": "kachelofen", "state": "stop"}])
        d.zustand(STATE_RECORDING, True)
        _thread(d)
        stand["r"] = [{"room": "kachelofen", "state": "play"}]
        self.assertFalse(d.musik_gestartet())


if __name__ == "__main__":
    unittest.main()
