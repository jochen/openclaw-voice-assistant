"""Offline-Tests fuer die Torfrage und die kompakte Intent-Grammatik.

Laeuft OHNE Netz und ohne LLM: die Antworten des llama-servers werden
nachgebaut, geprueft wird, was der Aktuator daraus macht.

Ausfuehren:
    ow-venv/bin/python tests/test_actuator_tor.py

Warum es diese Datei gibt
-------------------------
Die Torfrage (2026-09-23) ist eine zusaetzliche Stelle, an der ein Satz zum
Brain abbiegt. Die Bruchlinie ist dieselbe wie in test_actuator_verdict.py,
nur umgekehrt: faellt die Torfrage aus (Timeout, kaputte Antwort), darf der
Satz NICHT geschaltet werden — er geht an den Brain. Und ein Tor, das "ja"
sagt, darf nichts entscheiden, was nicht danach noch classify() und
verdict() durchlaeuft.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import ActuatorConfig  # noqa: E402
from voice_assistant.services.actuator import (  # noqa: E402
    Actuator,
    _intent_grammatik,
)

CAPS = {
    "version": "test1",
    "ziele": [
        {"id": "flurlicht", "namen": ["Flurlicht"], "typ": "licht", "aktionen": ["ein", "aus"]},
        {"id": "tuerrollo", "namen": ["Türrollo"], "typ": "rollo",
         "aktionen": ["auf", "zu", "setzen"], "wert": {"einheit": "prozent", "min": 0, "max": 100}},
        {"id": "alle_rollos", "namen": ["alle Rollos"], "typ": "rollo",
         "aktionen": ["auf", "zu", "setzen"], "wert": {"einheit": "prozent", "min": 0, "max": 100},
         "mitglieder": ["tuerrollo"]},
    ],
}


def _actuator(tor_enabled: bool = True) -> Actuator:
    act = Actuator(ActuatorConfig(enabled=True, token_file="/nonexistent",
                                  tor_enabled=tor_enabled))
    act._fetch_capabilities = lambda: CAPS
    assert act.refresh()
    return act


def _antwort(content: str, top: list[tuple[str, float]] | None) -> io.BytesIO:
    choice = {"message": {"content": content}}
    if top is not None:
        choice["logprobs"] = {"content": [{"top_logprobs": [
            {"token": t, "logprob": math.log(p)} for t, p in top]}]}
    return io.BytesIO(json.dumps({"choices": [choice]}).encode())


class TorPromptTest(unittest.TestCase):

    def test_tor_prompt_nur_wenn_eingeschaltet(self) -> None:
        self.assertIsNone(_actuator(tor_enabled=False).tor_prompt)
        self.assertIsNotNone(_actuator(tor_enabled=True).tor_prompt)

    def test_tor_prompt_traegt_ziele_und_mehrzahl_regel(self) -> None:
        """Die Mehrzahl-Regel der Torfrage ist eine andere als die der
        Klassifikation: "die Rollos zu" muss durchs Tor, sonst erreicht der
        Satz die lokale Mehrzahl-Regel nie."""
        p = _actuator().tor_prompt
        self.assertIn("- tuerrollo:", p)
        self.assertNotIn("{ziel_liste}", p)
        self.assertNotIn("{gruppen_regel}", p)
        self.assertIn('Die Mehrzahl "Rollos" ist ja', p)


class TorUrteilTest(unittest.TestCase):

    def setUp(self) -> None:
        self.act = _actuator()

    def _tor(self, antwort):
        with mock.patch("urllib.request.urlopen", return_value=antwort):
            return self.act.tor("egal")

    def test_ja_mit_p(self) -> None:
        u = self._tor(_antwort("ja", [("ja", 0.9), ("ne", 0.1)]))
        self.assertTrue(u.ja)
        self.assertAlmostEqual(u.p_ja, 0.9, places=3)

    def test_nein_ist_zwei_token_gewertet_am_ersten(self) -> None:
        """'nein' ist bei Gemma ne+in — P(ja) muss 'ne' als Gegenstueck zaehlen."""
        u = self._tor(_antwort("nein", [("ne", 0.98), ("ja", 0.02)]))
        self.assertIs(u.ja, False)
        self.assertAlmostEqual(u.p_ja, 0.02, places=3)

    def test_ohne_logprobs_entscheidet_das_token(self) -> None:
        u = self._tor(_antwort("ja", None))
        self.assertTrue(u.ja)
        self.assertIsNone(u.p_ja)

    def test_ausfall_ist_kein_ja(self) -> None:
        """Timeout/Netzfehler: ja=None. Der Aufrufer behandelt das wie nein —
        der Satz geht an den Brain, nie ungeprueft an classify()."""
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            u = self.act.tor("Mach das Flurlicht an")
        self.assertIsNone(u.ja)
        self.assertFalse(bool(u.ja))
        self.assertIn("timed out", u.fehler)

    def test_unerwartete_antwort_ist_kein_ja(self) -> None:
        u = self._tor(_antwort("vielleicht", [("ja", 0.5), ("ne", 0.5)]))
        self.assertIsNone(u.ja)

    def test_ohne_prompt_kein_aufruf(self) -> None:
        act = _actuator(tor_enabled=False)
        with mock.patch("urllib.request.urlopen") as m:
            u = act.tor("egal")
        m.assert_not_called()
        self.assertIsNone(u.ja)


class IntentGrammatikTest(unittest.TestCase):

    def test_classify_nutzt_grammatik_statt_schema(self) -> None:
        act = _actuator()
        self.assertIn("grammar", act.request_template)
        self.assertNotIn("response_format", act.request_template)

    def test_grammatik_kennt_jede_id_als_json_string(self) -> None:
        g = _intent_grammatik(["flurlicht", "tuerrollo"], ["ein", "zu"], ["prozent"])
        # GBNF-Literal fuer den JSON-String "flurlicht"
        self.assertIn(r'"\"flurlicht\""', g)
        self.assertIn(r'"\"tuerrollo\""', g)
        self.assertIn(r'"\"\""', g)   # leeres Ziel fuer ist_kommando=false
        # kompakt: kein Leerraum zwischen den Feldern erlaubt
        self.assertIn(r'",\"ziel\":"', g)
        self.assertNotIn(" space", g)

    def test_classify_parst_kompaktes_json(self) -> None:
        act = _actuator()
        roh = json.dumps({"choices": [{"message": {"content":
              '{"ist_kommando":true,"aktion":"setzen","ziel":"tuerrollo","wert":40,"einheit":"prozent"}'}}]})
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(roh.encode())):
            intent = act.classify("Türrollo auf 40 Prozent")
        self.assertEqual(intent["ziel"], "tuerrollo")
        self.assertEqual(intent["wert"], 40)


if __name__ == "__main__":
    unittest.main()
