"""Neue Stimmen per Telegram (services/stimmen_eingang.py), ohne Netz."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import wave

import numpy as np

from voice_assistant.services import stimmen_eingang as se
from voice_assistant.services.stimmen import StimmenLager

TEST = se.TESTSATZ


def _wav_datei(pfad: str, pcm: np.ndarray, rate: int = se.RATE) -> None:
    with wave.open(pfad, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())


def _probe(saetze_s: list[float], pause_s: float = 0.5) -> np.ndarray:
    """Geräusch-„Sätze“ mit Pausen (leises Rauschen dazwischen, wie ein Hörspiel)."""
    rng = np.random.default_rng(1)
    teile = []
    for d in saetze_s:
        teile.append(rng.normal(0, 30, int(pause_s * se.RATE)))
        teile.append(rng.normal(0, 6000, int(d * se.RATE)))
    teile.append(rng.normal(0, 30, int(pause_s * se.RATE)))
    return np.concatenate(teile).clip(-32000, 32000).astype("<i2")


class FakeAsr:
    """Probe-Stücke bekommen ``probe_text``, gerechnete Testsätze (konstante
    Samples aus FakeTts) ``satz_text`` — oder was ``satz_text`` als Funktion sagt."""

    def __init__(self, probe_text="Das ist ein ganz ruhiger Satz.", satz_text=TEST):
        self.probe_text, self.satz_text = probe_text, satz_text

    def _anfrage(self, wav_bytes: bytes, _kontext):
        with wave.open(io.BytesIO(wav_bytes)) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        mitte = x[len(x) // 4: 3 * len(x) // 4]
        if mitte.size and np.ptp(mitte.astype(np.int32)) < 50:   # FakeTts-Ausgabe
            return self.satz_text(len(x) / 16000) if callable(self.satz_text) else self.satz_text
        return self.probe_text


class FakeTts:
    def __init__(self, dauer_s=4.0):
        self.dauer_s = dauer_s
        self.aufrufe = []

    def _roh(self, text, name):
        self.aufrufe.append((text, name))
        d = self.dauer_s(name) if callable(self.dauer_s) else self.dauer_s
        return np.full(int(d * 24000), 1000, dtype="<i2"), 24000


class Grundlagen(unittest.TestCase):
    def test_name(self):
        self.assertEqual(se.name_pruefen("Opa Günter"), "opa_guenter")
        for falsch in ("", "1abc", "x", "../etc"):
            with self.assertRaises(ValueError):
                se.name_pruefen(falsch) if falsch != "../etc" else se.name_pruefen("..")

    def test_datei_nur_aus_eingang(self):
        with tempfile.TemporaryDirectory() as d:
            erlaubt = os.path.join(d, "media")
            os.makedirs(erlaubt)
            gut = os.path.join(erlaubt, "a.ogg")
            with open(gut, "wb") as f:
                f.write(b"x")
            self.assertEqual(se.datei_pruefen(gut, [erlaubt]), os.path.realpath(gut))
            fremd = os.path.join(d, "geheim.wav")
            with open(fremd, "wb") as f:
                f.write(b"x")
            for p in (fremd, os.path.join(erlaubt, "..", "geheim.wav")):
                with self.assertRaises(ValueError):
                    se.datei_pruefen(p, [erlaubt])
            # Symlink aus der Ablage hinaus zählt nicht
            os.symlink(fremd, os.path.join(erlaubt, "link.wav"))
            with self.assertRaises(ValueError):
                se.datei_pruefen(os.path.join(erlaubt, "link.wav"), [erlaubt])

    def test_vorauswahl_ohne_doppelte(self):
        r = se.RATE
        kand = [(0, 6 * r), (int(0.2 * r), int(6.1 * r)), (7 * r, 13 * r), (0, 3 * r)]
        aus = se.vorauswahl(kand, n=5)
        self.assertIn((0, 6 * r), aus)
        self.assertIn((7 * r, 13 * r), aus)
        self.assertNotIn((int(0.2 * r), int(6.1 * r)), aus)   # fast dasselbe Stück

    def test_satzende_der_probe(self):
        self.assertTrue(se.probe_endet_sauber("Wenn er von der Zufuhr ausgeschlossen ist."))
        self.assertFalse(se.probe_endet_sauber("Wenn er von der Zufuhr eines Rohstoffs ab"))
        self.assertFalse(se.probe_endet_sauber("Ja."))

    def test_anfang_fortgesetzt(self):
        s = "Brauchst du daraus eine Einkaufsliste für die Zutaten?"
        self.assertTrue(se.anfang_sauber(s, s))
        self.assertFalse(se.anfang_sauber(s, "Abbrauchst du daraus eine Einkaufsliste für die Zutaten?"))
        self.assertFalse(se.anfang_sauber(s, s + " Ihr? Na."))

    def test_abbruch_regel(self):
        s = "Brauchst du daraus eine Einkaufsliste für die Zutaten?"
        self.assertTrue(se.ist_abbruch(s, "Brauchst du daraus eine Einkaufs", 3.0, 0.08))
        self.assertFalse(se.ist_abbruch(s, s, 4.3, 0.08))
        # Qwen verhört das Ende, die Länge passt: nicht gezählt
        self.assertFalse(se.ist_abbruch(s, "Brauchst du daraus eine Einkaufsliste.", 4.3, 0.08))


class Ablauf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.media = os.path.join(d, "media")
        os.makedirs(self.media)
        self.lager = StimmenLager(os.path.join(d, "stimmen"), "haus", "http://x", "cosyvoice3", "gaston")
        os.makedirs(self.lager.ordner)
        self.texte, self.audios, self.vorbereitet = [], [], []
        self.tts = FakeTts()

    def tearDown(self):
        self.tmp.cleanup()

    def _eingang(self, asr=None, wdh=2):
        return se.StimmenEingang(
            self.lager, asr or FakeAsr(), self.texte.append,
            lambda ogg, cap: self.audios.append((ogg, cap)),
            os.path.join(self.tmp.name, "eingang"), [self.media],
            vorbereiten=self.vorbereitet.append, tts_bauen=lambda _d: self.tts, abbruch_wdh=wdh)

    def _datei(self, saetze=(5.0, 6.0)):
        p = os.path.join(self.media, "probe.wav")
        _wav_datei(p, _probe(list(saetze)))
        return p

    def test_zerlegen_schickt_hoerproben(self):
        e = self._eingang()
        e._zerlegen(self._datei(), "opa", "tief")
        self.assertTrue(self.audios)
        self.assertLessEqual(len(self.audios), se.HOERPROBEN_MAX)
        self.assertTrue(all(o[:4] == b"OggS" for o, _ in self.audios))
        self.assertIn("Stück 1", self.audios[0][1])
        self.assertIn("nimm Stück", self.texte[-1])
        auftrag = json.load(open(os.path.join(e.ordner, "opa", "auftrag.json")))
        self.assertEqual([h["nr"] for h in auftrag["hoerproben"]], list(range(1, len(self.audios) + 1)))
        self.assertTrue(all(t == TEST for t, _ in self.tts.aufrufe))

    def test_mitten_im_wort_fliegt_raus(self):
        e = self._eingang(FakeAsr(probe_text="Wenn er von der Zufuhr eines Rohstoffs ab"))
        e._zerlegen(self._datei(), "opa", "")
        self.assertEqual(self.audios, [])
        self.assertIn("kein brauchbares Stück", self.texte[-1])
        self.assertEqual(self.tts.aufrufe, [])     # gar nicht erst gerechnet

    def test_fortgesetzte_probe_fliegt_raus(self):
        e = self._eingang(FakeAsr(satz_text="Ab " + TEST))
        e._zerlegen(self._datei(), "opa", "")
        self.assertEqual(self.audios, [])

    def test_waehlen_besteht_und_legt_an(self):
        e = self._eingang()
        e._zerlegen(self._datei(), "opa", "tief")
        auftrag = e._auftrag("opa")
        self.tts.aufrufe.clear()
        e._pruefen("opa", auftrag["hoerproben"][0], "Der richtige Wortlaut.")
        self.assertTrue(self.lager.hat("opa"))
        with open(os.path.join(self.lager.ordner, "opa", "probe.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "Der richtige Wortlaut.")
        meta = json.load(open(os.path.join(self.lager.ordner, "opa", "meta.json")))
        self.assertEqual(meta["beschreibung"], "tief")
        self.assertEqual(meta["abbruch"], f"0/{2 * len(se.ABBRUCH_SAETZE)}")
        self.assertEqual(self.vorbereitet, ["opa"])
        self.assertIn("✅", self.texte[-1])
        self.assertEqual(len(self.tts.aufrufe), 2 * len(se.ABBRUCH_SAETZE))

    def test_waehlen_mit_abbruch_legt_nicht_an(self):
        e = self._eingang()
        e._zerlegen(self._datei(), "opa", "")
        stueck = e._auftrag("opa")["hoerproben"][0]
        # jetzt bricht jeder dritte Satz ab: kurz und ohne Ende
        n = {"i": 0}

        def satz(_dauer):
            n["i"] += 1
            return "Brauchst du daraus eine" if n["i"] % 3 == 0 else TEST
        e.asr = FakeAsr(satz_text=satz)
        self.tts.dauer_s = lambda _name: 1.0 if n["i"] % 3 == 2 else 4.0
        e._pruefen("opa", stueck, None)
        self.assertFalse(self.lager.hat("opa"))
        self.assertIn("brachen", self.texte[-1])
        self.assertEqual(self.vorbereitet, [])

    def test_einreichen_prueft_vorher(self):
        e = self._eingang()
        os.makedirs(os.path.join(self.lager.ordner, "gaston"))
        with self.assertRaises(ValueError):
            e.einreichen(self._datei(), "gaston")          # Name vergeben
        with self.assertRaises(ValueError):
            e.einreichen("/etc/hostname", "opa")           # nicht aus der Ablage
        with self.assertRaises(ValueError):
            e.waehlen(1)                                   # nichts offen

    def test_nur_ein_auftrag(self):
        e = self._eingang()
        e._lock.acquire()
        e.laeuft = "anderer"
        try:
            with self.assertRaises(RuntimeError):
                e.einreichen(self._datei(), "opa")
        finally:
            e._lock.release()

    def test_waehlen_unbekanntes_stueck(self):
        e = self._eingang()
        e._zerlegen(self._datei(), "opa", "")
        with self.assertRaises(ValueError):
            e.waehlen(99)


if __name__ == "__main__":
    unittest.main()
