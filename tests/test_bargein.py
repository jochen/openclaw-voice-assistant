"""Offline-Tests fuer den Abbruch mitten im Turn ("Stopp Gaston").

Laeuft OHNE Mikrofon, ohne Netz, ohne Modell — die Wakeword-Engine wird durch
eine Score-Folge ersetzt, OpenClaw und TTS kommen nicht vor.

Ausfuehren:
    ow-venv/bin/python tests/test_bargein.py
oder:
    ow-venv/bin/python -m unittest tests.test_bargein -v

Warum es diese Datei gibt
-------------------------
Ein Abbruch ist die Funktion, die ausgerechnet dann tragen muss, wenn schon
etwas schiefgegangen ist (Fehltrigger, falsch verstandener Satz). Drei
Bruchlinien sind dabei lautlos, und um die gehen die Tests:

1. **Das Gate muss dasselbe sein wie im Leerlauf.** Waere der Abbruch schwerer
   ausloesbar als der Ruf selbst, koennte man gerufen werden, aber nicht
   abbrechen. Deshalb teilen ``bargein.py`` und ``assistant.py`` die Regel
   (``wake_gate.py``) — und deshalb wird hier geprueft, dass beide Wege
   dasselbe Urteil faellen.

2. **Die Turn-Nummer.** Ein abgebrochener Worker laeuft noch ein paar
   Millisekunden weiter, waehrend die Hauptschleife schon im naechsten Turn
   aufnimmt. Ohne Nummer wuerde er in den NEUEN Turn hineinsprechen oder ihn
   sogar stoppen — der Fehler waere sporadisch und kaum reproduzierbar.

3. **turn=None darf nie gestoppt sein.** Ansagen von aussen
   (``voice_speak_text`` → speak_server), Aktuator-Antworten und Quittungen
   gehoeren zu keinem abbrechbaren Turn. Verschmelzen die beiden Faelle, dann
   schaltet ein Abbruch, auf den kein neuer Turn folgt, den Assistenten
   lautlos stumm — bis irgendwann zufaellig der naechste Turn beginnt.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant import config as cfg  # noqa: E402
from voice_assistant.assistant import _is_stop_command  # noqa: E402
from voice_assistant.bargein import (  # noqa: E402
    BargeInDetector,
    BargeInHit,
    BargeInMiss,
)
from voice_assistant.state import (  # noqa: E402
    TurnControl,
    turn_control,
    turn_stopped,
)
from voice_assistant.wake_gate import gate_passed  # noqa: E402
from voice_assistant.wakeword.base import WakewordHit  # noqa: E402


class FakeEngine:
    """Wakeword-Engine mit vorgegebener Score-Folge.

    Liefert pro feed() den naechsten Score; None bedeutet "noch keine
    Prediction" (so verhaelt sich openwakeword zwischen den 1280-Sample-
    Fenstern, und der Detektor muss diesen Fall aushalten).
    """

    def __init__(self, scores, threshold=0.5, min_hits=3, min_peak=0.0,
                 min_peak_short=0.0, min_peak_single=0.0, name="testwake") -> None:
        self._scores = list(scores)
        self._i = 0
        self.threshold = threshold
        self.min_hits = min_hits
        self.min_peak = min_peak
        self.min_peak_short = min_peak_short
        self.min_peak_single = min_peak_single
        self.name = name
        self.resets = 0

    def feed(self, chunk):
        if self._i >= len(self._scores):
            return None
        score = self._scores[self._i]
        self._i += 1
        if score is None:
            return None
        return WakewordHit(
            name=self.name,
            score=score,
            threshold=self.threshold,
            min_hits=self.min_hits,
            min_peak=self.min_peak,
            min_peak_short=self.min_peak_short,
            min_peak_single=self.min_peak_single,
        )

    def reset(self):
        self.resets += 1


def _chunk(amplitude: int = 3000, n: int = 1280) -> np.ndarray:
    """Ein Chunk mit gegebenem Pegel (Sinus, damit der RMS definiert ist)."""
    t = np.arange(n)
    return (amplitude * np.sin(2 * np.pi * 300 * t / 16000)).astype(np.int16)


def _run(detector: BargeInDetector, frames: int, amplitude: int = 3000):
    """Detektor mit `frames` Chunks fuettern; letztes Ergebnis zurueckgeben.

    Achtung bei den Score-Folgen: nach einem Streak ab min_hits wird der ERSTE
    leise Frame noch als Luecke toleriert (genau wie im Leerlauf), die
    Entscheidung faellt also erst beim zweiten. Eine Folge, die mit nur einem
    leisen Frame endet, liefert deshalb None — das ist kein Fehler, sondern die
    Toleranz, die echte "Gaston"-Rufe zusammenhaelt.
    """
    ergebnis = None
    for _ in range(frames):
        res = detector.feed(_chunk(amplitude))
        if res is not None:
            ergebnis = res
    return ergebnis


class GateIstDasselbeTest(unittest.TestCase):
    """(1) Der Abbruch benutzt genau das Gate des Leerlaufs."""

    def test_streak_ueber_min_hits_loest_aus(self) -> None:
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.9, 0.1, 0.1]))
        res = _run(det, 5)
        self.assertIsInstance(res, BargeInHit)
        self.assertEqual(res.hits, 3)
        self.assertAlmostEqual(res.peak, 0.9)

    def test_zu_kurzer_streak_ist_near_miss(self) -> None:
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.1, 0.1]))
        res = _run(det, 4)
        self.assertIsInstance(res, BargeInMiss)
        self.assertEqual(res.failed_on, "min_hits")

    def test_peak_zu_flach_ist_near_miss(self) -> None:
        det = BargeInDetector(FakeEngine([0.6, 0.6, 0.6, 0.1, 0.1], min_peak=0.9))
        res = _run(det, 5)
        self.assertIsInstance(res, BargeInMiss)
        self.assertEqual(res.failed_on, "min_peak")

    def test_eine_luecke_im_streak_wird_toleriert(self) -> None:
        # 3 Hits, Luecke, 1 Hit: dieselbe Toleranz wie im Leerlauf — echte
        # "Gaston"-Rufe tauchen mitten im Wort kurz unter den Threshold.
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.9, 0.1, 0.9, 0.1, 0.1]))
        res = _run(det, 7)
        self.assertIsInstance(res, BargeInHit)
        self.assertEqual(res.hits, 4)

    def test_luecke_vor_min_hits_naeht_nicht_zusammen(self) -> None:
        # 2 Hits, Luecke, 2 Hits duerfen NICHT zu einem 4er-Streak werden:
        # sonst verbindet die Toleranz zwei unabhaengige Rausch-Spitzen.
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.1, 0.9, 0.9, 0.1]))
        res = _run(det, 6)
        self.assertIsInstance(res, BargeInMiss)

    def test_keine_prediction_stoert_den_streak_nicht(self) -> None:
        det = BargeInDetector(FakeEngine([0.9, None, 0.9, None, 0.9, 0.1, 0.1]))
        res = _run(det, 7)
        self.assertIsInstance(res, BargeInHit)
        self.assertEqual(res.hits, 3)

    def test_urteil_deckt_sich_mit_gate_passed(self) -> None:
        """Dieselben Zahlen, beide Wege — das ist der Sinn von wake_gate.py.

        Verschwindet die gemeinsame Funktion und jemand schreibt die Regel im
        Barge-in nach, faellt dieser Test um, sobald sich eine der beiden
        Kopien aendert.
        """
        faelle = [
            # (hits, peak, min_hits, min_peak, min_peak_short, min_peak_single)
            (3, 0.95, 3, 0.9, 0.9, 0.0),
            (2, 0.95, 2, 0.5, 0.93, 0.0),
            (2, 0.80, 2, 0.5, 0.93, 0.0),
            (1, 0.99, 3, 0.5, 0.9, 0.95),
            (1, 0.90, 3, 0.5, 0.9, 0.95),
        ]
        for hits, peak, min_hits, min_peak, min_peak_short, min_peak_single in faelle:
            scores = [peak] * hits + [0.01, 0.01]
            det = BargeInDetector(FakeEngine(
                scores, min_hits=min_hits, min_peak=min_peak,
                min_peak_short=min_peak_short, min_peak_single=min_peak_single,
            ))
            res = _run(det, len(scores))
            erwartet = gate_passed(
                hits, peak, min_hits, min_peak, min_peak_short, min_peak_single
            )
            self.assertEqual(
                isinstance(res, BargeInHit), erwartet,
                f"hits={hits} peak={peak} min_hits={min_hits}: "
                f"Detektor und gate_passed sind uneinig",
            )


class PegelGateTest(unittest.TestCase):
    """(2) Das Pegel-Gate wirkt auch im Abbruch — und blockt sichtbar."""

    def test_zu_leise_wird_min_rms_near_miss(self) -> None:
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.9, 0.1, 0.1]), rms_min=5000.0)
        res = _run(det, 5, amplitude=100)
        self.assertIsInstance(res, BargeInMiss)
        self.assertEqual(res.failed_on, "min_rms")
        self.assertGreater(res.rms, 0.0)  # gemessener Wert gehoert ins Log

    def test_laut_genug_loest_aus(self) -> None:
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.9, 0.1, 0.1]), rms_min=100.0)
        res = _run(det, 5, amplitude=8000)
        self.assertIsInstance(res, BargeInHit)

    def test_gate_aus_bedeutet_kein_rms(self) -> None:
        det = BargeInDetector(FakeEngine([0.9, 0.9, 0.9, 0.1, 0.1]), rms_min=0.0)
        res = _run(det, 5, amplitude=1)
        self.assertIsInstance(res, BargeInHit)
        self.assertEqual(res.rms, 0.0)


class RingUndResetTest(unittest.TestCase):
    """(3) Pre-Roll-Puffer: das "Stopp" vor dem Wakewort muss darin stehen."""

    def test_ring_haelt_audio_fuer_den_pre_roll(self) -> None:
        det = BargeInDetector(FakeEngine([0.1] * 30))
        _run(det, 30)
        samples = sum(len(c) for c in det.ring)
        self.assertGreater(samples, int(16000 * 1.5),
                           "Ring muss mindestens den Pre-Roll (1,5 s) fassen")

    def test_ring_laeuft_nicht_unbegrenzt_voll(self) -> None:
        det = BargeInDetector(FakeEngine([0.1] * 400))
        _run(det, 400)
        samples = sum(len(c) for c in det.ring)
        self.assertLessEqual(samples, int(16000 * det.RING_SECONDS) + 1280)

    def test_reset_leert_ring_und_engine(self) -> None:
        engine = FakeEngine([0.1] * 20)
        det = BargeInDetector(engine)
        _run(det, 20)
        vorher = engine.resets
        det.reset()
        self.assertEqual(len(det.ring), 0)
        self.assertEqual(engine.resets, vorher + 1)


class TurnControlTest(unittest.TestCase):
    """(4) Die Turn-Nummer — die stille Bruchlinie."""

    def test_cancel_gilt_nur_einmal(self) -> None:
        tc = TurnControl()
        t = tc.begin()
        self.assertTrue(tc.cancel("barge_in"))
        self.assertFalse(tc.cancel("noch mal"),
                         "nur der erste Abbruch darf True liefern (Quittung, "
                         "Vermerk und Protokollzeile genau einmal)")
        self.assertEqual(tc.reason, "barge_in")
        self.assertTrue(tc.cancelled(t))

    def test_alter_turn_bleibt_gestoppt_neuer_laeuft(self) -> None:
        tc = TurnControl()
        alt = tc.begin()
        tc.cancel("barge_in")
        neu = tc.begin()
        self.assertTrue(tc.cancelled(alt),
                        "der verwaiste Worker des alten Turns muss aufhoeren")
        self.assertFalse(tc.cancelled(neu),
                         "der neue Turn darf nicht vom alten Abbruch getroffen sein")

    def test_ueberholter_turn_gilt_als_gestoppt_ohne_abbruch(self) -> None:
        tc = TurnControl()
        alt = tc.begin()
        tc.begin()  # naechster Turn, ganz ohne Abbruch
        self.assertTrue(tc.cancelled(alt))

    def test_closer_wird_beim_abbruch_gerufen(self) -> None:
        tc = TurnControl()
        t = tc.begin()
        gerufen = []
        tc.register_closer(t, lambda: gerufen.append("zu"))
        tc.cancel("barge_in")
        self.assertEqual(gerufen, ["zu"],
                         "ohne diesen Aufruf bleibt die SSE-Verbindung offen "
                         "und der Brain arbeitet weiter")

    def test_closer_nach_abbruch_wird_sofort_gerufen(self) -> None:
        tc = TurnControl()
        t = tc.begin()
        tc.cancel("barge_in")
        gerufen = []
        tc.register_closer(t, lambda: gerufen.append("zu"))
        self.assertEqual(gerufen, ["zu"],
                         "eine Verbindung, die eine Mikrosekunde zu spaet "
                         "aufgebaut wurde, muss trotzdem zugehen")

    def test_closer_eines_ueberholten_turns_wird_sofort_gerufen(self) -> None:
        tc = TurnControl()
        alt = tc.begin()
        tc.begin()
        gerufen = []
        tc.register_closer(alt, lambda: gerufen.append("zu"))
        self.assertEqual(gerufen, ["zu"])

    def test_closer_ohne_turn_wird_nicht_gerufen(self) -> None:
        tc = TurnControl()
        tc.begin()
        gerufen = []
        tc.register_closer(None, lambda: gerufen.append("zu"))
        self.assertEqual(gerufen, [],
                         "turn=None heisst 'kein abbrechbarer Turn' — eine "
                         "gerade geoeffnete Verbindung darf nicht zugehen")

    def test_turn_none_ist_nie_gestoppt(self) -> None:
        # turn_stopped() fragt die prozessweite Instanz — die muss der Test
        # auch benutzen, sonst prueft er gegen eine fremde Turn-Nummer.
        t = turn_control.begin()
        turn_control.cancel("barge_in")
        self.assertTrue(turn_stopped(t))
        self.assertFalse(
            turn_stopped(None),
            "Ansagen von aussen, Aktuator-Antworten und Quittungen gehoeren zu "
            "keinem Turn und duerfen von einem Abbruch nicht stumm werden",
        )

    def test_turn_stopped_folgt_der_nummer(self) -> None:
        t = turn_control.begin()
        self.assertFalse(turn_stopped(t))
        alt = t
        neu = turn_control.begin()
        self.assertTrue(turn_stopped(alt))
        self.assertFalse(turn_stopped(neu))


class StoppWortImBargeInTest(unittest.TestCase):
    """(5) Ein einzelnes "Stopp" muss im Barge-in reichen.

    Im Leerlauf verlangt das Erst-Muster zwei Woerter, weil das Mikrofon dort
    jedem Hintergrundgeraeusch offensteht. Nach einem Barge-in hat der Nutzer
    dagegen bewusst in eine laufende Ansage hineingesprochen — dieselbe
    Ueberlegung wie bei Follow-up und Klaerungs-Rueckfrage.
    """

    def test_einzelnes_stopp_reicht_im_bargein(self) -> None:
        self.assertTrue(_is_stop_command("Stopp", 1))
        self.assertTrue(_is_stop_command("Stopp Gaston", 1))
        self.assertTrue(_is_stop_command("Gaston Stopp", 1))

    def test_einzelnes_stopp_reicht_im_leerlauf_nicht(self) -> None:
        self.assertFalse(_is_stop_command("Stopp", 0))

    def test_normales_kommando_ist_kein_abbruch(self) -> None:
        # Das Gegenstueck: ein Barge-in ohne Stopp-Wort ist ein neuer Auftrag.
        self.assertFalse(_is_stop_command("Gaston mach das Küchenlicht an", 1))
        self.assertFalse(_is_stop_command("Wie warm ist es draußen", 1))

    def test_hoeflich_ausschalten_bleibt_ein_kommando(self) -> None:
        # Die Falle vom 2026-07-25: "bitte aus" darf kein Abbruch sein.
        self.assertFalse(
            _is_stop_command("Schalt das Küchenlicht bitte aus", 0)
        )


class TtsLockWettlaufTest(unittest.TestCase):
    """(8) Der Abbruch muss auch den Satz stoppen, der schon am tts_lock haengt.

    Live beobachtet am 2026-09-20 um 15:45:49, und es war der schlechteste
    denkbare Satz: die Bestaetigung sprach noch (haelt tts_lock), der erste Satz
    der Antwort stand fertig in der Warteschlange und hatte die
    Abbruch-Pruefung schon passiert. Dann kam "Stopp Gaston". Die Bestaetigung
    brach korrekt ab — und gab damit den Lock frei, worauf der wartende Satz
    ("Alles klar, Jochen.") gesprochen wurde. Aus Sicht des Nutzers hat der
    Abbruch also eine Antwort NICHT verhindert, sondern nur verzoegert.

    Deshalb wird innerhalb des Locks ein zweites Mal geprueft. Verschwindet
    diese zweite Pruefung, ist der Fehler lautlos zurueck — er braucht nur die
    richtige halbe Sekunde.
    """

    def test_satz_der_am_lock_wartete_wird_nicht_mehr_gesprochen(self) -> None:
        import threading

        from voice_assistant.services import tts as tts_mod
        from voice_assistant.state import tts_lock

        gespielt: list = []

        class FakeSpeaches:
            class state:
                @staticmethod
                def tts_ok():
                    return True

            @staticmethod
            def synth(text):
                return None  # Piper-Fallback greift, wird unten abgefangen

        class FakeLeds:
            def set_phase(self, *_a, **_k):
                pass

        sp = tts_mod.ReplySpeaker(
            FakeSpeaches(), lambda pfad: gespielt.append(pfad), FakeLeds()
        )
        # Piper-Rendering ersetzen: kein Modell, kein Audio — nur Buchfuehrung.
        orig_piper = tts_mod.piper_synth
        tts_mod.piper_synth = lambda text, model=None: gespielt.append(text) or None

        turn = turn_control.begin()
        try:
            # Lock belegen, wie es die laufende Bestaetigung tut.
            tts_lock.acquire()
            t = threading.Thread(
                target=sp.speak, args=("Alles klar, Jochen.",), kwargs={"turn": turn},
                daemon=True,
            )
            t.start()
            time.sleep(0.3)          # Satz haengt jetzt am Lock
            self.assertTrue(t.is_alive())
            turn_control.cancel("barge_in")   # Abbruch WAEHREND des Wartens
            tts_lock.release()                # Bestaetigung bricht ab, Lock frei
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(
                gespielt, [],
                "der am Lock wartende Satz darf nach dem Abbruch nicht mehr "
                "gesprochen werden — genau das passierte am 2026-09-20",
            )
        finally:
            tts_mod.piper_synth = orig_piper
            if tts_lock.locked():
                try:
                    tts_lock.release()
                except RuntimeError:
                    pass

    def test_ohne_abbruch_wird_der_satz_normal_gesprochen(self) -> None:
        """Gegenprobe: die zweite Pruefung darf nicht alles verschlucken."""
        import threading

        from voice_assistant.services import tts as tts_mod
        from voice_assistant.state import tts_lock

        gespielt: list = []

        class FakeSpeaches:
            class state:
                @staticmethod
                def tts_ok():
                    return False   # direkt in den Piper-Zweig

        class FakeLeds:
            def set_phase(self, *_a, **_k):
                pass

        sp = tts_mod.ReplySpeaker(
            FakeSpeaches(), lambda pfad: gespielt.append(pfad), FakeLeds()
        )
        orig_piper = tts_mod.piper_synth
        tts_mod.piper_synth = lambda text, model=None: gespielt.append(text) or None

        turn = turn_control.begin()
        try:
            tts_lock.acquire()
            t = threading.Thread(
                target=sp.speak, args=("Alles klar, Jochen.",), kwargs={"turn": turn},
                daemon=True,
            )
            t.start()
            time.sleep(0.3)
            tts_lock.release()       # kein Abbruch
            t.join(timeout=5.0)
            self.assertTrue(gespielt, "ohne Abbruch muss gesprochen werden")
        finally:
            tts_mod.piper_synth = orig_piper
            if tts_lock.locked():
                try:
                    tts_lock.release()
                except RuntimeError:
                    pass


class StreamAbbruchTest(unittest.TestCase):
    """(7) Der Abbruch muss die SSE-Verbindung wirklich schliessen.

    Darauf steht die ganze Funktion: laut OpenClaws Doku zu /v1/responses
    bricht ein getrennter HTTP-Client den Agent-Run ab. Bliebe die Verbindung
    offen, waere "Stopp" blosses Schweigen, waehrend der Brain weiterarbeitet
    — also genau der Fall, den der Abbruch verhindern soll (Fablab 2026-09-18).

    Gemessen gegen einen SSE-Server auf localhost, der endlos weitersendet:
    ohne Abbruch laeuft query_stream ewig, mit Abbruch kehrt es zurueck. Kein
    Netz nach draussen, kein OpenClaw.
    """

    def setUp(self) -> None:
        import http.server
        import threading

        self.verbindung_zu = threading.Event()

        class SSEHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            zu = self.verbindung_zu

            def do_POST(self):
                laenge = int(self.headers.get("Content-Length", 0))
                self.rfile.read(laenge)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                # Endlos senden, bis der Client die Verbindung wegnimmt.
                try:
                    # 200 Bloecke a 10 ms = 2 s; lang genug, dass der
                    # Abbruch mitten hineinfaellt, kurz genug fuer einen
                    # zuegigen Testlauf.
                    for i in range(200):
                        block = (
                            'event: x\r\ndata: {"type":"response.output_text.delta",'
                            '"delta":"Wort "}\r\n\r\n'
                        ).encode()
                        self.wfile.write(b"%x\r\n" % len(block) + block + b"\r\n")
                        self.wfile.flush()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.zu.set()

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), SSEHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_abbruch_schliesst_die_verbindung(self) -> None:
        import threading

        from voice_assistant.services import openclaw

        orig_url = openclaw.OPENCLAW_RESPONSES_URL
        openclaw.OPENCLAW_RESPONSES_URL = f"http://127.0.0.1:{self.port}/v1/responses"
        tc = TurnControl()
        turn = tc.begin()
        ergebnis = {}

        def lauf():
            ergebnis["text"], ergebnis["timeout"] = openclaw.query_stream(
                "hallo", token="t", session="s", control=tc, turn=turn,
            )

        try:
            t = threading.Thread(target=lauf, daemon=True)
            t.start()
            time.sleep(0.5)          # Stream laeuft an
            self.assertTrue(t.is_alive(), "Stream sollte noch laufen")
            tc.cancel("barge_in")
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "query_stream muss nach dem Abbruch zurueckkehren")
            self.assertFalse(
                ergebnis["timeout"],
                "ein Abbruch darf NICHT als Timeout gelten — sonst fragt der "
                "Worker per query_status nach und bringt den gestoppten Turn "
                "doch noch zu Ende",
            )
            self.assertTrue(
                self.verbindung_zu.wait(timeout=5.0),
                "die Serverseite muss den Verbindungsabbruch sehen — genau das "
                "bricht den Agent-Run ab",
            )
        finally:
            openclaw.OPENCLAW_RESPONSES_URL = orig_url

    def test_ohne_control_laeuft_der_stream_normal(self) -> None:
        """Gegenprobe: ohne Abbruch-Schranke darf nichts geschlossen werden.

        Haelt den Fehler fest, den register_closer(None, ...) ohne Sonderfall
        gemacht hat: die gerade geoeffnete Verbindung wurde sofort wieder
        zugemacht, weil turn=None als "ueberholter Turn" gelesen wurde.
        """
        import threading

        from voice_assistant.services import openclaw

        orig_url = openclaw.OPENCLAW_RESPONSES_URL
        openclaw.OPENCLAW_RESPONSES_URL = f"http://127.0.0.1:{self.port}/v1/responses"
        tc = TurnControl()
        tc.begin()
        saetze = []
        try:
            t = threading.Thread(
                target=lambda: openclaw.query_stream(
                    "hallo", token="t", session="s",
                    on_sentence=saetze.append, control=tc, turn=None,
                ),
                daemon=True,
            )
            t.start()
            time.sleep(0.5)
            self.assertTrue(
                t.is_alive(),
                "ohne Turn-Nummer darf die Verbindung nicht geschlossen werden",
            )
            # Ohne Turn-Nummer greift die Schranke nicht — der Stream laeuft
            # bis zum Ende des Servers (2 s). Genau das ist das alte Verhalten.
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive())
        finally:
            openclaw.OPENCLAW_RESPONSES_URL = orig_url


class ConfigTest(unittest.TestCase):
    """(6) Ohne `barge_in:`-Block verhaelt sich ein Profil wie vorher."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config_path = cfg.CONFIG_PATH
        self._orig_profile_env = os.environ.get("GASTON_PROFILE")

    def tearDown(self) -> None:
        cfg.CONFIG_PATH = self._orig_config_path
        if self._orig_profile_env is None:
            os.environ.pop("GASTON_PROFILE", None)
        else:
            os.environ["GASTON_PROFILE"] = self._orig_profile_env
        self._tmpdir.cleanup()

    def _load(self, yaml_text: str, profile_name: str = "testprofile"):
        path = os.path.join(self._tmpdir.name, "config.yaml")
        with open(path, "w") as f:
            f.write(yaml_text)
        cfg.CONFIG_PATH = path
        os.environ["GASTON_PROFILE"] = profile_name
        return cfg.load_profile()

    def test_ohne_block_ist_abbruch_aus(self) -> None:
        profile = self._load("""
profiles:
  testprofile:
    wake_rms_min: 400
    wakewords:
    - bundle: gaston
""")
        self.assertFalse(profile.barge_in.enabled)

    def test_block_ohne_wakewords_erbt_die_des_profils(self) -> None:
        profile = self._load("""
profiles:
  testprofile:
    wake_rms_min: 400
    wakewords:
    - bundle: gaston
      threshold: 0.7
    barge_in:
      enabled: true
""")
        self.assertTrue(profile.barge_in.enabled)
        self.assertEqual([w.bundle for w in profile.barge_in.wakewords], ["gaston"])
        self.assertEqual(profile.barge_in.wakewords[0].threshold, 0.7)
        self.assertEqual(profile.barge_in.rms_min, 400.0,
                         "ohne eigenen Wert gilt das Pegel-Gate des Profils")

    def test_eigenes_bundle_ist_reine_config(self) -> None:
        # Stufe 2 des Plans: ein nachtrainiertes "stopp_gaston" darf ohne
        # Code-Aenderung eingehaengt werden.
        profile = self._load("""
profiles:
  testprofile:
    wake_rms_min: 400
    wakewords:
    - bundle: gaston
    barge_in:
      enabled: true
      rms_min: 250
      ack: ""
      notify_brain: false
      wakewords:
      - bundle: stopp_gaston
        min_hits: 2
""")
        bi = profile.barge_in
        self.assertEqual([w.bundle for w in bi.wakewords], ["stopp_gaston"])
        self.assertEqual(bi.wakewords[0].min_hits, 2)
        self.assertEqual(bi.rms_min, 250.0)
        self.assertEqual(bi.ack, "", "leere Quittung = stumm abbrechen")
        self.assertFalse(bi.notify_brain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
