# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Assistant

```bash
source ~/ow-venv/bin/activate && python voice_assistant.py
```

The script self-reinvokes with the venv Python (`/home/pi/ow-venv/bin/python`) on startup if not already running inside it — no manual activation needed at that point.

Override the profile: `GASTON_PROFILE=openclaw python voice_assistant.py`

## Architecture

Single-file voice assistant pipeline running on Raspberry Pi:

```
Microphone → openWakeWord ("hey jarvis") → WebRTC VAD + recording
  → STT (Speaches GPU server, fallback: faster-whisper local)
  → Confirmation TTS in parallel thread ("Ich habe verstanden: ...")
  → POST /v1/chat/completions to OpenClaw (streaming SSE)
  → Sentence-by-sentence TTS as tokens arrive
  → Telegram notification
```

## Profile System

Two hardware profiles are selected automatically by hostname or `GASTON_PROFILE` env var:

- **`clawdpi`** — main Pi (clawdpi1, 192.168.111.126 area), DEVICE_INDEX=1, 48kHz mic resampled to 16kHz
- **`openclaw`** — second Pi, DEVICE_INDEX=0, native 16kHz mic, different Telegram group/session

Config constants (`DEVICE_INDEX`, `SPEACHES_*`, `OPENCLAW_TOKEN`, `OPENCLAW_SESSION`, `TELEGRAM_*`, `TTS_PREFIX`) are all loaded from `PROFILES[PROFILE_NAME]`.

## Key External Dependencies

| Service | URL | Purpose |
|---|---|---|
| Speaches (GPU container) | `http://192.168.111.126:8000` | STT (`/v1/audio/transcriptions`) + TTS (`/v1/audio/speech`) — OpenAI-compatible |
| OpenClaw | `http://127.0.0.1:18789/v1/chat/completions` | AI brain, streaming SSE, session-based |
| WLED controller | `/home/pi/.openclaw/workspace/wled/wled_controller.py` | LED status feedback |
| Piper TTS | `/home/pi/.local/share/piper/*.onnx` | Local TTS fallback |
| Telegram Bot API | `https://api.telegram.org/...` | Mirror queries and replies |

STT/TTS both use a 60-second cooldown before retrying Speaches after failure (`SpeachesState` class).

## State Machine

Five states in the main audio loop:
1. **LISTENING** — feeds audio to openwakeword continuously; triggers on score > 0.5
2. **RECORDING** — collects chunks; ends on silence (25 silent chunks after speech) or 15s timeout
3. **PROCESSING** — waits for STT thread result (non-blocking `queue.get_nowait`)
4. **WAITING** — waits for `reply_done_event` set by `openclaw_worker` after TTS completes
5. **PAUSE** — 1-second dead zone before returning to LISTENING

## Threading Model

- `stt_worker` — separate thread; puts transcription result into `stt_queue`
- `openclaw_worker` — separate thread; streams SSE tokens, builds sentence buffer, enqueues sentences for TTS
- `tts_player` (inside `openclaw_worker`) — acquires `tts_lock` to block until confirmation TTS ("Ich habe verstanden") finishes, then plays sentences from `tts_queue` sentence by sentence
- `_thinking_worker` — fires "Einen Moment bitte." phrases every 20s via Piper if OpenClaw is slow
- `tts_lock` — prevents overlapping audio playback

## OpenClaw Request Format

Voice queries are wrapped with a prompt directive:
```
🎤 {user_text}

[VOICE: Ruf zuerst alle nötigen Tools auf, dann antworte in max 2-3 gesprochenen Sätzen auf Deutsch. Kein Markdown, keine Listen. Niemals etwas erfinden — entweder Tool aufrufen oder sagen was du nicht weißt.]
```

The `x-openclaw-session-key` header carries the session identifier (e.g. `agent:main:telegram:group:-1003807266328`).

## LED States

| LED index | Color | Meaning |
|---|---|---|
| 0 | Blue | Ready, waiting for wakeword |
| 1 | Green | Wakeword detected, recording |
| 2 | Yellow | STT processing |
| 3 | Red | Pause after recording |
| 4 | Purple | Waiting for OpenClaw response |
| 5 | Cyan | Speaking reply |

## File Paths

- Workspace: `/home/pi/.openclaw/workspace`
- Piper "Ja?" pre-rendered WAV: `/home/pi/.openclaw/workspace/ja.wav`
- Piper models: `/home/pi/.local/share/piper/de_DE-thorsten_emotional-medium.onnx`, `de_DE-thorsten-low.onnx`
- openwakeword models: `/tmp/ow_models_min`
- Venv: `/home/pi/ow-venv`
