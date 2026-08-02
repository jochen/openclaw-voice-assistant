# openclaw-voice-assistant

> [Deutsche Version](README.de.md)

Wakeword-driven voice assistant for Raspberry Pi. Connects local speech input to [OpenClaw](https://github.com/openclaw/openclaw) as the AI backend and [Speaches](https://github.com/speaches-ai/speaches) for GPU-accelerated STT/TTS.

## Pipeline

```
Audio Frontend (ALSA mic  OR  ReSpeaker XVF3800 via ESPHome)
  → openWakeWord ("hey jarvis")
  → WebRTC VAD + recording (max 30 s)
  → STT: Speaches /v1/audio/transcriptions  (fallback: faster-whisper local)
  → Diarization (parallel): Speaches /v1/audio/diarization with known speakers
  → Voice actuator (optional): a switching command? → run locally (~0.5 s), skip the rest
  → Confirmation TTS ("I understood…") — parallel thread
  → POST /v1/responses → OpenClaw  (wrapper: "🎤 [Sprecher: jochen|unbekannt] {text}")
  → Reply TTS sentence by sentence: Speaches /v1/audio/speech  (fallback: Piper local)
  → Mirror query + reply to Telegram
```

## Requirements

- Raspberry Pi (tested: Pi 4/5, ARM64, Raspberry Pi OS Bookworm)
- **Python 3.11.9** (exact — `openwakeword` + `tflite-runtime` require this version on ARM64)
- [OpenClaw](https://openclaw.dev) running locally on `http://127.0.0.1:18789`
- [Speaches](https://github.com/speaches-ai/speaches) GPU container reachable (default: `http://<speaches-host>:8000`)
- *(optional)* `voice-analysis` container (default: `http://<speaches-host>:8001`) — adds the acoustic mood signal and the `voice_analyze_last_output` tool; the assistant runs fine without it.

**Mode: local** — ALSA microphone + ALSA speaker + optional WLED LED strip

**Mode: respeaker** — ReSpeaker XVF3800 4-mic array + XIAO ESP32-S3, controlled via ESPHome Native API (`aioesphomeapi`). No Home Assistant required.

## Installation

### 1. Clone the repository

```bash
# Verzeichnisname bewusst mit Unterstrichen: die systemd-Unit erwartet
# %h/openclaw_voice_assist
git clone https://github.com/jochen/openclaw-voice-assistant.git openclaw_voice_assist
cd openclaw_voice_assist
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
python -m venv ~/openclaw_voice_assist/ow-venv
source ~/openclaw_voice_assist/ow-venv/bin/activate
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

### 6. Download Piper TTS models (local fallback + wakeword ack)

`piper-tts` is in `requirements.txt`; the models are gitignored and loaded from hardcoded paths under `<project>/models/piper/`:

```bash
mkdir -p models/piper && cd models/piper
BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE
wget $BASE/thorsten/low/de_DE-thorsten-low.onnx
wget $BASE/thorsten/low/de_DE-thorsten-low.onnx.json
wget $BASE/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx
wget $BASE/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx.json
cd ../..
```

Each model needs both the `.onnx` and its `.onnx.json` sidecar. If the models already exist elsewhere (e.g. `~/.local/share/piper/`), symlinking them into `models/piper/` works too.

## Running

```bash
source ~/openclaw_voice_assist/ow-venv/bin/activate
python -m voice_assistant
```

The entry point re-execs itself inside the correct venv automatically.

Override profile: `GASTON_PROFILE=clawdpi_rs python -m voice_assistant`

## Autostart (systemd user service)

A unit template ships in [`systemd/openclaw-voice-assist.service`](systemd/openclaw-voice-assist.service):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/openclaw-voice-assist.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now openclaw-voice-assist
loginctl enable-linger pi        # start without an active login session
```

The unit pins `PATH` to include `ow-venv/bin` so the service finds `piper` and `aplay` for its subprocess calls. Logs:

```bash
journalctl _SYSTEMD_USER_UNIT=openclaw-voice-assist.service -f
```

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

## Wake-word level gate (`wake_rms_min`)

Besides the score gate, there is an optional **level gate**: the RMS of the
loudest 300 ms window in the wake-ring buffer must clear a threshold, or the
trigger does not fire — even when the score says yes. Its purpose is to block
**soft false triggers** (TV, keyboard, distant talk) that slip past the score
gate because the model scores high on that particular sound.

- Off by default (`wake_rms_min: 0.0`). Leave the entry out and the profile
  behaves exactly as before. Deliberately a separate parameter, not
  `vad_voice_rms_min` (which is already in use for VAD/endpointing).
- The threshold belongs in the **profile**, not in the bundle's
  `manifest.yaml`: it depends on microphone and gain, not on the wake word.

**Finding your own value** (three steps, ~10 minutes):

```bash
# 1. Record guided takes — ALL styles, especially soft/distant/turned-away:
ow-venv/bin/python -m wakeword_studio record --speaker <name>
# 2. Get a threshold suggestion from those takes alone (no daily archive needed):
ow-venv/bin/python -m tools.wake_rms_replay --nur-studio
# 3. Put the suggested value in the profile, then restart the service:
#      wake_rms_min: <value>
systemctl --user restart openclaw-voice-assist.service
```

The `--nur-studio` mode proposes `round(lowest take × 0.7)` and warns if the
difficult styles (soft, distant, turned-away, casual) are missing — without
them every proposal comes out **too high** and costs you soft calls later. It
delivers a safe lower bound, **not** an effectiveness figure: without a daily
archive it cannot say how many false triggers the threshold blocks. Once you
have one, the full run is the better source:

```bash
ow-venv/bin/python -m tools.wake_rms_replay     # with archive: recall + precision
```

> **Example, not a preset.** Our installation runs at `wake_rms_min: 300`
> (derived from 57 everyday calls + 24 ear-checked false triggers, lowest
> genuine call at RMS 402, on a ReSpeaker mic with ×4 gain). **Absolute RMS
> values are tied to your microphone and amplification.** Copy our 300 onto
> different hardware and you lose either every call (gain lower) or block
> nothing (gain higher). Measure your own.

**Signs the threshold is wrong:** calls blocked by the gate are archived as
near-misses with `failed_on: "min_rms"` in `~/.openclaw/workspace/wake_events.log`.
If intended calls pile up there, the threshold is too high for the current
gain. The same signal also flags a hardware/gain change — revisit the value.

## Voice actuator (optional)

Switching commands like "turn on the kitchen light" normally take the same road
as any other question: through the language model in the backend. That takes
seconds. The voice actuator intercepts them right after speech recognition and
runs them locally — measured at **about 0.5 seconds** instead of several.

```
STT text → small LLM forms ONE JSON intent → POST /intent to your home
           automation → its reply text is read out verbatim
```

If the sentence is not a switching command, everything continues to the backend
as before. Same if the small model is unavailable — the actuator is a shortcut,
never a bottleneck.

**The assistant only contains the speech side.** You provide the executing side
yourself: two HTTP endpoints, `GET /capabilities` (what may be switched) and
`POST /intent` (do it). What you build them with is up to you — Node-RED, Home
Assistant, openHAB or a fifty-line script.

**→ [`ACTUATOR_INTERFACE.md`](ACTUATOR_INTERFACE.md)** (German) describes the
contract in full, including the reasoning behind every design decision, a
working minimal implementation in Flask and an acceptance check via `curl`.

The device vocabulary is **not** maintained in the assistant: schema and prompt
for the small model are generated at runtime from `/capabilities`. Adding a new
device there is enough — it becomes speakable immediately, without a restart and
without touching the prompt.

### The same path for the brain (MCP)

The actuator only handles what it recognises as a switching command with
confidence. Everything else goes to the large model — including sentences that
*were* meant to switch something, just phrased in a way the actuator could not
place. Without a path of its own, the large model then reaches for the raw home
automation API: no target whitelist, no value check, no confirmation. That is
exactly how the 2026-08-01 incident behind this section happened — a misheard
device name was guessed at, and every shutter in the house moved.

`voice_assistant/mcp_actuator.py` is a stdio MCP server that gives the large
model the same guarded interface, with two tools:

| Tool | Effect |
|---|---|
| `haus_ziele()` | Every target with names, permitted actions, value range, `kosten`, `reversibel` — a **closed vocabulary** |
| `haus_schalten(ziel, aktion, …)` | `POST /intent`, response envelope passed through unchanged |

There is deliberately **no** free-text tool: the whole point is that a device
name absent from `haus_ziele()` does not exist — rather than being guessed at.

Requests carry their own `quelle`, so the executing side applies its gate just
as it does for the actuator, and reserving further `quelle` values for other
callers stays possible. Every call is appended to the same log file as the
actuator turns — otherwise the large model's switching would be invisible,
which is precisely why nobody noticed the incident.

The actuator is enabled per profile; without the block it stays off:

```yaml
    actuator:
      enabled: true
      base_url: "http://<home-automation>:1880/voiceact"
      llm_url:  "http://localhost:8090/v1/chat/completions"
      mqtt_host: ""          # optional: instant notification on changes;
                             # empty = polling every 10 min only
```

The local model is expected to be a small instruction-following LLM behind an
OpenAI-compatible API (tested: Gemma-4-E2B-Q4 via llama.cpp). It must support
`response_format: json_schema` — the closed form of the intent is half the
safety, the executing side checks the other half.

The token for the endpoints lives in `voiceact-token.txt` in the project
directory (gitignored) and is sent as the `X-Actuator-Token` header.

**The classifier prompt is German by default — and replaceable.** It is the
only language-dependent part of this project. Set `actuator.system_prompt` in
your profile to run it in English (or any other language); the code itself
contains no fixed target id and no device word. Target list, contrast examples
and the rule for device plurals without a room are generated from
`/capabilities` on every refresh and substituted into the placeholders
`{ziel_liste}`, `{kontrast}` and `{gruppen_regel}`. See
`config.example.yaml` for the full set of keys.

**If you replace the prompt, measure it.** `tools/actuator_grammar_test.py`
runs a fixed suite against the real `classify()` path and prints the
capabilities version it was measured against — a score is only ever valid for
one version of your device list. Two findings from our own measurements that
appear to be language-independent: fewer few-shot examples beat more, and the
plural rule only takes effect when placed *after* the target list, never
before it. Translate the test sentences along with the prompt.

Turns handled by the actuator itself are never seen by the backend — they are
written as JSONL to `<workspace>/actuator_turns.log` instead.

### Overseer (Stage 1 — read and report only)

Since the actuator bypasses the backend, nothing double-checks whether what
was said matches what was switched. The overseer closes that gap conservatively:
it checks each actuator turn and reports discrepancies — it does **not**
intervene, switch, or correct anything.

The semantic check uses an LLM (OpenAI-compatible API, like the actuator
itself). A short system prompt asks: does the transcript match the intent?
The LLM recognises subtler patterns that a regex heuristic would miss —
negations, restrictions, prepositions vs. actions („auf 10%" is a SET action,
not the OPEN action). See `voice_assistant/services/watcher.py`.

Additionally, a deterministic structural check runs without an LLM:

- **EXEC_DIFFERS** — the home automation executed a different target/action
  than the intent requested.
- **STATUS_PROBLEM** — the automation did not answer „ausgefuehrt" (rejected,
  unknown target, deferred). Archived but not sent to Telegram (too noisy).

On LLM errors (provider down, timeout): the overseer sends a message to the
Telegram chat („Überwachung konnte nicht erfolgen weil …") instead of failing
silently. Timeout is 30s with one retry on network/timeout errors.

The overseer runs as an event-driven daemon thread — `check_turn()` is called
immediately after each actuator turn (not on a polling timer), so a mismatch
is reported within ~1 second.

One-off CLI tool (reads the log, classifies, deduplicates):

```bash
ow-venv/bin/python -m tools.actuator_watch
ow-venv/bin/python -m tools.actuator_watch --seit 3   # last 3 days only
ow-venv/bin/python -m tools.actuator_watch --alles     # re-show seen ones
```

Background worker inside the assistant, reporting to a separate Telegram chat
(not the family voice chat):

```yaml
    watcher:
      enabled: true
      chat_id: "<telegram-chat-id>"   # separate group, not the voice mirror
      quiet_start: 1                   # no messages 01:00–07:00
      quiet_end: 7
      llm_url: "https://<provider>/v1/chat/completions"
      llm_model: "<model>"
      llm_api_key: "<api-key>"
      llm_timeout: 30
```

Only `LLM_MISMATCH` and `EXEC_DIFFERS` are sent to Telegram; during quiet
hours findings are collected and held. Stage 1 is deliberately conservative —
the most expensive mistake the overseer could make is a *hallucinated*
correction, physically in the house, possibly at night. Later stages
(group completion, proactive follow-up on objective signals) build on this
once stage 1 has proven reliable over weeks.

## OpenClaw Integration

### Session Key

`openclaw_session` determines which session voice requests land in. For voice and Telegram chat to share context, this key must match the Telegram session key.

Find it in the OpenClaw dashboard under **Sessions** or in:
```
~/.openclaw/agents/main/sessions/sessions.json
```

Typical format: `agent:main:telegram:group:-1003XXXXXXXXX`

The script sets the HTTP header `x-openclaw-session-key`. Without it, OpenClaw creates a separate `openresponses-user:` namespace and voice turns are isolated from chat history.

### AGENTS.md (voice directives)

The OpenClaw workspace file `~/.openclaw/workspace/AGENTS.md` shapes how the assistant behaves on voice. Frame these as **goals**, not prohibitions — describe what you want to achieve so the model can act sensibly in context. A minimal voice section:

```markdown
## Voice (🎤)

Messages starting with 🎤 arrive via speech recognition, and your reply is **read out loud**. That's the yardstick: talk like a person would in conversation.

- Reply in the user's language, in natural spoken sentences.
- Length follows the content — usually one to four sentences, more when the topic needs it; each sentence clear and complete.
- It's spoken, so it should sound good — leave out what can't be heard (markdown, lists, numbering, emojis).
- Transcriptions have small errors; interpret generously and act once the intent is clear — when understanding, not when actuating (see below).
- You are the keeper of the voice channel: a person is waiting at the speaker, and every second of silence feels long. When a task turns out to take longer — foreseeable up front or only mid-task — give a short spoken acknowledgement right away, do the work in the background, and announce the result via `voice_speak_text`. That mandate is about answering, not about actuating: a short question back beats a guessed device.

### Answering and actuating are not the same risk

Interpreting generously comes from the information side, and there it is right: a misheard name meets resistance — the calendar, the notes and the files hold the correct names, a wrong guess visibly fails to fit and costs one sentence. When actuating, that resistance is absent entirely. Nothing checks whether the guessed device was the intended one, and the mistake then stands physically in the room. The noun you generously skim past when answering *is* the target when actuating.

This does not imply a duty to ask back — an assistant that confirms every switch is unusable. It implies a different way of looking:

- A device name you cannot place is a finding, and it belongs in your answer — not a gap to be closed by inference until some target is left over.
- Uncertainty narrows, it never widens: "I don't know which one" must never become "then all of them". Widening the blast radius does the most damage exactly when you know the least.
- Others act in the house too — people, other voice assistants, automations. And devices need time: a shutter reporting sixty-eight percent on its way to fifty is working, not failing. Repeating a command because the target value hasn't arrived yet fights both the device and the person in the room.

### Speaker awareness & safety

Each 🎤 message is prefixed with `[Sprecher: …]` (the recognised speaker, or `unbekannt`). Goal: impactful or hard-to-undo actions should only happen when it's clear a trusted person wants them. For an `unbekannt` speaker, be freely helpful with harmless things (info, status, simple queries); for anything with loss or damage potential, get confirmation from a known speaker first.

### Mood signal (acoustic)

Some 🎤 messages carry a line with `arousal` / `valence` / `dominance` values (0–1, ~0.5 neutral) measured from the voice — the *tone*, not the content. Let it inform your picture of the person and how you act, naturally, like a human picking up on someone's tone. It's rough; interpret in context, don't over-read, and don't usually name it out loud.

### Voice & speaking rate

You can freely choose and switch your own voice and speaking rate (`voice_list_voices`, `voice_set_voice`, `voice_set_speed`); pass a speaker's name as `for_speaker` to remember a preferred voice per person.
```

The deployed `AGENTS.md` holds the full version (incl. voice → chat continuation handling).

With the [voice actuator](#voice-actuator-optional) enabled, one more point belongs here: clean switching commands are handled by the actuator itself and never reach the brain. A switching sentence that arrives there anyway was **not** recognised as a command by the guarded path, or that path was unavailable — a reason for more caution, not more ambition. Switching therefore goes through [that same guarded path](#the-same-path-for-the-brain-mcp) rather than the raw home automation API: standing later in the chain gives the brain no more powerful route, only the same one.

## Speaker Recognition & Enrolment

Each recording runs through Speaches diarization in parallel to STT. The dominant speaker is forwarded to OpenClaw in the wrapper prefix:

```
🎤 [Sprecher: jochen] How is the weather?
🎤 [Sprecher: unbekannt] How is the weather?
```

### Workspace layout

```
~/.openclaw/workspace/voice/
  last_recording.wav             current recording (overwritten per trigger)
  speakers/
    jochen.wav                   active reference (sent to Speaches)
  originals/
    jochen-2026-05-09T22-15.wav  timestamped backup, never overwritten
```

### Enrolment HTTP server

The voice_assistant exposes a small loopback HTTP server on `127.0.0.1:18791` that lets external tools manage speaker references:

| Method | Path                | Body / Effect |
|---|---|---|
| `POST` | `/enroll`           | `{"name": "Jochen"}` — copies `last_recording.wav` to `speakers/jochen.wav` + timestamped backup |
| `GET`  | `/speakers`         | `{"speakers": ["jochen", "katrin"]}` |
| `DELETE` | `/speakers/<name>` | removes the reference |

Names are normalized (lowercase, alphanumeric + `-_`).

### OpenClaw plugin (voice tools)

The companion plugin in [`openclaw-plugin/`](openclaw-plugin/) registers the tools the LLM can call during a voice turn:

| Tool | Purpose |
|---|---|
| `voice_speak_text(text)` | speak a short text out loud (fire-and-forget) |
| `voice_enroll_speaker(name)` | store the last recording as this speaker's reference |
| `voice_list_speakers()` / `voice_remove_speaker(name)` | manage known speakers |
| `voice_list_voices()` / `voice_set_voice(…)` / `voice_set_speed(…)` | switch TTS voice / rate (also per speaker via `for_speaker`) |
| `voice_analyze_last_output(…)` | re-analyse the assistant's own last spoken reply (text fidelity, timing, prosody) |

Install / register:

```bash
openclaw plugins install --link ~/openclaw_voice_assist/openclaw-plugin/
openclaw gateway restart
openclaw plugins inspect voice-enrol --runtime --json   # status should be "loaded"
```

> **Important:** every tool must also be listed in `openclaw.plugin.json` under `contracts.tools` — OpenClaw (≥ 2026.6) silently refuses any tool that is only registered in `index.js`. After a restart, verify the gateway log has no `must declare contracts.tools for: …` lines.

These tools call the assistant's loopback HTTP servers (enrolment `:18791`, speak `:18792`), so the voice_assistant must be running. See [`openclaw-plugin/README.md`](openclaw-plugin/README.md) for details.

### Limitations

- Speaches diarization needs **at least 16 kHz mono audio with 2–10 s of real speech** (silence does not contribute). Very short follow-up answers (≤ 2 s) often classify as "unknown".
- Recordings longer than ~10 s would OOM the GPU (Wespeaker resnet34 buffer allocation), so the diarization client truncates input + references to 8 s before the request. The original full recording is still preserved in `originals/` and `last_recording.wav`.
- The first-time enrolment uses the same recording the user spoke their request in (Variant 1). Quality scales with recording length and noise level.

## Speaches Integration

STT: `POST {speaches_base}/v1/audio/transcriptions` — model `guillaumekln/faster-whisper-medium`

TTS: `POST {speaches_base}/v1/audio/speech` — model `speaches-ai/piper-de_DE-thorsten-medium`

Diarization: `POST {speaches_base}/v1/audio/diarization` — models `Wespeaker/wespeaker-voxceleb-resnet34-LM` + `fedirz/segmentation_community_1` (Speaches ≥ v0.9.0-rc.3)

60-second cooldown after connection failures. On failure the local fallback activates automatically:
- STT fallback: `faster-whisper` (model `small`, runs on the Pi)
- TTS fallback: Piper (`~/.local/share/piper/de_DE-thorsten-low.onnx`)
- Diarization has no local fallback — falls through to "speaker: unknown"

### Piper TTS (local fallback)

When Speaches is unreachable, TTS falls back to Piper running on the Pi, using the models from `<project>/models/piper/` (see installation step 6). The pre-rendered "Ja?" wakeword acknowledgement also uses Piper.

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
