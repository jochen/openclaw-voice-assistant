"""Offline-Tests fuer Actuator.verdict() — den Dreiweg-Dispatch.

Laeuft OHNE Netz, ohne Node-RED, ohne Klassifikations-LLM: geprueft wird nur
die Entscheidung ueber ein fertiges Intent-Dict gegen einen gesetzten Digest.

Ausfuehren:
    ow-venv/bin/python tests/test_actuator_verdict.py

Warum es diese Datei gibt
-------------------------
Der Vorfall vom 2026-08-01 12:33 entstand nicht daran, dass eine Pruefung
gefehlt haette — is_actionable() hat den Satz korrekt abgelehnt. Er entstand
daran, was DANACH mit dem abgelehnten Satz passierte: er ging an den Brain,
also an die Stelle mit freiem exec und Home-Assistant-Token, und der hat
geraten und alle dreizehn Rollos gefahren.

Die Regressionsgefahr liegt deshalb genau hier: verschmelzen "kein Kommando"
und "Kommando, aber nicht ausfuehrbar" wieder zu einem Zweig, ist das Loch
zurueck — und zwar lautlos, denn beide Faelle sehen im Journal gleich aus.
Der erste Test unten ist der Vorfall selbst, mit den echten Werten.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import ActuatorConfig  # noqa: E402
from voice_assistant.services.actuator import (  # noqa: E402
    VERDICT_AUSFUEHRBAR,
    VERDICT_KEIN_KOMMANDO,
    VERDICT_UNKLAR,
    Actuator,
)

# Ausschnitt aus einem echten /capabilities-Digest (Version eaf5c0b3).
# Wichtig ist allein der Unterschied: ein Licht kann ein/aus, ein Rollo
# zusaetzlich setzen. Genau daran ist der Vorfall gescheitert.
DIGEST = {
    "grosseszimmerlicht": {
        "namen": ["Licht im großen Zimmer"],
        "typ": "licht",
        "aktionen": ["ein", "aus"],
        "wert": None,
    },
    "tuerrollo": {
        "namen": ["Türrollo"],
        "typ": "rollo",
        "aktionen": ["auf", "zu", "setzen"],
        "wert": {"einheit": "prozent", "min": 0, "max": 100},
    },
}


def _actuator() -> Actuator:
    """Actuator ohne refresh(): Digest von Hand gesetzt, kein Netz noetig."""
    cfg = ActuatorConfig(enabled=True, base_url="http://127.0.0.1:0",
                         token_file="/nonexistent")
    act = Actuator(cfg)
    act.digest = dict(DIGEST)
    act.system_prompt = "(Test)"
    return act


class TestVerdict(unittest.TestCase):
    def setUp(self) -> None:
        self.act = _actuator()

    def test_vorfall_20260801_geht_nicht_an_den_brain(self) -> None:
        """Der Originalfall. STT machte aus "Gaston Türrollo 50%" das Wort
        "Tyrolo"; das Klassifikations-LLM lieferte dafuer stabil (5/5 Laeufe)
        grosseszimmerlicht/setzen. Ein Licht kennt kein setzen — das ist
        weder ausfuehrbar noch "kein Kommando", sondern unklar."""
        intent = {"ist_kommando": True, "ziel": "grosseszimmerlicht",
                  "aktion": "setzen", "wert": 50, "einheit": "prozent"}
        verdict, grund = self.act.verdict(intent)
        self.assertEqual(verdict, VERDICT_UNKLAR)
        self.assertNotEqual(verdict, VERDICT_KEIN_KOMMANDO)  # der Brain-Zweig
        self.assertIn("setzen", grund)
        self.assertIn("grosseszimmerlicht", grund)

    def test_gueltiges_kommando_ist_ausfuehrbar(self) -> None:
        intent = {"ist_kommando": True, "ziel": "tuerrollo",
                  "aktion": "setzen", "wert": 50, "einheit": "prozent"}
        self.assertEqual(self.act.verdict(intent)[0], VERDICT_AUSFUEHRBAR)

    def test_frage_geht_weiter_an_den_brain(self) -> None:
        """Der Regelfall des Brain-Pfads darf sich NICHT geaendert haben —
        sonst waere der Assistent fuer alles ausser Schalten tot."""
        intent = {"ist_kommando": False, "ziel": "", "aktion": None,
                  "wert": None, "einheit": None}
        self.assertEqual(self.act.verdict(intent)[0], VERDICT_KEIN_KOMMANDO)

    def test_classify_ausfall_geht_an_den_brain(self) -> None:
        """classify() liefert None bei LLM-Fehler/Timeout. Ohne Urteil laesst
        sich Frage nicht von Befehl trennen; bliebe der Turn hier haengen,
        waere bei jedem Ausfall des Klassifikators auch das normale Fragen
        tot. Bewusste Entscheidung, kein Versehen."""
        self.assertEqual(self.act.verdict(None)[0], VERDICT_KEIN_KOMMANDO)

    def test_unbekanntes_ziel_ist_unklar(self) -> None:
        intent = {"ist_kommando": True, "ziel": "gibtsnicht",
                  "aktion": "zu", "wert": None, "einheit": None}
        verdict, grund = self.act.verdict(intent)
        self.assertEqual(verdict, VERDICT_UNKLAR)
        self.assertIn("gibtsnicht", grund)

    def test_leeres_ziel_trotz_kommando_ist_unklar(self) -> None:
        """ziel="" ist laut Schema nur mit ist_kommando=false vorgesehen.
        Haelt sich das Modell nicht daran, ist das kein Freifahrtschein zum
        Brain."""
        intent = {"ist_kommando": True, "ziel": "", "aktion": "zu",
                  "wert": None, "einheit": None}
        self.assertEqual(self.act.verdict(intent)[0], VERDICT_UNKLAR)

    def test_fehlende_aktion_ist_unklar(self) -> None:
        intent = {"ist_kommando": True, "ziel": "tuerrollo", "aktion": None,
                  "wert": None, "einheit": None}
        self.assertEqual(self.act.verdict(intent)[0], VERDICT_UNKLAR)

    def test_ohne_digest_ist_unklar_nicht_brain(self) -> None:
        """Vor dem ersten refresh() ist der Digest None. Ein Schaltbefehl darf
        in diesem Fenster nicht zum Brain durchrutschen."""
        act = _actuator()
        act.digest = None
        intent = {"ist_kommando": True, "ziel": "tuerrollo", "aktion": "zu",
                  "wert": None, "einheit": None}
        self.assertEqual(act.verdict(intent)[0], VERDICT_UNKLAR)

    def test_verdict_deckt_is_actionable(self) -> None:
        """verdict() darf nie AUSFUEHRBAR sagen, wo is_actionable() False ist
        — sonst haette der Aktuator eine zweite, schwaechere Tuer."""
        faelle = [
            {"ist_kommando": True, "ziel": "grosseszimmerlicht", "aktion": "setzen"},
            {"ist_kommando": True, "ziel": "tuerrollo", "aktion": "setzen"},
            {"ist_kommando": True, "ziel": "tuerrollo", "aktion": None},
            {"ist_kommando": True, "ziel": "", "aktion": "zu"},
            {"ist_kommando": False, "ziel": "tuerrollo", "aktion": "zu"},
        ]
        for intent in faelle:
            with self.subTest(intent=intent):
                verdict = self.act.verdict(intent)[0]
                self.assertEqual(
                    verdict == VERDICT_AUSFUEHRBAR,
                    self.act.is_actionable(intent),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
