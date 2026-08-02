"""Offline-Tests für den Gaston-MCP-Aktuator — quelle, Handshake, Logging.

Läuft OHNE Netz, ohne Node-RED, ohne MCP-Transport: urlopen wird durch einen
Fake ersetzt, der die gesendeten Bodies einfängt und beliebige Envelopes
zurückgibt. Muster wie tests/test_actuator_verdict.py.

Ausführen:
    ow-venv/bin/python tests/test_mcp_actuator.py

Warum es diese Datei gibt
-------------------------
Der Auftrag (scratchpad/auftrag_mcp_aktuator.md) legt drei Garantien fest,

1. der MCP-Pfad sendet quelle="gaston", niemals "brain" — letzteres schaltet
   im Node-RED-Gate die Rückfrage ab und ist reserviert für einen Überwacher,
   der noch nicht existiert.
2. ein zurueckgestellt-Envelope (die Rückfrage des Gates) kommt unverfälscht
   durch — der Brain stellt ihre Frage dem Nutzer und ruft mit derselben
   request_id + bestaetigt erneut auf.
3. der Default von Actuator.execute() bleibt "aktuator", damit der Voice-Pfad
   sich nicht ändert.

Diese Tests sperren genau das fest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import ActuatorConfig  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402
from voice_assistant.mcp_actuator import GastonMCP  # noqa: E402


DIGEST = {
    "wohnzimmerrollo": {
        "namen": ["Wohnzimmerrollo"],
        "typ": "rollo",
        "aktionen": ["auf", "zu", "setzen"],
        "wert": {"einheit": "prozent", "min": 0, "max": 100},
    },
}


class _Resp:
    """Context-Manager, den urllib.request.urlopen zurückgibt."""

    def __init__(self, payload: dict) -> None:
        self._bytes = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._bytes


class _FakeUrlopen:
    """Ersetzt urllib.request.urlopen. Fängt die Requests und antwortet fix."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return _Resp(self.reply)


def _actuator() -> Actuator:
    cfg = ActuatorConfig(enabled=True, base_url="http://127.0.0.1:0",
                         token_file="/nonexistent")
    act = Actuator(cfg)
    act.digest = dict(DIGEST)
    act.ziele = [{
        "id": "wohnzimmerrollo", "namen": ["Wohnzimmerrollo"], "typ": "rollo",
        "aktionen": ["auf", "zu", "setzen"],
        "wert": {"einheit": "prozent", "min": 0, "max": 100},
        "kosten": "niedrig", "reversibel": True, "mitglieder": None,
    }]
    return act


INTENT = {"ist_kommando": True, "ziel": "wohnzimmerrollo", "aktion": "setzen",
          "wert": 50, "einheit": "prozent"}


class _LogIsolatedTestCase(unittest.TestCase):
    """Leitet den Aktuator-Log zentral um — JEGLICHER haus_schalten-Aufruf
    schreibt sonst eine echte Zeile nach ~/.openclaw/workspace/actuator_turns.log.

    _log_turn in mcp_actuator.py importiert ACTUATOR_LOG_PATH per
    `from voice_assistant.config import ...` erst zur Aufrufzeit, daher greift
    das Patchen des Modulattributs. setUp biegt es auf eine Tempdatei um,
    tearDown räumt sie weg. Das gilt für jeden Test der Unterklasse, egal ob
    er schaltet oder nicht — die Umleitung ist nicht pro Test, sondern zentral.
    """

    def setUp(self) -> None:
        super().setUp()
        self._log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._log_dir.cleanup)
        self._log_path = os.path.join(self._log_dir.name, "actuator_turns.log")
        patcher = mock.patch("voice_assistant.config.ACTUATOR_LOG_PATH",
                             self._log_path)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestQuelle(_LogIsolatedTestCase):
    def test_default_quelle_bleibt_aktuator(self) -> None:
        """Der Voice-Pfad ruft execute() ohne quelle auf — der Default muss
        "aktuator" bleiben, sonst ändert sich das Verhalten des laufenden
        Dienstes ohne Anlass."""
        fake = _FakeUrlopen({"status": "ausgefuehrt"})
        act = _actuator()
        with mock.patch("urllib.request.urlopen", fake):
            act.execute(INTENT, "rid-default")
        body = json.loads(fake.requests[-1].data.decode())
        self.assertEqual(body["quelle"], "aktuator")

    def test_execute_mit_quelle_gaston(self) -> None:
        fake = _FakeUrlopen({"status": "ausgefuehrt"})
        act = _actuator()
        with mock.patch("urllib.request.urlopen", fake):
            act.execute(INTENT, "rid-gaston", quelle="gaston")
        body = json.loads(fake.requests[-1].data.decode())
        self.assertEqual(body["quelle"], "gaston")

    def test_quelle_brain_wird_abgewiesen(self) -> None:
        """quelle 'brain' schaltet im Gate die Rückfrage ab und ist reserviert.
        execute() wehrt es ab, statt es auf die Reise zu schicken — die Garantie
        hängt nicht am guten Willen des Aufrufers."""
        act = _actuator()
        with self.assertRaises(ValueError):
            act.execute(INTENT, "rid-brain", quelle="brain")


