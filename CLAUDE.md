# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Assistant

```bash
source ~/ow-venv/bin/activate && python -m voice_assistant
```

The entry point (`voice_assistant/__main__.py`) self-reinvokes with the venv Python
(`/home/pi/ow-venv/bin/python`) on startup if not already running inside it.

Override the profile: `GASTON_PROFILE=openclaw python -m voice_assistant`

Der alte Monolith `voice_assistant.py` wurde in ein Package refaktoriert und
liegt übergangsweise als `voice_assistant_legacy.py` weiter im Repo (zum
Nachschlagen bei Regressions). Nicht starten.

## Architecture

Python-Package `voice_assistant/` — Pipeline bleibt inhaltlich identisch zum
alten Skript:

```
AudioSource (ALSA | ReSpeaker via ESPHome) → WakewordEngine
  → WebRTC VAD + recording
  → STT (Speaches, fallback: faster-whisper local)
  → Confirmation TTS in parallel thread ("Ich habe verstanden: ...")
  → POST /v1/responses to OpenClaw (vollständiger Agentic Loop, non-streaming)
  → Antwort satzweise via TTS (Speaches, fallback: Piper) über AudioSink
  → Telegram notification
```

### Package-Struktur

```
voice_assistant/
  __main__.py            entry: python -m voice_assistant (venv-Re-Exec)
  assistant.py           run() — Hauptloop + State-Machine
  state.py               STATE_*, tts_lock, reply_done_event, stt_queue
  config.py              Profile-Dataclass + YAML-Loader (alt + neu)
  workers.py             Workers: start_stt, start_confirmation, start_openclaw_turn
  audio/
    base.py              AudioSource/AudioSink Protocols
    alsa.py              PyAudio + aplay
    respeaker.py         ESPHome Native API (Stub — Schritt 2)
  wakeword/
    base.py              WakewordEngine Protocol
    openwakeword_engine.py
    respeaker.py         micro_wakeword vom ESP (Stub — Schritt 2)
  services/
    leds.py              WledLeds + RespeakerRing + LedDirector
    telegram.py
    speaches.py          SpeachesState + Start-Check
    stt.py               SpeachesStt + LocalWhisperStt + SttPipeline
    tts.py               SpeachesTts + Piper + ReplySpeaker + ThinkingWorker
    openclaw.py          /v1/responses Client
```

## Profile System

Zwei Profile werden automatisch per Hostname oder `GASTON_PROFILE` gewählt:

- **`clawdpi`** — `clawdpi1`, Mic Index 1 @ 48 kHz (resample), WLED
- **`openclaw`** — zweiter Pi, Mic Index 0 @ 16 kHz, eigenes Telegram/Session

Jedes Profil hat einen **`mode`**-Schalter:

- `mode: local` — ALSA-Mic + ALSA-Speaker + openwakeword auf dem Pi (bisheriges Verhalten)
- `mode: respeaker` — Mic + LED-Ring + optional Speaker über ReSpeaker XVF3800 + XIAO ESP32-S3
  (ESPHome Native API, `micro_wakeword` läuft auf dem ESP)

Das alte flache YAML-Schema wird weiter akzeptiert und als `mode: local`
interpretiert (Rückwärtskompatibilität in `voice_assistant/config.py`).

## Key External Dependencies

| Service | URL | Purpose |
|---|---|---|
| Speaches (GPU container) | `http://192.168.111.126:8000` | STT + TTS (OpenAI-compatible) |
| OpenClaw | `http://127.0.0.1:18789/v1/responses` | AI brain, non-streaming, session-based |
| WLED controller | `wled_controller.py` (repo-local) | LED-Status |
| Piper TTS | `/home/pi/.local/share/piper/*.onnx` | Lokaler TTS-Fallback |
| Telegram Bot API | `https://api.telegram.org/...` | Mirror queries and replies |
| ReSpeaker (ESPHome) | `<host>:6053` | Native API (Audio-Stream, Wakeword-Events, LED-Ring) — optional |

STT/TTS beide nutzen 60-Sekunden-Cooldown nach Fehler vor erneutem
Speaches-Versuch (`services/speaches.py:SpeachesState`).

## State Machine

Fünf Zustände in der Hauptschleife (`voice_assistant/assistant.py`):

1. **LISTENING** — WakewordEngine bekommt jeden 16-kHz-Chunk; triggert bei Score > 0.5
2. **RECORDING** — Chunks werden gesammelt; endet bei Stille (25 stille Chunks
   nach Sprache) oder nach 15 s Timeout
3. **PROCESSING** — wartet auf STT-Ergebnis aus `state.stt_queue`
4. **WAITING** — wartet auf `state.reply_done_event` (openclaw_worker setzt es)
5. **PAUSE** — 1 s Totzone bevor es zurück in LISTENING geht

## Threading Model

- STT läuft in eigenem Thread, Ergebnis über `state.stt_queue`.
- Bestätigungs-TTS ("Ich habe verstanden: …") läuft in eigenem Thread (`ReplySpeaker`).
- `_openclaw_turn` in eigenem Thread: OpenClaw anfragen → Telegram spiegeln
  → Antwort satzweise vorlesen → `state.reply_done_event` setzen.
- `ThinkingWorker` feuert alle 20 s eine Lebenszeichen-Phrase (Piper), wenn
  OpenClaw zu langsam antwortet.
- `state.tts_lock` verhindert überlappende Audio-Wiedergabe.

## OpenClaw Request Format

Voice-Anfragen werden mit einer Prompt-Direktive umhüllt
(`services/openclaw.py`):

```
🎤 {user_text}

[VOICE: Ruf zuerst alle nötigen Tools auf, dann antworte in max 2-3 gesprochenen
Sätzen auf Deutsch. Kein Markdown, keine Listen. Niemals etwas erfinden —
entweder Tool aufrufen oder sagen was du nicht weißt.]
```

Der `x-openclaw-session-key`-Header trägt die Session-Kennung (z.B.
`agent:main:telegram:group:-1003807266328`) und teilt die Session mit dem
Telegram-Chat.

## LED States

| LED-Index | Farbe   | Bedeutung |
|---|---|---|
| 0 | Blau    | Bereit, wartet auf Wakeword |
| 1 | Grün    | Wakeword erkannt, Aufnahme läuft |
| 2 | Gelb    | STT verarbeitet |
| 3 | Rot     | Pause nach Aufnahme |
| 4 | Lila    | Wartet auf OpenClaw |
| 5 | Cyan    | Liest Antwort vor |

Der `LedDirector` verteilt die Kommandos auf **alle aktiven** LED-Senken
(WLED und/oder ReSpeaker-Ring). Beide können parallel betrieben werden.

## File Paths

- Workspace: `/home/pi/.openclaw/workspace`
- Piper "Ja?" pre-rendered WAV: `/home/pi/.openclaw/workspace/ja.wav`
- Piper models: `/home/pi/.local/share/piper/de_DE-thorsten_emotional-medium.onnx`,
  `de_DE-thorsten-low.onnx`
- openwakeword models: `/tmp/ow_models_min`
- Venv (Python 3.11 für openwakeword/tflite): `/home/pi/ow-venv`
- ESPHome venv (getrennt, nur fürs Flashen): `/home/pi/openclaw_voice_assist/esphome-venv`
