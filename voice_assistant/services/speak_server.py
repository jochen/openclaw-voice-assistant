"""Lokaler HTTP-Server: OpenClaw kann Text über den Lautsprecher ansagen lassen.

Endpoints:
  POST /speak   Body {"text": "..."} → legt Text in announce_queue
  GET  /status  → {"status": "ok"}
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from voice_assistant.config import SPEAK_SERVER_HOST, SPEAK_SERVER_PORT
from voice_assistant.state import (
    STATE_LISTENING,
    announce_queue,
    current_state,
)
from voice_assistant.services.tts import ReplySpeaker


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        print(f"[speak-server] {self.address_string()}: {format % args}")

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/status":
            self._send_json(200, {"status": "ok", "queue": announce_queue.qsize()})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/speak":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
            text = str(payload.get("text", "")).strip()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if not text:
            self._send_json(400, {"error": "missing or empty text"})
            return
        announce_queue.put(text)
        print(f"[speak-server] queued: '{text[:60]}{'...' if len(text) > 60 else ''}'")
        self._send_json(202, {"queued": True, "length": len(text)})


def start_speak_server() -> threading.Thread:
    server = HTTPServer((SPEAK_SERVER_HOST, SPEAK_SERVER_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, name="speak-server", daemon=True)
    t.start()
    print(f"📢  Speak server listening on http://{SPEAK_SERVER_HOST}:{SPEAK_SERVER_PORT}")
    return t


def start_announce_worker(reply_speaker: ReplySpeaker) -> threading.Thread:
    """Drainiert announce_queue, aber nur wenn der Voice Assistant im LISTENING-State ist."""
    from voice_assistant.services.leds import LED_IDLE

    def _run() -> None:
        while True:
            text = announce_queue.get()  # blockiert bis was da ist
            # Warten bis wir im LISTENING-State sind (nicht aufnehmen, nicht antworten)
            while current_state[0] != STATE_LISTENING:
                time.sleep(0.25)
            print(f"📢  Announcing: '{text[:80]}{'...' if len(text) > 80 else ''}'")
            reply_speaker.speak(text)
            reply_speaker.leds.set_phase(LED_IDLE)

    t = threading.Thread(target=_run, name="announce-worker", daemon=True)
    t.start()
    return t
