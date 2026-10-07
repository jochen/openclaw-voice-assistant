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


def _actuator(**cfg) -> Actuator:
    act = Actuator(ActuatorConfig(enabled=True, token_file="/nonexistent",
                                  laya_url="http://laya.invalid", tor_enabled=True, **cfg))
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

    def test_setzen_ohne_zahl_wird_rueckfrage(self) -> None:
        """'Rollo etwas nach unten': setzen ohne Wert darf nicht ausfuehrbar sein."""
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, "tuerrollo", "setzen")):
            u = frage_laya("http://x", "Türrollo etwas nach unten", self.digest)
        i = als_intent(u, self.digest)
        self.assertEqual(self.act.verdict(i, "Türrollo etwas nach unten")[0], "unklar")

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


def _gemma(act, tor_ja=True, p=0.99, intent=None):
    """Gemmas Kette nachbauen: tor() und classify() als Mocks."""
    act.tor = mock.Mock(return_value=TorUrteil(tor_ja, p, 300.0))
    act.classify = mock.Mock(return_value=intent)


class KettenTest(unittest.TestCase):
    """aktuator_schatten.entscheiden(): wer entscheidet, und was bei Ausfall."""

    def test_laya_entscheidet_ohne_gemma_anzufassen(self) -> None:
        act = _actuator(klassifikator="laya")
        _gemma(act)
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, "flurlicht", "ein")):
            e = aktuator_schatten.entscheiden(act, "Flurlicht an")
        self.assertEqual((e.wer, e.ausgang), ("laya", "flurlicht/ein"))
        act.tor.assert_not_called()
        act.classify.assert_not_called()

    def test_laya_ausfall_gemma_entscheidet_im_selben_turn(self) -> None:
        """Container gestoppt (jedes Training!) darf nicht heissen: alles an den Brain."""
        act = _actuator(klassifikator="laya")
        _gemma(act, intent={"ist_kommando": True, "ziel": "flurlicht", "aktion": "ein"})
        with mock.patch("urllib.request.urlopen", side_effect=OSError("Verbindung abgelehnt")):
            e = aktuator_schatten.entscheiden(act, "Flurlicht an")
        self.assertEqual((e.wer, e.ausgang, e.laya_ausfall), ("gemma", "flurlicht/ein", True))
        self.assertIn("Verbindung abgelehnt", e.laya["fehler"])

    def test_default_ist_gemma(self) -> None:
        act = _actuator()
        _gemma(act, intent={"ist_kommando": True, "ziel": "flurlicht", "aktion": "aus"})
        e = aktuator_schatten.entscheiden(act, "Flurlicht aus")
        self.assertEqual((e.wer, e.ausgang), ("gemma", "flurlicht/aus"))

    def test_gemma_tor_nein_ist_brain_kein_ausfall(self) -> None:
        act = _actuator()
        _gemma(act, tor_ja=False, p=0.01)
        e = aktuator_schatten.entscheiden(act, "Erzähl einen Witz")
        self.assertEqual(e.ausgang, "Brain")
        self.assertFalse(e.ausfall)
        act.classify.assert_not_called()

    def test_rueckfrage_wenn_tor_ja_aber_kein_geraet(self) -> None:
        act = _actuator(klassifikator="laya")
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, KEIN_ZIEL, "zu")):
            e = aktuator_schatten.entscheiden(act, "Rollo zu")
        self.assertEqual(e.ausgang, "Rückfrage")

    def test_rueckfrage_frisst_die_mehrzahl_nicht(self) -> None:
        """'Rollos zu' faengt die Mehrzahl-Regel auf — das muss vor der
        Rueckfrage-Regel greifen, sonst fragt Gaston bei jedem 'Rollos zu' nach."""
        act = _actuator(klassifikator="laya")
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, KEIN_ZIEL, "zu")):
            e = aktuator_schatten.entscheiden(act, "Rollos zu")
        self.assertEqual(e.ausgang, "alle_rollos/zu")

    def test_ohne_rueckfrage_regel_brain(self) -> None:
        act = _actuator(klassifikator="laya", laya_rueckfrage=False)
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, KEIN_ZIEL, "zu")):
            e = aktuator_schatten.entscheiden(act, "Rollo zu")
        self.assertEqual(e.ausgang, "Brain")

    def test_tor_nein_bei_laya_ist_brain_nicht_rueckfrage(self) -> None:
        act = _actuator(klassifikator="laya")
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.1, KEIN_ZIEL, "ein")):
            e = aktuator_schatten.entscheiden(act, "Trag Spaghetti ein")
        self.assertEqual(e.ausgang, "Brain")


