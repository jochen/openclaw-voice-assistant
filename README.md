# openclaw-voice-assistant

> [Deutsche Version](README.de.md)

Wakeword-driven voice assistant for Raspberry Pi. Connects local speech input to [OpenClaw](https://github.com/openclaw/openclaw) as the AI backend and [Speaches](https://github.com/speaches-ai/speaches) for GPU-accelerated STT/TTS.

## Pipeline

```
Audio Frontend (ALSA mic  OR  ReSpeaker XVF3800 via ESPHome)
  → openWakeWord ("hey jarvis")
  → WebRTC VAD + recording
  → STT: Speaches /v1/audio/transcriptions  (fallback: faster-whisper local)
  → Confirmation TTS ("I understood…") — parallel thread
  → POST /v1/responses → OpenClaw (full agentic loop incl. tool calls)
  → Reply TTS sentence by sentence: Speaches /v1/audio/speech  (fallback: Piper local)
  → Mirror query + reply to Telegram
```

## Requirements

- Raspberry Pi (tested: Pi 4/5, ARM64, Raspberry Pi OS Bookworm)
- **Python 3.11.9** (exact — `openwakeword` + `tflite-runtime` require this version on ARM64)
- [OpenClaw](https://openclaw.dev) running locally on `http://127.0.0.1:18789`
- [Speaches](https://github.com/speaches-ai/speaches) GPU container reachable (default: `http://192.168.111.126:8000`)

**Mode: local** — ALSA microphone + ALSA speaker + optional WLED LED strip

**Mode: respeaker** — ReSpeaker XVF3800 4-mic array + XIAO ESP32-S3, controlled via ESPHome Native API (`aioesphomeapi`). No Home Assistant required.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/jochen/openclaw-voice-assistant.git
cd openclaw-voice-assistant
```

### 2. Install Python 3.11.9 via pyenv

`openwakeword` and `tflite-runtime` are not available for newer Python versions on ARM64. **Exactly Python 3.11.9** is required.

```bash
curl https://pyenv.run | bash

# Add to ~/.bashrc or ~/.zshrc:
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev

pyenv install 3.11.9
```

The repo includes a `.python-version` file — pyenv activates 3.11.9 automatically.

### 3. Create venv and install dependencies

```bash
python -m venv ~/ow-venv
source ~/ow-venv/bin/activate
pip install -r requirements.txt
```

### 4. Download openWakeWord models

```bash
python -c "
from openwakeword.model import Model
Model(wakeword_models=['hey_jarvis'], inference_framework='tflite')
"
```

Models are downloaded to `/tmp/ow_models_min`.

### 5. Create configuration

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`. Common fields:

| Field | Description |
|---|---|
| `speaches_base` | URL of the Speaches container |
| `openclaw_token` | API token from the OpenClaw dashboard |
| `openclaw_session` | Session key (see below) |
| `telegram_bot_token` | Telegram bot token from @BotFather |
| `telegram_chat_id` | Telegram group ID (with `-` prefix) |

**Mode: local** — additional fields:

| Field | Description |
|---|---|
| `device_index` | ALSA microphone index (`arecord -l`) |
| `rate_in` | Microphone sample rate (48000 or 16000) |
| `wled_host` | Hostname or IP of the WLED controller (optional) |

**Mode: respeaker** — additional fields:

| Field | Description |
|---|---|
| `respeaker.host` | Hostname or IP of the ESP32-S3 (e.g. `respeaker-openclaw.local`) |
| `respeaker.volume` | Speaker volume 0.0–1.0 (set at connect, no OTA needed) |
| `respeaker.use_speaker` | `true` = TTS via ReSpeaker DAC; `false` = local ALSA speaker |

## Running

```bash
source ~/ow-venv/bin/activate
python -m voice_assistant
```

The entry point re-execs itself inside the correct venv automatically.

Override profile: `GASTON_PROFILE=clawdpi_rs python -m voice_assistant`

## Profiles

Profile is selected automatically by hostname, or set via `GASTON_PROFILE`:

| Profile | Hostname match | Mode | Notes |
|---|---|---|---|
| `clawdpi` | `clawdpi*` | local | Index 1, 48kHz (resampled), WLED |
| `openclaw` | `openclaw*` | local | Index 0, 16kHz native |
| `clawdpi_rs` | — | respeaker | ReSpeaker XVF3800 on `clawdpi` |

## ReSpeaker Setup (mode: respeaker)

The ESP32-S3 firmware is in `esphome/respeaker.yaml`. Flash via:

```bash
# Initial (USB):
esphome-venv/bin/esphome run esphome/respeaker.yaml --device /dev/ttyACM0

# OTA:
esphome-venv/bin/esphome run esphome/respeaker.yaml --device respeaker-openclaw.local
```

The ESPHome venv is separate from `ow-venv`:

```bash
python -m venv esphome-venv
esphome-venv/bin/pip install esphome
```

**How it works:** The Pi connects to the ESP via ESPHome Native API (port 6053, `aioesphomeapi`). Audio streams continuously via the `voice_assistant` component in API_AUDIO mode. TTS output is sent back as WAV via the ESP's `media_player` announce API — the Pi serves the WAV over HTTP (port 18800) and the ESP fetches and plays it.

Wakeword detection (`openwakeword`) runs on the Pi against the audio stream.

## OpenClaw Integration

### Session Key

`openclaw_session` determines which session voice requests land in. For voice and Telegram chat to share context, this key must match the Telegram session key.

Find it in the OpenClaw dashboard under **Sessions** or in:
```
~/.openclaw/agents/main/sessions/sessions.json
```

Typical format: `agent:main:telegram:group:-1003XXXXXXXXX`

The script sets the HTTP header `x-openclaw-session-key`. Without it, OpenClaw creates a separate `openresponses-user:` namespace and voice turns are isolated from chat history.

### AGENTS.md

For correct voice behaviour, add to `~/.openclaw/workspace/AGENTS.md`:

```markdown
## Voice Commands

Messages starting with 🎤 are voice commands from speech recognition.
Strict rules — no exceptions:

- Always respond in the user's language
- Maximum 2-3 short sentences
- No markdown, no lists, no numbering
- No emojis
- Natural spoken language

### Voice → Chat transitions

If a regular chat message follows a 🎤 message (within a few minutes, same topic),
treat it as a continuation or correction of the last voice task:

1. Reconstruct the original task from context
2. Re-execute the task with the correction applied — fully
3. No meta-commentary about your own mistake
4. The 2-3 sentence limit does not apply to chat replies
```

## Speaches Integration

STT: `POST {speaches_base}/v1/audio/transcriptions` — model `guillaumekln/faster-whisper-medium`

TTS: `POST {speaches_base}/v1/audio/speech` — model `speaches-ai/piper-de_DE-thorsten-medium`

60-second cooldown after connection failures. On failure the local fallback activates automatically:
- STT fallback: `faster-whisper` (model `small`, runs on the Pi)
- TTS fallback: Piper (`~/.local/share/piper/de_DE-thorsten-low.onnx`)

### Piper TTS (local fallback)

```bash
pip install piper-tts
mkdir -p ~/.local/share/piper && cd ~/.local/share/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx.json
```

## LED Status

WLED (mode: local) and ReSpeaker LED ring (mode: respeaker) are mutually exclusive.

### ReSpeaker LED Ring — 12 phases

| Phase | State | Animation |
|---|---|---|
| 0 | BOOT | LEDs light up sequentially: WiFi(1–3) → API(4–6) → Speaches(7–9) → Wakeword(10–12) |
| 1 | IDLE | All LEDs very dim blue; one slightly brighter dot travels extremely slowly (~36s/rotation) |
| 2 | WAKEWORD | All 12 LEDs bright red |
| 3 | RECORDING | Red base + beam direction highlight (XVF3800 DOA, ESP-internal) |
| 4 | STT | Rotating dot, blue, slow (150ms/step) |
| 5 | CONFIRMATION | Rotating dot, blue, faster (100ms/step) |
| 6 | OPENCLAW_WAIT | Rotating dot, red-purple, fast (50ms/step) |
| 7 | ANSWER_GLOW | All LEDs green, static |
| 8 | AUDIO_OUT | All LEDs green, pulsing |
| 9 | END | All off — Pi transitions to IDLE after 1s pause |
| 10 | ERROR | 6 LEDs (half ring), red, static |
| 11 | FOLLOWUP | Warm yellow, gentle pulse — reserved for future follow-up question feature |

### WLED Strip (mode: local)

| LED | Color | State |
|---|---|---|
| 0 | Blue | Idle |
| 1 | Red | Wakeword / Recording |
| 2 | Orange | STT / Confirmation |
| 4 | Purple | Waiting for OpenClaw |
| 5 | Green | Speaking reply |

## License

MIT — see [LICENSE](LICENSE).
