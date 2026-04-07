# openclaw-voice-assist

Wakeword-gesteuerter Sprachassistent für Raspberry Pi. Verbindet lokale Spracheingabe mit [OpenClaw](https://github.com/openclaw/openclaw) als KI-Backend und [Speaches](https://github.com/speaches-ai/speaches) für GPU-beschleunigtes STT/TTS.

## Pipeline

```
Mikrofon → openWakeWord ("hey jarvis")
  → WebRTC VAD + Aufnahme
  → STT: Speaches /v1/audio/transcriptions  (Fallback: faster-whisper lokal)
  → Bestätigung vorlesen ("Ich habe verstanden…") — paralleler Thread
  → POST /v1/responses → OpenClaw (vollständiger Agentic Loop)
  → Antwort Satz für Satz via TTS: Speaches /v1/audio/speech  (Fallback: Piper lokal)
  → Antwort + Anfrage per Telegram spiegeln
```

## Voraussetzungen

- Raspberry Pi (getestet: Pi 4/5, ARM64, Raspberry Pi OS Bookworm)
- **Python 3.11.9** (exakt — `openwakeword` + `tflite-runtime` erfordern diese Version, siehe unten)
- [OpenClaw](https://openclaw.dev) läuft lokal auf `http://127.0.0.1:18789`
- [Speaches](https://github.com/speaches-ai/speaches) GPU-Container erreichbar (Standard: `http://192.168.111.126:8000`)
- Mikrofon mit ALSA-Unterstützung
- Optional: WLED-Controller für LED-Status, Piper TTS für lokalen Fallback

## Installation

### 1. Repository klonen

```bash
git clone https://github.com/jochen/openclaw-voice-assistant.git
cd openclaw-voice-assistant
```

### 2. Python 3.11.9 via pyenv installieren

`openwakeword` und `tflite-runtime` sind auf neueren Python-Versionen auf ARM64 nicht verfügbar. Daher wird **exakt Python 3.11.9** benötigt.

```bash
# pyenv installieren (falls noch nicht vorhanden)
curl https://pyenv.run | bash

# Shell-Integration (in ~/.bashrc oder ~/.zshrc eintragen):
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

# Python 3.11.9 bauen (Buildabhängigkeiten vorher installieren)
sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev

pyenv install 3.11.9
```

Das Repo enthält eine `.python-version`-Datei — `pyenv` aktiviert 3.11.9 automatisch sobald du in das Verzeichnis wechselst.

### 3. Venv anlegen und Dependencies installieren

```bash
# Im Projektverzeichnis (pyenv aktiviert automatisch 3.11.9)
python -m venv ~/ow-venv
source ~/ow-venv/bin/activate

pip install -r requirements.txt
```

### 3. openwakeword-Modelle herunterladen

```bash
python -c "
from openwakeword.model import Model
Model(wakeword_models=['hey_jarvis'], inference_framework='tflite')
"
```

Modelle landen unter `/tmp/ow_models_min` (konfigurierbar im Script).

### 4. Konfiguration anlegen

```bash
cp config.example.yaml config.yaml
```

`config.yaml` editieren — mindestens diese Felder für dein Profil ausfüllen:

| Feld | Beschreibung |
|---|---|
| `device_index` | ALSA-Mikrofon-Index (`arecord -l` zeigt verfügbare Geräte) |
| `rate_in` | Samplerate des Mikrofons (48000 oder 16000) |
| `speaches_base` | URL des Speaches-Containers |
| `openclaw_token` | API-Token aus OpenClaw-Dashboard |
| `openclaw_session` | Session-Key (siehe unten) |
| `telegram_bot_token` | Telegram Bot Token von @BotFather |
| `telegram_chat_id` | Telegram Gruppen-ID (mit `-` prefix) |

## OpenClaw-Integration

### Session-Key

Der `openclaw_session` bestimmt, **in welcher Session** Voice-Anfragen landen. Damit Voice und Telegram-Chat denselben Kontext teilen, muss dieser Key mit der Session-Key deiner Telegram-Gruppe übereinstimmen.

Den Key findest du im OpenClaw-Dashboard unter **Sessions** oder in:
```
~/.openclaw/agents/main/sessions/sessions.json
```

Typisches Format: `agent:main:telegram:group:-1003XXXXXXXXX`

**Warum das wichtig ist:** Das Script setzt den HTTP-Header `x-openclaw-session-key`, der in OpenClaw Vorrang vor dem automatisch generierten Namespace hat. Ohne diesen Header würde OpenClaw einen separaten `openresponses-user:`-Namespace anlegen und Voice-Turns wären vom Chat-Verlauf getrennt.

### AGENTS.md konfigurieren

Für korrektes Voice-Verhalten ergänze in `~/.openclaw/workspace/AGENTS.md` die Sektion `## Sprachbefehle (Voice)`. Vorlage:

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

Speaches läuft als Docker-Container mit OpenAI-kompatibler API.

### STT (Sprache → Text)

```
POST {speaches_base}/v1/audio/transcriptions
model: guillaumekln/faster-whisper-medium   # oder anderes Modell
language: de
```

### TTS (Text → Sprache)

```
POST {speaches_base}/v1/audio/speech
model: speaches-ai/piper-de_DE-thorsten-medium
voice: de_DE-thorsten-medium
```

Beide Dienste haben einen 60-Sekunden-Cooldown nach Verbindungsfehlern bevor ein Retry versucht wird (`SpeachesState`-Klasse im Script). Bei Ausfall greift automatisch der lokale Fallback:
- STT: `faster-whisper` (Modell `small`, läuft direkt auf dem Pi)
- TTS: Piper (`~/.local/share/piper/de_DE-thorsten-low.onnx`)

## Piper TTS installieren (lokaler Fallback)

```bash
pip install piper-tts

# Deutsches Modell herunterladen
mkdir -p ~/.local/share/piper
cd ~/.local/share/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/low/de_DE-thorsten-low.onnx.json
```

## Profile

Zwei Profile sind vorkonfiguriert (in `config.yaml`). Das aktive Profil wird automatisch per Hostname erkannt oder via Umgebungsvariable gesetzt:

```bash
GASTON_PROFILE=openclaw python voice_assistant.py
```

| Profil | Hostname-Match | Mikrofon | Besonderheit |
|---|---|---|---|
| `clawdpi` | `clawdpi*` | Index 1, 48kHz (resampelt) | Hauptgerät |
| `openclaw` | `openclaw*` | Index 0, 16kHz nativ | Zweiter Pi |

## Starten

```bash
source ~/ow-venv/bin/activate
python voice_assistant.py
```

Das Script erkennt selbst ob es im richtigen Venv läuft und startet sich bei Bedarf neu.

## LED-Status (WLED)

| LED | Farbe | Zustand |
|---|---|---|
| 0 | Blau | Bereit, warte auf Wakeword |
| 1 | Grün | Wakeword erkannt, höre zu |
| 2 | Gelb | STT verarbeitet |
| 3 | Rot | Kurze Pause nach Aufnahme |
| 4 | Lila | Warte auf OpenClaw-Antwort |
| 5 | Cyan | Liest Antwort vor |

WLED-Controller: `~/.openclaw/workspace/wled/wled_controller.py`