class SchattenTest(unittest.TestCase):

    def setUp(self) -> None:
        fd, self.log = tempfile.mkstemp()
        os.close(fd)
        self.patch = mock.patch.object(aktuator_schatten, "ACTUATOR_SCHATTEN_LOG_PATH", self.log)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        os.unlink(self.log)

    def _zeilen(self):
        return [json.loads(z) for z in open(self.log) if z.strip()]

    def _schatten(self, act, echt, **laya):
        """schatten_starten() ohne echten Thread: den Lauf direkt ausfuehren."""
        with mock.patch.object(aktuator_schatten.threading, "Thread") as T:
            aktuator_schatten.schatten_starten(act, "Flurlicht an", "gaston", echt)
        if T.called:
            with mock.patch("urllib.request.urlopen", **laya):
                T.call_args.kwargs["target"](*T.call_args.kwargs["args"])
        return T.called

    def test_gemma_entscheidet_laya_im_schatten_schaltet_nie(self) -> None:
        act = _actuator()
        _gemma(act, tor_ja=False, p=0.01)
        echt = aktuator_schatten.entscheiden(act, "Flurlicht an")
        self._schatten(act, echt, return_value=_laya_antwort(0.99, "flurlicht", "ein"))
        z = self._zeilen()[-1]
        act.execute.assert_not_called()
        self.assertEqual((z["entscheider"], z["gemma_ausgang"], z["laya_ausgang"]),
                         ("gemma", "Brain", "flurlicht/ein"))

    def test_laya_entscheidet_gemma_im_schatten(self) -> None:
        act = _actuator(klassifikator="laya")
        with mock.patch("urllib.request.urlopen", return_value=_laya_antwort(0.99, "flurlicht", "ein")):
            echt = aktuator_schatten.entscheiden(act, "Flurlicht an")
        _gemma(act, intent={"ist_kommando": True, "ziel": "flurlicht", "aktion": "ein"})
        self._schatten(act, echt)
        z = self._zeilen()[-1]
        act.execute.assert_not_called()
        self.assertEqual((z["entscheider"], z["laya_ausgang"], z["gemma_ausgang"]),
                         ("laya", "flurlicht/ein", "flurlicht/ein"))

    def test_laya_ausfall_wird_geloggt_ohne_schatten(self) -> None:
        act = _actuator(klassifikator="laya")
        _gemma(act, intent={"ist_kommando": True, "ziel": "flurlicht", "aktion": "ein"})
        with mock.patch("urllib.request.urlopen", side_effect=OSError("weg")):
            echt = aktuator_schatten.entscheiden(act, "Flurlicht an")
        lief = self._schatten(act, echt)
        z = self._zeilen()[-1]
        self.assertFalse(lief)
        self.assertEqual((z["entscheider"], z["gemma_ausgang"], z["laya_ausgang"]),
                         ("gemma (Laya-Ausfall)", "flurlicht/ein", "Ausfall→Brain"))

    def test_laya_weg_im_schatten_wird_logzeile(self) -> None:
        act = _actuator()
        _gemma(act, intent={"ist_kommando": True, "ziel": "flurlicht", "aktion": "ein"})
        echt = aktuator_schatten.entscheiden(act, "Flurlicht an")
        self._schatten(act, echt, side_effect=OSError("Verbindung abgelehnt"))
        z = self._zeilen()[-1]
        self.assertIn("Verbindung abgelehnt", z["laya"]["fehler"])
        self.assertEqual(z["laya_ausgang"], "Ausfall→Brain")

    def test_kaputte_kette_wirft_nicht(self) -> None:
        act = _actuator()
        echt = aktuator_schatten.Entscheidung("gemma", None, "kein_kommando", None, 0.0)
        with mock.patch.object(aktuator_schatten, "kette_laya", side_effect=RuntimeError("boom")):
            aktuator_schatten._lauf(act, "x", echt, "gaston")    # darf nicht werfen

    def test_schatten_aus(self) -> None:
        act = _actuator(klassifikator="laya", schatten=False)
        echt = aktuator_schatten.Entscheidung("laya", None, "kein_kommando", None, 0.0)
        self.assertFalse(self._schatten(act, echt))



