"""Satz-Planer (services/satz_planer.py) — Regel, Schätzer, Puffer und der
Ablauf in ReplyStreamSession mit echtem Strom (ohne Gerät, mit echter Uhr).

Der Strom füllt Lücken mit Stille (audio/strom.py), die ausgelieferten Bytes
sind also die Zeitachse der Wiedergabe. Jeder Satz bekommt hier einen eigenen,
konstanten Pegel; so lässt sich im Ausgang ablesen, ob ein Satz am Stück lief
und wie lang die Pausen dazwischen waren.
"""

import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voice_assistant.audio.strom import BYTES_JE_S, RATE  # noqa: E402
from voice_assistant.services import tts  # noqa: E402
from voice_assistant.services.satz_planer import SatzPuffer, Schaetzer, freigeben  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.state import tts_lock  # noqa: E402
from test_tts_strom import FakeLeds, FakeSenke  # noqa: E402

BLOCK_S = 0.05


def _block(pegel: int, sekunden: float = BLOCK_S) -> bytes:
    n = int(sekunden * RATE)
    return np.full(2 * n, pegel, dtype=np.int16).tobytes()


class RegelTest(unittest.TestCase):
    def test_ohne_audio_nie(self):
        self.assertFalse(freigeben(0.0, 10.0, 0.0, None, 0.0, 1.0, 5.0))

    def test_schneller_dienst_sofort(self):
        self.assertTrue(freigeben(0.0, 0.0, 0.5, 0.0, 0.5, 4.0, 10.0))

    def test_gleichmaessig_wie_vorher(self):
        # Wachstum 1, kleine Schübe ≈ gleichmäßiger Fluss: Tempo 0,8, Satz 5 s
        args = dict(letzter_schub_t=0.0, letzter_schub_s=0.05, dauer_s=5.0, tempo=0.8,
                    wachstum=1.0, schub_max_s=0.05, reserve_s=0.2)
        self.assertFalse(freigeben(0.0, 0.0, 1.0, **args))
        self.assertTrue(freigeben(0.0, 0.0, 1.3, **args))

    def test_grosser_schub_verlangt_mehr_vorrat(self):
        # 1 s gepuffert, nächster Schub 2 s bei Tempo 0,8 kommt erst nach 2,5 s
        self.assertFalse(freigeben(0.0, 0.0, 1.0, 0.0, 1.0, 3.0, 0.8, 2.0, 4.0, 0.3))
        # mit 2 s Rest im Strom davor reicht es
        self.assertTrue(freigeben(0.0, 2.0, 1.0, 0.0, 1.0, 3.0, 0.8, 2.0, 4.0, 0.3))

    def test_alles_da_sofort(self):
        self.assertTrue(freigeben(0.0, 0.0, 3.0, 0.0, 1.0, 3.0, 0.5))


class SchaetzerTest(unittest.TestCase):
    def test_lernt_tempo_und_sprechdauer(self):
        s = Schaetzer(tempo_start=1.0, s_je_zeichen=0.1, gewicht=0.5)
        s.lernen(zeichen=50, audio_s=6.0, tempo=0.8)
        self.assertAlmostEqual(s.tempo, 0.9)
        self.assertAlmostEqual(s.s_je_zeichen, 0.11)
        s.lernen(zeichen=5, audio_s=0.3, tempo=None)    # zu kurz: nichts lernen
        self.assertAlmostEqual(s.s_je_zeichen, 0.11)
        self.assertAlmostEqual(s.tempo, 0.9)


