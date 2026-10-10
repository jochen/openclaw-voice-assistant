"""kuechentimer.KuechenTimer — mit falscher Uhr und falschen Senken, ohne
Netz und Audio. Festgehalten sind die Entscheidungen vom 2026-10-10
(Docstring des Moduls) und der Senken-Vertrag (TIMER_INTERFACE.md)."""

import json
import os
import tempfile
import threading
import unittest

from voice_assistant.services.kuechentimer import KuechenTimer, Senke, dauer_text
from voice_assistant.services.timer_parser import parse


class Uhr:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Grundlage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pfad = os.path.join(self.dir, "timer.json")
        self.uhr = Uhr()
        self.gesendet: list[dict] = []
        self.senke_klingelt = True
        self.ansagen: list[str] = []
        self.lautsprecher: list[int] = []
        self.timer = self._neu()

    def _neu(self, senken=True):
        def senden(s, stand, timeout):
            self.gesendet.append(stand)
            ids = [t["id"] for t in stand["timer"] if t["rest_s"] <= 0 and not t["still"]]
            return {"anzeigen": 1, "klingelt": ids if self.senke_klingelt else []}

        def klingeln(n, still):
            self.lautsprecher.append(n)

        return KuechenTimer(self.pfad, [Senke("test", "http://x")] if senken else [],
                            klingeln=3, klingel_abstand_s=5, nachlauf_max_s=600,
                            ansage=self.ansagen.append, lautsprecher_klingeln=klingeln,
                            uhr=self.uhr, senden=senden)

    def sag(self, text):
        b = parse(text)
        self.assertIsNotNone(b, text)
        return self.timer.ausfuehren(b)



class BefehleTest(Grundlage):
    def test_stellen_und_quittung(self):
        e = self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        self.assertEqual(e.text, "Nudel-Timer, acht Minuten.")
        e = self.sag("Gaston, Timer zwei Minuten dreißig.")
        self.assertEqual(e.text, "Timer, zwei Minuten dreißig Sekunden.")
        self.assertEqual(len(self.timer.schnappschuss()["timer"]), 2)

    def test_namenloser_ersetzt_namenlosen(self):
        self.sag("Gaston, Timer fünf Minuten.")
        self.sag("Gaston, Timer zehn Minuten.")
        timer = self.timer.schnappschuss()["timer"]
        self.assertEqual(len(timer), 1)
        self.assertEqual(timer[0]["rest_s"], 600)

    def test_gleicher_name_verschiedene_formen(self):
        self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        self.sag("Gaston, Timer für die Nudeln, fünf Minuten.")
        self.assertEqual(len(self.timer.schnappschuss()["timer"]), 1)

    def test_verlaengern_laufend_und_abgelaufen(self):
        self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        self.uhr.t += 60
        e = self.sag("Gaston, verlängere den Nudeltimer um eine Minute.")
        self.assertEqual(e.text, "Nudel-Timer läuft noch acht Minuten.")
        self.uhr.t += 9 * 60                       # 1 min über
        self.timer.tick()
        self.sag("Gaston, Nudeltimer noch drei Minuten.")
        t = self.timer.schnappschuss()["timer"][0]
        self.assertEqual(t["rest_s"], 180)          # ab jetzt, nicht ab altem Ende
        self.assertFalse(t["still"])

    def test_noch_ohne_timer_stellt_neu(self):
        e = self.sag("Gaston, noch fünf Minuten auf den Timer.")
        self.assertTrue(e.ok)
        self.assertEqual(self.timer.schnappschuss()["timer"][0]["rest_s"], 300)

    def test_loeschen_mehrere_fragt_zurueck(self):
        self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        self.sag("Gaston, stell einen Timer für die Pizza auf zwölf Minuten.")
        e = self.timer.loeschen(None)
        self.assertTrue(e.rueckfrage)
        self.assertEqual(e.text, "Welchen Timer? Nudel oder Pizza?")
        self.assertEqual(len(self.timer.schnappschuss()["timer"]), 2)
        self.sag("Gaston, alle Timer löschen.")
        self.assertEqual(self.timer.schnappschuss()["timer"], [])

    def test_loeschen_namenlos_nimmt_den_namenlosen(self):
        self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        self.sag("Gaston, Timer fünf Minuten.")
        e = self.timer.loeschen(None)
        self.assertEqual(e.text, "Der Timer ist gelöscht.")
        self.assertEqual([t["name"] for t in self.timer.schnappschuss()["timer"]], ["Nudel"])

    def test_klingelanzahl_nur_fuer_diesen_timer(self):
        self.sag("Gaston, Timer fünf Minuten.")
        e = self.sag("Gaston, der Timer soll zehn Mal klingen.")
        self.assertEqual(e.text, "Der Timer klingelt zehnmal.")
        self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        klingeln = {t["name"]: t["klingeln"] for t in self.timer.schnappschuss()["timer"]}
        self.assertEqual(klingeln, {None: 10, "Nudel": 3})

    def test_abfragen(self):
        self.assertEqual(self.timer.abfragen(None).text, "Es läuft kein Timer.")
        self.sag("Gaston, stell einen Nudeltimer auf acht Minuten.")
        self.uhr.t += 8 * 60 + 150
        self.timer.tick()
        self.assertEqual(self.sag("Gastor, welche Timer laufen?").text,
                         "Nudel-Timer ist seit zwei Minuten dreißig Sekunden abgelaufen.")