def _health(caps, name="aktuator-v9"):
    return io.BytesIO(json.dumps({"status": "ok", "checkpoint": {
        "name": name, "capabilities": caps, "seed": 7}}).encode())


class CheckpointAbgleichTest(unittest.TestCase):
    """Checkpoint-Version gegen Live-Version — die Luecke, durch die v1 am
    2026-10-01 und v3 bis 2026-10-06 still schlechter wurden."""

    def _pruefe(self, act, melden, **urlopen):
        with mock.patch("urllib.request.urlopen", **urlopen), \
                mock.patch("builtins.print"):
            return aktuator_schatten.pruefe_checkpoint(act, melden)

    def test_passt(self) -> None:
        melden = mock.Mock()
        self.assertEqual(self._pruefe(_actuator(), melden, return_value=_health("test1")), "passt")
        melden.assert_not_called()

    def test_abweichung_wird_einmal_gemeldet(self) -> None:
        act, melden = _actuator(), mock.Mock()
        for _ in range(3):      # drei Refreshes mit derselben Lage
            self.assertEqual(self._pruefe(act, melden, side_effect=lambda *a, **k: _health("alt")),
                             "abweichung")
        melden.assert_called_once()
        self.assertIn("alt", melden.call_args.args[0])
        self.assertIn("test1", melden.call_args.args[0])

    def test_nach_passt_wird_neue_abweichung_wieder_gemeldet(self) -> None:
        act, melden = _actuator(), mock.Mock()
        self._pruefe(act, melden, return_value=_health("alt"))
        self._pruefe(act, melden, return_value=_health("test1"))
        self._pruefe(act, melden, return_value=_health("alt"))
        self.assertEqual(melden.call_count, 2)

    def test_container_ohne_checkpoint_feld_ist_unbekannt(self) -> None:
        melden = mock.Mock()
        alt = io.BytesIO(json.dumps({"status": "ok"}).encode())
        self.assertEqual(self._pruefe(_actuator(), melden, return_value=alt), "unbekannt")
        melden.assert_not_called()

    def test_container_weg_ist_unbekannt_und_wirft_nicht(self) -> None:
        melden = mock.Mock()
        self.assertEqual(self._pruefe(_actuator(), melden, side_effect=OSError("weg")), "unbekannt")
        melden.assert_not_called()

    def test_kaputtes_melden_wirft_nicht(self) -> None:
        melden = mock.Mock(side_effect=RuntimeError("Telegram weg"))
        self.assertEqual(self._pruefe(_actuator(), melden, return_value=_health("alt")), "abweichung")

    def test_nach_refresh_hook_laeuft(self) -> None:
        act = _actuator()
        hook = mock.Mock()
        act.nach_refresh.append(hook)
        with mock.patch.object(act, "aufwaermen"), \
                mock.patch("voice_assistant.services.actuator.threading.Thread") as T:
            T.side_effect = lambda target, daemon: mock.Mock(start=target)
            self.assertTrue(act._refresh_und_aufwaermen())
        hook.assert_called_once()


if __name__ == "__main__":
    unittest.main()
