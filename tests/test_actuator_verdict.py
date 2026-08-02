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
    _gruppen_beleg,
)

# Ausschnitt aus einem echten /capabilities-Digest (Version eaf5c0b3).
# Wichtig ist allein der Unterschied: ein Licht kann ein/aus, ein Rollo
# zusaetzlich setzen. Genau daran ist der Vorfall gescheitert.
#
# `alle_rollos` ist eine Gruppe (mit `mitglieder`) — fuer Regel A. Ihre
# Namen reduzieren sich auf den Beleg {"rollos"}: "alle" ist ein Stopwort,
# "rollos" ist >=4 Zeichen. Genau dieser Beleg entscheidet, ob ein Satz die
# Gruppe rechtfertigt. "Tyrol" und "Rollo" (Einzahl) sind NICHT "rollos".
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
    "alle_rollos": {
        "namen": ["alle Rollos"],
        "typ": "rollo",
        "aktionen": ["auf", "zu", "setzen"],
        "wert": {"einheit": "prozent", "min": 0, "max": 100},
        "mitglieder": ["wohnzimmerrollo", "tuerrollo"],
    },
    # Gruppe, deren Namen nur Funktions-/Kurzwoerter liefern -> leere Beleg-
    # Menge. Fuer sie entfaellt die Pruefung (nicht ablehnen), siehe Regel A.
    "leere_gruppe": {
        "namen": ["alle"],
        "typ": "szene",
        "aktionen": ["aktivieren"],
        "wert": None,
        "mitglieder": ["tuerrollo"],
    },
}


def _actuator() -> Actuator:
    """Actuator ohne refresh(): Digest von Hand gesetzt, kein Netz noetig.

    gruppen_beleg wird hier aus DEMSELBEN Hand-Digest gebaut wie in refresh(),
    damit Regel A offline greift. Leere Mengen fallen raus (wie in refresh())."""
    cfg = ActuatorConfig(enabled=True, base_url="http://127.0.0.1:0",
                         token_file="/nonexistent")
    act = Actuator(cfg)
    act.digest = dict(DIGEST)
    act.gruppen_beleg = {
        zid: beleg for zid, z in DIGEST.items()
        if z.get("mitglieder") and (beleg := _gruppen_beleg(z))
    }
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


class TestRegelAGruppenbeleg(unittest.TestCase):
    """Regel A: ein Gruppen-Ziel muss im Satz belegt sein, sonst UNKLAR.

    Anlass 2026-08-02: 'Gastau Tyrol auf 40%' -> alle_rollos war laut Digest
    legal, verdict() sah nichts Falsches, neun Rollos fuhren. Ein unbekanntes
    Wort darf nie zu einer Gruppe werden. Siehe actuator.verdict() Docstring.
    """

    def setUp(self) -> None:
        self.act = _actuator()

    def test_gruppe_ohne_beleg_ist_unklar(self) -> None:
        """Der Vorfall: "Tyrol" ist kein Beleg fuer alle_rollos ({rollos})."""
        intent = {"ist_kommando": True, "ziel": "alle_rollos",
                  "aktion": "setzen", "wert": 40, "einheit": "prozent"}
        verdict, grund = self.act.verdict(intent, "Gastau Tyrol auf 40%")
        self.assertEqual(verdict, VERDICT_UNKLAR)
        self.assertIn("alle_rollos", grund)
        self.assertIn("rollos", grund)  # Beleg-Menge im Grund genannt

    def test_gruppe_mit_beleg_ist_ausfuehrbar(self) -> None:
        """Enthaelt der Satz das Gruppenwort, ist die Gruppe gerechtfertigt."""
        intent = {"ist_kommando": True, "ziel": "alle_rollos",
                  "aktion": "zu", "wert": None, "einheit": None}
        self.assertEqual(
            self.act.verdict(intent, "Mach die Rollos zu")[0], VERDICT_AUSFUEHRBAR
        )

    def test_einzelziel_ist_unberuehrt(self) -> None:
        """Regel A greift NUR bei Gruppen (mitglieder). Ein Einzelziel wird
        auch dann ausgefuehrt, wenn sein Wort gar nicht im Satz steht — dafuer
        ist is_actionable()+Schema zustaendig, nicht der Beleg."""
        intent = {"ist_kommando": True, "ziel": "tuerrollo",
                  "aktion": "setzen", "wert": 40, "einheit": "prozent"}
        self.assertEqual(
            self.act.verdict(intent, "irgendwas ganz anderes")[0], VERDICT_AUSFUEHRBAR
        )

    def test_leere_beleg_menge_ablehnt_nicht(self) -> None:
        """Eine Gruppe, deren Namen nur Funktions-/Kurzwoerter liefern, hat
        eine leere Beleg-Menge. Fuer sie entfaellt die Pruefung — nicht
        ablehnen, sonst sperrt man eine legitime Szene ohne Grund."""
        intent = {"ist_kommando": True, "ziel": "leere_gruppe",
                  "aktion": "aktivieren", "wert": None, "einheit": None}
        # "leere_gruppe" ist in gruppen_beleg NICHT (Menge leer -> rausgefallen),
        # also wird die Pruefung uebersprungen und ausgefuehrt.
        self.assertNotIn("leere_gruppe", self.act.gruppen_beleg)
        self.assertEqual(
            self.act.verdict(intent, "nix passendes")[0], VERDICT_AUSFUEHRBAR
        )

    def test_einzahl_ist_kein_beleg_fuer_die_gruppe(self) -> None:
        """Der teure Fall: "Rollo auf 70%" (Einzahl) darf nicht alle_rollos
        rechtfertigen. "rollo" != "rollos" — exakter Tokenvergleich, kein
        Praeffix. Genau das, was die ROLLO-OHNE-RAUM-Regel im Prompt verhindert."""
        intent = {"ist_kommando": True, "ziel": "alle_rollos",
                  "aktion": "setzen", "wert": 70, "einheit": "prozent"}
        self.assertEqual(
            self.act.verdict(intent, "Rollo auf 70%")[0], VERDICT_UNKLAR
        )

    def test_mehrzahl_pfad_wird_nicht_gebrochen(self) -> None:
        """Die lokale Mehrzahl-Regel (_mehrzahl_gruppe) baut ihr Intent aus
        einem Muster, das das Gruppenwort IM SATZ fordert (z.B. "Rollos
        runter"). Ihr Treffer enthaelt "rollos" -> Beleg ist erbracht. Regel A
        darf diesen Weg nicht plötzlich ablehnen."""
        # So wie _mehrzahl_gruppe es liefern wuerde:
        intent = {"ist_kommando": True, "aktion": "zu", "ziel": "alle_rollos",
                  "wert": None, "einheit": None}
        self.assertEqual(
            self.act.verdict(intent, "Rollos runter")[0], VERDICT_AUSFUEHRBAR
        )

    def test_ohne_transkript_wird_pruefung_uebersprungen(self) -> None:
        """transcript=None (Offline ohne Text) -> Pruefung uebersprungen, nicht
        abgelehnt. So bleiben aeltere Tests und der classify-Ausfallpfad gruen."""
        intent = {"ist_kommando": True, "ziel": "alle_rollos",
                  "aktion": "setzen", "wert": 40, "einheit": "prozent"}
        self.assertEqual(
            self.act.verdict(intent)[0], VERDICT_AUSFUEHRBAR
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
