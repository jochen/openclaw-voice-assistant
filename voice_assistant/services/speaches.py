"""Speaches-Verfügbarkeit: Cooldown nach Fehler, Start-Check für STT+TTS."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from voice_assistant.config import SPEACHES_RETRY_COOLDOWN


class SpeachesState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stt_available = False
        self.tts_available = False
        self.stt_fail_time = 0.0
        self.tts_fail_time = 0.0

    def stt_ok(self) -> bool:
        with self._lock:
            if self.stt_available:
                return True
            return time.time() - self.stt_fail_time > SPEACHES_RETRY_COOLDOWN

    def tts_ok(self) -> bool:
        with self._lock:
            if self.tts_available:
                return True
            return time.time() - self.tts_fail_time > SPEACHES_RETRY_COOLDOWN

    def mark_stt_ok(self) -> None:
        with self._lock:
            self.stt_available = True
            self.stt_fail_time = 0.0

    def mark_tts_ok(self) -> None:
        with self._lock:
            self.tts_available = True
            self.tts_fail_time = 0.0

    def mark_stt_failed(self) -> None:
        with self._lock:
            self.stt_available = False
            self.stt_fail_time = time.time()

    def mark_tts_failed(self) -> None:
        with self._lock:
            self.tts_available = False
            self.tts_fail_time = time.time()


def check_at_startup(state: SpeachesState, base: str, stt_model: str, tts_model: str) -> None:
    """Prüft ob Speaches erreichbar ist und die konfigurierten Modelle geladen sind."""
    print(f"🔍 Prüfe Speaches ({base})...")
    try:
        req = urllib.request.Request(
            f"{base}/v1/models",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            model_ids = [m["id"] for m in data.get("data", [])]
        print(f"✅ Speaches erreichbar. Modelle: {model_ids}")

        if any(stt_model.lower() in m.lower() for m in model_ids):
            print(f"✅ STT-Modell '{stt_model}' gefunden → Speaches STT aktiv")
            state.mark_stt_ok()
        else:
            print(f"⚠️  STT-Modell '{stt_model}' nicht gefunden → Fallback faster-whisper")
            state.mark_stt_failed()

        if any(tts_model.lower() in m.lower() for m in model_ids):
            print(f"✅ TTS-Modell '{tts_model}' gefunden → Speaches TTS aktiv")
            state.mark_tts_ok()
        else:
            print(f"⚠️  TTS-Modell '{tts_model}' nicht gefunden → Fallback Piper")
            state.mark_tts_failed()
    except Exception as e:
        print(f"⚠️  Speaches nicht erreichbar: {e} → Fallback aktiv")
        state.mark_stt_failed()
        state.mark_tts_failed()
