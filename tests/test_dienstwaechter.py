"""Dienst-Waechter — ohne Netz, ohne Podman, mit falscher Uhr.

Haelt den Vorfall vom 2026-10-06 fest: Qwen-STT und Laya waren 15 Stunden
weg, die Rueckfall-Ketten haben es verdeckt, niemand hat es gemerkt.
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services.dienstwaechter import (  # noqa: E402
    Dienst, DienstWaechter, health_url,
)

URL = "http://127.0.0.1:8094/health"


class Welt:
    """Uhr, Dienst und Container zum Hineinschauen."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.dienst_ok = True
        self.heilt = True               # bringt ein Neustart den Dienst zurueck?
        self.container: str | None = "llamacpp-qwenasr"
        self.training = False
        self.stunde = 12
        self.gesendet: list[str] = []
        self.geheilt: list[str] = []

    def pruefe(self, url: str) -> str | None:
        return None if self.dienst_ok else "[Errno 111] Connection refused"

    def heile(self, name: str) -> str:
        self.geheilt.append(name)
        if self.heilt:
            self.dienst_ok = True
        return "gestoppt, gestartet"

    def schlafe(self, s: float) -> None:
        self.t += s

    def waechter(self, **kw) -> DienstWaechter:
        return DienstWaechter(
            [Dienst("Qwen-STT", URL)], self.gesendet.append,
            gnadenfrist=180, heil_abstand=1800, max_heilversuche=2, heil_warte=30,
            ruhe_units=("laya-nachtraining.service",),
            pruefe=self.pruefe, finde_container=lambda url: self.container,
            heile=self.heile, ruhe=lambda unit: self.training,
            jetzt=lambda: self.t, uhr=lambda: datetime(2026, 10, 6, self.stunde),
            schlafe=self.schlafe, **kw,
        )

    def runden(self, w: DienstWaechter, n: int) -> None:
        for _ in range(n):
            w.schritt()
            self.t += 60


class GnadenfristTest(unittest.TestCase):
    def test_kurzer_ausfall_meldet_nichts(self) -> None:
        welt = Welt()
        w = welt.waechter()
        welt.dienst_ok = False
        welt.runden(w, 3)               # 0, 60, 120 s — unter der Frist
        welt.dienst_ok = True
        welt.runden(w, 1)
        self.assertEqual(welt.gesendet, [])
        self.assertEqual(welt.geheilt, [])


class HeilenTest(unittest.TestCase):
    def test_ausfall_wird_geheilt_und_in_einer_meldung_berichtet(self) -> None:
        welt = Welt()
        w = welt.waechter()
        welt.dienst_ok = False
        welt.runden(w, 5)
        self.assertEqual(welt.geheilt, ["llamacpp-qwenasr"])
        self.assertEqual(len(welt.gesendet), 1)
        self.assertIn("nicht erreichbar", welt.gesendet[0])
        self.assertIn("wieder erreichbar", welt.gesendet[0])

    def test_erfolgloses_heilen_hat_abstand_und_gibt_auf(self) -> None:
        welt = Welt()
        welt.heilt = False
        w = welt.waechter()
        welt.dienst_ok = False
        welt.runden(w, 10)              # ~10 min: genau ein Versuch
        self.assertEqual(len(welt.geheilt), 1)
        welt.runden(w, 30)              # nach 30 min der zweite, dann Schluss
        welt.runden(w, 60)
        self.assertEqual(len(welt.geheilt), 2)
        self.assertIn("gebe auf", welt.gesendet[-1])
        welt.dienst_ok = True
        welt.runden(w, 1)
        self.assertIn("wieder erreichbar", welt.gesendet[-1])

    def test_ohne_container_nur_melden(self) -> None:
        welt = Welt()
        welt.container = None
        w = welt.waechter()
        welt.dienst_ok = False
        welt.runden(w, 10)
        self.assertEqual(welt.geheilt, [])
        self.assertEqual(len(welt.gesendet), 1)     # einmal, nicht jede Runde
        self.assertIn("Kein lokaler Container", welt.gesendet[0])

    def test_heilen_aus_meldet_nur(self) -> None:
        welt = Welt()
        w = welt.waechter(heilen=False)
        welt.dienst_ok = False
        welt.runden(w, 10)
        self.assertEqual(welt.geheilt, [])
        self.assertEqual(len(welt.gesendet), 1)


