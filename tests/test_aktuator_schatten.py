"""Offline-Tests fuer den Schattenbetrieb (Laya neben Gemma) und laya_intent.

Laeuft OHNE Netz: laya-serve wird nachgebaut.

Ausfuehren:
    ow-venv/bin/python tests/test_aktuator_schatten.py

Warum es diese Datei gibt
-------------------------
Der Schatten laeuft in jedem Aktuator-Turn mit. Drei Dinge darf er nie:
schalten, den Hauptloop mit einer Exception treffen, und im Log einen Tor-
Nein-Turn als Ausfall ausgeben (dann waere die Auswertung falsch, ohne dass
es jemand merkt). Und die Umrechnung Laya -> Intent muss dieselben Ausgaenge
erzeugen, die Gemmas Intent erzeugt, sonst misst der Vergleich Formatfehler
statt Klassifikatoren.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import ActuatorConfig  # noqa: E402
from voice_assistant.services import aktuator_schatten  # noqa: E402
from voice_assistant.services.actuator import (  # noqa: E402
    VERDICT_AUSFUEHRBAR, VERDICT_KEIN_KOMMANDO, Actuator, TorUrteil,
)
from voice_assistant.services.laya_intent import (  # noqa: E402
    KEIN_ZIEL, LayaUrteil, als_intent, fragen, frage_laya, lese_wert,
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


def _actuator() -> Actuator:
    act = Actuator(ActuatorConfig(enabled=True, token_file="/nonexistent",
                                  schatten_url="http://laya.invalid"))
    act._fetch_capabilities = lambda: CAPS
    assert act.refresh()
    act.execute = mock.Mock(side_effect=AssertionError("Schatten hat geschaltet"))
    return act


def _laya_antwort(p_ja, ziel, aktion, p=0.9):
    return io.BytesIO(json.dumps({"answers": {
        "tor": {"noul": p_ja},
        "ziel": {"choice": ziel, "probabilities": {ziel: p}},
        "aktion": {"choice": aktion, "probabilities": {aktion: p}},
    }}).encode())


class LayaIntentTest(unittest.TestCase):

    def setUp(self) -> None:
        self.act = _actuator()
        self.digest = self.act.digest

    def test_fragen_aus_digest_ohne_feste_ids(self) -> None:
        q = fragen(self.digest)
        self.assertEqual(set(q["ziel"]["criteria"]), {"flurlicht", "tuerrollo", "alle_rollos", KEIN_ZIEL})
        self.assertEqual(q["ziel"]["criteria"]["tuerrollo"], "Türrollo")

    def test_setzen_bekommt_wert_und_einheit(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, "tuerrollo", "setzen")):
            u = frage_laya("http://x", "Türrollo auf 40%", self.digest)
        i = als_intent(u, self.digest)
        self.assertEqual(i, {"ist_kommando": True, "aktion": "setzen", "ziel": "tuerrollo",
                             "wert": 40, "einheit": "prozent"})
        self.assertEqual(self.act.verdict(i, "Türrollo auf 40%")[0], VERDICT_AUSFUEHRBAR)

    def test_tor_nein_und_keins_sind_kein_kommando(self) -> None:
        for p, ziel in ((0.2, "flurlicht"), (0.99, KEIN_ZIEL)):
            u = LayaUrteil(p_ja=p, ziel=ziel, p_ziel=0.9, aktion="ein", p_aktion=0.9)
            self.assertFalse(als_intent(u, self.digest)["ist_kommando"])

    def test_ausfall_ist_none_wie_classify(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=OSError("weg")):
            u = frage_laya("http://x", "Flurlicht an", self.digest)
        self.assertIsNotNone(u.fehler)
        self.assertIsNone(als_intent(u, self.digest))

    def test_mehrzahl_regel_greift_auch_fuer_laya(self) -> None:
        """'keins' -> kein Kommando -> _mehrzahl_gruppe kann die Mehrzahl
        auffangen, genau wie bei Gemmas 'Rollo ohne Raum'-Regel."""
        u = LayaUrteil(p_ja=0.99, ziel=KEIN_ZIEL, p_ziel=0.9, aktion="zu", p_aktion=0.9)
        i = self.act._mehrzahl_gruppe("Rollos zu", als_intent(u, self.digest), still=True)
        self.assertEqual((i.get("ziel"), i.get("aktion")), ("alle_rollos", "zu"))

    def test_lese_wert(self) -> None:
        for satz, soll in (("Rollo auf 70%", 70), ("halb zu", 50), ("auf zwanzig Prozent", 20),
                           ("einundzwanzig Grad", 21), ("Mach das Licht an", None)):
            self.assertEqual(lese_wert(satz), soll, satz)


class SchattenTest(unittest.TestCase):

    def setUp(self) -> None:
        self.act = _actuator()
        fd, self.log = tempfile.mkstemp()
        os.close(fd)
        self.patch = mock.patch.object(aktuator_schatten, "ACTUATOR_SCHATTEN_LOG_PATH", self.log)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        os.unlink(self.log)

    def _zeilen(self):
        return [json.loads(z) for z in open(self.log) if z.strip()]

    def _lauf(self, antwort, tor_urteil, intent, verdict):
        meta = {"wakeword": "gaston"}
        with mock.patch.object(aktuator_schatten.threading, "Thread") as T:
            aktuator_schatten.starten(self.act, "Flurlicht an", "gaston", tor_urteil, intent, verdict)
            meta = T.call_args.kwargs["args"][2]
        with mock.patch("urllib.request.urlopen", **antwort):
            aktuator_schatten._lauf(self.act, "Flurlicht an", meta)
        return self._zeilen()[-1]

    def test_schaltet_nie_und_schreibt_beide_seiten(self) -> None:
        z = self._lauf({"return_value": _laya_antwort(0.99, "flurlicht", "ein")},
                       TorUrteil(False, 0.01, 300.0), None, VERDICT_KEIN_KOMMANDO)
        self.act.execute.assert_not_called()
        self.assertEqual(z["gemma_ausgang"], "Brain")
        self.assertEqual(z["laya_ausgang"], "flurlicht/ein")

    def test_tor_nein_ist_kein_ausfall_classify_ausfall_schon(self) -> None:
        z = self._lauf({"return_value": _laya_antwort(0.1, KEIN_ZIEL, "ein")},
                       TorUrteil(False, 0.01, 300.0), None, VERDICT_KEIN_KOMMANDO)
        self.assertEqual(z["gemma_ausgang"], "Brain")
        z = self._lauf({"return_value": _laya_antwort(0.1, KEIN_ZIEL, "ein")},
                       TorUrteil(True, 0.99, 300.0), None, VERDICT_KEIN_KOMMANDO)
        self.assertEqual(z["gemma_ausgang"], "Ausfall→Brain")

    def test_laya_weg_wird_logzeile_keine_exception(self) -> None:
        z = self._lauf({"side_effect": OSError("Verbindung abgelehnt")},
                       TorUrteil(True, 0.99, 300.0), {"ist_kommando": True, "ziel": "flurlicht",
                                                      "aktion": "ein"}, VERDICT_AUSFUEHRBAR)
        self.assertIn("Verbindung abgelehnt", z["laya"]["fehler"])
        self.assertEqual(z["laya_ausgang"], "Ausfall→Brain")
        self.assertEqual(z["gemma_ausgang"], "flurlicht/ein")

    def test_kaputter_actuator_wirft_nicht(self) -> None:
        kaputt = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(aktuator_schatten, "frage_laya", kaputt):
            aktuator_schatten._lauf(self.act, "x", {"gemma_ausgang": "Brain"})   # darf nicht werfen


if __name__ == "__main__":
    unittest.main()
