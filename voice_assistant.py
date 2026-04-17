#!/usr/bin/env python3
"""
OpenClaw Voice Assistant

Wakeword-gesteuerter Sprachassistent für Raspberry Pi.
Verbindet lokale Spracheingabe mit OpenClaw (KI-Backend) und
Speaches (GPU-Container für STT/TTS).

Pipeline:
  Mikrofon → openWakeWord → Aufnahme + WebRTC VAD
      → STT: Speaches /v1/audio/transcriptions  (Fallback: faster-whisper lokal)
      → Bestätigung vorlesen (parallel)
      → POST /v1/responses → OpenClaw (vollständiger Agentic Loop)
      → TTS: Speaches /v1/audio/speech  (Fallback: Piper lokal)
      → Anfrage + Antwort per Telegram spiegeln

STT-Priorität:
  1. Speaches /v1/audio/transcriptions  (OpenAI-kompatibel, GPU-Container)
  2. faster-whisper lokal               (Fallback)

TTS-Priorität:
  1. Speaches /v1/audio/speech          (OpenAI-kompatibel, GPU-Container)
  2. Piper lokal                        (Fallback)

LED-Status:
- LED0 = Blau     (Bereit, warte auf Wakeword)
- LED1 = Grün     (Wakeword erkannt, höre zu)
- LED2 = Gelb     (STT verarbeitet)
- LED3 = Rot      (kurze Pause nach Aufnahme)
- LED4 = Lila     (wartet auf OpenClaw-Antwort)
- LED5 = Cyan     (liest Antwort vor)

Profil-Auswahl: Hostname oder Env-Variable GASTON_PROFILE
  GASTON_PROFILE=clawdpi  →  clawdpi1 (192.168.7.105)
  GASTON_PROFILE=openclaw →  zweiter Pi

Usage: source ~/ow-venv/bin/activate && python live_wakeword_and_wisphertts10.py
"""

import os
import sys
import io
import time
import wave
import socket
import pyaudio
import numpy as np
import subprocess
import threading
import queue
import json
import tempfile
import re
import webrtcvad
import urllib.request
import urllib.error
from scipy.signal import resample_poly

# ---------------------------------------------------------------------------
# Venv-Python sicherstellen
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(_SCRIPT_DIR, "ow-venv", "bin", "python")
if sys.executable != VENV and os.path.exists(VENV):
    os.execv(VENV, [VENV] + sys.argv)

# ===========================================================================
# KONFIGURATION LADEN
# ===========================================================================
# Profil-Daten werden aus config.yaml gelesen (liegt neben diesem Script).
# Vorlage: config.example.yaml
#
# Profil-Auswahl (Priorität):
#   1. Env-Variable GASTON_PROFILE
#   2. Hostname (Teilstring-Vergleich, lowercase)
#   3. Fallback: erstes definiertes Profil

def _load_config() -> dict:
    try:
        import yaml
    except ImportError:
        print("❌  PyYAML nicht installiert: pip install pyyaml")
        sys.exit(1)
    config_path = os.path.join(_SCRIPT_DIR, "config.yaml")
    if not os.path.exists(config_path):
        print(f"❌  config.yaml nicht gefunden: {config_path}")
        print(f"    Kopiere config.example.yaml nach config.yaml und trage deine Werte ein.")
        sys.exit(1)
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def _detect_profile(cfg: dict) -> str:
    profiles      = cfg.get("profiles", {})
    hostname_map  = cfg.get("hostname_map", {})
    env = os.environ.get("GASTON_PROFILE", "").strip().lower()
    if env and env in profiles:
        return env
    hostname = socket.gethostname().lower()
    for key, profile in hostname_map.items():
        if key in hostname:
            return profile
    fallback = next(iter(profiles), None)
    if fallback:
        print(f"⚠️  Kein Profil für Hostname '{hostname}' → verwende '{fallback}'")
        return fallback
    print("❌  Keine Profile in config.yaml definiert.")
    sys.exit(1)

_RAW_CFG     = _load_config()
PROFILE_NAME = _detect_profile(_RAW_CFG)
CFG          = _RAW_CFG["profiles"][PROFILE_NAME]
print(f"🖥️  Profil: {PROFILE_NAME} (Hostname: {socket.gethostname()})")

# ===========================================================================
# Konfiguration aus Profil laden
# ===========================================================================

