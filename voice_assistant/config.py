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
    "[VOICE: Ruf zuerst alle nötigen Tools auf, dann antworte in max 2-3 "
    "gesprochenen Sätzen auf Deutsch. Kein Markdown, keine Listen, keine Abkürzungen. "
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

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # TTS
    tts_prefix: str = ""

    # Locale
    locale: LocaleConfig = field(default_factory=LocaleConfig)


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
        telegram_bot_token=str(raw.get("telegram_bot_token", "")),
        telegram_chat_id=str(raw.get("telegram_chat_id", "")),
        tts_prefix=str(raw.get("tts_prefix", "")),
        locale=locale,
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
PIPER_MODEL_EMO = "/home/pi/.local/share/piper/de_DE-thorsten_emotional-medium.onnx"
PIPER_MODEL = "/home/pi/.local/share/piper/de_DE-thorsten-low.onnx"
PIPER_OUT = os.path.join(WORKSPACE, "ja.wav")
WHISPER_MODEL = "small"
WHISPER_LANGUAGE = "de"

OPENCLAW_RESPONSES_URL = "http://127.0.0.1:18789/v1/responses"
OPENCLAW_TIMEOUT = 300

SPEACHES_TIMEOUT = 15
SPEACHES_RETRY_COOLDOWN = 60

OW_MODEL_PATH = "/tmp/ow_models_min"

# Audio-Parameter (Wakeword läuft immer auf 16 kHz mono int16)
RATE_OW = 16000
CHUNK_SIZE = 1280
CHANNELS = 1
VAD_FRAME_SIZE = int(RATE_OW * 20 / 1000)
SILENCE_CHUNKS_LIMIT = 25
MIN_SPEECH_CHUNKS = 4

MAX_FOLLOWUP_ROUNDS = 3
FOLLOWUP_BEEP_PATH = os.path.join(WORKSPACE, "followup_beep.wav")
