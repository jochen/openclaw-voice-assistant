# openclaw-voice-assistant

> [Deutsche Version](README.de.md)

Wakeword-driven voice assistant for Raspberry Pi. Connects local speech input to [OpenClaw](https://github.com/openclaw/openclaw) as the AI backend and [Speaches](https://github.com/speaches-ai/speaches) for GPU-accelerated STT/TTS.

## Pipeline

```
Microphone → openWakeWord ("hey jarvis")
  → WebRTC VAD + recording
  → STT: Speaches /v1/audio/transcriptions  (fallback: faster-whisper local)
  → Confirmation TTS ("I understood…") — parallel thread
  → POST /v1/responses → OpenClaw (full agentic loop incl. tool calls)
  → Reply TTS sentence by sentence: Speaches /v1/audio/speech  (fallback: Piper local)
  → Mirror query + reply to Telegram
```

## Requirements

- Raspberry Pi (tested: Pi 4/5, ARM64, Raspberry Pi OS Bookworm)
- **Python 3.11.9** (exact — `openwakeword` + `tflite-runtime` require this version on ARM64, see below)
- [OpenClaw](https://openclaw.dev) running locally on `http://127.0.0.1:18789`
- [Speaches](https://github.com/speaches-ai/speaches) GPU container reachable (default: `http://192.168.111.126:8000`)
- Microphone supported by ALSA
- Optional: WLED controller for LED status, Piper TTS for local fallback

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/jochen/openclaw-voice-assistant.git
cd openclaw-voice-assistant
```

### 2. Install Python 3.11.9 via pyenv

`openwakeword` and `tflite-runtime` are not available for newer Python versions on ARM64. **Exactly Python 3.11.9** is required.

```bash
# Install pyenv (if not already present)
curl https://pyenv.run | bash

# Add to ~/.bashrc or ~/.zshrc:
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

# Install build dependencies
sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev

pyenv install 3.11.9
```

The repo includes a `.python-version` file — pyenv activates 3.11.9 automatically when you enter the directory.

### 3. Create venv and install dependencies

```bash
# Inside the project directory (pyenv activates 3.11.9 automatically)
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

Models are downloaded to `/tmp/ow_models_min` (configurable in the script).

### 5. Create configuration

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` — fill in at least these fields for your profile:

| Field | Description |
|---|---|
| `device_index` | ALSA microphone index (`arecord -l` lists available devices) |
| `rate_in` | Microphone sample rate (48000 or 16000) |
| `speaches_base` | URL of the Speaches container |
| `openclaw_token` | API token from the OpenClaw dashboard |
| `openclaw_session` | Session key (see below) |
| `telegram_bot_token` | Telegram bot token from @BotFather |
| `telegram_chat_id` | Telegram group ID (with `-` prefix) |
| `wled_host` | Hostname or IP of the WLED controller |

## OpenClaw Integration

### Session Key

`openclaw_session` determines **which session** voice requests land in. For voice and Telegram chat to share the same context, this key must match the session key of your Telegram group.

Find it in the OpenClaw dashboard under **Sessions** or in:
```
~/.openclaw/agents/main/sessions/sessions.json
```

Typical format: `agent:main:telegram:group:-1003XXXXXXXXX`

**Why this matters:** The script sets the HTTP header `x-openclaw-session-key`, which takes precedence over OpenClaw's auto-generated namespace. Without this header, OpenClaw creates a separate `openresponses-user:` namespace and voice turns are isolated from the chat history.

### AGENTS.md configuration

For correct voice behaviour, add a `## Voice Commands` section to `~/.openclaw/workspace/AGENTS.md`:

```markdown
## Voice Commands

Messages starting with 🎤 are voice commands from speech recognition.
Strict rules apply — no exceptions:

- Always respond in the user's language
- Maximum 2-3 short sentences
- No markdown, no lists, no numbering
- No emojis
- Natural spoken language — imagine speaking, not writing

### Voice → Chat transitions

If a regular chat message follows a 🎤 message (within a few minutes, same topic),
treat it as a continuation or correction of the last voice task:

1. Reconstruct the original task from context
2. Re-execute the task with the correction applied — fully
3. No meta-commentary about your own mistake
4. The 2-3 sentence limit does not apply to chat replies
```

## Speaches Integration

Speaches runs as a Docker container with an OpenAI-compatible API.

### STT (speech → text)

```
POST {speaches_base}/v1/audio/transcriptions
model: guillaumekln/faster-whisper-medium
language: de
```

### TTS (text → speech)

```
POST {speaches_base}/v1/audio/speech
model: speaches-ai/piper-de_DE-thorsten-medium
voice: de_DE-thorsten-medium
```

Both services have a 60-second cooldown after connection failures before retrying (`SpeachesState` class). On failure the local fallback kicks in automatically:
- STT: `faster-whisper` (model `small`, runs on the Pi)
- TTS: Piper (`~/.local/share/piper/de_DE-thorsten-low.onnx`)

## Piper TTS (local fallback)

```bash
pip install piper-tts

mkdir -p ~/.local/share/piper
cd ~/.local/share/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx.json
```

## Profiles

Two profiles are pre-configured in `config.yaml`. The active profile is detected automatically by hostname or set via environment variable:

```bash
GASTON_PROFILE=openclaw python voice_assistant.py
```

| Profile | Hostname match | Microphone | Notes |
|---|---|---|---|
| `clawdpi` | `clawdpi*` | Index 1, 48kHz (resampled) | Primary device |
| `openclaw` | `openclaw*` | Index 0, 16kHz native | Second Pi |

## Running

```bash
source ~/ow-venv/bin/activate
python voice_assistant.py
```

The script detects whether it is running inside the correct venv and re-execs itself if needed.

## LED Status (WLED)

| LED | Color | State |
|---|---|---|
| 0 | Blue | Ready, waiting for wakeword |
| 1 | Green | Wakeword detected, recording |
| 2 | Yellow | STT processing |
| 3 | Red | Short pause after recording |
| 4 | Purple | Waiting for OpenClaw response |
| 5 | Cyan | Speaking reply |

WLED controller: `wled_controller.py` — host configured via `wled_host` in `config.yaml`.

## License

MIT — see [LICENSE](LICENSE).
