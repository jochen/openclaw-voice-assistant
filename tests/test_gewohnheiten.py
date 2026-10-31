"""Gewohnheiten als Hinweis an den Brain (services/gewohnheiten.py, 2026-10-31).

Anlass 2026-10-30 07:19: „Gaston, mach die Rollos auf“ — Laya: `auf`, kein
Ziel; Rückfrage des Aktuators, dann noch eine des Brains. Morgens war an 30
von 30 Tagen „alle Rollos“ gemeint. Gesperrt wird hier vor allem, wann es
KEINEN Hinweis gibt: dann bleibt die Rückfrage wie vorher.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import ActuatorConfig, GewohnheitenConfig, LocaleConfig, _parse_profile  # noqa: E402
from voice_assistant.mcp_actuator import GastonMCP, _call_tool  # noqa: E402
from voice_assistant.services import gewohnheiten  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402

# 07:20 MEZ an einem Werktag
MORGEN = datetime(2026, 11, 2, 6, 20, tzinfo=timezone.utc)
OHNE_ZIEL = {"ist_kommando": True, "aktion": "auf", "ziel": None, "wert": None, "einheit": None}


def _datei(**ueber):
    g = {"id": "rollos_auf_morgens", "geraetetyp": "rollo", "aktion": "auf",
         "gruppe": "alle_rollos", "fenster_beginn": "06:51", "fenster_ende": "07:55",
         "fenster_tz": "Europe/Berlin", "tage": 30, "treffer": 30, "konfidenz": 1.0}
    g.update(ueber.pop("g", {}))
    daten = {"schema": "hauswacht.gewohnheiten.v3",
             "letzte_berechnung": "2026-10-31T08:06:33+00:00",
             "gaston_gruppen": {"alle_rollos": [], "rollos_ganzes_haus": []},
             "gewohnheiten": [g]}
    daten.update(ueber)
    return daten


class FindenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pfad = os.path.join(self.tmp.name, "gewohnheiten.json")
        self.cfg = GewohnheitenConfig(enabled=True, datei=self.pfad)

    def _finden(self, daten=None, text="Gaston, mach die Rollos auf.",
                intent=OHNE_ZIEL, jetzt=MORGEN):
        with open(self.pfad, "w") as f:
            json.dump(daten if daten is not None else _datei(), f)
        return gewohnheiten.finden(self.cfg, text, intent, jetzt)

    def test_der_fall_vom_30_10(self):
        g, grund = self._finden()
        self.assertEqual(g.gruppe, "alle_rollos")
        self.assertEqual((g.treffer, g.tage), (30, 30))
        self.assertEqual(grund, "rollos_auf_morgens")

    def test_stt_dreier(self):
        g, _ = self._finden(text="Die Rolllos aufmachen.")
        self.assertIsNotNone(g)

    def test_mit_ort_nicht(self):
        g, grund = self._finden(text="Mach die Küchenrollos auf.")
        self.assertIsNone(g)
        self.assertEqual(grund, "kein Gerätewort")

    def test_kein_rollo_nicht(self):
        self.assertIsNone(self._finden(text="Mach das Licht an.")[0])

    def test_ausserhalb_des_fensters(self):
        abends = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)   # 10:30 MEZ
        self.assertIsNone(self._finden(jetzt=abends)[0])

    def test_andere_aktion(self):
        zu = dict(OHNE_ZIEL, aktion="zu")
        self.assertIsNone(self._finden(intent=zu)[0])

    def test_konfidenz_zu_schwach(self):
        g, grund = self._finden(_datei(g={"treffer": 21, "konfidenz": 0.7}))
        self.assertIsNone(g)
        self.assertIn("zu schwach", grund)

    def test_zu_wenige_tage(self):
        self.assertIsNone(self._finden(_datei(g={"tage": 5, "treffer": 5}))[0])

    def test_datei_alt(self):
        g, grund = self._finden(_datei(letzte_berechnung="2026-10-01T06:00:00+00:00"))
        self.assertIsNone(g)
        self.assertIn("Tage alt", grund)

    def test_fremdes_schema(self):
        self.assertIsNone(self._finden(_datei(schema="hauswacht.gewohnheiten.v9"))[0])

    def test_pflichtfeld_fehlt(self):
        d = _datei()
        del d["gewohnheiten"][0]["fenster_tz"]
        self.assertIsNone(self._finden(d)[0])

    def test_datei_fehlt_oder_kaputt(self):
        self.assertIsNone(gewohnheiten.finden(self.cfg, "Rollos auf", OHNE_ZIEL, MORGEN)[0])
        with open(self.pfad, "w") as f:
            f.write("{halb")
        self.assertIsNone(gewohnheiten.finden(self.cfg, "Rollos auf", OHNE_ZIEL, MORGEN)[0])

    def test_unbelegte_gruppe_zaehlt_wie_ohne_ziel(self):
        # Gemma wählt die Gruppe, Regel A lehnt ab: derselbe Satz
        g, _ = self._finden(intent=dict(OHNE_ZIEL, ziel="rollos_ganzes_haus"))
        self.assertEqual(g.gruppe, "alle_rollos")

    def test_einzelziel_nicht(self):
        self.assertIsNone(self._finden(intent=dict(OHNE_ZIEL, ziel="klorollo"))[0])

    def test_aus(self):
        self.cfg.enabled = False
        self.assertIsNone(self._finden()[0])


class HinweisTest(unittest.TestCase):
    def test_platzhalter(self):
        g = gewohnheiten.Gewohnheit("x", "alle_rollos", "auf", 30, 30, 1.0, "06:51", "07:55")
        h = LocaleConfig().gewohnheit_hinweis_fuer(g, "alle Rollos")
        for teil in ("alle Rollos", "alle_rollos", "30 von 30", "06:51", "07:55",
                     "ziel_erschlossen=true"):
            self.assertIn(teil, h)
        self.assertNotIn("{", h)

    def test_config(self):
        p = _parse_profile("t", {"gewohnheiten": {"enabled": True, "datei": "~/g.json",
                                                  "min_konfidenz": 0.7}})
        self.assertTrue(p.gewohnheiten.enabled)
        self.assertEqual(p.gewohnheiten.datei, os.path.expanduser("~/g.json"))
        self.assertEqual(p.gewohnheiten.min_konfidenz, 0.7)
        self.assertEqual(p.gewohnheiten.min_tage, 10)
        self.assertFalse(_parse_profile("t", {}).gewohnheiten.enabled)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class ZielErschlossenTest(unittest.TestCase):
    def setUp(self):
        self.bodies = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch("voice_assistant.config.ACTUATOR_LOG_PATH",
                       os.path.join(tmp.name, "actuator_turns.log"))
        p.start()
        self.addCleanup(p.stop)

        def fake(req, timeout=None):
            self.bodies.append(json.loads(req.data.decode()))
            return _Resp({"status": "ausgefuehrt"})
        u = mock.patch("urllib.request.urlopen", fake)
        u.start()
        self.addCleanup(u.stop)
        self.act = Actuator(ActuatorConfig(enabled=True, base_url="http://127.0.0.1:0",
                                           token_file="/nonexistent"))

    def test_ohne_feld_wie_bisher(self):
        self.act.execute({"ziel": "alle_rollos", "aktion": "auf"}, "r1")
        self.assertNotIn("ziel_erschlossen", self.bodies[-1])

    def test_brain_setzt_es(self):
        _call_tool(GastonMCP(self.act), "haus_schalten",
                   {"ziel": "alle_rollos", "aktion": "auf", "ziel_erschlossen": True})
        self.assertIs(self.bodies[-1]["ziel_erschlossen"], True)
        self.assertEqual(self.bodies[-1]["quelle"], "gaston")

    def test_nur_echtes_true(self):
        _call_tool(GastonMCP(self.act), "haus_schalten",
                   {"ziel": "alle_rollos", "aktion": "auf", "ziel_erschlossen": "ja"})
        self.assertNotIn("ziel_erschlossen", self.bodies[-1])


if __name__ == "__main__":
    unittest.main()