class RuheTest(unittest.TestCase):
    def test_training_stoppt_container_absichtlich(self) -> None:
        # Gegenprobe zum Heilen: mitten im Training einen Container zu
        # starten, verdraengte das Training aus dem Grafikspeicher.
        welt = Welt()
        w = welt.waechter()
        welt.training = True
        welt.dienst_ok = False
        welt.runden(w, 30)
        self.assertEqual((welt.gesendet, welt.geheilt), ([], []))
        # Danach zaehlt die Frist neu: die erste Runde meldet nicht sofort.
        welt.training = False
        welt.runden(w, 2)
        self.assertEqual(welt.gesendet, [])
        welt.runden(w, 3)
        self.assertEqual(len(welt.geheilt), 1)

    def test_stille_stunden_heilen_aber_sammeln(self) -> None:
        welt = Welt()
        welt.stunde = 3
        w = welt.waechter()
        welt.dienst_ok = False
        welt.runden(w, 5)
        self.assertEqual(len(welt.geheilt), 1)
        self.assertEqual(welt.gesendet, [])
        welt.stunde = 7
        welt.runden(w, 1)
        self.assertEqual(len(welt.gesendet), 1)
        self.assertIn("stillen Zeit gesammelt", welt.gesendet[0])


class ContainerFuerUrlTest(unittest.TestCase):
    """Form wie `podman ps -a --format json` (gekuerzt, 2026-10-06)."""

    PS = [
        {"Names": ["llamacpp-gemma"], "State": "exited",
         "Ports": [{"host_ip": "", "host_port": 8090}]},
        {"Names": ["llamacpp-gemma-vega"], "State": "running",
         "Ports": [{"host_ip": "", "host_port": 8091}]},
        {"Names": ["alt-8091"], "State": "exited",
         "Ports": [{"host_ip": "", "host_port": 8091}]},
        {"Names": ["laya"], "State": "running",
         "Ports": [{"host_ip": "127.0.0.1", "host_port": 8096}]},
        {"Names": ["fremd"], "State": "running",
         "Ports": [{"host_ip": "192.168.1.5", "host_port": 8787}]},
        {"Names": ["x1"], "State": "exited", "Ports": [{"host_ip": "", "host_port": 9000}]},
        {"Names": ["x2"], "State": "exited", "Ports": [{"host_ip": "", "host_port": 9000}]},
    ]

    def setUp(self) -> None:
        import json
        import subprocess
        from voice_assistant.services import container
        self.c = container
        self._run, self._da = container._run, container.verfuegbar
        container._run = lambda *cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps(self.PS), "")
        container.verfuegbar = lambda: True

    def tearDown(self) -> None:
        self.c._run, self.c.verfuegbar = self._run, self._da

    def test_zuordnung(self) -> None:
        f = self.c.container_fuer_url
        self.assertEqual(f("http://127.0.0.1:8096/health"), "laya")
        self.assertEqual(f("http://localhost:8091/health"), "llamacpp-gemma-vega")  # laufender gewinnt
        self.assertEqual(f("http://127.0.0.1:8090/health"), "llamacpp-gemma")      # eindeutig gestoppt
        self.assertIsNone(f("http://127.0.0.1:9000/health"))   # zwei gestoppte: nicht raten
        self.assertIsNone(f("http://127.0.0.1:8787/health"))   # nur auf fremder IP
        self.assertIsNone(f("http://speaches-host:8000/health"))  # nicht lokal


class HealthUrlTest(unittest.TestCase):
    def test_aus_dienst_url(self) -> None:
        self.assertEqual(health_url("http://localhost:8091/v1/chat/completions"),
                         "http://localhost:8091/health")
        self.assertEqual(health_url("http://127.0.0.1:8094"), "http://127.0.0.1:8094/health")


if __name__ == "__main__":
    unittest.main()
