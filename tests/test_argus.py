"""Argus (Überwacher) mit Weltmodell — Prompt-Bau, Antwort-Lesen, Versions-Cache.

Ohne Netz, ohne Modelle. Was ein besserer Prompt am Urteil ändert, misst
nicht dieser Test, sondern tools/argus_replay.py gegen das echte Modell.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import watcher  # noqa: E402
from voice_assistant.services.haus_mcp import (  # noqa: E402
    HausMcpError, Weltmodell, _antwort_lesen,
)


class FakeMcp:
    def __init__(self, version="v1", modell=None, kaputt=False):
        self.version = version
        self.modell = modell or {"version": version, "raeume": []}
        self.kaputt = kaputt
        self.voll_geladen = 0

    def tool(self, name, arguments=None):
        if self.kaputt:
            raise HausMcpError("haus_weltmodell: HTTP 401")
        if (arguments or {}).get("nur_version"):
            return json.dumps({"version": self.version})
        self.voll_geladen += 1
        return json.dumps({**self.modell, "version": self.version})


class WeltmodellCacheTest(unittest.TestCase):
    def test_laedt_nur_bei_neuer_version(self):
        mcp = FakeMcp()
        w = Weltmodell(mcp)
        self.assertEqual(w.aktuell()[0], "v1")
        w.aktuell()
        self.assertEqual(mcp.voll_geladen, 1)
        mcp.version = "v2"
        self.assertEqual(w.aktuell()[0], "v2")
        self.assertEqual(mcp.voll_geladen, 2)

    def test_ausfall_behaelt_letztes_modell_und_sagt_warum(self):
        mcp = FakeMcp()
        w = Weltmodell(mcp)
        w.aktuell()
        mcp.kaputt = True
        version, text, fehler = w.aktuell()
        self.assertEqual(version, "v1")
        self.assertTrue(text)
        self.assertIn("401", fehler)

    def test_ausfall_ohne_je_ein_modell(self):
        version, text, fehler = Weltmodell(FakeMcp(kaputt=True)).aktuell()
        self.assertIsNone(version)
        self.assertEqual(text, "")
        self.assertTrue(fehler)


class PromptTest(unittest.TestCase):
    def test_weltmodell_steht_am_ende(self):
        p = watcher.system_prompt('{"version":"x"}')
        self.assertTrue(p.rstrip().endswith('{"version":"x"}'))
        # gedanke vor ok: erst begründen, dann urteilen
        self.assertLess(p.index('"gedanke"'), p.index('"ok"'))

    def test_ohne_weltmodell_ein_hinweis_statt_leere(self):
        self.assertIn("nicht verfügbar", watcher.system_prompt(None))

    def test_kontext(self):
        turn = {"transcript": "Licht an", "intent": {"ziel": "x"},
                "status": "ausgefuehrt", "ausgefuehrt": {"ziel": "x"}}
        self.assertIn("Erstansprache", watcher.user_nachricht(turn))
        self.assertIn("Rückfrage", watcher.user_nachricht({**turn, "unklar_round": 1}))
        v1 = watcher.user_nachricht(turn, mit_kontext=False)
        self.assertNotIn("Status", v1)


class AntwortLesenTest(unittest.TestCase):
    def test_reihenfolge_egal_und_text_drumherum(self):
        r = watcher.antwort_lesen('Hier: {"gedanke": "passt {nicht}", "ok": false, "grund": "g"} fertig')
        self.assertFalse(r["ok"])
        self.assertEqual(r["gedanke"], "passt {nicht}")

    def test_sse_antwort_des_mcp(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"x":1}}\n\n'
        self.assertEqual(_antwort_lesen(body, 3)["result"]["x"], 1)
        self.assertIsNone(_antwort_lesen(body, 4))


class OverseerTest(unittest.TestCase):
    def _lauf(self, weltmodell):
        gesehen = {}

        def fake_pruefe(turn, *a, system=None, user=None):
            gesehen["system"], gesehen["user"] = system, user
            return {"art": "LLM_MISMATCH", "detail": "d"}

        alt_pruefe, alt_path = watcher._llm_pruefe, watcher.WATCH_PATH
        watcher._llm_pruefe = fake_pruefe
        watcher.WATCH_PATH = os.devnull
        try:
            o = watcher.Overseer(chat_id="", bot_token="", quiet_start=0, quiet_end=24,
                                 llm_url="u", llm_model="m", weltmodell=weltmodell)
            o._send_telegram = lambda msg: None
            o._check_turn_sync({"transcript": "t", "intent": {"ist_kommando": True}})
        finally:
            watcher._llm_pruefe, watcher.WATCH_PATH = alt_pruefe, alt_path
        return gesehen

    def test_ohne_weltmodell_bleibt_v1(self):
        g = self._lauf(None)
        self.assertIsNone(g["system"])
        self.assertIsNone(g["user"])

    def test_mit_weltmodell_v2(self):
        g = self._lauf(Weltmodell(FakeMcp(modell={"raeume": ["wohnzimmer"]})))
        self.assertIn("wohnzimmer", g["system"])
        self.assertIn("Erstansprache", g["user"])


if __name__ == "__main__":
    unittest.main()