class TestHandshakeDurchleitung(_LogIsolatedTestCase):
    def test_zurueckgestellt_envelope_kommt_unverfaelscht_durch(self) -> None:
        """Zurueckstellen ist kein Fehler: das Gate stellt in gesprochen eine
        Rückfrage. Der MCP-Server gibt den Umschlag 1:1 zurück, damit der Brain
        sie vorliest und mit derselben request_id neu anfragt."""
        reply = {"status": "zurueckgestellt",
                 "gesprochen": "Wirklich das Wohnzimmerrollo auf fünfzig Prozent?",
                 "grund": None, "ausgefuehrt": None,
                 "request_id": "fest-vom-gate"}
        fake = _FakeUrlopen(reply)
        srv = GastonMCP(_actuator())
        with mock.patch("urllib.request.urlopen", fake):
            env = srv.schalten("wohnzimmerrollo", "setzen", wert=50, einheit="prozent",
                               request_id="fest-vom-gate")
        self.assertEqual(env, reply)
        self.assertEqual(env["status"], "zurueckgestellt")
        # Und die gesendete Quelle war gaston, nicht brain.
        body = json.loads(fake.requests[-1].data.decode())
        self.assertEqual(body["quelle"], "gaston")
        self.assertNotEqual(body["quelle"], "brain")

    def test_bestaetigt_flag_steht_im_body(self) -> None:
        """Der zweite Handshake-Schritt setzt bestaetigt=true — der Body muss
        es tragen, sonst fragt das Gate endlos nach."""
        fake = _FakeUrlopen({"status": "ausgefuehrt"})
        srv = GastonMCP(_actuator())
        with mock.patch("urllib.request.urlopen", fake):
            srv.schalten("wohnzimmerrollo", "setzen", wert=50, einheit="prozent",
                        bestaetigt=True, request_id="rid")
        body = json.loads(fake.requests[-1].data.decode())
        self.assertTrue(body.get("bestaetigt"))

    def test_request_id_wird_erzeugt_wenn_keine(self) -> None:
        """Ohne request_id erzeugt der Server eine — sie ist Idempotenz- und
        Handshake-Träger, also Pflicht. Sie kehrt im Umschlag zurück."""
        fake = _FakeUrlopen({"status": "ausgefuehrt"})
        srv = GastonMCP(_actuator())
        with mock.patch("urllib.request.urlopen", fake):
            env = srv.schalten("wohnzimmerrollo", "auf")
        self.assertTrue(env["request_id"])
        body = json.loads(fake.requests[-1].data.decode())
        self.assertEqual(body["request_id"], env["request_id"])

    def test_transportstoerung_liefert_konsistenten_umschlag(self) -> None:
        """execute() liefert None nach erfolglosem Retry. Der Server gibt dem
        Brain dann einen konsistenten Umschlag zurück statt None."""
        def boom(*a, **k):
            raise OSError("connection refused")
        srv = GastonMCP(_actuator())
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            env = srv.schalten("wohnzimmerrollo", "auf", request_id="rid")
        self.assertEqual(env["status"], "nicht_erreichbar")
        self.assertEqual(env["request_id"], "rid")
        self.assertIn("gesprochen", env)


class TestZieleUndLogging(_LogIsolatedTestCase):
    def test_haus_ziele_enthaelt_kosten_und_reversibel(self) -> None:
        """Der Digest der Stimme wirft kosten/reversibel ab — der Brain braucht
        sie gerade. haus_ziele muss sie aus den rohen capabilities liefern."""
        srv = GastonMCP(_actuator())
        ziele = srv.ziele()
        self.assertEqual(len(ziele), 1)
        self.assertEqual(ziele[0]["id"], "wohnzimmerrollo")
        self.assertEqual(ziele[0]["kosten"], "niedrig")
        self.assertIs(ziele[0]["reversibel"], True)
        self.assertEqual(ziele[0]["aktionen"], ["auf", "zu", "setzen"])

    def test_log_zeile_phase_gaston(self) -> None:
        """Jeder haus_schalten-Aufruf schreibt eine JSONL-Zeile mit phase
        'gaston' — sonst bleiben die Schaltungen des Brains unsichtbar, und
        genau das hat den Vorfall ungemeldet gelassen. Best-effort, scheitert
        nie den Aufruf.

        Der Log-Pfad kommt aus setUp (self._log_path) — KEIN pro-Test-Patch
        mehr, die Umleitung ist zentral."""
        fake = _FakeUrlopen({"status": "ausgefuehrt",
                             "gesprochen": "Wohnzimmerrollo auf fünfzig Prozent.",
                             "grund": None, "ausgefuehrt": None,
                             "request_id": "rid-log"})
        srv = GastonMCP(_actuator())
        with mock.patch("urllib.request.urlopen", fake):
            srv.schalten("wohnzimmerrollo", "setzen", wert=50,
                         einheit="prozent", request_id="rid-log")
        with open(self._log_path, "r") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["phase"], "gaston")
        self.assertEqual(entry["request_id"], "rid-log")
        self.assertEqual(entry["ziel"], "wohnzimmerrollo")
        self.assertEqual(entry["aktion"], "setzen")
        self.assertEqual(entry["wert"], 50)
        self.assertEqual(entry["status"], "ausgefuehrt")
        self.assertIn("ts", entry)

    def test_log_fehler_scheitert_nicht_den_aufruf(self) -> None:
        """Ein Log-Fehler (hier: Logpfad ist ein Verzeichnis) darf den Schalt-
        aufruf nie scheitern lassen — Best-effort ist Pflicht.

        Überschreibt die zentrale Umleitung aus setUp für diesen EINEN Test:
        self._log_dir.name ist ein Verzeichnis — open(<verz>, 'a') schlägt fehl.
        Der innere Patch gewinnt; setUp räumt seinen eigenen Patch danach auf."""
        fake = _FakeUrlopen({"status": "ausgefuehrt"})
        srv = GastonMCP(_actuator())
        with mock.patch("voice_assistant.config.ACTUATOR_LOG_PATH",
                        self._log_dir.name):
            with mock.patch("urllib.request.urlopen", fake):
                env = srv.schalten("wohnzimmerrollo", "auf", request_id="rid")
        self.assertEqual(env["status"], "ausgefuehrt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
