"""NO_REPLY ueber das Gateway: der Leer-Platzhalter gilt als Stille.

2026-10-17: Der Brain (GLM ueber openai-completions) antwortete auf drei
Fernseh-Turns richtig NO_REPLY. Das Gateway filterte es, wie vorgesehen, und
schickte "No response from OpenClaw." -- Gaston las das vor, spiegelte es und
oeffnete das Follow-up. Der SSE-Server hier spielt die Ereignisfolge nach, die
openresponses-http in diesem Fall sendet. Kein Netz nach draussen.
"""

import http.server
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import openclaw  # noqa: E402
from voice_assistant.services.openclaw import GATEWAY_LEER_PLATZHALTER, is_no_reply  # noqa: E402


class PlatzhalterTest(unittest.TestCase):
    def test_platzhalter_ist_stille(self):
        self.assertTrue(is_no_reply(GATEWAY_LEER_PLATZHALTER))
        self.assertTrue(is_no_reply(f"  {GATEWAY_LEER_PLATZHALTER}\n"))

    def test_nur_als_ganze_antwort(self):
        self.assertFalse(is_no_reply(
            f"Das Licht ist an. {GATEWAY_LEER_PLATZHALTER}"))
        self.assertFalse(is_no_reply("No response from the server."))

    def test_no_reply_unveraendert(self):
        self.assertTrue(is_no_reply("NO_REPLY"))
        self.assertFalse(is_no_reply("Alles klar."))
        self.assertFalse(is_no_reply(None))
        self.assertFalse(is_no_reply(""))

    def test_uebersetzte_formen(self):
        # GLM schreibt NO_REPLY gern auf Deutsch (2026-10-16, 2026-10-18)
        self.assertTrue(is_no_reply("KEIN_REPLY"))
        self.assertTrue(is_no_reply("KEINE_ANTWORT"))
        self.assertTrue(is_no_reply("Keine Antwort."))
        self.assertFalse(is_no_reply("Keine Antwort von der Haustür."))


def _ereignisse(*evts: dict) -> list[bytes]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in evts]


# Was openresponses-http bei stiller Antwort sendet: nichts gestreamt, dann
# in maybeFinalize der Platzhalter als einziges Delta und response.completed.
STILL = _ereignisse(
    {"type": "response.created"},
    {"type": "response.output_text.delta", "delta": GATEWAY_LEER_PLATZHALTER},
    {"type": "response.output_text.done", "text": GATEWAY_LEER_PLATZHALTER},
    {"type": "response.completed"},
)
FEHLER = _ereignisse(
    {"type": "response.created"},
    {"type": "response.failed"},
)


class GatewayStreamTest(unittest.TestCase):
    def _server(self, bloecke: list[bytes]) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for b in bloecke:
                    self.wfile.write(b)
                self.wfile.flush()

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

    def test_stille_antwort_wird_nicht_gesprochen(self):
        self._server(STILL)
        saetze: list[str] = []
        text, timeout = openclaw.query_stream(
            "Fotografieren ist hier nicht erlaubt.", token="t", session="s",
            on_sentence=saetze.append)
        self.assertFalse(timeout)
        self.assertTrue(is_no_reply(text), text)
        # Jeder Satz, der den Worker erreicht, faellt durch dessen Filter
        # (workers.guarded_feed), sonst waere er schon gesprochen.
        self.assertTrue(saetze)
        for s in saetze:
            self.assertTrue(is_no_reply(s), s)

    def test_fehler_bleibt_fehler(self):
        self._server(FEHLER)
        text, timeout = openclaw.query_stream("hallo", token="t", session="s")
        self.assertIsNone(text)
        self.assertFalse(timeout)


if __name__ == "__main__":
    unittest.main()
