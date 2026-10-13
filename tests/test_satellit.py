"""Satelliten-Weiche (audio/satellit.py) — ohne Netz und ohne Handy.

Festgehalten wird, wem ein Turn gehoert: Aufnahme vom Geraet, das ihn
ausgeloest hat, Antwort dorthin, und keine Session, die offen bleibt, ohne
dass jemand zuhoert (das Handy stuende sonst auf "listening").
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio.satellit import SatellitQuelle, SatellitSenke  # noqa: E402
from voice_assistant.config import FOLLOWUP_BEEP_PATH  # noqa: E402
from voice_assistant.state import (  # noqa: E402
    STATE_FOLLOWUP,
    STATE_LISTENING,
    STATE_PAUSE,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_WAITING,
)


class FakeHaupt:
    def __init__(self) -> None:
        self.flushes = 0
        self.gespielt: list[str] = []

    def read_chunk(self):
        return np.full(640, 1, dtype=np.int16)

    def flush(self):
        self.flushes += 1

    def play_wav(self, path):
        self.gespielt.append(path)


class FakeClient:
    def __init__(self) -> None:
        self.session = False
        self.neu = None
        self.beendet = 0
        self.ist_verbunden = True

    def oeffnen(self, phrase="Hey Jarvis"):
        self.session = True
        self.neu = phrase

    def in_session(self):
        return self.session

    def sitzung_abholen(self):
        neu, self.neu = self.neu, None
        return neu

    def sitzung_beenden(self):
        self.beendet += 1
        self.session = False

    def verbunden(self):
        return self.ist_verbunden

    def read_chunk(self):
        return np.full(640, 2, dtype=np.int16)

    def flush(self):
        pass


class FakeSenke:
    def __init__(self) -> None:
        self.gespielt: list[tuple[str, bool]] = []

    def play_wav(self, path, mikro_danach=False):
        self.gespielt.append((path, mikro_danach))


def _bauen():
    haupt, handy = FakeHaupt(), FakeClient()
    quelle = SatellitQuelle(haupt, {"jochen": handy})
    senke_handy = FakeSenke()
    senke = SatellitSenke(haupt, quelle, {"jochen": senke_handy})
    return haupt, handy, quelle, senke, senke_handy


def _schritt(quelle, state):
    """Ein Durchlauf der Hauptschleife: erst zustand(), dann read_chunk()."""
    quelle.zustand(state)
    return quelle.read_chunk()


class TurnGehoertDemGeraetTest(unittest.TestCase):
    def test_ohne_session_liest_das_haupt_mikrofon(self):
        _, _, quelle, _, _ = _bauen()
        self.assertEqual(_schritt(quelle, STATE_LISTENING)[0], 1)
        self.assertIsNone(quelle.satellit_trigger())

    def test_session_im_leerlauf_wird_trigger_und_quelle(self):
        _, handy, quelle, _, _ = _bauen()
        handy.oeffnen("Hey Jarvis")
        self.assertEqual(_schritt(quelle, STATE_LISTENING)[0], 2)
        self.assertEqual(quelle.satellit_trigger(), ("jochen", "Hey Jarvis"))
        self.assertIsNone(quelle.satellit_trigger(), "Trigger nur einmal")
        # Aufnahme laeuft: weiter vom Handy, Session bleibt offen.
        self.assertEqual(_schritt(quelle, STATE_RECORDING)[0], 2)
        self.assertEqual(handy.beendet, 0)

    def test_ende_der_aufnahme_schliesst_die_session(self):
        haupt, handy, quelle, _, _ = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)
        _schritt(quelle, STATE_PROCESSING)
        self.assertEqual(handy.beendet, 1)
        # Bis zum Leerlauf gehoert der Turn dem Handy (WAITING, PAUSE).
        self.assertEqual(_schritt(quelle, STATE_WAITING)[0], 2)
        self.assertEqual(quelle.aktiv, "jochen")
        flushes = haupt.flushes
        self.assertEqual(_schritt(quelle, STATE_LISTENING)[0], 1)
        self.assertIsNone(quelle.aktiv)
        self.assertGreater(haupt.flushes, flushes, "Rueckstau des Haupt-Mikrofons verwerfen")

    def test_keine_sprache_beendet_session_und_turn(self):
        _, handy, quelle, _, _ = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)
        _schritt(quelle, STATE_LISTENING)
        self.assertEqual(handy.beendet, 1)
        self.assertIsNone(quelle.aktiv)

    def test_followup_session_gehoert_zum_turn(self):
        _, handy, quelle, _, _ = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        quelle.satellit_trigger()
        _schritt(quelle, STATE_RECORDING)
        _schritt(quelle, STATE_PROCESSING)
        _schritt(quelle, STATE_PAUSE)
        handy.oeffnen("")            # start_conversation nach dem Beep
        self.assertEqual(_schritt(quelle, STATE_FOLLOWUP)[0], 2)
        self.assertEqual(handy.beendet, 1, "Follow-up-Session nicht schliessen")
        self.assertIsNone(quelle.satellit_trigger(), "kein neuer Turn")
        _schritt(quelle, STATE_PROCESSING)
        self.assertEqual(handy.beendet, 2)


class VerwaisteSessionTest(unittest.TestCase):
    def test_ruf_waehrend_eines_haupt_turns_wird_geschlossen(self):
        _, handy, quelle, _, _ = _bauen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)     # Haupt-Mikrofon hat getriggert
        handy.oeffnen()
        self.assertEqual(_schritt(quelle, STATE_RECORDING)[0], 1)
        self.assertEqual(handy.beendet, 1)
        self.assertIsNone(quelle.aktiv)

    def test_erneuter_ruf_im_eigenen_turn_ausserhalb_der_aufnahme(self):
        _, handy, quelle, _, _ = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)
        _schritt(quelle, STATE_WAITING)
        handy.oeffnen()
        _schritt(quelle, STATE_WAITING)
        self.assertEqual(handy.beendet, 2)


class AnsageGehtDorthinTest(unittest.TestCase):
    def test_ohne_satelliten_turn_spielt_das_haupt_geraet(self):
        haupt, _, quelle, senke, senke_handy = _bauen()
        _schritt(quelle, STATE_LISTENING)
        senke.play_wav("/x.wav")
        self.assertEqual(haupt.gespielt, ["/x.wav"])
        self.assertEqual(senke_handy.gespielt, [])

    def test_antwort_und_spaetere_ansagen_gehen_aufs_handy(self):
        haupt, handy, quelle, senke, senke_handy = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)
        senke.play_wav("/antwort.wav")
        _schritt(quelle, STATE_LISTENING)
        senke.play_wav("/spaeter.wav")       # voice_speak_text, Timer …
        self.assertEqual(senke_handy.gespielt,
                         [("/antwort.wav", False), ("/spaeter.wav", False)])
        self.assertEqual(haupt.gespielt, [])

    def test_followup_beep_oeffnet_das_mikro_nur_im_eigenen_turn(self):
        _, handy, quelle, senke, senke_handy = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_PAUSE)
        senke.play_wav(FOLLOWUP_BEEP_PATH)
        _schritt(quelle, STATE_LISTENING)
        senke.play_wav(FOLLOWUP_BEEP_PATH)
        self.assertEqual(senke_handy.gespielt,
                         [(FOLLOWUP_BEEP_PATH, True), (FOLLOWUP_BEEP_PATH, False)])

    def test_haupt_trigger_holt_die_ansagen_zurueck(self):
        haupt, handy, quelle, senke, _ = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)    # jetzt das Haupt-Mikrofon
        senke.play_wav("/k.wav")
        self.assertEqual(haupt.gespielt, ["/k.wav"])

    def test_handy_weg_ansagen_ueber_das_haupt_geraet(self):
        haupt, handy, quelle, senke, _ = _bauen()
        handy.oeffnen()
        _schritt(quelle, STATE_LISTENING)
        _schritt(quelle, STATE_RECORDING)
        _schritt(quelle, STATE_LISTENING)
        handy.ist_verbunden = False
        senke.play_wav("/x.wav")
        self.assertEqual(haupt.gespielt, ["/x.wav"])


if __name__ == "__main__":
    unittest.main()