class AblaufTest(Grundlage):
    def test_senke_klingelt_lautsprecher_nicht(self):
        self.sag("Gaston, Timer fünf Minuten.")
        self.uhr.t += 300
        self.timer.tick()
        self.assertEqual(self.ansagen, ["Der Timer ist abgelaufen."])
        self.assertEqual(self.lautsprecher, [])
        self.timer.tick()                          # nur einmal gemeldet
        self.assertEqual(len(self.ansagen), 1)

    def test_keine_senke_bestaetigt_lautsprecher_klingelt(self):
        self.senke_klingelt = False
        self.sag("Gaston, Timer fünf Minuten.")
        self.uhr.t += 300
        self.timer.tick()
        for th in threading.enumerate():
            if th.name == "timer-klingeln":
                th.join(1)
        self.assertEqual(self.lautsprecher, [3])

    def test_klingelt_von_selbst_aus_und_bleibt_sichtbar(self):
        self.sag("Gaston, Timer fünf Minuten.")
        self.uhr.t += 301
        self.timer.tick()
        self.assertTrue(self.timer.klingelt_gerade())
        self.uhr.t += 15                           # 3 x 5 s
        self.assertFalse(self.timer.klingelt_gerade())
        self.timer.tick()                          # Senken erfahren es als still
        t = self.timer.schnappschuss()["timer"][0]
        self.assertTrue(t["still"])
        self.assertLess(t["rest_s"], 0)            # zählt ins Negative

    def test_stopp_beendet_nur_das_klingeln(self):
        self.sag("Gaston, Timer fünf Minuten.")
        self.uhr.t += 300
        self.timer.tick()
        self.assertTrue(self.timer.klingeln_aus())
        self.assertFalse(self.timer.klingeln_aus())   # nichts klingelt mehr
        self.assertEqual(len(self.timer.schnappschuss()["timer"]), 1)

    def test_nachlauf_max_entfernt(self):
        self.sag("Gaston, Timer fünf Minuten.")
        self.uhr.t += 300 + 601
        self.timer.tick()
        self.assertEqual(self.timer.schnappschuss()["timer"], [])

    def test_ohne_senken_klingelt_lautsprecher(self):
        self.timer = self._neu(senken=False)
        self.timer.stellen(None, 10)
        self.uhr.t += 10
        self.timer.tick()
        for th in threading.enumerate():
            if th.name == "timer-klingeln":
                th.join(1)
        self.assertEqual(self.lautsprecher, [3])


class NeustartTest(Grundlage):
    def test_ueberlebt_neustart(self):
        self.timer.stellen("Pizza", 720)
        self.uhr.t += 100
        neu = self._neu()
        self.assertEqual(neu.schnappschuss()["timer"][0]["rest_s"], 620)

    def test_waehrend_neustart_abgelaufen(self):
        self.timer.stellen("Pizza", 60)
        self.timer.stellen(None, 60)
        with open(self.pfad) as f:
            self.assertEqual(len(json.load(f)["timer"]), 2)
        self.uhr.t += 60 + 30                      # kurz vorbei: klingelt noch
        neu = self._neu()
        neu.tick()
        self.assertEqual(len(self.ansagen), 2)
        # Klingelzeit zählt ab dem Bemerken, nicht ab dem Ablauf — sonst
        # wäre sie nach 30 s Neustart schon vorbei, bevor es klingelt.
        self.assertTrue(neu.klingelt_gerade())
        self.ansagen.clear()
        self.uhr.t += 300                          # lange vorbei: nur anzeigen
        spaet = self._neu()
        spaet.tick()
        self.assertEqual(self.ansagen, [])
        self.assertEqual(len(spaet.schnappschuss()["timer"]), 2)


class TextTest(unittest.TestCase):
    def test_dauer_text(self):
        self.assertEqual(dauer_text(480), "acht Minuten")
        self.assertEqual(dauer_text(60), "eine Minute")
        self.assertEqual(dauer_text(4200), "eine Stunde zehn Minuten")
        self.assertEqual(dauer_text(30), "dreißig Sekunden")
        self.assertEqual(dauer_text(-150), "zwei Minuten dreißig Sekunden")


if __name__ == "__main__":
    unittest.main()
