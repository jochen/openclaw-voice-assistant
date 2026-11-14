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


class FakeAntwort:
    """AntwortTts für feste Sätze: n Fassungen, Übernahme wird notiert."""

    def __init__(self, n=3):
        self.n, self.uebernommen = n, []

    def fest_kandidaten(self, text, name, n, versuche):
        return [(np.full(int((0.4 + 0.1 * i) * 24000), 500, dtype="<i2"), 24000)
                for i in range(min(n, self.n))]

    def fest_uebernehmen(self, text, name, pfad, quelle=""):
        self.uebernommen.append((text, name, os.path.basename(pfad), quelle))
        return pfad


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

    def _eingang(self, asr=None, wdh=2, antwort=None):
        return se.StimmenEingang(
            self.lager, asr or FakeAsr(), self.texte.append,
            lambda ogg, cap: self.audios.append((ogg, cap)),
            os.path.join(self.tmp.name, "eingang"), [self.media],
            vorbereiten=self.vorbereitet.append, tts_bauen=lambda _d: self.tts, abbruch_wdh=wdh,
            antwort=antwort, kurze_saetze=["Ja?", "Okay."] if antwort else ())

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

    def test_nach_dem_anlegen_erst_ja_dann_okay(self):
        antwort = FakeAntwort()
        e = self._eingang(antwort=antwort)
        e._zerlegen(self._datei(), "opa", "")
        n = len(self.audios)
        e._pruefen("opa", e._auftrag("opa")["hoerproben"][0], None)
        caps = [c for _, c in self.audios[n:]]
        self.assertEqual(len(caps), 3)                     # nur „Ja?“, Okay erst nach der Wahl
        self.assertTrue(all("„Ja?“" in c for c in caps))
        self.assertIn("„Ja?“", self.texte[-1])
        # Wahl: Satz ohne Satzzeichen, Groß/klein egal; danach kommt „Okay.“
        erg = e.kurz_waehlen("ja", 2)
        e.faden.join(5)
        self.assertEqual(antwort.uebernommen, [("Ja?", "opa", "s0_f2.wav", "telegram")])
        self.assertIn("Fassung 2", erg["text"])
        self.assertIn("Jetzt „Okay.“", erg["text"])
        caps = [c for _, c in self.audios[n + 3:]]
        self.assertEqual(len(caps), 3)
        self.assertTrue(all("„Okay.“" in c for c in caps))
        e.kurz_waehlen("Okay", 1)
        # Runde zu Ende: eine neue Ja-Wahl schickt nichts mehr nach
        m = len(self.audios)
        erg = e.kurz_waehlen("Ja", 3)
        self.assertNotIn("Jetzt", erg["text"])
        self.assertEqual(len(self.audios), m)
        for falsch in (("Ja?", 4), ("Hallo", 1)):
            with self.assertRaises(ValueError):
                e.kurz_waehlen(*falsch)

    def test_hoerproben_enden_mit_pause(self):
        e = self._eingang(antwort=FakeAntwort())
        alt = se.ogg_opus
        se.ogg_opus = lambda wav_bytes: wav_bytes          # WAV statt Opus, damit messbar
        try:
            e._zerlegen(self._datei(), "opa", "")
            e._pruefen("opa", e._auftrag("opa")["hoerproben"][0], None)
        finally:
            se.ogg_opus = alt
        fassung1 = next(o for o, c in self.audios if "„Ja?“ Fassung 1" in c)
        with wave.open(io.BytesIO(fassung1)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            rate = w.getframerate()
        self.assertAlmostEqual(len(pcm) / rate, 0.4 + se.NACHPAUSE_S, places=2)
        self.assertFalse(pcm[-int(se.NACHPAUSE_S * rate):].any())
        with wave.open(os.path.join(e._dir("opa"), "kurz", "s0_f1.wav")) as w:
            self.assertAlmostEqual(w.getnframes() / w.getframerate(), 0.4, places=2)  # gespeichert ohne

    def test_angelegte_stimme_wird_nicht_ersetzt(self):
        e = self._eingang()
        e._zerlegen(self._datei(), "opa", "")
        hp = e._auftrag("opa")["hoerproben"][0]
        e._pruefen("opa", hp, None)
        self.assertTrue(self.lager.hat("opa"))
        with open(os.path.join(self.lager.ordner, "opa", "probe.wav"), "rb") as f:
            vorher = f.read()
        with self.assertRaises(ValueError):
            e.waehlen(hp["nr"], "opa")
        e._pruefen("opa", hp, "Ganz anderer Text.")       # falls es doch so weit kommt
        self.assertIn("nicht ersetzt", self.texte[-1])
        with open(os.path.join(self.lager.ordner, "opa", "probe.wav"), "rb") as f:
            self.assertEqual(f.read(), vorher)
        with open(os.path.join(self.lager.ordner, "opa", "probe.txt"), encoding="utf-8") as f:
            self.assertNotEqual(f.read(), "Ganz anderer Text.")

    def test_kurz_anhoeren_fuer_vorhandene_stimme(self):
        e = self._eingang(antwort=FakeAntwort(n=2))
        with self.assertRaises(ValueError):
            e.kurz_anhoeren("gibtsnicht")
        with self.assertRaises(ValueError):
            e.kurz_waehlen("Ja?", 1)                      # noch nichts angehört
        os.makedirs(os.path.join(self.lager.ordner, "gaston"))
        for d, inhalt in (("probe.wav", b"x"), ("probe.txt", "Hallo.")):
            with open(os.path.join(self.lager.ordner, "gaston", d), "wb" if isinstance(inhalt, bytes) else "w") as f:
                f.write(inhalt)
        with self.assertRaises(ValueError):
            e.kurz_anhoeren("gaston", "Hallo")
        # nur „Okay.“ gewünscht: eine Runde aus diesem einen Satz
        erg = e.kurz_anhoeren("gaston", "okay")
        e.faden.join(5)
        self.assertEqual(erg["satz"], "Okay.")
        self.assertEqual(len(self.audios), 2)
        erg = e.kurz_waehlen("Okay", 2)
        self.assertEqual(e.antwort.uebernommen[0][:3], ("Okay.", "gaston", "s1_f2.wav"))
        self.assertNotIn("Jetzt", erg["text"])
        # ohne Satz: ganze Runde, alte Fassungen sind weg
        e.kurz_anhoeren("gaston")
        e.faden.join(5)
        self.assertEqual(len(self.audios), 4)
        with self.assertRaises(ValueError):
            e.kurz_waehlen("Okay", 1)                     # gehört nicht mehr zur Runde

    def test_ohne_feste_saetze_nicht_eingerichtet(self):
        with self.assertRaises(ValueError):
            self._eingang().kurz_anhoeren()

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
