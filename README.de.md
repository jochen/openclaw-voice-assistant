# openclaw-voice-assistant

> [English version](README.md)

Wakeword-gesteuerter Sprachassistent für Raspberry Pi. Verbindet lokale Spracheingabe mit [OpenClaw](https://github.com/openclaw/openclaw) als KI-Backend und [Speaches](https://github.com/speaches-ai/speaches) für GPU-beschleunigtes STT/TTS.

## Pipeline

```
Audio-Frontend (ALSA-Mikrofon  ODER  ReSpeaker XVF3800 via ESPHome)
  → openWakeWord ("hey jarvis")
  → WebRTC VAD + Aufnahme (max 30 s)
  → STT: Speaches /v1/audio/transcriptions  (Fallback: faster-whisper lokal)
  → Diarization (parallel): Speaches /v1/audio/diarization mit bekannten Sprechern
  → Voice-Aktuator (optional): Schaltbefehl? → lokal ausführen (~0,5 s), Rest entfällt
  → Bestätigung vorlesen ("Ich habe verstanden…") — paralleler Thread
  → POST /v1/responses → OpenClaw  (Wrapper: "🎤 [Sprecher: jochen|unbekannt] {text}")
  → Antwort Satz für Satz via TTS: Speaches /v1/audio/speech  (Fallback: Piper lokal)
  → Anfrage + Antwort per Telegram spiegeln
```

## Voraussetzungen

- Raspberry Pi (getestet: Pi 4/5, ARM64, Raspberry Pi OS Bookworm)
- **Python 3.11.9** (exakt — `openwakeword` + `tflite-runtime` erfordern diese Version auf ARM64)
- [OpenClaw](https://openclaw.dev) läuft lokal auf `http://127.0.0.1:18789`
- [Speaches](https://github.com/speaches-ai/speaches) GPU-Container erreichbar (Standard: `http://<speaches-host>:8000`)
- *(optional)* `voice-analysis`-Container (Standard: `http://<speaches-host>:8001`) — liefert das akustische Stimmungssignal und das Tool `voice_analyze_last_output`; der Assistent läuft auch ohne ihn.

**Mode: local** — ALSA-Mikrofon + ALSA-Lautsprecher + optionaler WLED-LED-Streifen

**Mode: respeaker** — ReSpeaker XVF3800 4-Mikrofon-Array + XIAO ESP32-S3, gesteuert über ESPHome Native API (`aioesphomeapi`). Kein Home Assistant erforderlich.

## Installation

### 1. Repository klonen

```bash
# Verzeichnisname bewusst mit Unterstrichen: die systemd-Unit erwartet
# %h/openclaw_voice_assist
git clone https://github.com/jochen/openclaw-voice-assistant.git openclaw_voice_assist
cd openclaw_voice_assist
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
python -m venv ~/openclaw_voice_assist/ow-venv
source ~/openclaw_voice_assist/ow-venv/bin/activate
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

### 6. Piper-TTS-Modelle herunterladen (lokaler Fallback + Wakeword-Bestätigung)

`piper-tts` ist in `requirements.txt` enthalten; die Modelle sind gitignored und werden aus fest kodierten Pfaden unter `<Projekt>/models/piper/` geladen:

```bash
mkdir -p models/piper && cd models/piper
BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE
wget $BASE/thorsten/low/de_DE-thorsten-low.onnx
wget $BASE/thorsten/low/de_DE-thorsten-low.onnx.json
wget $BASE/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx
wget $BASE/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx.json
cd ../..
```

Jedes Modell benötigt sowohl die `.onnx`-Datei als auch die zugehörige `.onnx.json`-Sidecar-Datei. Falls die Modelle bereits woanders liegen (z.B. `~/.local/share/piper/`), funktioniert auch ein Symlink in `models/piper/`.

## Starten

```bash
source ~/openclaw_voice_assist/ow-venv/bin/activate
python -m voice_assistant
```

Der Entry-Point startet sich automatisch im richtigen Venv neu falls nötig.

Profil überschreiben: `GASTON_PROFILE=clawdpi_rs python -m voice_assistant`

## Autostart (systemd User Service)

Eine Unit-Vorlage liegt in [`systemd/openclaw-voice-assist.service`](systemd/openclaw-voice-assist.service):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/openclaw-voice-assist.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now openclaw-voice-assist
loginctl enable-linger pi        # auch ohne aktive Login-Session starten
```

Die Unit setzt `PATH` so, dass `ow-venv/bin` enthalten ist — damit findet der Dienst `piper` und `aplay` für seine Subprocess-Aufrufe. Logs:

```bash
journalctl _SYSTEMD_USER_UNIT=openclaw-voice-assist.service -f
```

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

## Pegel-Gate fürs Wakewort (`wake_rms_min`)

Neben dem Score-Gate gibt es ein optionales **Pegel-Gate**: der RMS des
lautesten 300-ms-Fensters im Wake-Ringpuffer muss eine Schwelle erreichen,
sonst feuert der Trigger nicht — selbst wenn der Score das hergibt. Sein
Zweck ist, **leise Fehltrigger** (Fernseher, Tastatur, ferne Gespräche) zu
blocken, die am Score-Gate vorbeikommen, weil das Modell auf das jeweilige
Geräusch hoch scoret.

- Per Default aus (`wake_rms_min: 0.0`). Eintrag weglassen, und das Profil
  verhält sich exakt wie bisher. Bewusst ein eigener Parameter, nicht
  `vad_voice_rms_min` (der ist schon für VAD/Endpointing in Gebrauch).
- Die Schwelle gehört ins **Profil**, nicht ins Bundle (`manifest.yaml`): sie
  hängt an Mikrofon und Gain, nicht am Wakewort.

**Eigenen Wert bestimmen** (drei Schritte, ~10 Minuten):

```bash
# 1. Geführte Takes aufnehmen — ALLE Stile, besonders leise/fern/abgewandt:
ow-venv/bin/python -m wakeword_studio record --speaker <name>
# 2. Schwellenvorschlag allein aus diesen Takes (ohne Alltagsarchiv):
ow-venv/bin/python -m tools.wake_rms_replay --nur-studio
# 3. Vorschlag ins Profil eintragen, dann Service neu starten:
#      wake_rms_min: <wert>
systemctl --user restart openclaw-voice-assist.service
```

Der Modus `--nur-studio` schlägt `round(leisester Take × 0,7)` vor und warnt,
wenn die schwierigen Stile (leise, fern, abgewandt, beiläufig) fehlen — ohne
sie fällt jeder Vorschlag **zu hoch** aus und kostet später leise Rufe. Er
liefert eine sichere Untergrenze, **keine** Wirksamkeitsaussage: ohne
Alltagsarchiv kann er nicht sagen, wie viele Fehltrigger die Schwelle blockt.
Sobald eines existiert, ist der volle Lauf die bessere Quelle:

```bash
ow-venv/bin/python -m tools.wake_rms_replay     # mit Archiv: Recall + Precision
```

> **Beispiel, kein Vorgabewert.** Unsere Installation läuft mit
> `wake_rms_min: 400` (abgeleitet aus 88 echten Rufen + 20 per Ohr geprüften
> Fehltriggern, leisester echter Ruf bei RMS 336, auf einem ReSpeaker-Mic mit
> ×4 Gain). Die ersten drei Wochen lief sie mit 300 und wurde dann auf Basis
> von Messungen erhöht — siehe unten. **Absolute RMS-Werte hängen an deinem
> Mikrofon und deiner Verstärkung.** Übernimmst du unsere Zahl auf andere
> Hardware, verlierst du entweder alle Rufe (Gain niedriger) oder blockst
> nichts (Gain höher). Miss deinen eigenen Wert.

**Woran du merkst, dass die Schwelle falsch steht:** vom Gate geblockte Rufe
landen als Near-Miss mit `failed_on: "min_rms"` im
`~/.openclaw/workspace/wake_events.log`. Häufen sich dort gemeinte Rufe, ist
die Schwelle zu hoch für den aktuellen Gain. Dasselbe Signal zeigt auch einen
Hardware-/Gainwechsel an — dann den Wert neu bestimmen.

**Nach ein paar Wochen nachprüfen; entschieden wird es aus demselben Log.**
Hat das Gate eine ordentliche Zahl Streaks geblockt und war *kein einziger*
gemeinter Ruf darunter, während Fehltrigger weiter durchkommen, ist die
Schwelle zu niedrig für deinen Raum — Sweep neu laufen lassen und eine Stufe
höher gehen. Bei uns lief das so von 300 auf 400: 16 geblockte Streaks in 20
Tagen, kein einziger echter Ruf darunter. Zwei Dinge, bei denen man sich dabei
nicht in die Tasche lügen sollte:

- **Den Preis benennen.** Ab irgendeinem Wert kostet es echte Rufe. Bei uns
  kosteten 350 und 400 denselben einen Ruf, 400 blockte aber doppelt so viele
  Fehltrigger, und 450 kostete sechs — 400 war also der Knick, keine
  Geschmacksfrage.
- **Eine absolute Schwelle passt nicht auf beide Enden des Tages.** Unser
  lautester Fehltrigger lag bei 1691, der aufgegebene echte Ruf bei 336. Ein
  Pegel-Gate kauft dir die leisen Fehltrigger ab und sonst nichts; die lauten
  sind ein Modellproblem, kein Schwellenproblem.

**Gelabelte Clips sichern — das Archiv löscht sich selbst, deine Labels nicht.**
`triggers/` wird beim Service-Start nach 30 Tagen aufgeräumt, die Labels in
`wake_review.jsonl` bleiben. Verschwindet das Audio, zählen diese Labels still
nicht mehr mit und der Sweep misst unbemerkt einen kleineren Bestand. Uns hat
das sechs per Ohr geprüfte Fehltrigger gekostet, bevor es auffiel:

```bash
ow-venv/bin/python -m tools.wake_corpus sichern   # gelabelte Clips aus dem selbstlöschenden Verzeichnis holen
ow-venv/bin/python -m tools.wake_corpus bilanz    # was gesichert ist — und welche Labels ihr Audio verloren haben
ow-venv/bin/python -m tools.wake_corpus messen    # das laufende Bundle gegen diesen Korpus scoren
```

## Voice-Aktuator (optional)

Schaltbefehle wie „Mach das Küchenlicht an" gehen normalerweise denselben Weg
wie jede andere Frage: über das Sprachmodell im Backend. Das dauert Sekunden.
Der Voice-Aktuator fängt sie direkt nach der Spracherkennung ab und führt sie
lokal aus — gemessen **rund 0,5 Sekunden** statt mehrerer Sekunden.

```
STT-Text → kleines LLM formt EINEN JSON-Intent → POST /intent an die
           Hausautomation → deren Antworttext wird wörtlich vorgelesen
```

Ist der Satz kein Schaltbefehl, läuft alles unverändert weiter zum Backend.
Fällt das kleine Modell aus, ebenfalls — der Aktuator ist ein Abkürzer, kein
Nadelöhr.

**Der Assistent enthält nur die Sprachseite.** Die ausführende Seite stellst du
selbst bereit: zwei HTTP-Endpunkte, `GET /capabilities` (was darf geschaltet
werden) und `POST /intent` (tu es). Womit du sie baust, ist dem Assistenten
gleich — Node-RED, Home Assistant, openHAB oder ein fünfzigzeiliges Skript.

**→ [`ACTUATOR_INTERFACE.md`](ACTUATOR_INTERFACE.md)** beschreibt den Vertrag
vollständig, samt Begründung jeder Design-Entscheidung, einer lauffähigen
Mindest-Implementierung in Flask und einer Abnahme-Prüfung per `curl`.

Das Gerätevokabular wird **nicht** im Assistenten gepflegt: Schema und Prompt
des kleinen Modells entstehen zur Laufzeit aus `/capabilities`. Ein neues Gerät
dort anlegen genügt — es ist sofort sprechbar, ohne Neustart und ohne
Prompt-Änderung.

### Derselbe Weg für den Brain (MCP)

Der Aktuator erledigt nur, was er sicher als Schaltbefehl erkennt. Alles
andere geht an das große Modell — darunter Sätze, die *doch* schalten sollen,
nur eben in einer Form, die der Aktuator nicht zuordnen konnte. Ohne einen
eigenen Weg greift das große Modell dann zur rohen Hausautomations-API: ohne
Ziel-Whitelist, ohne Wertprüfung, ohne Rückfrage. Genau daraus entstand am
2026-08-01 der Vorfall, der diese Sektion veranlasst hat — ein verhörter
Gerätename wurde erraten und traf alle Rollos im Haus.

`voice_assistant/mcp_actuator.py` ist ein stdio-MCP-Server, der dem großen
Modell dieselbe abgesicherte Schnittstelle gibt, mit zwei Werkzeugen:

| Werkzeug | Wirkung |
|---|---|
| `haus_ziele()` | Alle Ziele mit Namen, erlaubten Aktionen, Wertebereich, `kosten`, `reversibel` — ein **geschlossenes Vokabular** |
| `haus_schalten(ziel, aktion, …)` | `POST /intent`, Antwort-Envelope unverändert |

Es gibt bewusst **kein** Freitext-Werkzeug: der ganze Gewinn ist, dass ein
Gerätename, der in `haus_ziele()` nicht vorkommt, nicht existiert — statt
erraten zu werden.

Gepostet wird mit einer eigenen `quelle`, damit die ausführende Seite ihr Gate
genauso anwendet wie beim Aktuator; das Reservieren weiterer `quelle`-Werte
für andere Aufrufer bleibt dadurch möglich. Jeder Aufruf landet in derselben
Protokolldatei wie die Aktuator-Turns — sonst wären die Schaltvorgänge des
großen Modells unsichtbar, und genau deshalb hat den Vorfall niemand bemerkt.

Aktiviert wird der Aktuator pro Profil; ohne den Block ist er aus:

```yaml
    actuator:
      enabled: true
      base_url: "http://<hausautomation>:1880/voiceact"
      llm_url:  "http://localhost:8090/v1/chat/completions"
      mqtt_host: ""          # optional: sofortige Benachrichtigung bei
                             # Änderungen; leer = nur Poll alle 10 min
```

Als lokales Modell wird ein kleines instruktionsfähiges LLM hinter einer
OpenAI-kompatiblen API erwartet (getestet: Gemma-4-E2B-Q4 über llama.cpp).
Es muss `response_format: json_schema` beherrschen — die geschlossene Form des
Intents ist die halbe Sicherheit, die andere Hälfte prüft die ausführende Seite.

Der Token für die Endpunkte steht in `voiceact-token.txt` im Projektverzeichnis
(gitignored) und geht als Header `X-Actuator-Token` mit.

**Der Prompt des Klassifikators ist deutsch — und austauschbar.** Er ist die
einzige sprachabhängige Stelle des Projekts. Wer ihn auf Englisch (oder eine
andere Sprache) betreiben will, setzt `actuator.system_prompt` im Profil; der
Code selbst enthält keine feste Ziel-id und kein Gerätewort. Ziel-Liste,
Kontrast-Beispiele und die Regel für Geräte-Mehrzahl ohne Raumangabe entstehen
bei jedem Refresh aus `/capabilities` und werden über die Platzhalter
`{ziel_liste}`, `{kontrast}` und `{gruppen_regel}` eingesetzt. Alle Schlüssel
stehen in `config.example.yaml`.

**Wer den Prompt ersetzt, misst neu.** `tools/actuator_grammar_test.py` fährt
ein festes Test-Set gegen den echten `classify()`-Pfad und schreibt die
capabilities-Version mit in die Ausgabe — eine Zahl gilt immer nur für einen
Stand der Geräteliste. Zwei Erkenntnisse aus unseren Messungen, die
sprachunabhängig sein dürften: wenige Few-Shots schlagen viele, und die
Mehrzahl-Regel wirkt nur *hinter* der Ziel-Liste, nie davor. Die Testsätze
gehören mit übersetzt.

Turns, die der Aktuator selbst erledigt, sieht das Backend nicht — sie landen
deshalb als JSONL in `<workspace>/actuator_turns.log`.

### Überwacher (Stufe 1 — nur lesen und melden)

Da der Aktuator das Backend überspringt, prüft nichts nach, ob das Gesagte
mit dem Geschalteten übereinstimmt. Der Überwacher schließt diese Lücke
konservativ: er prüft jeden Aktuator-Turn und meldet Diskrepanzen — er
greift **nicht** ein, schaltet nicht, korrigiert nicht.

Die semantische Prüfung nutzt ein LLM (OpenAI-kompatibles API, wie der
Aktuator selbst). Ein kurzer System-Prompt fragt: passt das Transkript zum
Intent? Das LLM erkennt subtilere Muster die eine Regex-Heuristik verfehlt
— Verneinungen, Einschränkungen, Präposition vs. Aktion („auf 10%" ist eine
SETZEN-Aktion, nicht die Aktion „auf" = ganz öffnen). Siehe
`voice_assistant/services/watcher.py`.

Zusätzlich eine deterministische strukturelle Prüfung ohne LLM:

- **EXEC_DIFFERS** — die Haussteuerung hat ein anderes Ziel/eine andere
  Aktion ausgeführt als der Intent verlangte.
- **STATUS_PROBLEM** — die Automation antwortet nicht „ausgefuehrt" (abgelehnt,
  unbekanntes Ziel, zurückgestellt). Wird archiviert, aber nicht nach Telegram
  geschickt (zu laut).

Bei LLM-Fehlern (Provider down, Timeout): der Überwacher schickt eine Meldung
an den Telegram-Chat („Überwachung konnte nicht erfolgen weil …") statt im
Dunkeln zu versagen. Timeout ist 30s mit einem Retry bei Netzwerk-/Timeout-
Fehlern.

Der Überwacher läuft als event-gesteuerter Daemon-Thread — `check_turn()`
wird sofort nach jedem Aktuator-Turn aufgerufen (nicht als periodischer
Timer), sodass ein Mismatch binnen ~1 Sekunde gemeldet wird.

Einmaliges CLI-Tool (liest das Log, klassifiziert, dedupliziert):

```bash
ow-venv/bin/python -m tools.actuator_watch
ow-venv/bin/python -m tools.actuator_watch --seit 3   # nur letzte 3 Tage
ow-venv/bin/python -m tools.actuator_watch --alles     # auch schon gesehene
```

Hintergrund-Worker im Assistant, der in einen separaten Telegram-Chat
meldet (nicht den Familien-Voice-Chat):

```yaml
    watcher:
      enabled: true
      chat_id: "<telegram-chat-id>"   # separate Gruppe, nicht der Voice-Spiegel
      quiet_start: 1                   # keine Meldungen 01:00–07:00 Uhr
      quiet_end: 7
      llm_url: "https://<provider>/v1/chat/completions"
      llm_model: "<modell>"
      llm_api_key: "<api-key>"
      llm_timeout: 30
```

Nur `LLM_MISMATCH` und `EXEC_DIFFERS` werden nach Telegram geschickt;
während der Stille-Zeit werden Befunde gesammelt und zurückgehalten. Stufe 1
ist bewusst konservativ — der teuerste Fehler des Überwachers wäre eine
*eingebildete* Korrektur, physisch im Haus, womöglich nachts. Spätere Stufen
(Gruppen-Vervollständigung, proaktive Rückfrage bei objektivem Signal)
bauen darauf auf, sobald Stufe 1 sich über Wochen bewährt hat.

## OpenClaw-Integration

### Session-Key

`openclaw_session` bestimmt, in welcher Session Voice-Anfragen landen. Damit Voice und Telegram-Chat denselben Kontext teilen, muss dieser Key mit dem Telegram-Session-Key übereinstimmen.

Den Key findest du im OpenClaw-Dashboard unter **Sessions** oder in:
```
~/.openclaw/agents/main/sessions/sessions.json
```

Typisches Format: `agent:main:telegram:group:-1003XXXXXXXXX`

Das Script setzt den HTTP-Header `x-openclaw-session-key`. Ohne ihn legt OpenClaw einen separaten `openresponses-user:`-Namespace an — Voice-Turns wären vom Chat-Verlauf getrennt.

### AGENTS.md (Voice-Direktiven)

Die OpenClaw-Workspace-Datei `~/.openclaw/workspace/AGENTS.md` prägt das Voice-Verhalten des Assistenten. Formuliere diese Vorgaben als **Ziele**, nicht als Verbote — beschreibe, was erreicht werden soll, damit das Modell im Kontext sinnvoll handeln kann. Eine minimale Voice-Sektion:

```markdown
## Voice (🎤)

Nachrichten mit 🎤 kommen über Spracherkennung herein, und deine Antwort wird **laut vorgelesen**. Das ist der Maßstab: rede so, wie ein Mensch im Gespräch reden würde.

- Antworte in der Sprache des Nutzers, in natürlich gesprochenen Sätzen.
- Die Länge richtet sich nach dem Inhalt — meist ein bis vier Sätze, bei komplexen Themen mehr; jeder Satz klar und vollständig.
- Es wird vorgelesen, also soll es gut klingen — lass weg, was man nicht hören kann (Markdown, Listen, Nummerierungen, Emojis).
- Transkriptionen haben kleine Fehler; interpretiere großzügig und handle, sobald der Sinn klar ist — beim Verstehen, nicht beim Schalten (siehe unten).
- Du hütest den Sprachkanal: dort wartet ein Mensch, für den jede Sekunde Stille lang ist. Wird eine Aufgabe spürbar länger — vorab absehbar oder erst mitten in der Arbeit —, gib sofort eine kurze gesprochene Rückmeldung, erledige die Arbeit im Hintergrund und sag das Ergebnis über `voice_speak_text` an. Das gilt fürs Antworten, nicht fürs Schalten: lieber eine kurze Rückfrage als ein geratenes Gerät.

### Auskunft und Steuerung sind nicht dasselbe Risiko

Großzügig zu interpretieren stammt aus dem Auskunfts-Betrieb, und dort ist es richtig: ein falsch verstandener Name stößt auf Widerstand — Kalender, Notizen und Dateien halten die richtigen Namen, ein Fehlgriff passt sichtbar nicht und kostet einen Satz. Beim Schalten fehlt dieser Widerstand vollständig. Nichts prüft nach, ob das geratene Gerät das gemeinte war, und der Fehler steht danach physisch im Raum. Das Substantiv, das beim Auskunftgeben großzügig überlesen wird, ist beim Schalten das Ziel.

Daraus folgt keine Rückfragepflicht — ein Assistent, der jede Schaltung bestätigen lässt, ist unbrauchbar. Es folgt eine andere Blickrichtung:

- Ein Gerätename, der sich nicht zuordnen lässt, ist ein Befund und gehört ausgesprochen — keine Lücke, die durch Schlussfolgern geschlossen wird, bis irgendein Ziel übrig bleibt.
- Unsicherheit verengt, sie weitet nie: „ich weiß nicht welches" wird niemals zu „dann eben alle". Den Wirkungsbereich auszudehnen richtet den größten Schaden genau dann an, wenn am wenigsten bekannt ist.
- Im Haus schalten auch andere — Menschen, andere Sprachassistenten, Automatisierungen. Und Geräte brauchen Zeit: ein Rollo, das auf dem Weg zu fünfzig Prozent gerade achtundsechzig meldet, arbeitet; es ist nicht fehlgeschlagen. Einen Befehl zu wiederholen, weil der Zielwert noch nicht dasteht, kämpft gegen das Gerät und gegen die Person im Raum.

### Sprechererkennung & Sicherheit

Jede 🎤-Nachricht ist mit `[Sprecher: …]` versehen (erkannter Sprecher oder `unbekannt`). Ziel: folgenreiche oder schwer umkehrbare Aktionen sollen nur passieren, wenn klar ist, dass eine berechtigte Person sie will. Bei `unbekannt`em Sprecher sei frei hilfsbereit für Harmloses (Auskünfte, Status, einfache Abfragen); für alles mit Verlust- oder Schadenspotenzial hol vorher die Bestätigung eines bekannten Sprechers.

### Stimmungssignal (akustisch)

Manche 🎤-Nachrichten tragen eine Zeile mit `arousal` / `valence` / `dominance` (0–1, ~0.5 neutral), gemessen aus der Stimme — dem *Tonfall*, nicht dem Inhalt. Lass es dein Bild der Person und dein Vorgehen natürlich mitprägen, wie ein Mensch den Tonfall mitbekommt. Es ist grob; im Kontext deuten, nicht überinterpretieren, normalerweise nicht explizit benennen.

### Stimme & Sprechtempo

Du kannst deine Stimme und dein Tempo frei wählen und wechseln (`voice_list_voices`, `voice_set_voice`, `voice_set_speed`); gib den Namen eines Sprechers als `for_speaker` mit, um eine bevorzugte Stimme pro Person zu merken.
```

Die ausgerollte `AGENTS.md` enthält die vollständige Fassung (inkl. Voice→Chat-Fortsetzung).

Ist der [Voice-Aktuator](#voice-aktuator-optional) aktiv, gehört ein weiterer Punkt dazu: saubere Schaltbefehle erledigt er selbst, sie erreichen den Brain nie. Ein schaltender Satz, der trotzdem dort ankommt, wurde von der abgesicherten Stelle **nicht** als Kommando erkannt oder sie war nicht verfügbar — ein Grund für mehr Vorsicht, nicht für mehr Ehrgeiz. Geschaltet wird deshalb über [dieselbe abgesicherte Stelle](#derselbe-weg-für-den-brain-mcp) und nicht über die rohe Hausautomations-API: später in der Kette zu stehen gibt dem Brain keinen mächtigeren Weg, sondern denselben.

## Sprechererkennung & Enrolment

Jede Aufnahme läuft parallel zur STT durch die Speaches-Diarization. Der dominante Sprecher wird im Wrapper-Prefix an OpenClaw mitgegeben:

```
🎤 [Sprecher: jochen] Wie wird das Wetter?
🎤 [Sprecher: unbekannt] Wie wird das Wetter?
```

### Workspace-Layout

```
~/.openclaw/workspace/voice/
  last_recording.wav             aktuelle Aufnahme (wird pro Trigger überschrieben)
  speakers/
    jochen.wav                   aktive Referenz (geht an Speaches)
  originals/
    jochen-2026-05-09T22-15.wav  Backup mit Zeitstempel, bleibt erhalten
```

### Enrolment-HTTP-Server

Der voice_assistant öffnet einen kleinen Loopback-HTTP-Server auf `127.0.0.1:18791`, über den externe Tools die Sprecher-Referenzen verwalten:

| Methode | Pfad                | Body / Effekt |
|---|---|---|
| `POST` | `/enroll`           | `{"name": "Jochen"}` — kopiert `last_recording.wav` nach `speakers/jochen.wav` + Backup |
| `GET`  | `/speakers`         | `{"speakers": ["jochen", "katrin"]}` |
| `DELETE` | `/speakers/<name>` | entfernt die Referenz |

Namen werden normalisiert (lower-case, alphanumerisch + `-_`).

### OpenClaw-Plugin (Voice-Tools)

Das Plugin in [`openclaw-plugin/`](openclaw-plugin/) registriert die Tools, die das LLM während eines Voice-Turns aufrufen kann:

| Tool | Zweck |
|---|---|
| `voice_speak_text(text)` | kurzen Text laut vorlesen (fire-and-forget) |
| `voice_enroll_speaker(name)` | letzte Aufnahme als Referenz dieses Sprechers speichern |
| `voice_list_speakers()` / `voice_remove_speaker(name)` | bekannte Sprecher verwalten |
| `voice_list_voices()` / `voice_set_voice(…)` / `voice_set_speed(…)` | TTS-Stimme / Tempo wechseln (auch pro Sprecher via `for_speaker`) |
| `voice_analyze_last_output(…)` | die eigene zuletzt gesprochene Antwort erneut analysieren (Texttreue, Timing, Prosodie) |

Installieren / registrieren:

```bash
openclaw plugins install --link ~/openclaw_voice_assist/openclaw-plugin/
openclaw gateway restart
openclaw plugins inspect voice-enrol --runtime --json   # status should be "loaded"
```

> **Wichtig:** Jedes Tool muss zusätzlich in `openclaw.plugin.json` unter `contracts.tools` eingetragen sein — OpenClaw (≥ 2026.6) lehnt Tools stillschweigend ab, die nur in `index.js` registriert sind. Nach einem Neustart prüfen, ob das Gateway-Log keine `must declare contracts.tools for: …`-Zeilen enthält.

Diese Tools rufen die Loopback-HTTP-Server des Assistenten auf (Enrolment `:18791`, Speak `:18792`) — der voice_assistant muss also laufen. Details: [`openclaw-plugin/README.md`](openclaw-plugin/README.md).

### Einschränkungen

- Speaches-Diarization braucht **mindestens 16 kHz mono mit 2–10 s echter Sprache** (Stille zählt nicht). Sehr kurze Follow-up-Antworten (≤ 2 s) werden oft als "unbekannt" klassifiziert.
- Aufnahmen länger als ~10 s würden den GPU-Speicher sprengen (Wespeaker resnet34 Buffer-Allokation), daher kürzt der Diarization-Client Eingabe + Referenzen vor dem Request auf 8 s. Die volle Originalaufnahme bleibt in `originals/` und `last_recording.wav` erhalten.
- Erst-Enrolment nutzt dieselbe Aufnahme, mit der der Nutzer den Befehl gesprochen hat (Variante 1). Qualität skaliert mit Aufnahmelänge und Geräuschpegel.

## Speaches-Integration

STT: `POST {speaches_base}/v1/audio/transcriptions` — Modell `guillaumekln/faster-whisper-medium`

TTS: `POST {speaches_base}/v1/audio/speech` — Modell `speaches-ai/piper-de_DE-thorsten-medium`

Diarization: `POST {speaches_base}/v1/audio/diarization` — Modelle `Wespeaker/wespeaker-voxceleb-resnet34-LM` + `fedirz/segmentation_community_1` (Speaches ≥ v0.9.0-rc.3)

60-Sekunden-Cooldown nach Verbindungsfehlern. Bei Ausfall greift automatisch der lokale Fallback:
- STT: `faster-whisper` (Modell `small`, läuft auf dem Pi)
- TTS: Piper (`~/.local/share/piper/de_DE-thorsten-low.onnx`)
- Diarization hat keinen lokalen Fallback — wird zu "Sprecher: unbekannt"

### Piper TTS (lokaler Fallback)

Wenn Speaches nicht erreichbar ist, fällt TTS auf Piper zurück, das auf dem Pi läuft — mit den Modellen aus `<Projekt>/models/piper/` (siehe Installationsschritt 6). Die vorgerenderte "Ja?"-Wakeword-Bestätigung verwendet ebenfalls Piper.

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
| 12 | NEAR_MISS | Beam-LED orange hell, Rest sehr dunkel — Wakeword fast erkannt (600ms) |

### WLED-Streifen (mode: local)

| LED | Farbe | Zustand |
|---|---|---|
| 0 | Blau | Idle |
| 1 | Rot | Wakeword / Aufnahme |
| 2 | Orange | STT / Bestätigung |
| 4 | Lila | Warte auf OpenClaw |
| 5 | Grün | Liest Antwort vor |

## Mess-Werkzeuge — Parameter messen, nicht raten

Jeder Parameter, der das Verhalten spürbar ändert, hat ein Werkzeug, das seine
Wirkung an aufgezeichneten echten Daten nachweist. **Geändert wird ein
Parameter nur gegen sein Werkzeug**, und die gemessene Zahl steht im Docstring
des Werkzeugs — nicht im Chat, nicht im Kopf. Einige dieser Werkzeuge stehen
oben in ihrem Fachabschnitt (Aktuator, Wakeword, Endpointing); hier sind alle,
geordnet nach dem Zeitpunkt, ab dem man sie nutzen kann.

Zwei Lehren haben diese Disziplin geprägt, beide schmerzhaft gelernt:

- Eine Messung, die nur in einem Scratchpad stand, war einen Tag später weder
  reproduzierbar noch gültig. Zahlen, die nicht beim Werkzeug committet sind,
  sind morgen wertlos.
- Ein Werkzeug druckte sein Fazit als festen Text statt es zu rechnen — es
  behauptete vier Tage lang einen Effekt, den seine eigenen Zahlen widerlegten.
  Ein Werkzeug muss sein Urteil aus den aktuellen Daten *berechnen*, nicht
  behaupten.

Die meisten dieser Werkzeuge brauchen **einige Tage Betrieb**, bevor sie etwas
liefern, weil sie auf dem Trigger-Archiv und `wake_events.log` aufsetzen. Am
Tag eins wirken sie kaputt — sind sie nicht, es fehlt einfach das Material.

Die Rohdaten liegen unter `~/.openclaw/workspace/`: `wake_events.log` (eine
Zeile je Wake-Entscheidung), `endpoint.log` (eine je Aufnahme),
`actuator_turns.log` (eine je Schalt-Turn, den der Aktuator selbst erledigt
hat) und `voice/triggers/` (die archivierten Wake-/Aufnahme-/Near-Miss-WAVs).

**Am Tag eins — braucht nur ein Mikrofon:**

- `wakeword_studio record` — geführte echte Aufnahmen des Wakeworts in
  verschiedenen Stilen (Distanz, Tempo, Lautstärke, Winkel). Scoret zudem
  jeden Take gegen das Modell. Die Grundlage für alles Weitere.
  ```bash
  ow-venv/bin/python -m wakeword_studio record --speaker <name>
  ```
- `wake_rms_replay --nur-studio` — schlägt eine Pegel-Gate-Schwelle allein
  aus diesen Takes vor, kein Alltagsarchiv nötig (siehe Pegel-Gate-Abschnitt).
  ```bash
  ow-venv/bin/python -m tools.wake_rms_replay --nur-studio
  ```

**Nach einigen Tagen Betrieb (sobald das Archiv existiert):**

- `wake_triage` — sortiert archivierte Wake-/Near-Miss-Clips in ECHTER RUF /
  RAUSCHEN / UNKLAR, zuerst aus Selbst-Labels (Handlungen), dann per STT. Listet
  die UNKLAR-Fälle zum Anhören. Braucht Trigger-Archiv + `wake_events.log` + STT.
  ```bash
  ow-venv/bin/python -m tools.wake_triage --seit 3 --auch-trigger
  ```
- `endpoint_replay` — spielt die Endpointing-Logik über die archivierten
  Aufnahmen und zeigt, wo eine andere Nachlauf-/Deckel-Einstellung eine
  Aufnahme beendet hätte — per STT belegt, ob gesprochenes Material verloren
  ging. Braucht Trigger-Archiv + `wake_events.log` + STT.
  ```bash
  ow-venv/bin/python -m tools.endpoint_replay --stt
  ```
- `wake_rms_replay` (voll) — misst das Pegel-Gate gegen das Archiv: echte Rufe
  verloren (der Preis) gegen Fehltrigger geblockt (der Gewinn), mit
  Schwellen-Sweep und Fisher-exaktem Test. Braucht Archiv + gelabelte Clips.
  ```bash
  ow-venv/bin/python -m tools.wake_rms_replay
  ```
- `wake_corpus` — hebt gelabelte Clips aus dem selbstlöschenden Archiv in einen
  Dauer-Korpus, meldet **Erosion** (Labels, deren Audio schon weg ist), und
  scored das laufende Bundle gegen diesen Korpus — die Vorher-Zahl, die ein
  Nachtraining schlagen muss. Braucht gelabelte Clips.
  ```bash
  ow-venv/bin/python -m tools.wake_corpus bilanz
  ow-venv/bin/python -m tools.wake_corpus sichern
  ow-venv/bin/python -m tools.wake_corpus messen
  ```
- `actuator_watch` — liest `actuator_turns.log` und erkennt Diskrepanzen
  (Intent vs. ausgeführt, Statusprobleme). Braucht `actuator_turns.log`.
  ```bash
  ow-venv/bin/python -m tools.actuator_watch --seit 3
  ```
- `gruppenbeleg_replay` — spielt die Gruppen-Ziel-Regel (Regel A) über die
  echten Schalt-Turns. Braucht `actuator_turns.log`.
  ```bash
  ow-venv/bin/python -m tools.gruppenbeleg_replay
  ```
- `actuator_grammar_test` — das Test-Set für den Klassifikator-Prompt; nach
  jeder Capability-Änderung neu messen. Braucht den capabilities-Endpunkt.
  ```bash
  ow-venv/bin/python -m tools.actuator_grammar_test
  ```

**Mit etwas Handarbeit:**

- `review_audio` — exportiert Clips (Wake + Folgeaufnahme verkettet) zum
  Anhören und liest die Sortierung als harte Ohr-Labels zurück
  (`wake_review.jsonl`). Ein Ohr-Urteil sticht jede automatische Einstufung.
  Braucht Trigger-Archiv + `wake_events.log`.
  ```bash
  ow-venv/bin/python -m tools.review_audio export
  ow-venv/bin/python -m tools.review_audio import
  ```
- `verifier_probe` — kreuzt das Wakewort-Modell auf beiden Achsen (Recall und
  Precision) gegen Archiv plus Studio-Takes ab. Braucht Archiv + Studio-Takes.
  ```bash
  ow-venv/bin/python -m tools.verifier_probe
  ```

## Lizenz

MIT — siehe [LICENSE](LICENSE).