# Audio
DEVICE_INDEX     = CFG["device_index"]
PLAYBACK_DEVICE  = CFG.get("playback_device", None)
RATE_IN          = CFG["rate_in"]
DO_RESAMPLE      = CFG["resample"]

# Speaches
SPEACHES_BASE        = CFG["speaches_base"]
SPEACHES_STT_MODEL   = CFG["speaches_stt_model"]
SPEACHES_TTS_MODEL   = CFG["speaches_tts_model"]
SPEACHES_TTS_VOICE   = CFG["speaches_tts_voice"]
SPEACHES_TIMEOUT     = 15
SPEACHES_RETRY_COOLDOWN = 60

# OpenClaw
OPENCLAW_RESPONSES_URL = "http://127.0.0.1:18789/v1/responses"
OPENCLAW_TOKEN    = CFG["openclaw_token"]
OPENCLAW_SESSION  = CFG["openclaw_session"]
OPENCLAW_TIMEOUT  = 180

# Telegram
TELEGRAM_BOT_TOKEN = CFG["telegram_bot_token"]
TELEGRAM_CHAT_ID   = CFG["telegram_chat_id"]

# TTS
TTS_PREFIX = CFG.get("tts_prefix", "")

# WLED
WLED_HOST = CFG.get("wled_host", "wled.local")

# ---------------------------------------------------------------------------
# Pfade & Konfiguration – Lokal (Fallback)
# ---------------------------------------------------------------------------
WORKSPACE        = "/home/pi/.openclaw/workspace"
PIPER_MODEL_EMO  = "/home/pi/.local/share/piper/de_DE-thorsten_emotional-medium.onnx"
PIPER_MODEL      = "/home/pi/.local/share/piper/de_DE-thorsten-low.onnx"
PIPER_OUT        = os.path.join(WORKSPACE, "ja.wav")
WHISPER_MODEL    = "small"
WHISPER_LANGUAGE = "de"

# ---------------------------------------------------------------------------
# Shared State
# ---------------------------------------------------------------------------
tts_lock         = threading.Lock()
reply_done_event = threading.Event()
pending_reply    = threading.Event()
pending_reply_text = [None]

# ---------------------------------------------------------------------------
# WLED Controller
# ---------------------------------------------------------------------------
CTRL_CMD = [VENV, os.path.join(_SCRIPT_DIR, "wled_controller.py"), f"--host={WLED_HOST}"]

