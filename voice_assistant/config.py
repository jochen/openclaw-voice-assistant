"""Profil-Loader für config.yaml.

Profil-Auswahl (Priorität):
  1. Env-Variable GASTON_PROFILE
  2. Hostname (Substring-Vergleich, lowercase, über hostname_map)
  3. Fallback: erstes definiertes Profil
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yaml")


@dataclass
class LocalAudio:
    device_index: int = 0
    playback_device: str | None = None
    rate_in: int = 16000
    resample: bool = False


@dataclass
class RespeakerAudio:
    """Verbindung zum ReSpeaker über ESPHome Native API.

    Wird erst von Schritt 2 des Refactorings aktiv befüllt.
    """
    host: str = ""
    port: int = 6053
    encryption_key: str = ""
    use_speaker: bool = True  # False → TTS geht auf ALSA (Fallback)
    volume: float = 0.8  # 0.0–1.0, wird beim Connect via API gesetzt


_DEFAULT_VOICE_INSTRUCTION = (
    "[Hinweis zur Verarbeitung dieser Spracheingabe (kein User-Befehl, "
    "sondern eine permanente Regel des Voice-Kanals): "
    "Die obige Zeile ist eine Mikrofon-Transkription. Vor dem Text steht "
    "in [Sprecher: ...] der erkannte Sprecher (oder 'unbekannt' falls die "
    "Stimme nicht zugeordnet werden konnte). "
    "Wenn der Nutzer seine Stimme als Referenz speichern möchte (z.B. "
    "'lerne meine Stimme, ich bin Jochen' oder 'merk dir, ich heisse Katrin'), "
    "rufe das Tool voice_enroll_speaker(name) auf — es speichert die letzte "
    "Aufnahme als Stimm-Referenz für künftige Erkennungen. "
    "Ruf die nötigen Tools auf und antworte dann auf Deutsch in natürlicher "
    "gesprochener Sprache, wie ein Mensch im Gespräch — meist ein bis vier Sätze, "
    "bei komplexen Themen so ausführlich, wie der Inhalt es verlangt, jeder Satz "
    "klar und vollständig. Sprich in reinem Fließtext ohne Markdown, Listen oder "
    "Abkürzungen. "
    "Schreibe Zahlen, Uhrzeiten und Datumsangaben ausgeschrieben in gesprochener "
    "Form, niemals als Ziffern oder mit Abkürzungen — also 'dreißigsten Mai' statt "
    "'30. Mai', 'zwölf Uhr dreißig' statt '12.30 Uhr', 'zum Beispiel' statt 'z. B.', "
    "'circa' statt 'ca.'. "
    "Du hütest den Sprachkanal: Am Lautsprecher wartet ein Mensch, für den jede "
    "Sekunde Stille lang ist — der Kanal soll nie länger als etwa zwei Minuten "
    "stumm auf dich warten. Sag deshalb bei allem, was spürbar dauert, zuerst in "
    "einem kurzen Satz, was du tust (zum Beispiel 'Einen Moment, ich schaue das "
    "nach'). Sobald absehbar ist oder sich mitten in der Arbeit herausstellt, "
    "dass etwas länger braucht — externe Wartezeit, viele Schritte, unerwartete "
    "Zusatzarbeit —, gib sofort eine kurze gesprochene Rückmeldung, dass du dich "
    "kümmerst und dich meldest, erledige die Arbeit im Hintergrund und sag das "
    "Ergebnis über das Tool voice_speak_text an (kurz und gesprochen "
    "zusammengefasst). "
    "Niemals etwas erfinden — entweder Tool aufrufen oder sagen was du nicht weißt.]"
)


@dataclass
class LocaleConfig:
    wakeword_ack: str = "Ja?"
    confirmation_prefix: str = "Ich habe verstanden: "
    no_reply_fallback: str = "Entschuldigung, ich konnte keine Antwort erhalten."
    openclaw_voice_instruction: str = _DEFAULT_VOICE_INSTRUCTION
    thinking_phrases: list = field(default_factory=lambda: [
        "Einen Moment bitte.",
        "Ich schaue kurz nach.",
        "Ich bin noch dabei.",
        "Fast fertig.",
        "Noch einen Augenblick.",
    ])


@dataclass
class LedsConfig:
    wled_enabled: bool = True
    wled_host: str = "wled.local"
    respeaker_ring_enabled: bool = False


@dataclass
class ActuatorConfig:
    """Voice-Aktuator v1 — schneller lokaler Schalt-Pfad (siehe ACTUATOR_V1_PLAN.md
    und voice_assistant/services/actuator.py). Default enabled=False: Profile ohne
    den `actuator:`-Block verhalten sich exakt wie vor diesem Umbau."""
    enabled: bool = False
    base_url: str = "http://<hausautomation>:1880/voiceact"
    # leer -> Default <repo-root>/voiceact-token.txt (siehe _parse_profile,
    # aus PROJECT_DIR abgeleitet statt hart kodiert)
    token_file: str = ""
    llm_url: str = "http://localhost:8090/v1/chat/completions"
    llm_timeout: float = 5.0
    intent_timeout: float = 1.5
    mqtt_host: str = "<hausautomation>"
    mqtt_port: int = 1883
    refresh_poll_sec: int = 600


@dataclass
class WakewordConfig:
    """Ein aktives Wakeword + sein Routing-Ziel (Multi-Wakeword, Meilenstein 1
    der Wakeword-Studio-Spec, siehe Wakeword_Studio_Spec.md Teil 2).

    bundle: Name eines Bundle-Verzeichnisses unter models/wakewords/<bundle>/
        ODER ein eingebauter openwakeword-Modellname ('hey_jarvis', 'alexa', …).
        Auflösung passiert in wakeword/openwakeword_engine.py.
    threshold: None = aus manifest.yaml (oder Default 0.5) ableiten.
    """
    bundle: str
    session: str = ""
    ack: str = ""
    tts_voice: str = ""
    threshold: float | None = None
    # None = aus manifest.yaml (oder Default 3) ableiten. Siehe WakewordHit.min_hits.
    min_hits: int | None = None
    # None = aus manifest.yaml (oder Default 0.0 = aus) ableiten.
    # Siehe WakewordHit.min_peak.
    min_peak: float | None = None
    # None = aus manifest.yaml (oder Fallback auf min_peak) ableiten.
    # Siehe WakewordHit.min_peak_short.
    min_peak_short: float | None = None


@dataclass
class Profile:
    """Gebündelte Profil-Konfiguration, nach Sachgebiet gruppiert."""
    name: str

    # mode = "local" (ALSA + openwakeword) oder "respeaker" (ESPHome Stream)
    mode: str = "local"

    local_audio: LocalAudio = field(default_factory=LocalAudio)
    respeaker: RespeakerAudio = field(default_factory=RespeakerAudio)
    leds: LedsConfig = field(default_factory=LedsConfig)

    # Speaches
    speaches_base: str = ""
    speaches_stt_model: str = ""
    speaches_tts_model: str = ""
    speaches_tts_voice: str = ""

    # OpenClaw
    openclaw_token: str = ""
    openclaw_session: str = ""
    # Streaming-Antwort (/v1/responses mit stream=true): Sätze werden gesprochen,
    # sobald sie generiert sind. Bei Fehler automatischer Fallback auf non-streaming.
    openclaw_stream: bool = True

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # TTS
    tts_prefix: str = ""

    # VAD-Empfindlichkeit — per Profil überschreibbar
    # vad_aggressiveness: 0 (least) … 3 (most aggressive noise rejection)
    vad_aggressiveness: int = 3
    # RMS-Mindestschwelle für Sprachdetektion; 0 = deaktiviert.
    # Chunks unter diesem Pegel zählen nie als Sprache, auch wenn VAD True sagt.
    # Nützlich in Lärm-Umgebungen (Fablab): Hintergrundrauschen-RMS messen,
    # dann Schwelle knapp darüber setzen (z.B. 900 wenn Rauschen ca. 724 RMS).
    vad_voice_rms_min: float = 0.0
    # Endpointing: Stille-Dauer (Sekunden) bis die Aufnahme beendet wird.
    # ZEITBASIERT — gilt identisch auf allen Profilen, egal wie lang ein
    # Audio-Chunk je nach Quelle real ist (ALSA-16k=80ms, ALSA-48k-resample≈27ms,
    # ReSpeaker=40ms). Der frühere chunk-basierte Wert war je Profil 0,67–2,0 s.
    silence_seconds: float = 2.0
    # Legacy-Override in *Chunks*. > 0 schlägt silence_seconds; nur für
    # Rückwärtskompatibilität. Bevorzugt silence_seconds setzen.
    silence_chunks_limit: int = 0

    # Locale
    locale: LocaleConfig = field(default_factory=LocaleConfig)

    # Wakewords — fehlt der Block in config.yaml: ein Eintrag 'hey_jarvis'
    # mit Profil-Defaults (Rückwärtskompatibilität, siehe _parse_wakewords).
    wakewords: list = field(default_factory=list)

    # Voice-Aktuator v1 — fehlt der Block: enabled=False, Profil verhält sich
    # wie bisher (siehe ActuatorConfig).
    actuator: ActuatorConfig = field(default_factory=ActuatorConfig)


def _load_yaml() -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("❌  PyYAML not installed: pip install pyyaml")
        sys.exit(1)
    if not os.path.exists(CONFIG_PATH):
        print(f"❌  config.yaml not found: {CONFIG_PATH}")
        print("    Copy config.example.yaml to config.yaml and fill in your values.")
        sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _detect_profile_name(cfg: dict[str, Any]) -> str:
    profiles = cfg.get("profiles", {})
    hostname_map = cfg.get("hostname_map", {})

    env = os.environ.get("GASTON_PROFILE", "").strip().lower()
    if env and env in profiles:
        return env

    hostname = socket.gethostname().lower()
    for key, profile in hostname_map.items():
        if key in hostname:
            return profile

    fallback = next(iter(profiles), None)
    if fallback:
        print(f"⚠️  No profile for hostname '{hostname}' → using '{fallback}'")
        return fallback

    print("❌  No profiles defined in config.yaml.")
    sys.exit(1)


def _parse_wakewords(
    raw: dict[str, Any], openclaw_session: str, speaches_tts_voice: str, wakeword_ack: str
) -> list[WakewordConfig]:
    """Baut die Liste aktiver Wakewords aus dem optionalen `wakewords:`-Block.

    Fehlt der Block: ein Eintrag 'hey_jarvis' mit den Profil-Defaults — exakt
    das bisherige Verhalten (Rückwärtskompatibilität wie beim alten flachen
    YAML-Schema).
    """
    entries_raw = raw.get("wakewords")
    if not entries_raw:
        return [
            WakewordConfig(
                bundle="hey_jarvis",
                session=openclaw_session,
                ack=wakeword_ack,
                tts_voice=speaches_tts_voice,
            )
        ]

    result: list[WakewordConfig] = []
    for entry in entries_raw:
        bundle = str((entry or {}).get("bundle", "")).strip()
        if not bundle:
            print("⚠️  wakewords-Eintrag ohne 'bundle' übersprungen")
            continue
        threshold_raw = entry.get("threshold")
        min_hits_raw = entry.get("min_hits")
        min_peak_raw = entry.get("min_peak")
        min_peak_short_raw = entry.get("min_peak_short")
        result.append(
            WakewordConfig(
                bundle=bundle,
                session=str(entry.get("session") or openclaw_session),
                ack=str(entry.get("ack") or wakeword_ack),
                tts_voice=str(entry.get("tts_voice") or speaches_tts_voice),
                threshold=float(threshold_raw) if threshold_raw is not None else None,
                min_hits=int(min_hits_raw) if min_hits_raw is not None else None,
                min_peak=float(min_peak_raw) if min_peak_raw is not None else None,
                min_peak_short=float(min_peak_short_raw) if min_peak_short_raw is not None else None,
            )
        )
    if not result:
        print("⚠️  wakewords-Block leer/ungültig → Fallback auf 'hey_jarvis'")
        return [
            WakewordConfig(
                bundle="hey_jarvis",
                session=openclaw_session,
                ack=wakeword_ack,
                tts_voice=speaches_tts_voice,
            )
        ]
    return result


def _parse_profile(name: str, raw: dict[str, Any]) -> Profile:
    """Baut aus dem rohen Profil-Dict ein Profile-Objekt.

    Unterstützt sowohl das *alte* flache Schema (device_index, playback_device, …
    direkt auf Profil-Ebene) als auch das neue geschachtelte Schema (mit
    `mode`, `local_audio`, `respeaker`, `leds`).
    """
    mode = str(raw.get("mode", "local")).lower()
    if mode not in ("local", "respeaker"):
        print(f"⚠️  Unknown mode '{mode}' in profile '{name}' → using 'local'")
        mode = "local"

    # --- Local-Audio: neues Schema hat Vorrang, altes ist Fallback ---
    local_raw = raw.get("local_audio") or raw.get("alsa") or {}
    local_audio = LocalAudio(
        device_index=int(local_raw.get("device_index", raw.get("device_index", 0))),
        playback_device=local_raw.get("playback_device", raw.get("playback_device")),
        rate_in=int(local_raw.get("rate_in", raw.get("rate_in", 16000))),
        resample=bool(local_raw.get("resample", raw.get("resample", False))),
    )

    # --- Respeaker ---
    resp_raw = raw.get("respeaker") or {}
    respeaker = RespeakerAudio(
        host=str(resp_raw.get("host", "")),
        port=int(resp_raw.get("port", 6053)),
        encryption_key=str(resp_raw.get("encryption_key", "")),
        use_speaker=bool(resp_raw.get("use_speaker", True)),
        volume=float(resp_raw.get("volume", 0.8)),
    )

    # --- LEDs: neues Schema + Rückwärtskompatibilität für wled_host ---
    leds_raw = raw.get("leds") or {}
    wled_raw = leds_raw.get("wled") or {}
    ring_raw = leds_raw.get("respeaker_ring") or {}
    wled_host = wled_raw.get("host") or raw.get("wled_host") or "wled.local"
    leds = LedsConfig(
        wled_enabled=bool(wled_raw.get("enabled", True)),
        wled_host=str(wled_host),
        respeaker_ring_enabled=bool(ring_raw.get("enabled", False)),
    )

    # --- Aktuator: neues Schema, analog zum leds-Block ---
    actuator_raw = raw.get("actuator") or {}
    _dact = ActuatorConfig()
    actuator = ActuatorConfig(
        enabled=bool(actuator_raw.get("enabled", _dact.enabled)),
        base_url=str(actuator_raw.get("base_url", _dact.base_url)),
        token_file=str(actuator_raw.get("token_file") or os.path.join(PROJECT_DIR, "voiceact-token.txt")),
        llm_url=str(actuator_raw.get("llm_url", _dact.llm_url)),
        llm_timeout=float(actuator_raw.get("llm_timeout", _dact.llm_timeout)),
        intent_timeout=float(actuator_raw.get("intent_timeout", _dact.intent_timeout)),
        mqtt_host=str(actuator_raw.get("mqtt_host", _dact.mqtt_host)),
        mqtt_port=int(actuator_raw.get("mqtt_port", _dact.mqtt_port)),
        refresh_poll_sec=int(actuator_raw.get("refresh_poll_sec", _dact.refresh_poll_sec)),
    )

    locale_raw = raw.get("locale") or {}
    _dloc = LocaleConfig()
    locale = LocaleConfig(
        wakeword_ack=str(locale_raw.get("wakeword_ack", _dloc.wakeword_ack)),
        confirmation_prefix=str(locale_raw.get("confirmation_prefix", _dloc.confirmation_prefix)),
        no_reply_fallback=str(locale_raw.get("no_reply_fallback", _dloc.no_reply_fallback)),
        openclaw_voice_instruction=str(locale_raw.get("openclaw_voice_instruction", _dloc.openclaw_voice_instruction)),
        thinking_phrases=list(locale_raw.get("thinking_phrases", _dloc.thinking_phrases)),
    )

    return Profile(
        name=name,
        mode=mode,
        local_audio=local_audio,
        respeaker=respeaker,
        leds=leds,
        speaches_base=str(raw.get("speaches_base", "")),
        speaches_stt_model=str(raw.get("speaches_stt_model", "")),
        speaches_tts_model=str(raw.get("speaches_tts_model", "")),
        speaches_tts_voice=str(raw.get("speaches_tts_voice", "")),
        openclaw_token=str(raw.get("openclaw_token", "")),
        openclaw_session=str(raw.get("openclaw_session", "")),
        openclaw_stream=bool(raw.get("openclaw_stream", True)),
        telegram_bot_token=str(raw.get("telegram_bot_token", "")),
        telegram_chat_id=str(raw.get("telegram_chat_id", "")),
        tts_prefix=str(raw.get("tts_prefix", "")),
        vad_aggressiveness=int(raw.get("vad_aggressiveness", 3)),
        vad_voice_rms_min=float(raw.get("vad_voice_rms_min", 0.0)),
        silence_seconds=float(raw.get("silence_seconds", 2.0)),
        silence_chunks_limit=int(raw.get("silence_chunks_limit", 0)),
        locale=locale,
        actuator=actuator,
        wakewords=_parse_wakewords(
            raw,
            openclaw_session=str(raw.get("openclaw_session", "")),
            speaches_tts_voice=str(raw.get("speaches_tts_voice", "")),
            wakeword_ack=locale.wakeword_ack,
        ),
    )


def load_profile() -> Profile:
    cfg = _load_yaml()
    name = _detect_profile_name(cfg)
    raw = cfg["profiles"][name]
    profile = _parse_profile(name, raw)
    print(f"🖥️  Profile: {name} (hostname: {socket.gethostname()}, mode: {profile.mode})")
    return profile


# --- Konstanten, die profil-unabhängig sind ---
WORKSPACE = "/home/pi/.openclaw/workspace"
PIPER_MODEL_EMO = "/home/pi/openclaw_voice_assist/models/piper/de_DE-thorsten_emotional-medium.onnx"
PIPER_MODEL = "/home/pi/openclaw_voice_assist/models/piper/de_DE-thorsten-low.onnx"
PIPER_OUT = os.path.join(WORKSPACE, "ja.wav")
WHISPER_MODEL = "small"
WHISPER_LANGUAGE = "de"

OPENCLAW_RESPONSES_URL = "http://127.0.0.1:18789/v1/responses"
OPENCLAW_TIMEOUT = 300
# SSE-Stream: während langer Tool-Phasen kommen minutenlang keine Bytes —
# der Read-Timeout muss den kompletten Agentic-Loop überleben, sonst reißt
# die Verbindung kurz vor der fertigen Antwort ab (Vorfall 2026-07-02).
OPENCLAW_STREAM_TIMEOUT = 600
# Hauptschleife wartet maximal so lange auf reply_done_event; deckt den
# Stream-Timeout plus Rest ab. Verspätete Antworten werden trotzdem noch
# gesprochen (Worker-Thread läuft weiter, LED-Reset übernimmt der Worker).
OPENCLAW_OVERALL_TIMEOUT = OPENCLAW_STREAM_TIMEOUT + 60

VOICE_ANALYSIS_BASE = "http://<speaches-host>:8001"

SPEACHES_TIMEOUT = 15
SPEACHES_RETRY_COOLDOWN = 60

# Wakeword-Bundles (Wakeword-Studio, Teil 1 der Spec): models/wakewords/<name>/
# mit manifest.yaml + .tflite. Existiert kein Bundle-Verzeichnis für einen
# konfigurierten Namen, wird er als eingebauter openwakeword-Modellname
# durchgereicht (z.B. 'hey_jarvis', 'alexa').
WAKEWORDS_DIR = os.path.join(PROJECT_DIR, "models", "wakewords")

# Audio-Parameter (Wakeword läuft immer auf 16 kHz mono int16)
RATE_OW = 16000
CHUNK_SIZE = 1280
CHANNELS = 1
VAD_FRAME_SIZE = int(RATE_OW * 20 / 1000)
SILENCE_CHUNKS_LIMIT = 25
MIN_SPEECH_CHUNKS = 4

MAX_FOLLOWUP_ROUNDS = 3
FOLLOWUP_BEEP_PATH = os.path.join(WORKSPACE, "followup_beep.wav")
LAST_REPLY_WAV = os.path.join(WORKSPACE, "last_reply.wav")
LAST_REPLY_TXT = os.path.join(WORKSPACE, "last_reply.txt")

# Endpointing-Telemetrie: eine JSONL-Zeile pro Aufnahme/Follow-up zum
# empirischen Tunen von silence_seconds (Pausen-Verhalten je Sprecher).
ENDPOINT_LOG_PATH = os.path.join(WORKSPACE, "endpoint.log")

# Aufnahme-Hard-Cap (Silence-Detection beendet normal früher).
# 30 s erlaubt einen längeren Enrolment-Satz: "lerne meine Stimme, ich bin Jochen,
# und erzähle dir jetzt eine kleine Geschichte ..."
RECORDING_MAX_SEC = 30.0

# Voice-Workspace: Live-Aufnahme + Sprecher-Referenzen
VOICE_DIR = os.path.join(WORKSPACE, "voice")
LAST_RECORDING_PATH = os.path.join(VOICE_DIR, "last_recording.wav")
# Trigger-Audio-Archiv: pro Wakeword-Trigger der Mic-Mitschnitt rund ums
# Wakeword (Ringpuffer, ~3 s vor Trigger) + die anschließende Aufnahme.
# Zweck: False-Positive-Analyse und Retraining mit echten FP-Clips als
# adversarial negatives (Wakeword-Studio auf ai-stack) — die starken FPs
# (Peaks 0.94-0.98, Logs 2026-07-08..13) sind score-seitig nicht filterbar.
TRIGGER_AUDIO_DIR = os.path.join(VOICE_DIR, "triggers")
TRIGGER_AUDIO_MAX_AGE_DAYS = 30
SPEAKERS_DIR = os.path.join(VOICE_DIR, "speakers")
SPEAKER_ORIGINALS_DIR = os.path.join(VOICE_DIR, "originals")
SPEAKER_VOICES_PATH = os.path.join(VOICE_DIR, "speaker_voices.json")

# Schwellwert für „lange Pause" (Sekunden): Eine temporäre, besitzergebundene
# Stimme (per voice_set_voice ohne for_speaker gesetzt) bleibt nur erhalten,
# solange derselbe Sprecher innerhalb dieser Zeit weiterspricht. Vergeht mehr
# Zeit, fällt apply_speaker_default auf den Profil-Default (Thorsten) zurück.
VOICE_RESET_PAUSE_SEC = 180

# Lokaler Enrolment-HTTP-Server (von OpenClaw-Tool angesprochen)
ENROLL_SERVER_HOST = "127.0.0.1"
ENROLL_SERVER_PORT = 18791

# Lokaler Speak-HTTP-Server (OpenClaw kann Text vorlesen lassen)
SPEAK_SERVER_HOST = "127.0.0.1"
SPEAK_SERVER_PORT = 18792

DIARIZATION_TIMEOUT = 15
# Maximale Wartezeit auf Diarization-Ergebnis nach STT-Fertigstellung
DIARIZATION_JOIN_TIMEOUT = 2.0