class PufferTest(unittest.TestCase):
    def test_schuebe_und_tempo(self):
        uhr = [100.0]
        p = SatzPuffer("Satz.", BYTES_JE_S, uhr=lambda: uhr[0])
        p.dazu(_block(1, 0.25))
        uhr[0] += 0.01
        p.dazu(_block(1, 0.25))               # dicht dahinter: derselbe Schub
        self.assertEqual(len(p.schuebe), 1)
        self.assertIsNone(p.tempo_gemessen(), "erst ab dem zweiten Schub")
        uhr[0] += 1.0
        p.dazu(_block(1, 1.0))
        uhr[0] += 2.0
        p.dazu(_block(1, 2.0))
        self.assertEqual([round(x, 2) for _, x in p.schuebe], [0.5, 1.0, 2.0])
        self.assertAlmostEqual(p.tempo_gemessen(), 3.0 / 3.0, places=3)
        self.assertAlmostEqual(p.wachstum_gemessen(), 2.0, places=3)
        self.assertAlmostEqual(p.schub_max(), 2.0, places=3)
        self.assertEqual(p.letzter_schub()[1], 2.0)

    def test_ab_und_ende(self):
        p = SatzPuffer("Satz.", BYTES_JE_S)
        p.dazu(b"ab")
        p.dazu(b"cd")
        self.assertEqual(p.ab(1), ([b"cd"], False))
        p.ende()
        self.assertEqual(p.ab(2), ([], True))


class LangsamerDienst:
    """Erzeugt Sätze langsamer als Echtzeit (tempo < 1), einer nach dem
    anderen wie ein Dienst ohne Pipeline — der schwierigste Fall."""

    def __init__(self, tempo: float, laengen: dict[str, float], model: str = "cosyvoice3") -> None:
        self.state = SpeachesState()
        self.model = model
        self.voice = "test"
        self.tempo = tempo
        self.laengen = laengen
        self.pegel = {t: 1000 * (i + 1) for i, t in enumerate(laengen)}
        self._nacheinander = threading.Lock()
        self.angefragt: list[tuple[str, float]] = []

    def synth_strom(self, text):
        self.angefragt.append((text, time.monotonic()))
        with self._nacheinander:
            n = int(round(self.laengen[text] / BLOCK_S))
            for _ in range(n):
                time.sleep(BLOCK_S / self.tempo)
                yield _block(self.pegel[text])


class SchubDienst(LangsamerDienst):
    """Wie CosyVoice: ein Satz kommt in verdoppelnden Schüben (erst nichts,
    dann ein ganzer Abschnitt auf einmal), im Mittel mit tempo."""

    def __init__(self, tempo: float, laengen: dict[str, float], erster_s: float = 0.1,
                 schub_max_s: float = 0.8) -> None:
        super().__init__(tempo, laengen)
        self.erster_s = erster_s
        self.schub_max_s = schub_max_s

    def synth_strom(self, text):
        self.angefragt.append((text, time.monotonic()))
        with self._nacheinander:
            rest = self.laengen[text]
            schub = self.erster_s
            while rest > 1e-6:
                schub = min(schub, rest)
                time.sleep(schub / self.tempo)
                for _ in range(int(round(schub / BLOCK_S))):
                    yield _block(self.pegel[text])
                rest -= schub
                schub = min(2 * schub, self.schub_max_s)


def _abschnitte(daten: bytes) -> list[tuple[int, float, float]]:
    """(pegel, start_s, ende_s) zusammenhängender Strecken gleichen Pegels im
    linken Kanal; Stille (0) dazwischen trennt."""
    s = np.frombuffer(bytes(daten[44:]), dtype=np.int16)[0::2]
    if not len(s):
        return []
    wechsel = np.flatnonzero(np.diff(s)) + 1
    grenzen = np.concatenate(([0], wechsel, [len(s)]))
    out = []
    for a, b in zip(grenzen[:-1], grenzen[1:]):
        if s[a] != 0:
            out.append((int(s[a]), a / RATE, b / RATE))
    return out


