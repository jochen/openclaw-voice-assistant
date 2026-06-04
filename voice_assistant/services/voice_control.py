"""VoiceController — Laufzeit-Steuerung von TTS-Stimme und Sprechtempo.

Verwaltet:
- Laden/Entladen von Speaches-TTS-Modellen (REST API)
- Aktive Stimme + Speed via state.voice_state
- Sprecher→Stimme-Map (persistiert in SPEAKER_VOICES_PATH)

HTTP-Stil analog zu diarization.py: urllib, robuste try/except, Logging.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from voice_assistant import state as _state
from voice_assistant.config import SPEAKER_VOICES_PATH

log = logging.getLogger(__name__)


class VoiceController:
    """Steuert TTS-Stimme, Tempo und Sprecher-Zuordnung zur Laufzeit."""

    def __init__(
        self,
        base: str,
        default_model: str,
        default_voice: str,
        on_voice_changed=None,  # Callable[[], None] | None — nach erfolgreichem set_active
    ) -> None:
        self.base = base.rstrip("/")
        self.default_model = default_model
        self.default_voice = default_voice
        # Hook, der nach einem erfolgreichen Stimmwechsel feuert (z.B. um die
        # "Ja?"-Quittung PIPER_OUT in der neuen aktiven Stimme neu zu rendern).
        # Bewusst minimal gekoppelt: VoiceController kennt weder Quittungstext
        # noch Zielpfad — das steckt im Callback (in assistant.py verdrahtet).
        self.on_voice_changed = on_voice_changed

        # Sprecher→Stimme-Map laden (erstellt Verzeichnis falls nicht vorhanden)
        self._speaker_map: dict[str, dict[str, Any]] = {}
        self._load_map()

    # ------------------------------------------------------------------
    # Persistenz
    # ------------------------------------------------------------------

    def _load_map(self) -> None:
        try:
            os.makedirs(os.path.dirname(SPEAKER_VOICES_PATH), exist_ok=True)
            if os.path.exists(SPEAKER_VOICES_PATH):
                with open(SPEAKER_VOICES_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._speaker_map = data
                    log.debug("VoiceController: Map geladen (%d Sprecher)", len(data))
        except Exception as exc:
            log.warning("VoiceController: Fehler beim Laden der Speaker-Map: %s", exc)

    def _save_map(self) -> None:
        try:
            os.makedirs(os.path.dirname(SPEAKER_VOICES_PATH), exist_ok=True)
            with open(SPEAKER_VOICES_PATH, "w", encoding="utf-8") as f:
                json.dump(self._speaker_map, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.warning("VoiceController: Fehler beim Speichern der Speaker-Map: %s", exc)

    # ------------------------------------------------------------------
    # Speaches REST-Aufrufe
    # ------------------------------------------------------------------

    def list_available(self, lang: str = "de") -> list[dict]:
        """Gibt kompakte Liste {model, voice, language} der verfügbaren TTS-Stimmen.

        lang filtert auf dem language-Feld der Registry:
          "de"  → nur deutsche Stimmen (language enthält 'de'/'de_DE') [Default]
          "en"  → nur englische Stimmen (language enthält 'en')
          "all" → kein Filter
        Unbekannte Werte werden wie "de" behandelt.
        """
        lang = (lang or "de").lower()
        if lang not in ("de", "en", "all"):
            lang = "de"

        url = f"{self.base}/v1/registry?task=text-to-speech"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            log.warning("VoiceController.list_available: %s", exc)
            return []

        result: list[dict] = []
        for entry in data.get("data", []):
            model_id = entry.get("id", "")
            # language kann eine Liste oder ein String sein
            lang_raw = entry.get("language", "")
            if isinstance(lang_raw, list):
                lang_parts = [str(x) for x in lang_raw]
            else:
                lang_parts = [str(lang_raw)]
            language = ",".join(lang_parts)

            if lang != "all":
                prefix = lang  # "de" oder "en"
                matches = any(
                    p.lower() == prefix or p.lower().startswith(prefix + "_")
                    for p in lang_parts
                )
                if not matches:
                    continue

            for voice in entry.get("voices", []):
                voice_id = voice.get("id", "") if isinstance(voice, dict) else str(voice)
                result.append({"model": model_id, "voice": voice_id, "language": language})
        return result

    def loaded_models(self) -> set[str]:
        """Gibt IDs aller aktuell geladenen TTS-Modelle zurück."""
        url = f"{self.base}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            log.warning("VoiceController.loaded_models: %s", exc)
            return set()

        return {
            entry["id"]
            for entry in data.get("data", [])
            if entry.get("task") == "text-to-speech"
        }

    def ensure_loaded(self, model: str) -> bool:
        """Lädt das Modell, falls es noch nicht geladen ist. True = OK."""
        try:
            if model in self.loaded_models():
                return True
        except Exception:
            pass
        # POST zum Laden
        encoded = urllib.parse.quote(model, safe="")
        url = f"{self.base}/v1/models/{encoded}"
        req = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
            log.info("VoiceController: Modell geladen '%s' → HTTP %s", model, status)
            return True
        except urllib.error.HTTPError as exc:
            log.warning("VoiceController.ensure_loaded HTTP %s für '%s': %s",
                        exc.code, model, exc.read().decode(errors="replace")[:120])
            return False
        except Exception as exc:
            log.warning("VoiceController.ensure_loaded Fehler für '%s': %s", model, exc)
            return False

    def _unload_model(self, model: str) -> bool:
        """Entlädt ein Modell via DELETE. Default-Modell wird nie entladen."""
        if model == self.default_model:
            log.debug("VoiceController: Default-Modell wird nicht entladen.")
            return False
        encoded = urllib.parse.quote(model, safe="")
        url = f"{self.base}/v1/models/{encoded}"
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                log.info("VoiceController: Modell entladen '%s' → HTTP %s", model, resp.status)
            return True
        except urllib.error.HTTPError as exc:
            log.warning("VoiceController.unload HTTP %s für '%s'", exc.code, model)
            return False
        except Exception as exc:
            log.warning("VoiceController.unload Fehler für '%s': %s", model, exc)
            return False

    # ------------------------------------------------------------------
    # Öffentliche Steuer-Methoden
    # ------------------------------------------------------------------

    def set_active(self, model: str, voice: str, speed: float | None = None) -> bool:
        """Lädt model (falls nötig) und setzt es als aktiv.

        Das vorherige Nicht-Default-Modell wird nach dem Laden entladen,
        damit GPU-RAM freigegeben wird.
        """
        prev_model, _, _ = _state.voice_state.get()

        ok = self.ensure_loaded(model)
        if not ok:
            log.warning("VoiceController.set_active: '%s' konnte nicht geladen werden.", model)
            return False

        _state.voice_state.set(model=model, voice=voice, speed=speed)
        log.info("VoiceController: aktiv = model='%s' voice='%s' speed=%s", model, voice, speed)

        # Vorheriges Non-Default-Modell entladen (wenn es sich geändert hat)
        if (
            prev_model is not None
            and prev_model != model
            and prev_model != self.default_model
        ):
            self._unload_model(prev_model)

        # "Ja?"-Quittung in der nun aktiven Stimme neu rendern (best effort).
        if self.on_voice_changed is not None:
            try:
                self.on_voice_changed()
            except Exception as exc:
                log.warning("VoiceController.on_voice_changed Fehler: %s", exc)

        return True

    def set_speed(self, speed: float) -> None:
        """Setzt nur das Sprechtempo (ohne Modell/Voice zu ändern)."""
        _state.voice_state.set(speed=speed)
        log.info("VoiceController: speed=%s", speed)

    def set_for_speaker(
        self, speaker: str, model: str, voice: str, speed: float | None = None
    ) -> bool:
        """Setzt die Stimme und merkt sie dauerhaft für diesen Sprecher."""
        ok = self.set_active(model, voice, speed)
        if ok:
            entry: dict[str, Any] = {"model": model, "voice": voice}
            if speed is not None:
                entry["speed"] = speed
            self._speaker_map[speaker] = entry
            self._save_map()
            log.info("VoiceController: '%s' → %s gespeichert", speaker, entry)
        return ok

    def apply_speaker_default(self, speaker: str | None) -> None:
        """Setzt state.voice_state sofort auf den gespeicherten Wert für speaker.

        Das Laden des Modells (ensure_loaded) läuft fire-and-forget in einem
        Daemon-Thread, damit der Haupt-Loop nicht blockiert. Die state-Werte
        werden sofort gesetzt — SpeachesTts.synth macht den 404-Retry, falls
        das Modell beim ersten Satz noch nicht vollständig geladen ist.
        """
        if speaker and speaker in self._speaker_map:
            entry = self._speaker_map[speaker]
            m = entry.get("model", self.default_model)
            v = entry.get("voice", self.default_voice)
            sp = entry.get("speed", 1.0)
            _state.voice_state.set(model=m, voice=v, speed=sp)
            log.info("VoiceController: apply '%s' → model='%s' voice='%s' speed=%s",
                     speaker, m, v, sp)
            # Async laden (fire-and-forget)
            t = threading.Thread(target=self.ensure_loaded, args=(m,), daemon=True)
            t.start()
        else:
            # Keine gespeicherte Präferenz für diesen Sprecher → aktuell aktive
            # Stimme BEIBEHALTEN (nicht auf Profil-Default zurücksetzen). So
            # bleibt ein manueller Wechsel (voice_set_voice ohne for_speaker)
            # über Turns hinweg bestehen.
            log.debug("VoiceController: kein Eintrag für '%s' → aktive Stimme beibehalten", speaker)

    def unload(self, model: str) -> bool:
        """Entlädt ein Modell (Default wird nie entladen)."""
        return self._unload_model(model)
