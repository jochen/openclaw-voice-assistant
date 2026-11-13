"""Lokaler HTTP-Server für Sprecher-Enrolment.

OpenClaw-Tools posten hier hin, um die letzte Aufnahme als Stimm-Referenz
abzulegen.

Endpoints:
  POST   /enroll          Body {"name": "jochen"} → kopiert last_recording.wav nach speakers/jochen.wav
  GET    /speakers        → {"speakers": ["jochen", "katrin", ...]}
  DELETE /speakers/<name> → löscht Referenz

Eine BESTEHENDE Referenz überschreibt nur ein erkannter Sprecher (Jochen,
2026-10-24): sonst könnte sich ein Fremder als "jochen" anlernen und danach
die Sprecher-Schranke passieren (SPEAKER_STATE.md). Einen neuen Namen darf
auch ein unbekannter Sprecher anlernen — wer zum ersten Mal angelernt wird,
ist immer unbekannt. Entschieden wird hier und nicht nur in AGENTS.md: was
nur im Prompt steht, wird wegargumentiert.

Jedes Anlernen, Erneuern und Löschen meldet der Dienst selbst in den
Telegram-Kanal des Profils, mit dem Sprecher, der dabei erkannt war (seit
2026-11-11, Jochen). Eine neue Referenz ist eine Identität: wer sich unter
einem freien Namen anlernt ("felix"), wird danach als diese Person erkannt.
Verhindern lässt sich das nicht (der Erste ist immer unbekannt), aber sichtbar
machen — und zwar vom Code, damit es der Brain nicht vergessen kann.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from voice_assistant.config import (
    CURRENT_SPEAKER_PATH,
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


# Wie claw-power (SPEAKER_STATE.md): nur ein frischer Turn zählt. Zwischen
# Trainingssatz und Werkzeug-Aufruf liegen Sekunden, nicht Minuten.
_FENSTER_S = 120


def ueberschreiben_verweigert(path: str = CURRENT_SPEAKER_PATH,
                              jetzt: float | None = None) -> str | None:
    """Grund, warum eine bestehende Referenz NICHT überschrieben wird, sonst None.

    Fehlt die Datei, ist sie kaputt oder alt, zählt das als nicht erkannt —
    eine Lücke darf nie als Erlaubnis durchgehen.
    """
    jetzt = time.time() if jetzt is None else jetzt
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        status = st.get("status")
        alter = jetzt - float(st.get("epoch", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return "kein Sprecher-Zustand lesbar"
    if alter > _FENSTER_S:
        return f"letzter Sprach-Turn ist {int(alter)} s alt"
    if status != "bekannt":
        return f"letzter Sprach-Turn: Sprecher {st.get('label') or status}"
    return None


# Meldung in den Telegram-Kanal; von start_enroll_server gesetzt.
_melden: Callable[[str], None] | None = None


def _sprecher_jetzt(path: str = CURRENT_SPEAKER_PATH, jetzt: float | None = None) -> str:
    """Wer beim letzten frischen Sprach-Turn erkannt war, für die Meldung."""
    jetzt = time.time() if jetzt is None else jetzt
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if jetzt - float(st.get("epoch", 0)) > _FENSTER_S:
            return "kein frischer Sprach-Turn"
        label = st.get("label") or st.get("status") or "?"
        if st.get("status") == "bekannt" and st.get("sicher") is False:
            label += f" (nicht sicher, {st.get('aehnlichkeit')})"
        return label
    except (OSError, ValueError, TypeError, AttributeError):
        return "unbekannt (kein Sprecher-Zustand)"


def meldung(art: str, name: str, sprecher: str) -> str:
    if art == "neu":
        return (f"🎙️ Neue Stimme angelernt: „{name}“. Erkannt war dabei: {sprecher}. "
                f"Wer das nicht war, lässt die Referenz wieder löschen.")
    if art == "erneuert":
        return f"🎙️ Stimme „{name}“ neu angelernt. Erkannt war dabei: {sprecher}."
    return f"🗑️ Stimm-Referenz „{name}“ gelöscht. Letzter Sprecher: {sprecher}."


def _melde(art: str, name: str) -> None:
    """Im eigenen Thread: Telegram darf die Werkzeug-Antwort nicht aufhalten."""
    if _melden is None:
        return
    text = meldung(art, name, _sprecher_jetzt())
    threading.Thread(target=_melden, args=(text,), name="enroll-meldung", daemon=True).start()


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
        war_da = os.path.isfile(dst)
        if war_da:
            grund = ueberschreiben_verweigert()
            if grund:
                print(f"🚫 Enrolment verweigert: {name} existiert, {grund}")
                self._send_json(403, {"error": (
                    f"Für '{name}' gibt es schon eine Stimm-Referenz. Erneuern darf sie "
                    f"nur ein erkannter Sprecher ({grund}). Entweder einen neuen Namen "
                    "anlernen oder ein erkannter Sprecher gibt den Auftrag.")})
                return
        shutil.copy2(LAST_RECORDING_PATH, dst)

        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        backup = os.path.join(SPEAKER_ORIGINALS_DIR, f"{name}-{ts}.wav")
        shutil.copy2(LAST_RECORDING_PATH, backup)

        print(f"🎙  Speaker enrolled: {name} → {dst}")
        _melde("erneuert" if war_da else "neu", name)
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
        _melde("geloescht", name)
        self._send_json(200, {"removed": name})


def start_enroll_server(melden: Callable[[str], None] | None = None) -> threading.Thread:
    global _melden
    _melden = melden
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
