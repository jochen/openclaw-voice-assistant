# openclaw-voice-assistant

> [English version](README.md)

Wakeword-gesteuerter Sprachassistent für Raspberry Pi. Verbindet lokale Spracheingabe mit [OpenClaw](https://github.com/openclaw/openclaw) als KI-Backend und [Speaches](https://github.com/speaches-ai/speaches) für GPU-beschleunigtes STT/TTS.

## Pipeline

```
Audio-Frontend (ALSA-Mikrofon  ODER  ReSpeaker XVF3800 via ESPHome)
  → openWakeWord ("hey jarvis")
  → WebRTC VAD + Aufnahme
  → STT: Speaches /v1/audio/transcriptions  (Fallback: faster-whisper lokal)
  → Bestätigung vorlesen ("Ich habe verstanden…") — paralleler Thread
  → POST /v1/responses → OpenClaw (vollständiger Agentic Loop inkl. Tool-Calls)
  → Antwort Satz für Satz via TTS: Speaches /v1/audio/speech  (Fallback: Piper lokal)
  → Anfrage + Antwort per Telegram spiegeln
```

## Voraussetzungen

- Raspberry Pi (getestet: Pi 4/5, ARM64, Raspberry Pi OS Bookworm)
- **Python 3.11.9** (exakt — `openwakeword` + `tflite-runtime` erfordern diese Version auf ARM64)
- [OpenClaw](https://openclaw.dev) läuft lokal auf `http://127.0.0.1:18789`
- [Speaches](https://github.com/speaches-ai/speaches) GPU-Container erreichbar (Standard: `http://192.168.111.126:8000`)

**Mode: local** — ALSA-Mikrofon + ALSA-Lautsprecher + optionaler WLED-LED-Streifen

**Mode: respeaker** — ReSpeaker XVF3800 4-Mikrofon-Array + XIAO ESP32-S3, gesteuert über ESPHome Native API (`aioesphomeapi`). Kein Home Assistant erforderlich.

## Installation

### 1. Repository klonen

```bash
git clone https://github.com/jochen/openclaw-voice-assistant.git
cd openclaw-voice-assistant
```

### 2. Python 3.11.9 via pyenv installieren

`openwakeword` und `tflite-runtime` sind auf neueren Python-Versionen auf ARM64 nicht verfügbar. Daher wird **exakt Python 3.11.9** benötigt.

```bash
curl https://pyenv.run | bash

# Shell-Integration (in ~/.bashrc oder ~/.zshrc eintragen):
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev

pyenv install 3.11.9
```

Das Repo enthält eine `.python-version`-Datei — pyenv aktiviert 3.11.9 automatisch.

### 3. Venv anlegen und Dependencies installieren

```bash
python -m venv /home/pi/openclaw_voice_assist/ow-venv
source /home/pi/openclaw_voice_assist/ow-venv/bin/activate
pip install -r requirements.txt
```

### 4. openWakeWord-Modelle herunterladen

```bash
python -c "
from openwakeword.model import Model
Model(wakeword_models=['hey_jarvis'], inference_framework='tflite')
"
```

Modelle landen unter `/tmp/ow_models_min`.

### 5. Konfiguration anlegen

```bash
cp config.example.yaml config.yaml
```

`config.yaml` editieren. Gemeinsame Felder:

| Feld | Beschreibung |
|---|---|
| `speaches_base` | URL des Speaches-Containers |
| `openclaw_token` | API-Token aus dem OpenClaw-Dashboard |
| `openclaw_session` | Session-Key (siehe unten) |
| `telegram_bot_token` | Telegram Bot Token von @BotFather |
| `telegram_chat_id` | Telegram Gruppen-ID (mit `-` Prefix) |

**Mode: local** — zusätzliche Felder:

| Feld | Beschreibung |
|---|---|
| `device_index` | ALSA-Mikrofon-Index (`arecord -l`) |
| `rate_in` | Samplerate des Mikrofons (48000 oder 16000) |
| `wled_host` | Hostname oder IP des WLED-Controllers (optional) |

**Mode: respeaker** — zusätzliche Felder:

| Feld | Beschreibung |
|---|---|
| `respeaker.host` | Hostname oder IP des ESP32-S3 (z.B. `respeaker-openclaw.local`) |
| `respeaker.volume` | Lautstärke 0.0–1.0 (beim Connect gesetzt, kein OTA nötig) |
| `respeaker.use_speaker` | `true` = TTS über ReSpeaker-DAC; `false` = lokaler ALSA-Lautsprecher |

## Starten

```bash
source /home/pi/openclaw_voice_assist/ow-venv/bin/activate
python -m voice_assistant
```

Der Entry-Point startet sich automatisch im richtigen Venv neu falls nötig.

Profil überschreiben: `GASTON_PROFILE=clawdpi_rs python -m voice_assistant`

## Profile

Profil wird automatisch per Hostname erkannt oder via `GASTON_PROFILE` gesetzt:

| Profil | Hostname-Match | Mode | Besonderheit |
|---|---|---|---|
| `clawdpi` | `clawdpi*` | local | Index 1, 48kHz (resampelt), WLED |
| `openclaw` | `openclaw*` | local | Index 0, 16kHz nativ |
| `clawdpi_rs` | — | respeaker | ReSpeaker XVF3800 auf `clawdpi` |

## ReSpeaker-Setup (mode: respeaker)

Die ESP32-S3-Firmware liegt in `esphome/respeaker.yaml`. Flashen:

```bash
# Initial (USB):
esphome-venv/bin/esphome run esphome/respeaker.yaml --device /dev/ttyACM0

# OTA:
esphome-venv/bin/esphome run esphome/respeaker.yaml --device respeaker-openclaw.local
```

Das ESPHome-Venv ist vom `ow-venv` getrennt:

```bash
python -m venv esphome-venv
esphome-venv/bin/pip install esphome
```

**Funktionsweise:** Der Pi verbindet sich via ESPHome Native API (Port 6053, `aioesphomeapi`) mit dem ESP. Audio streamt kontinuierlich über die `voice_assistant`-Komponente im API_AUDIO-Modus. TTS-Ausgabe wird als WAV über die `media_player`-Announce-API zurückgespielt — der Pi stellt die WAV-Datei per HTTP (Port 18800) bereit, der ESP lädt und spielt sie ab.

Wakeword-Erkennung (`openwakeword`) läuft auf dem Pi gegen den Audio-Stream.

## OpenClaw-Integration

### Session-Key

`openclaw_session` bestimmt, in welcher Session Voice-Anfragen landen. Damit Voice und Telegram-Chat denselben Kontext teilen, muss dieser Key mit dem Telegram-Session-Key übereinstimmen.

Den Key findest du im OpenClaw-Dashboard unter **Sessions** oder in:
```
~/.openclaw/agents/main/sessions/sessions.json
```

Typisches Format: `agent:main:telegram:group:-1003XXXXXXXXX`

Das Script setzt den HTTP-Header `x-openclaw-session-key`. Ohne ihn legt OpenClaw einen separaten `openresponses-user:`-Namespace an — Voice-Turns wären vom Chat-Verlauf getrennt.

### AGENTS.md

Für korrektes Voice-Verhalten ergänze in `~/.openclaw/workspace/AGENTS.md`:

```markdown
## Sprachbefehle (Voice)

Nachrichten die mit 🎤 beginnen sind Sprachbefehle via Spracherkennung.
Für diese Nachrichten gelten STRENGE Regeln — keine Ausnahmen:

- Antworte IMMER auf Deutsch
- Maximal 2-3 kurze Sätze
- Absolut kein Markdown, keine Listen, keine Nummerierungen
- Keine Emojis
- Natürliche gesprochene Sprache

### Voice → Chat Übergänge

Wenn nach einer 🎤-Nachricht eine normale Chat-Nachricht folgt (zeitnah, thematisch verwandt),
ist das eine Fortsetzung oder Korrektur des letzten Voice-Tasks:

1. Original-Task aus dem Kontext rekonstruieren
2. Task mit der Korrektur neu ausführen — vollständig
3. Kein Meta-Kommentar über den eigenen Fehler
4. Für Chat-Antworten gilt die 2-3-Satz-Beschränkung nicht
```

## Speaches-Integration

STT: `POST {speaches_base}/v1/audio/transcriptions` — Modell `guillaumekln/faster-whisper-medium`

TTS: `POST {speaches_base}/v1/audio/speech` — Modell `speaches-ai/piper-de_DE-thorsten-medium`

60-Sekunden-Cooldown nach Verbindungsfehlern. Bei Ausfall greift automatisch der lokale Fallback:
- STT: `faster-whisper` (Modell `small`, läuft auf dem Pi)
- TTS: Piper (`~/.local/share/piper/de_DE-thorsten-low.onnx`)

### Piper TTS installieren (lokaler Fallback)

```bash
pip install piper-tts
mkdir -p ~/.local/share/piper && cd ~/.local/share/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx.json
```

## LED-Status

WLED (mode: local) und ReSpeaker LED-Ring (mode: respeaker) sind exklusiv.

### ReSpeaker LED-Ring — 12 Phasen

| Phase | Zustand | Animation |
|---|---|---|
| 0 | BOOT | LEDs leuchten sequenziell auf: WiFi(1–3) → API(4–6) → Speaches(7–9) → Wakeword(10–12) |
| 1 | IDLE | Alle LEDs sehr gedimmt blau; ein leicht hellerer Punkt wandert extrem langsam (~36s/Umdrehung) |
| 2 | WAKEWORD | Alle 12 LEDs hell rot |
| 3 | RECORDING | Rote Basis + Richtungsanzeige Sprechrichtung (XVF3800 DOA, ESP-intern) |
| 4 | STT | Rotierender Punkt, blau, langsam (150ms/Schritt) |
| 5 | CONFIRMATION | Rotierender Punkt, blau, schneller (100ms/Schritt) |
| 6 | OPENCLAW_WAIT | Rotierender Punkt, rot-lila, schnell (50ms/Schritt) |
| 7 | ANSWER_GLOW | Alle LEDs grün, statisch |
| 8 | AUDIO_OUT | Alle LEDs grün, pulsierend |
| 9 | END | Alle aus — Pi wechselt nach 1s Pause auf IDLE |
| 10 | ERROR | 6 LEDs (halber Ring), rot, statisch |
| 11 | FOLLOWUP | Warm-gelb, sanft pulsierend — reserviert für zukünftige Rückfrage-Funktion |

### WLED-Streifen (mode: local)

| LED | Farbe | Zustand |
|---|---|---|
| 0 | Blau | Idle |
| 1 | Rot | Wakeword / Aufnahme |
| 2 | Orange | STT / Bestätigung |
| 4 | Lila | Warte auf OpenClaw |
| 5 | Grün | Liest Antwort vor |

## Lizenz

MIT — siehe [LICENSE](LICENSE).
