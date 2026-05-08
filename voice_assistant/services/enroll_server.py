"""Lokaler HTTP-Server für Sprecher-Enrolment.

OpenClaw-Tools posten hier hin, um die letzte Aufnahme als Stimm-Referenz
abzulegen.

Endpoints:
  POST   /enroll          Body {"name": "jochen"} → kopiert last_recording.wav nach speakers/jochen.wav
  GET    /speakers        → {"speakers": ["jochen", "katrin", ...]}
  DELETE /speakers/<name> → löscht Referenz
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from voice_assistant.config import (
    ENROLL_SERVER_HOST,
    ENROLL_SERVER_PORT,
    LAST_RECORDING_PATH,
    SPEAKER_ORIGINALS_DIR,
    SPEAKERS_DIR,
)


def _safe_name(name: str) -> str:
    name = name.strip().lower()
    out: list[str] = []
    for c in name:
        if c.isalnum() or c in ("-", "_"):
            out.append(c)
        elif c in (" ", "."):
            out.append("_")
    return "".join(out)


def _list_speakers() -> list[str]:
    if not os.path.isdir(SPEAKERS_DIR):
        return []
    return [
        os.path.splitext(f)[0]
        for f in sorted(os.listdir(SPEAKERS_DIR))
        if f.lower().endswith(".wav")
    ]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        print(f"[enroll-server] {self.address_string()}: {format % args}")

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/speakers":
            self._send_json(200, {"speakers": _list_speakers()})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/enroll":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
            name = _safe_name(str(payload.get("name", "")))
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if not name:
            self._send_json(400, {"error": "missing or invalid name"})
            return
        if not os.path.isfile(LAST_RECORDING_PATH):
            self._send_json(409, {"error": "no recent recording available"})
            return

        os.makedirs(SPEAKERS_DIR, exist_ok=True)
        os.makedirs(SPEAKER_ORIGINALS_DIR, exist_ok=True)

        dst = os.path.join(SPEAKERS_DIR, f"{name}.wav")
        shutil.copy2(LAST_RECORDING_PATH, dst)

        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        backup = os.path.join(SPEAKER_ORIGINALS_DIR, f"{name}-{ts}.wav")
        shutil.copy2(LAST_RECORDING_PATH, backup)

        print(f"🎙  Speaker enrolled: {name} → {dst}")
        self._send_json(200, {"saved": dst, "original": backup, "name": name})

    def do_DELETE(self) -> None:
        prefix = "/speakers/"
        if not self.path.startswith(prefix):
            self._send_json(404, {"error": "not found"})
            return
        name = _safe_name(self.path[len(prefix):])
        if not name:
            self._send_json(400, {"error": "missing name"})
            return
        path = os.path.join(SPEAKERS_DIR, f"{name}.wav")
        if not os.path.isfile(path):
            self._send_json(404, {"error": f"speaker '{name}' not found"})
            return
        os.remove(path)
        print(f"🗑  Speaker removed: {name}")
        self._send_json(200, {"removed": name})


def start_enroll_server() -> threading.Thread:
    os.makedirs(SPEAKERS_DIR, exist_ok=True)
    os.makedirs(SPEAKER_ORIGINALS_DIR, exist_ok=True)
    server = HTTPServer((ENROLL_SERVER_HOST, ENROLL_SERVER_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, name="enroll-server", daemon=True)
    t.start()
    print(
        f"🎙  Enrolment server listening on "
        f"http://{ENROLL_SERVER_HOST}:{ENROLL_SERVER_PORT}"
    )
    return t
