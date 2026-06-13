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
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from voice_assistant import state as _state
from voice_assistant.config import SPEAKER_VOICES_PATH, VOICE_RESET_PAUSE_SEC

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

    @staticmethod
    def _norm(name: str | None) -> str | None:
        """Normalisiert einen Sprecher-Namen für case-insensitiven Vergleich.

        Diarization-Label (z.B. 'jochen', vom Enrollment-Dateinamen) und der
        vom LLM via for_speaker übergebene Name (z.B. 'Jochen') müssen sonst
        nicht zusammenpassen → Map-Lookup scheitert. casefold() + strip()
        macht den Abgleich robust.
        """
        if not name:
            return None
        return name.strip().casefold()

    def _load_map(self) -> None:
        try:
            os.makedirs(os.path.dirname(SPEAKER_VOICES_PATH), exist_ok=True)
            if os.path.exists(SPEAKER_VOICES_PATH):
                with open(SPEAKER_VOICES_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # Keys normalisieren (migriert Altbestand wie 'Jochen' → 'jochen')
                    migrated: dict[str, dict[str, Any]] = {}
                    for k, v in data.items():
                        migrated[self._norm(k) or k] = v
                    self._speaker_map = migrated
                    if migrated != data:
                        self._save_map()
                    log.debug("VoiceController: Map geladen (%d Sprecher)", len(migrated))
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

    def _apply(self, model: str, voice: str, speed: float | None = None) -> bool:
        """Lädt model (falls nötig) und setzt es als aktiv (state.voice_state).

        Das vorherige Nicht-Default-Modell wird nach dem Laden entladen,
        damit GPU-RAM freigegeben wird. Feuert on_voice_changed.

        Verwaltet KEINEN Besitz/Default-Status (voice_owner/last_apply_ts) —
        das machen die aufrufenden Methoden (set_active/set_for_speaker/
        apply_speaker_default).
        """
        prev_model, _, _ = _state.voice_state.get()

        ok = self.ensure_loaded(model)
        if not ok:
            log.warning("VoiceController._apply: '%s' konnte nicht geladen werden.", model)
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

    def set_active(self, model: str, voice: str, speed: float | None = None) -> bool:
        """Manueller Tool-Wechsel (voice_set_voice OHNE for_speaker).

        Markiert die Stimme als TEMPORÄR und besitzergebunden: Besitzer ist der
        aktuell erkannte Sprecher (last_speaker). Diese Stimme bleibt nur über
        Turns desselben Sprechers innerhalb VOICE_RESET_PAUSE_SEC erhalten.
        """
        ok = self._apply(model, voice, speed)
        if ok:
            _state.voice_state.set_voice_owner(_state.voice_state.get_last_speaker())
            _state.voice_state.set_last_apply_ts(time.monotonic())
        return ok

    def set_speed(self, speed: float) -> None:
        """Setzt nur das Sprechtempo (ohne Modell/Voice zu ändern)."""
        _state.voice_state.set(speed=speed)
        log.info("VoiceController: speed=%s", speed)

    def set_for_speaker(
        self, speaker: str, model: str, voice: str, speed: float | None = None
    ) -> bool:
        """Setzt die Stimme und merkt sie dauerhaft für diesen Sprecher.

        Die Stimme ist damit GESPEICHERT, nicht temporär — kein voice_owner.
        """
        ok = self._apply(model, voice, speed)
        if ok:
            entry: dict[str, Any] = {"model": model, "voice": voice}
            if speed is not None:
                entry["speed"] = speed
            self._speaker_map[self._norm(speaker) or speaker] = entry
            self._save_map()
            _state.voice_state.set_voice_owner(None)
            _state.voice_state.set_last_apply_ts(time.monotonic())
            log.info("VoiceController: '%s' → %s gespeichert", speaker, entry)
        return ok

    def apply_speaker_default(self, speaker: str | None) -> None:
        """Wählt die passende Stimme für den aktuell erkannten Sprecher.

        Besitzer- und zeitgebundene Semantik (Regeln in Reihenfolge):
          0. speaker NICHT identifiziert (None, z.B. sehr kurze Ansage):
             - innerhalb VOICE_RESET_PAUSE_SEC seit der letzten Anwendung →
               aktuell aktive Stimme HALTEN (nur Zeitstempel auffrischen).
               'None' bedeutet „nicht zuordenbar", nicht „anderer Sprecher" —
               erst ein positiv erkannter ANDERER Sprecher wechselt die Stimme.
             - sonst (Pause abgelaufen, Gesprächsanfang ohne ID) → Default.
          1. speaker hat eine GESPEICHERTE Stimme → diese anwenden (temp-Besitz
             aufheben).
          2. sonst, wenn aktuell eine TEMPORÄRE Stimme aktiv ist UND ihr
             Besitzer == speaker UND die Pause seit der letzten Anwendung
             < VOICE_RESET_PAUSE_SEC → temporäre Stimme BEIBEHALTEN.
          3. sonst → auf Profil-Default (Thorsten, speed 1.0) zurückfallen
             (temp-Besitz aufheben).

        Das Laden des Modells (ensure_loaded) läuft fire-and-forget in einem
        Daemon-Thread, damit der Haupt-Loop nicht blockiert. Die state-Werte
        werden sofort gesetzt — SpeachesTts.synth macht den 404-Retry, falls
        das Modell beim ersten Satz noch nicht vollständig geladen ist.
        """
        now = time.monotonic()
        key = self._norm(speaker)

        # Regel 0: Sprecher nicht identifiziert (kurze Ansage / Diarization-Timeout)
        if key is None:
            last_ts = _state.voice_state.get_last_apply_ts()
            if last_ts and (now - last_ts) < VOICE_RESET_PAUSE_SEC:
                # Direkter Follow-up: aktive Stimme halten, nicht auf Default
                # zurückfallen. Zeitstempel auffrischen, damit eine Kette
                # kurzer Ansagen die Stimme am Leben hält.
                _state.voice_state.set_last_apply_ts(now)
                log.info("VoiceController: apply '?' Regel=halten (Sprecher unbekannt, %.0fs Pause)",
                         now - last_ts)
                return
            # Pause abgelaufen / Gesprächsanfang ohne ID → Default-Reset
            self._apply(self.default_model, self.default_voice, 1.0)
            _state.voice_state.set_voice_owner(None)
            _state.voice_state.set_last_apply_ts(now)
            log.info("VoiceController: apply '?' Regel=default-reset (unbekannt, Pause abgelaufen)")
            return

        # Regel 1: gespeicherte Präferenz
        if key and key in self._speaker_map:
            entry = self._speaker_map[key]
            m = entry.get("model", self.default_model)
            v = entry.get("voice", self.default_voice)
            sp = entry.get("speed", 1.0)
            _state.voice_state.set(model=m, voice=v, speed=sp)
            _state.voice_state.set_voice_owner(None)
            _state.voice_state.set_last_apply_ts(now)
            log.info("VoiceController: apply '%s' Regel=gespeichert → model='%s' voice='%s' speed=%s",
                     speaker, m, v, sp)
            t = threading.Thread(target=self.ensure_loaded, args=(m,), daemon=True)
            t.start()
            return

        # Regel 2: temporäre Stimme desselben Besitzers, Pause noch nicht abgelaufen
        owner = _state.voice_state.get_voice_owner()
        last_ts = _state.voice_state.get_last_apply_ts()
        if owner is not None and self._norm(owner) == key and (now - last_ts) < VOICE_RESET_PAUSE_SEC:
            # nur Zeitstempel auffrischen, Stimme/Besitz unberührt lassen
            _state.voice_state.set_last_apply_ts(now)
            log.info("VoiceController: apply '%s' Regel=temp-behalten (Besitzer, %.0fs Pause)",
                     speaker, now - last_ts)
            return

        # Regel 3: Default-Reset (anderer/voiceless/unbekannter Sprecher oder
        # lange Pause beim selben Sprecher)
        self._apply(self.default_model, self.default_voice, 1.0)
        _state.voice_state.set_voice_owner(None)
        _state.voice_state.set_last_apply_ts(now)
        log.info("VoiceController: apply '%s' Regel=default-reset → model='%s' voice='%s'",
                 speaker, self.default_model, self.default_voice)

    def unload(self, model: str) -> bool:
        """Entlädt ein Modell (Default wird nie entladen)."""
        return self._unload_model(model)
