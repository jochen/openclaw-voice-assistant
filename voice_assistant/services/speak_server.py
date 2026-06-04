"""Lokaler HTTP-Server: OpenClaw kann Text über den Lautsprecher ansagen lassen.

Endpoints:
  POST /speak         Body {"text": "..."} → legt Text in announce_queue
  GET  /status        → {"status": "ok"}
  GET  /analyze-last  → analysiert die zuletzt gesprochene Antwort (WAV + Text)
                         via VOICE_ANALYSIS_BASE/analyze, gibt Report zurück
  GET  /voices        → verfügbare/geladene TTS-Stimmen + aktiver Zustand
  POST /voice/set     → Stimme (optional Tempo / pro Sprecher merken) setzen
  POST /voice/speed   → nur Sprechtempo ändern
  POST /voice/unload  → ein TTS-Modell entladen
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from voice_assistant.config import (
    LAST_REPLY_TXT,
    LAST_REPLY_WAV,
    SPEAK_SERVER_HOST,
    SPEAK_SERVER_PORT,
    VOICE_ANALYSIS_BASE,
)
from voice_assistant.state import (
    STATE_LISTENING,
    announce_queue,
    current_state,
)
from voice_assistant.services import telegram
from voice_assistant.services.tts import ReplySpeaker

# Vom start_speak_server() gesetzt, damit der Request-Handler ihn erreicht
# (gleiches Muster wie der modul-globale announce_queue-Zustand).
_voice_controller = None


def _resolve_model_for_voice(available: list[dict], voice: str) -> str | None:
    """Findet das Modell zu einer Stimme. Bevorzugt -medium, sonst erstes."""
    matches = [e for e in available if e.get("voice") == voice]
    if not matches:
        return None
    for e in matches:
        if "-medium" in (e.get("model") or ""):
            return e["model"]
    return matches[0].get("model")


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
        if self.path == "/analyze-last":
            self._handle_analyze_last()
            return
        if self.path == "/voices":
            self._handle_voices()
            return
        self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------
    # Voice-Control-Routen
    # ------------------------------------------------------------------

    def _active_state(self) -> dict:
        from voice_assistant.state import voice_state

        model, voice, speed = voice_state.get()
        return {"model": model, "voice": voice, "speed": speed}

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _handle_voices(self) -> None:
        from voice_assistant.state import voice_state

        if _voice_controller is None:
            self._send_json(503, {"error": "voice controller not available"})
            return
        try:
            available = _voice_controller.list_available()
            loaded = sorted(_voice_controller.loaded_models())
        except Exception as e:
            self._send_json(502, {"error": f"voice controller error: {e}"})
            return
        self._send_json(200, {
            "available": available,
            "loaded": loaded,
            "active": self._active_state(),
            "last_speaker": voice_state.get_last_speaker(),
        })

    def _handle_voice_set(self) -> None:
        if _voice_controller is None:
            self._send_json(503, {"error": "voice controller not available"})
            return
        body = self._read_json_body()
        voice = str(body.get("voice", "")).strip()
        if not voice:
            self._send_json(400, {"error": "missing required field 'voice'"})
            return
        model = body.get("model")
        model = str(model).strip() if model else ""
        for_speaker = body.get("for_speaker")
        for_speaker = str(for_speaker).strip() if for_speaker else ""
        speed = body.get("speed")
        if speed is not None:
            try:
                speed = float(speed)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "field 'speed' must be a number"})
                return

        if not model:
            try:
                available = _voice_controller.list_available()
            except Exception as e:
                self._send_json(502, {"error": f"voice controller error: {e}"})
                return
            model = _resolve_model_for_voice(available, voice)
            if not model:
                self._send_json(400, {
                    "error": f"no model found for voice '{voice}'",
                })
                return

        try:
            if for_speaker:
                ok = _voice_controller.set_for_speaker(for_speaker, model, voice, speed)
            else:
                ok = _voice_controller.set_active(model, voice, speed)
        except Exception as e:
            self._send_json(502, {"error": f"set voice failed: {e}"})
            return
        if not ok:
            self._send_json(502, {
                "error": f"could not load/activate model '{model}'",
            })
            return
        self._send_json(200, {"ok": True, "active": self._active_state()})

    def _handle_voice_speed(self) -> None:
        if _voice_controller is None:
            self._send_json(503, {"error": "voice controller not available"})
            return
        body = self._read_json_body()
        speed = body.get("speed")
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "field 'speed' must be a number"})
            return
        try:
            _voice_controller.set_speed(speed)
        except Exception as e:
            self._send_json(502, {"error": f"set speed failed: {e}"})
            return
        self._send_json(200, {"ok": True, "active": self._active_state()})

    def _handle_voice_unload(self) -> None:
        if _voice_controller is None:
            self._send_json(503, {"error": "voice controller not available"})
            return
        body = self._read_json_body()
        model = str(body.get("model", "")).strip()
        if not model:
            self._send_json(400, {"error": "missing required field 'model'"})
            return
        try:
            ok = _voice_controller.unload(model)
        except Exception as e:
            self._send_json(502, {"error": f"unload failed: {e}"})
            return
        self._send_json(200, {"ok": bool(ok), "model": model})

    def _handle_analyze_last(self) -> None:
        import os

        # Prüfen ob gepufferte Antwort vorliegt
        if not os.path.isfile(LAST_REPLY_WAV):
            self._send_json(404, {"error": "Noch keine gesprochene Antwort gepuffert."})
            return

        try:
            with open(LAST_REPLY_WAV, "rb") as fwav:
                wav_bytes = fwav.read()
        except OSError as e:
            self._send_json(500, {"error": f"WAV lesen fehlgeschlagen: {e}"})
            return

        intended = ""
        if os.path.isfile(LAST_REPLY_TXT):
            try:
                with open(LAST_REPLY_TXT, "r", encoding="utf-8") as ftxt:
                    intended = ftxt.read().strip()
            except OSError:
                pass

        # Multipart-POST an VOICE_ANALYSIS_BASE/analyze
        boundary = "----GastonAnalyzeBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="last_reply.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav_bytes + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="intended"\r\n\r\n'
            f"{intended}\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        req = urllib.request.Request(
            f"{VOICE_ANALYSIS_BASE}/analyze",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                report = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            print(f"[speak-server] Analyse HTTP {e.code}: {err_body[:200]}")
            self._send_json(502, {"error": f"Analyse-Dienst HTTP {e.code}: {err_body[:120]}"})
            return
        except Exception as e:
            print(f"[speak-server] Analyse-Fehler: {e}")
            self._send_json(502, {"error": f"Analyse-Dienst nicht erreichbar: {e}"})
            return

        # intended aus eigenem Puffer als Kontroll-Feld mitliefern
        report.setdefault("intended", intended)
        self._send_json(200, report)

    def do_POST(self) -> None:
        if self.path == "/voice/set":
            self._handle_voice_set()
            return
        if self.path == "/voice/speed":
            self._handle_voice_speed()
            return
        if self.path == "/voice/unload":
            self._handle_voice_unload()
            return
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


def start_speak_server(voice_controller=None) -> threading.Thread:
    global _voice_controller
    _voice_controller = voice_controller
    server = HTTPServer((SPEAK_SERVER_HOST, SPEAK_SERVER_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, name="speak-server", daemon=True)
    t.start()
    print(f"📢  Speak server listening on http://{SPEAK_SERVER_HOST}:{SPEAK_SERVER_PORT}")
    return t


def start_announce_worker(
    reply_speaker: ReplySpeaker,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> threading.Thread:
    """Drainiert announce_queue, aber nur wenn der Voice Assistant im LISTENING-State ist.

    Ansagen (z.B. von voice_speak_text bei Hintergrund-Tasks) werden zusätzlich
    in den Telegram-Chat gespiegelt, damit die geteilte Voice+Chat-Session
    vollständig bleibt.
    """
    from voice_assistant.services.leds import LED_IDLE

    def _run() -> None:
        while True:
            text = announce_queue.get()  # blockiert bis was da ist
            # Warten bis wir im LISTENING-State sind (nicht aufnehmen, nicht antworten)
            while current_state[0] != STATE_LISTENING:
                time.sleep(0.25)
            print(f"📢  Announcing: '{text[:80]}{'...' if len(text) > 80 else ''}'")
            if telegram_bot_token and telegram_chat_id:
                telegram.send(telegram_bot_token, telegram_chat_id, text, prefix="🔊 ")
            reply_speaker.speak(text)
            reply_speaker.leds.set_phase(LED_IDLE)

    t = threading.Thread(target=_run, name="announce-worker", daemon=True)
    t.start()
    return t
