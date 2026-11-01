"""Argus-Taktung (seit 2026-11-01): sofort nur bei Anlass, Rest im Tageslauf.

Ohne Netz, ohne Modelle: das LLM-Urteil ist ersetzt. Was hier festgehalten
wird, sind die Wege, auf denen ein Turn sonst still ungeprüft bliebe —
LLM-Ausfall, fehlender Schatten, stille Stunden, Wasserstand.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import aktuator_schatten, watcher  # noqa: E402


def _turn(i, ts="2026-11-02T10:00:00", status="ausgefuehrt", kommando=True):
    return {"ts": ts, "phase": "intent", "request_id": f"r{i}", "transcript": f"Satz {i}",
            "intent": {"ist_kommando": kommando, "ziel": "x", "aktion": "ein"},
            "status": status, "ausgefuehrt": {"ziel": "x", "aktion": "ein"}}


class Basis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.turns = os.path.join(self.tmp, "turns.log")
        self.gesendet = []
        self.urteile = {}            # transcript -> Rückgabe von _llm_pruefe
        self.aufrufe = []
        self._alt = (watcher._llm_pruefe, watcher.WATCH_PATH)

        def fake(turn, *a, **k):
            self.aufrufe.append(turn["transcript"])
            return self.urteile.get(turn["transcript"])
        watcher._llm_pruefe = fake
        watcher.WATCH_PATH = os.path.join(self.tmp, "watch.jsonl")

    def tearDown(self):
        watcher._llm_pruefe, watcher.WATCH_PATH = self._alt

    def overseer(self, uneinig=None, stand=None, **kw):
        o = watcher.Overseer(
            chat_id="c", bot_token="b", quiet_start=kw.pop("quiet_start", 3),
            quiet_end=kw.pop("quiet_end", 3), llm_url="u", llm_model="m",
            schatten_uneinig=uneinig, checkpoint=stand,
            zustand_pfad=os.path.join(self.tmp, "z.json"),
            geprueft_pfad=os.path.join(self.tmp, "g.jsonl"),
            turns_pfad=self.turns, **kw)
        o._send_telegram = self.gesendet.append
        return o

    def schreibe_turns(self, turns):
        with open(self.turns, "a") as f:
            for t in turns:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")


class SofortTest(Basis):
    def test_einig_wartet_auf_den_tageslauf(self):
        o = self.overseer(uneinig=lambda t: False)
        o._check_turn_sync(_turn(1))
        self.assertEqual(self.aufrufe, [])

    def test_uneinig_prueft_sofort_und_meldet(self):
        self.urteile["Satz 1"] = {"art": "LLM_MISMATCH", "detail": "passt nicht"}
        o = self.overseer(uneinig=lambda t: True)
        o._check_turn_sync(_turn(1))
        self.assertEqual(self.aufrufe, ["Satz 1"])
        self.assertEqual(len(self.gesendet), 1)
        self.assertIn("uneinig", self.gesendet[0])

    def test_ohne_schatten_kein_sofort(self):
        o = self.overseer(uneinig=None)
        o._check_turn_sync(_turn(1))
        self.assertEqual(self.aufrufe, [])

    def test_kein_kommando_wird_nie_geprueft(self):
        o = self.overseer(uneinig=lambda t: True)
        o._check_turn_sync(_turn(1, kommando=False))
        self.assertEqual(self.aufrufe, [])

    def test_stille_stunden_ueberlassen_es_dem_tageslauf(self):
        o = self.overseer(uneinig=lambda t: True, quiet_start=0, quiet_end=24)
        o._check_turn_sync(_turn(1))
        self.assertEqual(self.aufrufe, [])

    def test_llm_ausfall_meldet_nicht_und_laesst_offen(self):
        self.urteile["Satz 1"] = {"art": "LLM_ERROR", "detail": "HTTP 400"}
        o = self.overseer(uneinig=lambda t: True)
        o._check_turn_sync(_turn(1))
        self.assertEqual(self.gesendet, [])
        self.schreibe_turns([_turn(1, ts="2099-01-01T00:00:00")])
        self.assertEqual(len(o._offene_turns()), 1)


class CheckpointFensterTest(Basis):
    def test_neuer_stand_oeffnet_fenster_fuer_n_ausgefuehrte(self):
        stand = ["v9@abc"]
        o = self.overseer(uneinig=lambda t: False, stand=lambda: stand[0], fenster=2)
        o._check_turn_sync(_turn(1))
        o._check_turn_sync(_turn(2, status="abgelehnt"))   # zählt nicht
        o._check_turn_sync(_turn(3))
        o._check_turn_sync(_turn(4))                        # Fenster zu
        self.assertEqual(self.aufrufe, ["Satz 1", "Satz 2", "Satz 3"])
        stand[0] = "v10@abc"
        o._check_turn_sync(_turn(5))
        self.assertEqual(self.aufrufe[-1], "Satz 5")

    def test_fenster_ueberlebt_neustart(self):
        o = self.overseer(stand=lambda: "v9@abc", fenster=3)
        o._check_turn_sync(_turn(1))
        o2 = self.overseer(stand=lambda: "v9@abc", fenster=3)
        self.assertEqual(o2._zustand["fenster_rest"], 2)

    def test_stand_nicht_abfragbar_setzt_nichts_neu(self):
        o = self.overseer(stand=lambda: None, uneinig=lambda t: False)
        o._check_turn_sync(_turn(1))
        self.assertEqual(self.aufrufe, [])


class TageslaufTest(Basis):
    def test_erster_start_prueft_kein_archiv(self):
        self.schreibe_turns([_turn(1, ts="2020-01-01T00:00:00")])
        o = self.overseer()
        self.assertEqual(o.tageslauf()["offen"], 0)

    def test_prueft_nur_was_sofort_nicht_dran_war(self):
        o = self.overseer(uneinig=lambda t: t == "Satz 1")
        o._zustand["wasserstand"] = "2026-11-02T00:00:00"
        self.schreibe_turns([_turn(1), _turn(2), _turn(3, kommando=False)])
        o._check_turn_sync(_turn(1))
        self.aufrufe.clear()
        r = o.tageslauf(jetzt=datetime(2026, 11, 2, 22))
        self.assertEqual(self.aufrufe, ["Satz 2"])
        self.assertEqual(r["offen"], 1)
        self.assertEqual(o.tageslauf(jetzt=datetime(2026, 11, 2, 22))["offen"], 0)

    def test_ausfall_einmal_gemeldet_und_morgen_erneut(self):
        o = self.overseer()
        o._zustand["wasserstand"] = "2026-11-02T00:00:00"
        self.schreibe_turns([_turn(1), _turn(2, ts="2026-11-02T11:00:00")])
        self.urteile = {"Satz 1": {"art": "LLM_ERROR", "detail": "HTTP 400"},
                        "Satz 2": {"art": "LLM_ERROR", "detail": "HTTP 400"}}
        o.tageslauf(jetzt=datetime(2026, 11, 2, 22))
        self.assertEqual(len(self.gesendet), 1)
        self.assertIn("2 von 2", self.gesendet[0])
        self.assertEqual(o._zustand["wasserstand"], "2026-11-02T10:00:00")
        self.urteile = {}
        self.gesendet.clear()
        self.assertEqual(o.tageslauf(jetzt=datetime(2026, 11, 3, 22))["offen"], 2)
        self.assertEqual(self.gesendet, [])

    def test_befund_mit_kopf(self):
        o = self.overseer()
        o._zustand["wasserstand"] = "2026-11-02T00:00:00"
        self.schreibe_turns([_turn(1)])
        self.urteile["Satz 1"] = {"art": "LLM_MISMATCH", "detail": "d"}
        o.tageslauf(jetzt=datetime(2026, 11, 2, 22))
        self.assertTrue(self.gesendet[0].startswith("📅 Tagesprüfung"))

    def test_ruhiger_tag_bleibt_still_ausser_sonntags(self):
        o = self.overseer()
        o.tageslauf(jetzt=datetime(2026, 11, 2, 22))      # Montag
        self.assertEqual(self.gesendet, [])
        o.tageslauf(jetzt=datetime(2026, 11, 8, 22))      # Sonntag
        self.assertEqual(len(self.gesendet), 1)
        self.assertIn("Argus lebt", self.gesendet[0])
        o.tageslauf(jetzt=datetime(2026, 11, 8, 22))      # nicht zweimal
        self.assertEqual(len(self.gesendet), 1)


class SchattenUrteilTest(unittest.TestCase):
    def test_ohne_anmeldung_none(self):
        self.assertIsNone(aktuator_schatten.schatten_uneinig("nie gesagt", timeout=0.01))

    def test_wartet_auf_den_schatten(self):
        aktuator_schatten._urteil_anmelden("Licht an")
        threading.Timer(0.05, aktuator_schatten._urteil_setzen, ("Licht an", True)).start()
        self.assertTrue(aktuator_schatten.schatten_uneinig("Licht an", timeout=2))
        self.assertIsNone(aktuator_schatten.schatten_uneinig("Licht an", timeout=0.01))

    def test_schatten_zu_langsam_none(self):
        aktuator_schatten._urteil_anmelden("Rollo zu")
        self.assertIsNone(aktuator_schatten.schatten_uneinig("Rollo zu", timeout=0.05))


if __name__ == "__main__":
    unittest.main()
