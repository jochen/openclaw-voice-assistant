"""Turn ohne Antwort: Grund ansagen und spiegeln, keinen zweiten Auftrag (2026-10-31).

Fablab 2026-10-28 19:11: das Modell war beim Anbieter abgeschaltet (HTTP 400
"no keys found that support model: qsu/glm-5-3"). Der Stream endete mit
response.failed, der Worker schickte den Auftrag noch einmal non-streaming
(zweiter Lauf, gleicher Fehler), sagte "ich konnte keine Antwort erhalten",
und im Telegram stand nur die Frage — das sah aus wie Schweigen.
"""

import http.server
import json
import os
import socket
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant import state, workers  # noqa: E402
from voice_assistant.services import openclaw  # noqa: E402

# Wortlaut wie im Gateway-Log, samt Markdown-Maskierung
MELDUNG_ROH = "LLM request rejected: no keys found that support model\\: qsu\\/glm\\-5\\-3"
MELDUNG = "LLM request rejected: no keys found that support model: qsu/glm-5-3"


def _sse(evt: dict) -> bytes:
    return f"event: {evt['type']}\ndata: {json.dumps(evt)}\n\n".encode()


class Gateway(unittest.TestCase):
    """Echter HTTP-Server, wie in test_no_reply.py."""

    def _server(self, status: int, body: bytes, ctype: str) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.orig_url = openclaw.OPENCLAW_RESPONSES_URL
        openclaw.OPENCLAW_RESPONSES_URL = (
            f"http://127.0.0.1:{self.server.server_address[1]}/v1/responses")

    def tearDown(self) -> None:
        openclaw.OPENCLAW_RESPONSES_URL = self.orig_url
        self.server.shutdown()
        self.server.server_close()

    def test_stream_failed_mit_meldung(self):
        fehlerhaft = {"type": "response.failed", "response": {
            "status": "failed", "error": {"code": "invalid_request_error", "message": MELDUNG_ROH}}}
        self._server(200, _sse({"type": "response.created"}) + _sse(fehlerhaft),
                     "text/event-stream")
        fehler: dict = {}
        text, timeout = openclaw.query_stream("hallo", token="t", session="s", fehler=fehler)
        self.assertIsNone(text)
        self.assertFalse(timeout)
        self.assertEqual(fehler, {"art": openclaw.FEHLER_GEMELDET, "meldung": MELDUNG})

    def test_stream_http_fehler_ist_abgelehnt(self):
        body = json.dumps({"error": {"type": "invalid_request_error",
                                     "message": "Missing user message in `input`."}}).encode()
        self._server(400, body, "application/json")
        fehler: dict = {}
        text, _ = openclaw.query_stream("hallo", token="t", session="s", fehler=fehler)
        self.assertIsNone(text)
        self.assertEqual(fehler["art"], openclaw.FEHLER_ABGELEHNT)
        self.assertEqual(fehler["meldung"], "Missing user message in `input`.")

    def test_nicht_stream_http_400(self):
        body = json.dumps({"status": "failed", "output": [],
                           "error": {"code": "invalid_request_error", "message": MELDUNG_ROH}}).encode()
        self._server(400, body, "application/json")
        fehler: dict = {}
        self.assertIsNone(openclaw.query("hallo", token="t", session="s", fehler=fehler))
        self.assertEqual(fehler, {"art": openclaw.FEHLER_GEMELDET, "meldung": MELDUNG})



class GatewayWegTest(unittest.TestCase):
    def test_gateway_weg(self):
        frei = socket.socket()
        frei.bind(("127.0.0.1", 0))
        port = frei.getsockname()[1]
        frei.close()  # Port frei, niemand hoert
        with mock.patch.object(openclaw, "OPENCLAW_RESPONSES_URL",
                               f"http://127.0.0.1:{port}/v1/responses"):
            fehler: dict = {}
            text, _ = openclaw.query_stream("hallo", token="t", session="s", fehler=fehler)
        self.assertIsNone(text)
        self.assertEqual(fehler["art"], openclaw.FEHLER_WEG)


def _arbeiter(gesprochen):
    sprecher = types.SimpleNamespace(
        stream_session=lambda **kw: types.SimpleNamespace(feed=gesprochen.append, end=lambda: False),
        speak=lambda text, turn=None: gesprochen.append(text),
    )
    return types.SimpleNamespace(
        use_stream=True, speaker=sprecher, openclaw_token="t",
        openclaw_session="s", voice_instruction="", instructions_feld=True,
        voice_erinnerung="", telegram_bot_token="b", telegram_chat_id="c",
        thinking=types.SimpleNamespace(stop=lambda: None),
        no_reply_fallback="leer", brain_fehler="modellfehler", brain_weg="weg",
        abort_notify_brain=False, _finish_cancelled=lambda key: None,
    )


class WorkerTest(unittest.TestCase):
    def setUp(self):
        state.current_state[0] = state.STATE_WAITING

    def tearDown(self):
        state.current_state[0] = state.STATE_LISTENING

    def _turn(self, art, meldung="", nachher=None):
        """Stream endet ohne Text mit Fehlerart `art`; `nachher` liefert query()."""
        gesprochen, chat, anfragen = [], [], []

        def stream(text, fehler=None, **kw):
            if art:
                fehler.update(art=art, meldung=meldung)
            return None, art == openclaw.FEHLER_ZEIT

        def anfrage(text, **kw):
            anfragen.append(text)
            return nachher

        with mock.patch.object(workers.openclaw, "query_stream", stream), \
                mock.patch.object(workers.openclaw, "query", anfrage), \
                mock.patch.object(workers.telegram, "send",
                                  lambda tok, c, text, prefix="": chat.append(prefix + text)):
            workers.Workers._run_openclaw_turn(
                _arbeiter(gesprochen), "Mister Handy, höre zu.",
                speaker=None, turn=state.turn_control.begin())
        return gesprochen, chat, anfragen

    def test_gemeldeter_fehler(self):
        gesprochen, chat, anfragen = self._turn(openclaw.FEHLER_GEMELDET, MELDUNG)
        self.assertEqual(anfragen, [], "Lauf hatte begonnen: kein zweiter Auftrag")
        self.assertEqual(gesprochen, ["modellfehler"])
        self.assertEqual(chat, ["🎤 [unbekannt] Mister Handy, höre zu.",
                                f"⚠️ Keine Antwort (OpenClaw meldet einen Fehler): {MELDUNG}"])

    def test_abgerissen_kein_zweiter_auftrag(self):
        gesprochen, chat, anfragen = self._turn(openclaw.FEHLER_ABGERISSEN, "Stream ohne Abschluss")
        self.assertEqual(anfragen, [])
        self.assertEqual(gesprochen, ["leer"])

    def test_leer_kein_zweiter_auftrag(self):
        gesprochen, chat, anfragen = self._turn("")
        self.assertEqual(anfragen, [])
        self.assertEqual(chat[-1], "⚠️ Keine Antwort (leere Antwort)")

    def test_abgelehnt_darf_nachschieben(self):
        gesprochen, chat, anfragen = self._turn(openclaw.FEHLER_ABGELEHNT, "x", nachher="Ich höre.")
        self.assertEqual(len(anfragen), 1)
        self.assertEqual(gesprochen, ["Ich höre."])

    def test_gateway_weg(self):
        gesprochen, chat, _ = self._turn(openclaw.FEHLER_WEG, "Connection refused")
        self.assertEqual(gesprochen, ["weg"])
        self.assertIn("OpenClaw nicht erreichbar", chat[-1])


if __name__ == "__main__":
    unittest.main()