class AblaufTest(unittest.TestCase):
    def _lauf(self, dienst, planer: bool, pause: float = 0.2, tempo_start: float = 0.7):
        senke = FakeSenke()
        sp = tts.ReplySpeaker(dienst, senke.play_wav, FakeLeds(), strom_oeffnen=senke.strom_oeffnen,
                              satz_planer=planer, satz_pause_s=pause, antwort_tempo=tempo_start)
        sitz = sp.stream_session(restore_leds=False, turn=None)
        t0 = time.monotonic()
        for satz in dienst.laengen:
            sitz.feed(satz, bereinigt=True)
        feed_dauer = time.monotonic() - t0
        self.assertTrue(sitz.end())
        self.assertFalse(tts_lock.locked())
        self.assertEqual(len(senke.stroeme), 1)
        strom, aus = senke.stroeme[0]
        strom.fertig.wait(3)
        return _abschnitte(aus.daten), feed_dauer, dienst

    def test_langsamer_dienst_ohne_luecke_im_satz_und_mit_pausen(self):
        laengen = {"Eins.": 0.5, "Zwei ist länger.": 0.8, "Drei.": 0.4}
        teile, feed_dauer, dienst = self._lauf(LangsamerDienst(0.7, laengen), planer=True, pause=0.2)
        self.assertLess(feed_dauer, 0.3, "feed() stellt nur ein, blockiert nicht")
        pegel = [p for p, _, _ in teile]
        self.assertEqual(pegel, list(dienst.pegel.values()), f"jeder Satz am Stück, in Reihenfolge: {teile}")
        for (_, a, b), (_, c, _) in zip(teile, teile[1:]):
            self.assertGreaterEqual(c - b, 0.2 - 0.01, f"Mindestpause: {teile}")
        for (p, a, b), dauer in zip(teile, laengen.values()):
            self.assertAlmostEqual(b - a, dauer, delta=0.011)
        # höchstens einer voraus: Satz 3 wird erst angefragt, wenn Satz 2 Audio hat
        t2 = dienst.angefragt[1][1]
        t3 = dienst.angefragt[2][1]
        self.assertGreater(t3 - t2, 0.0)

    def test_kein_fehlalarm_bevor_der_player_abholt(self):
        laengen = {"Eins.": 0.4}
        ausgabe = []
        orig = print
        try:
            import builtins
            builtins.print = lambda *a, **k: ausgabe.append(" ".join(map(str, a)))
            self._lauf(LangsamerDienst(0.7, laengen), planer=True)
        finally:
            builtins.print = orig
        self.assertFalse([z for z in ausgabe if "aufgefüllt" in z], ausgabe)

    def test_schuebe_wie_cosyvoice_ohne_aussetzer(self):
        # Verhältnisse wie beim echten Dienst: Schübe 0,5 / 1 / 2 s, Tempo 0,8,
        # Textlänge passt zur Schätzung (35 Zeichen ≈ 3,5 s). Ein Planer, der
        # nur das mittlere Tempo kennt, gibt nach dem zweiten Schub frei und
        # läuft vor dem dritten rund 1 s leer (live gehört 2026-11-22).
        laengen = {"Ein Satz mit gut drei Sekunden Ton.": 3.5}
        teile, _, dienst = self._lauf(SchubDienst(0.8, laengen, erster_s=0.5, schub_max_s=2.0),
                                      planer=True, pause=0.2)
        self.assertEqual([p for p, _, _ in teile], list(dienst.pegel.values()),
                         f"jeder Satz am Stück trotz Schüben: {teile}")
        for (_, a, b), dauer in zip(teile, laengen.values()):
            self.assertAlmostEqual(b - a, dauer, delta=0.011)

    def test_ohne_planer_reisst_der_satz_auf(self):
        laengen = {"Eins.": 0.5, "Zwei ist länger.": 0.8}
        teile, _, dienst = self._lauf(LangsamerDienst(0.7, laengen), planer=False)
        self.assertGreater(len(teile), len(laengen), f"Gegenprobe: Lücken mitten im Satz: {teile}")

    def test_schneller_dienst_nur_mindestpause(self):
        laengen = {"Eins.": 0.3, "Zwei.": 0.3, "Drei.": 0.3}
        teile, _, dienst = self._lauf(LangsamerDienst(6.0, laengen), planer=True, pause=0.25, tempo_start=1.0)
        self.assertEqual([p for p, _, _ in teile], list(dienst.pegel.values()), teile)
        for (_, a, b), (_, c, _) in zip(teile, teile[1:]):
            self.assertAlmostEqual(c - b, 0.25, delta=0.06, msg=f"nur die Mindestpause: {teile}")


if __name__ == "__main__":
    unittest.main()