def set_led(idx: int, r: int, g: int, b: int):
    subprocess.run(
        CTRL_CMD + ["single", str(idx), str(r), str(g), str(b)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def all_leds_off():
    subprocess.run(CTRL_CMD + ["clear"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.05)

all_leds_off()
set_led(0, 0, 0, 255)
time.sleep(0.2)

# ---------------------------------------------------------------------------
# Speaches Availability State  (ersetzt LMStudioState)
# ---------------------------------------------------------------------------
class SpeachesState:
    def __init__(self):
        self._lock         = threading.Lock()
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

    def mark_stt_ok(self):
        with self._lock:
            self.stt_available = True
            self.stt_fail_time = 0.0

    def mark_tts_ok(self):
        with self._lock:
            self.tts_available = True
            self.tts_fail_time = 0.0

    def mark_stt_failed(self):
        with self._lock:
            self.stt_available = False
            self.stt_fail_time = time.time()

    def mark_tts_failed(self):
        with self._lock:
            self.tts_available = False
            self.tts_fail_time = time.time()

speaches = SpeachesState()


def check_speaches_at_startup():
    """Prüft ob der Speaches-Container erreichbar ist und das Modell verfügbar."""
    print(f"🔍 Prüfe Speaches ({SPEACHES_BASE})...")
    try:
        req = urllib.request.Request(
            f"{SPEACHES_BASE}/v1/models",
            headers={"Accept": "application/json"},
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data     = json.loads(resp.read())
            model_ids = [m["id"] for m in data.get("data", [])]
        print(f"✅ Speaches erreichbar. Modelle: {model_ids}")

        if any(SPEACHES_STT_MODEL.lower() in m.lower() for m in model_ids):
            print(f"✅ STT-Modell '{SPEACHES_STT_MODEL}' gefunden → Speaches STT aktiv")
            speaches.mark_stt_ok()
        else:
            print(f"⚠️  STT-Modell '{SPEACHES_STT_MODEL}' nicht gefunden → Fallback faster-whisper")
            speaches.mark_stt_failed()

        if any(SPEACHES_TTS_MODEL.lower() in m.lower() for m in model_ids):
            print(f"✅ TTS-Modell '{SPEACHES_TTS_MODEL}' gefunden → Speaches TTS aktiv")
            speaches.mark_tts_ok()
        else:
            print(f"⚠️  TTS-Modell '{SPEACHES_TTS_MODEL}' nicht gefunden → Fallback Piper")
            speaches.mark_tts_failed()

    except Exception as e:
        print(f"⚠️  Speaches nicht erreichbar: {e} → Fallback aktiv")
        speaches.mark_stt_failed()
        speaches.mark_tts_failed()

# ---------------------------------------------------------------------------
# faster-whisper STT (lokal, Fallback)
# ---------------------------------------------------------------------------
print("🔧 Lade faster-whisper (lokaler Fallback)...")
from faster_whisper import WhisperModel
stt_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print(f"✅ faster-whisper '{WHISPER_MODEL}' bereit")

check_speaches_at_startup()

# ---------------------------------------------------------------------------
# TTS vorbereiten ("Ja?")
# ---------------------------------------------------------------------------
print("🎤 Erstelle 'Ja?' Antwort mit Piper...")
try:
    subprocess.run(
        ["piper", "--model", PIPER_MODEL_EMO, "--output_file", PIPER_OUT,
         "--speaker=1", "Ja?"],
        check=True, capture_output=True
    )
    print(f"✅ Audiodatei erstellt: {PIPER_OUT}")
except Exception as e:
    print(f"⚠️ TTS-Setup fehlgeschlagen: {e}")

# ---------------------------------------------------------------------------
# openWakeWord
# ---------------------------------------------------------------------------
os.environ["OPENWAKEWORD_MODEL_PATH"] = "/tmp/ow_models_min"
from openwakeword import Model as WakeWordModel
print("🔧 Lade openwakeword 'hey jarvis'...")
wakeword_model = WakeWordModel(wakeword_models=["hey jarvis"])
print("✅ Modelle:", list(wakeword_model.models.keys()))

# ---------------------------------------------------------------------------
# WebRTC VAD
# ---------------------------------------------------------------------------
print("🔧 Initialisiere WebRTC VAD...")
vad = webrtcvad.Vad(1)
print("✅ WebRTC VAD bereit")

# ---------------------------------------------------------------------------
# Audio-Parameter (Profil-abhängig)
# ---------------------------------------------------------------------------
RATE_OW              = 16000
CHUNK_SIZE           = 1280
FORMAT               = pyaudio.paInt16
CHANNELS             = 1
VAD_FRAME_SIZE       = int(RATE_OW * 20 / 1000)
SILENCE_CHUNKS_LIMIT = 25
MIN_SPEECH_CHUNKS    = 4

print(f"🎙️  Audio: RATE_IN={RATE_IN}, RESAMPLE={DO_RESAMPLE}, DEVICE_INDEX={DEVICE_INDEX}")

# ---------------------------------------------------------------------------
# Hilfsfunktionen Audio
# ---------------------------------------------------------------------------
def resample_48_to_16(audio_48: np.ndarray) -> np.ndarray:
    return resample_poly(audio_48, 1, 3).astype(np.int16)

def to_16k(audio_in: np.ndarray) -> np.ndarray:
    """Resampelt nur wenn nötig (RATE_IN != 16000)."""
    if DO_RESAMPLE:
        return resample_48_to_16(audio_in)
    return audio_in

def is_speech_chunk(audio_16: np.ndarray) -> bool:
    result = False
    for i in range(0, len(audio_16), VAD_FRAME_SIZE):
        frame = audio_16[i:i + VAD_FRAME_SIZE]
        if len(frame) == VAD_FRAME_SIZE:
            result |= vad.is_speech(frame.tobytes(), RATE_OW)
    return result

def play_wav(path: str):
    cmd = ["aplay", "-q"]
    if PLAYBACK_DEVICE:
        cmd += ["-D", PLAYBACK_DEVICE]
    cmd.append(path)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def play_ja():
    if os.path.exists(PIPER_OUT):
        print("🔊 Spiele Ja? ...")
        play_wav(PIPER_OUT)

def chunks_to_wav_bytes(audio_chunks: list) -> bytes:
    audio = np.concatenate(audio_chunks)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Markdown-Bereinigung für TTS
# ---------------------------------------------------------------------------
def clean_for_tts(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    text = re.sub(r'#+\s*',         '',    text)
    text = re.sub(r'[-*•]\s+',      '',    text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'[^\w\s\.,!?;:\-äöüÄÖÜß]', '', text)
    text = re.sub(r'\n+',           ' ',   text)
    return text.strip()

# ---------------------------------------------------------------------------
# STT – Speaches (primär, OpenAI-kompatibel)
# ---------------------------------------------------------------------------
def _stt_speaches(wav_bytes: bytes) -> str | None:
    boundary = "----GastonSTTBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f'{SPEACHES_STT_MODEL}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="language"\r\n\r\n'
        f'de\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f'Content-Type: audio/wav\r\n\r\n'
    ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{SPEACHES_BASE}/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=SPEACHES_TIMEOUT) as resp:
            result = json.loads(resp.read())
            text = result.get("text", "").strip()
            speaches.mark_stt_ok()
            return text if text else None
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        print(f"⚠️  Speaches STT HTTP {e.code}: {body_err[:120]}")
        speaches.mark_stt_failed()
        return None
    except Exception as e:
        print(f"⚠️  Speaches STT Fehler: {e}")
        speaches.mark_stt_failed()
        return None

# ---------------------------------------------------------------------------
# STT – faster-whisper lokal (Fallback)
# ---------------------------------------------------------------------------
def _stt_local(audio_chunks: list) -> str:
    audio       = np.concatenate(audio_chunks)
    audio_float = audio.astype(np.float32) / 32768.0
    segments, info = stt_model.transcribe(
        audio_float,
        language=WHISPER_LANGUAGE,
        beam_size=3,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        no_speech_threshold=0.5,
        log_prob_threshold=-1.0,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    print(f"🗣  [faster-whisper] Erkannt: '{text}' ({info.language}, {info.duration:.1f}s)")
    return text

# ---------------------------------------------------------------------------
# STT Worker (Thread)
# ---------------------------------------------------------------------------
def stt_worker(audio_chunks: list, result_queue: queue.Queue):
    text = None
    if speaches.stt_ok():
        print("🔄 STT: Versuche Speaches...")
        wav_bytes = chunks_to_wav_bytes(audio_chunks)
        text = _stt_speaches(wav_bytes)
        if text is not None:
            print(f"🗣  [Speaches STT] Erkannt: '{text}'")
        else:
            print("⚠️  Speaches STT fehlgeschlagen → Fallback auf faster-whisper")
    if text is None:
        print("🔄 STT: Verwende faster-whisper (lokal)...")
        text = _stt_local(audio_chunks)
    result_queue.put(text)

# ---------------------------------------------------------------------------
# TTS – Speaches (primär, OpenAI-kompatibel)
# ---------------------------------------------------------------------------
def _tts_speaches(text: str) -> bytes | None:
    payload = json.dumps({
        "model":           SPEACHES_TTS_MODEL,
        "input":           text,
        "voice":           SPEACHES_TTS_VOICE,
        "response_format": "wav"
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{SPEACHES_BASE}/v1/audio/speech",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=SPEACHES_TIMEOUT) as resp:
            data = resp.read()
            if len(data) < 100:
                raise ValueError(f"TTS Antwort zu klein ({len(data)} Bytes)")
            speaches.mark_tts_ok()
            return data
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        print(f"⚠️  Speaches TTS HTTP {e.code}: {body_err[:120]}")
        speaches.mark_tts_failed()
        return None
    except Exception as e:
        print(f"⚠️  Speaches TTS Fehler: {e}")
        speaches.mark_tts_failed()
        return None

# ---------------------------------------------------------------------------
# TTS – Piper lokal (Fallback)
# ---------------------------------------------------------------------------
def _tts_piper(text: str) -> str | None:
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        subprocess.run(
            ["piper", "--model", PIPER_MODEL, "--output_file", tmp_wav,
             "--speaker=1", text],
            check=True, capture_output=True
        )
        return tmp_wav
    except Exception as e:
        print(f"⚠️  Piper TTS fehlgeschlagen: {e}")
        return None

# ---------------------------------------------------------------------------
# TTS Antwort vorlesen
# ---------------------------------------------------------------------------
def split_into_sentences(text: str) -> list[str]:
    """Splittet Text an Satzenden, filtert leere Teile."""
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if p.strip()]

def _tts_speaches_play(sentence: str):
    """Rendert einen Satz via Speaches und spielt ihn sofort ab."""
    audio_data = _tts_speaches(sentence)
    if audio_data:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_data)
                tmp_wav = f.name
            play_wav(tmp_wav)
        except Exception as e:
            print(f"⚠️  Wiedergabe fehlgeschlagen: {e}")
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                os.unlink(tmp_wav)
        return True
    return False

def speak_reply(text: str, restore_leds: bool = True):
    with tts_lock:
        clean = TTS_PREFIX + clean_for_tts(text)
        if not clean.strip():
            return
        print(f"🔊 Lese vor: '{clean}'")
        all_leds_off()
        set_led(5, 0, 255, 255)

        sentences = split_into_sentences(clean)
        print(f"🔊 {len(sentences)} Satz/Sätze")

        played = False
        if speaches.tts_ok():
            print("🔄 TTS: Speaches (satzweise)...")
            for i, sentence in enumerate(sentences):
                print(f"🔊 Satz {i+1}/{len(sentences)}: '{sentence}'")
                ok = _tts_speaches_play(sentence)
                if not ok:
                    print(f"⚠️  Speaches fehlgeschlagen bei Satz {i+1} → Fallback Piper")
                    remaining = " ".join(sentences[i:])
                    tmp_wav = _tts_piper(remaining)
                    if tmp_wav:
                        play_wav(tmp_wav)
                        os.unlink(tmp_wav)
                    break
            played = True

        if not played:
            print("🔄 TTS: Piper (lokal)...")
            tmp_wav = _tts_piper(clean)
            if tmp_wav:
                play_wav(tmp_wav)
                os.unlink(tmp_wav)
            else:
                print("❌ TTS vollständig fehlgeschlagen")

        all_leds_off()
        if restore_leds:
            set_led(0, 0, 0, 255)
        else:
            set_led(3, 255, 0, 0)
            set_led(4, 128, 0, 255)


#def speak_reply(text: str, restore_leds: bool = True):
#    clean = TTS_PREFIX + clean_for_tts(text)
#    if not clean.strip():
#        return
#    print(f"🔊 Lese vor: '{clean}'")
#    all_leds_off()
#    set_led(5, 0, 255, 255)
#    played = False
#    if speaches.tts_ok():
#        print("🔄 TTS: Versuche Speaches...")
#        audio_data = _tts_speaches(clean)
#        if audio_data:
#            print("🔊 [Speaches TTS] Spiele Audio...")
#            tmp_wav = None
#            try:
#                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
#                    f.write(audio_data)
#                    tmp_wav = f.name
#                play_wav(tmp_wav)
#                played = True
#            except Exception as e:
#                print(f"⚠️  Wiedergabe fehlgeschlagen: {e}")
#            finally:
#                if tmp_wav and os.path.exists(tmp_wav):
#                    os.unlink(tmp_wav)
#        else:
#            print("⚠️  Speaches TTS fehlgeschlagen → Fallback auf Piper")
#    if not played:
#        print("🔄 TTS: Verwende Piper (lokal)...")
#        tmp_wav = _tts_piper(clean)
#        if tmp_wav:
#            play_wav(tmp_wav)
#            os.unlink(tmp_wav)
#        else:
#            print("❌ TTS vollständig fehlgeschlagen")
#    all_leds_off()
#    if restore_leds:
#        set_led(0, 0, 0, 255)
#    else:
#        set_led(3, 255, 0, 0)
#        set_led(4, 128, 0, 255)

# ---------------------------------------------------------------------------
# Telegram – Text senden
# ---------------------------------------------------------------------------
def send_to_telegram(text: str, prefix: str = ""):
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    f"{prefix}{text}" if prefix else text
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"✅ Telegram: '{text[:60]}'")
    except Exception as e:
        print(f"⚠️ Telegram Fehler: {e}")

# ---------------------------------------------------------------------------
# Lebenszeichen während OpenClaw denkt
# ---------------------------------------------------------------------------
THINKING_PHRASES = [
    "Einen Moment bitte.",
    "Ich schaue kurz nach.",
    "Ich bin noch dabei.",
    "Fast fertig.",
    "Noch einen Augenblick.",
]
_thinking_stop = threading.Event()

def _thinking_worker():
    phrases = iter(THINKING_PHRASES)
    if _thinking_stop.wait(timeout=15):
        return
    while not _thinking_stop.is_set():
        phrase = next(phrases, "Ich bin noch dabei.")
        print(f"💭 Lebenszeichen: '{phrase}'")
        tmp_wav = _tts_piper(phrase)
        if tmp_wav:
            with tts_lock:
                if not _thinking_stop.is_set():
                    play_wav(tmp_wav)
            os.unlink(tmp_wav)
        _thinking_stop.wait(timeout=20)

def start_thinking():
    _thinking_stop.clear()
    t = threading.Thread(target=_thinking_worker, daemon=True)
    t.start()
    return t

def stop_thinking():
    _thinking_stop.set()

# ---------------------------------------------------------------------------
# OpenClaw – /v1/responses  (non-streaming, vollständiger Agentic Loop)
# ---------------------------------------------------------------------------
def _query_openclaw(text: str) -> str | None:
    """
    Sendet einen Voice-Turn an /v1/responses und gibt die finale Antwort zurück.
    Wartet auf den vollständigen Agentic Loop inkl. aller Tool-Calls.
    Gibt None zurück bei Fehler.
    """
    voice_input = (
        f"🎤 {text}\n\n"
        f"[VOICE: Ruf zuerst alle nötigen Tools auf, dann antworte in max 2-3 "
        f"gesprochenen Sätzen auf Deutsch. Kein Markdown, keine Listen. "
        f"Niemals etwas erfinden — entweder Tool aufrufen oder sagen was du nicht weißt.]"
    )
    payload = json.dumps({
        "model": "openclaw/main",
        "input": voice_input,
        "user":  OPENCLAW_SESSION,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENCLAW_RESPONSES_URL,
        data=payload,
        headers={
            "Content-Type":          "application/json",
            "Authorization":         f"Bearer {OPENCLAW_TOKEN}",
            "x-openclaw-session-key": OPENCLAW_SESSION,
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENCLAW_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        stop_thinking()
        # Antworttext aus output[].content[].text extrahieren
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    text_out = part.get("text", "").strip()
                    if text_out:
                        return text_out
        print("⚠️  Leere Antwort von /v1/responses")
        return None
    except urllib.error.HTTPError as e:
        print(f"❌ OpenClaw HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        stop_thinking()
        return None
    except Exception as e:
        print(f"❌ OpenClaw Fehler: {e}")
        stop_thinking()
        return None

# ---------------------------------------------------------------------------
# OpenClaw Worker (Thread)
# ---------------------------------------------------------------------------
def openclaw_worker(text: str):
    send_to_telegram(text, prefix="🎤 ")

    full_reply = _query_openclaw(text)

    if full_reply:
        print(f"✅ OpenClaw komplett: '{full_reply[:80]}...'")
        send_to_telegram(full_reply, prefix="🔊 ")
        pending_reply_text[0] = full_reply
        speak_reply(full_reply)
    else:
        pending_reply_text[0] = None
        speak_reply("Entschuldigung, ich konnte keine Antwort erhalten.")

    reply_done_event.set()

# ---------------------------------------------------------------------------
# Zustandsmaschine
# ---------------------------------------------------------------------------
STATE_LISTENING  = 0
STATE_RECORDING  = 1
STATE_PROCESSING = 2
STATE_WAITING    = 3
STATE_PAUSE      = 4

state       = STATE_LISTENING
state_start = time.time()

recorded_chunks = []
silence_counter = 0
speech_detected = False

stt_queue  = queue.Queue()
stt_thread = None

print("\n🎤 Bereit – warte auf 'hey jarvis'...\n")

# ---------------------------------------------------------------------------
# Audio-Stream
# ---------------------------------------------------------------------------
p = pyaudio.PyAudio()
stream = p.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE_IN,
    input=True,
    frames_per_buffer=CHUNK_SIZE,
    input_device_index=DEVICE_INDEX
)

# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------
try:
    while True:
        raw      = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        audio_in = np.frombuffer(raw, dtype=np.int16)
        audio_16 = to_16k(audio_in)
        now      = time.time()

        # --- LISTENING ---
        if state == STATE_LISTENING:
            result = wakeword_model.predict(audio_16)
            score  = result.get("hey jarvis", 0.0)

            if score > 0.5:
                print(f"[{now:.1f}s] 🟢 WAKE WORD! Score: {score:.3f}")
                all_leds_off()
                set_led(1, 0, 255, 0)
                play_ja()
                wakeword_model.reset()
                stream.stop_stream()
                stream.start_stream()
                state           = STATE_RECORDING
                state_start     = time.time()
                recorded_chunks = []
                silence_counter = 0
                speech_detected = False

        # --- RECORDING ---
        elif state == STATE_RECORDING:
            recorded_chunks.append(audio_16.copy())
            is_speech = is_speech_chunk(audio_16)
            if is_speech:
                speech_detected = True
                silence_counter = 0
            else:
                if speech_detected:
                    silence_counter += 1

            timeout = (now - state_start) > 15.0
            stop    = speech_detected and silence_counter >= SILENCE_CHUNKS_LIMIT

            if stop or timeout:
                reason = "Stille erkannt" if stop else "Timeout"
                print(f"[{now:.1f}s] ⏹  Aufnahme beendet ({reason}), "
                      f"{len(recorded_chunks) * 1280 / RATE_OW:.1f}s Audio")

                if speech_detected and len(recorded_chunks) >= MIN_SPEECH_CHUNKS:
                    all_leds_off()
                    set_led(2, 255, 180, 0)
                    state      = STATE_PROCESSING
                    stt_thread = threading.Thread(
                        target=stt_worker,
                        args=(recorded_chunks.copy(), stt_queue),
                        daemon=True
                    )
                    stt_thread.start()
                else:
                    print(f"[{now:.1f}s] ⚠️  Keine Sprache erkannt")
                    all_leds_off()
                    set_led(0, 0, 0, 255)
                    state = STATE_LISTENING

        # --- PROCESSING (STT läuft) ---
        elif state == STATE_PROCESSING:
            try:
                text = stt_queue.get_nowait()
                if text:
                    print(f"[{now:.1f}s] 📤 Sende an OpenClaw: '{text}'")

                    threading.Thread(
                        target=speak_reply,
                        args=(f"Ich habe verstanden: {text}",),
                        kwargs={"restore_leds": False},
                        daemon=True
                    ).start()

                    reply_done_event.clear()
                    pending_reply_text[0] = None
                    start_thinking()
                    threading.Thread(
                        target=openclaw_worker,
                        args=(text,),
                        daemon=True
                    ).start()

                    all_leds_off()
                    set_led(3, 255, 0, 0)
                    set_led(4, 128, 0, 255)
                    state       = STATE_WAITING
                    state_start = now
                    print(f"[{now:.1f}s] ⏳ Warte auf Antwort (max {OPENCLAW_TIMEOUT}s)...")
                else:
                    print(f"[{now:.1f}s] ⚠️  Leere Transkription")
                    all_leds_off()
                    set_led(0, 0, 0, 255)
                    state = STATE_LISTENING
            except queue.Empty:
                if now - state_start > 60.0:
                    print("⚠️  STT Timeout!")
                    all_leds_off()
                    set_led(0, 0, 0, 255)
                    state = STATE_LISTENING

        # --- WAITING (auf OpenClaw-Antwort + TTS) ---
        elif state == STATE_WAITING:
            if now - state_start > OPENCLAW_TIMEOUT + 30:
                # Großzügiger Timeout: OPENCLAW_TIMEOUT + 30s Puffer für TTS
                print(f"[{now:.1f}s] ⚠️  Gesamt-Timeout überschritten")
                stop_thinking()
                all_leds_off()
                set_led(0, 0, 0, 255)
                state = STATE_LISTENING

            elif reply_done_event.is_set():
                # openclaw_worker hat TTS bereits abgespielt und reply_done_event gesetzt
                reply_done_event.clear()
                all_leds_off()
                set_led(0, 0, 0, 255)
                state       = STATE_PAUSE
                state_start = now

        # --- PAUSE ---
        elif state == STATE_PAUSE:
            if now - state_start > 1.0:
                stream.stop_stream()
                stream.start_stream()
                wakeword_model.reset()
                all_leds_off()
                set_led(0, 0, 0, 255)
                state = STATE_LISTENING
                print(f"[{now:.1f}s] 🎤 Bereit – warte auf 'hey jarvis'...")

        time.sleep(0.001)

except KeyboardInterrupt:
    print("\n🛑 Beende...")
    all_leds_off()
    stream.stop_stream()
    stream.close()
    p.terminate()
