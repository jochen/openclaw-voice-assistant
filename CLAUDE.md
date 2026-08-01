# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Dieses Repo ist öffentlich — die READMEs sind die Vorlage für Fremde

`origin` ist ein öffentliches GitHub-Repo. `README.md` (englisch) und
`README.de.md` (deutsch) sind **inhaltlich parallel** und richten sich an
Leute mit anderer Hardware, anderen Geräten und anderer Sprache. Wer eine der
beiden ändert, ändert die andere mit — sonst driften sie auseinander und eine
von beiden wird still falsch.

**Verhaltensregeln aus der ausgerollten `~/.openclaw/workspace/AGENTS.md` sind
in der Voice-Sektion der READMEs gespiegelt** (dort neutral formuliert, ohne
Haus-Spezifika). Diese Spiegelung wird regelmäßig vergessen. Am 2026-08-01
kostete das konkret: die AGENTS.md wurde repariert, die READMEs trugen die
kaputte Regel als öffentliche Vorlage weiter — ausgerechnet die Fassung, die
Fremde übernehmen. Wer am Prompt-Verhalten etwas ändert, prüft beide Seiten.

Was hier **nie** hineingehört: Tokens, Ziel-ids dieser Installation
(`kuechenrollo_links` &c. — die sind je Haus andere), Hostnamen/IPs des
eigenen Netzes, Familien-Stimmproben (`models/wakewords/*/samples/`, eigenes
privates Repo).

## Running the Assistant

```bash
ow-venv/bin/python -m voice_assistant
```

The entry point (`voice_assistant/__main__.py`) self-reinvokes with the project venv
Python (`<repo>/ow-venv/bin/python`, aus dem Dateipfad abgeleitet) on startup if not
already running inside it. Der frühere separate venv `~/ow-venv` wurde aufgelöst — alle Pakete
liegen jetzt im Projekt-venv (`pip install` immer via `ow-venv/bin/python -m pip`).

Override the profile: `GASTON_PROFILE=openclaw python -m voice_assistant`

Im Regelbetrieb läuft er als User-Unit, nicht von Hand:

```bash
systemctl --user restart openclaw-voice-assist.service
journalctl --user -fu openclaw-voice-assist.service
```

Der Mic-Stream ist exklusiv — wer den Assistenten von Hand startet oder
Aufnahmen macht (`wakeword_studio record`), stoppt vorher die Unit.

Das kleine Klassifikations-LLM des Aktuators und die Speaches-Container
liegen in einem eigenen Repo (`openclaw-voice-stack`, compose je Host,
`restart: unless-stopped`) — nicht hier, und nicht von Hand gestartet.

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
  → Voice-Aktuator: Schaltbefehl? → lokal ausführen (~0,5 s), Rest der Kette entfällt
  → Confirmation TTS in parallel thread ("Ich habe verstanden: ...")
  → POST /v1/responses to OpenClaw (vollständiger Agentic Loop, SSE-Streaming;
    non-streaming Fallback)
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
  mcp_actuator.py        stdio-MCP-Server: haus_ziele/haus_schalten für den Brain
  audio/
    base.py              AudioSource/AudioSink Protocols
    alsa.py              PyAudio + aplay
    respeaker.py         ESPHome Native API (Stub — Schritt 2)
  wakeword/
    base.py              WakewordEngine Protocol
    openwakeword_engine.py
    respeaker.py         micro_wakeword vom ESP (Stub — Schritt 2)
  services/
    actuator.py          Voice-Aktuator: Schaltbefehle lokal statt via Brain
    leds.py              WledLeds + RespeakerRing + LedDirector
    telegram.py
    speaches.py          SpeachesState + Start-Check
    stt.py               SpeachesStt + LocalWhisperStt + SttPipeline
    tts.py               SpeachesTts + Piper + ReplySpeaker + ThinkingWorker
    openclaw.py          /v1/responses Client
```

### Wakeword-Studio-CLI (`wakeword_studio/`)

Eigenes Package neben `voice_assistant/`, gleicher venv-Re-Exec:

```bash
python -m wakeword_studio record --speaker <name>   # Phase A: geführte echte Aufnahmen
python -m wakeword_studio score [--bundle gaston]   # Test-Set gegen Modell scoren
```

`record` stoppt die User-Unit `openclaw-voice-assist.service` (Mic-Stream ist
exklusiv), nimmt geführt Takes über den Profil-Mic-Pfad auf, scored jeden Take
sofort mit Live-Trigger-Semantik (Streak ≥ 3 über Threshold, 1-Frame-Gap) und
startet den Service danach wieder. Ablage in
`models/wakewords/<bundle>/samples/<sprecher>/` — das ist ein eigenes privates
Git-Repo (Familienstimmen, nie auf GitHub; siehe `samples/README.md` dort).

## Voice-Aktuator (optional, pro Profil)

Schaltbefehle („Mach das Küchenlicht an") werden direkt nach der STT von einem
kleinen lokalen LLM zu einem JSON-Intent geformt und über zwei HTTP-Endpunkte
ausgeführt — der Brain wird dabei übersprungen (~0,5 s statt mehrerer Sekunden).
Ist der Satz kein Schaltbefehl, läuft alles unverändert weiter zum Brain.

Aktiviert wird er per Profil-Block `actuator:` (Default `enabled: false` — ohne
den Block verhält sich ein Profil wie vor dem Einbau). In dieser Installation
liegt die ausführende Seite auf Node-RED (noderedpi4), **das ist aber keine
Voraussetzung**.

Der System-Prompt des Klassifikations-LLM steht als `actuator.system_prompt`
in der Profil-Config (Default: `config.py:_DEFAULT_ACTUATOR_PROMPT`, deutsch).
Er ist die einzige sprachabhängige Stelle des Projekts — für Englisch wird er
dort ersetzt, der Code selbst enthält keine einzige feste Ziel-id. Ziel-Liste,
Kontrast-Beispiele und die Regel für Geräte-Mehrzahl ohne Raumangabe werden bei
jedem `refresh()` aus `/capabilities` erzeugt und über die Platzhalter
`{ziel_liste}`, `{kontrast}`, `{gruppen_regel}` eingesetzt.

**Prompt-Änderungen nur gegen `tools/actuator_grammar_test.py`.** Das Test-Set
liegt im Repo, weil eine frühere Messung ("20/20") nur in einem Scratchpad
stand und einen Tag später weder reproduzierbar noch gültig war. Wiederholen
nach jeder Änderung an den capabilities — die Zahl gilt immer nur für eine
capabilities-Version.

**Wer den Aktuator in einer anderen Umgebung betreibt oder die Gegenstelle neu
implementiert, liest `ACTUATOR_INTERFACE.md`** — dort steht der vollständige
Vertrag beider Endpunkte samt Begründung jeder Design-Entscheidung, eine
Mindest-Implementierung und eine Abnahme-Prüfung. Entstehungsgeschichte und
Messungen: `ACTUATOR_V1_PLAN.md`.

Turns, die der Aktuator selbst erledigt, sieht der Brain nicht — sie landen
deshalb in `~/.openclaw/workspace/actuator_turns.log` (Rohmaterial für den
Überwacher). Bewusst nicht in Telegram und nicht in der Haus-Session.

### Überwacher Stufe 1 (nur LESEN + MELDEN)

`tools/actuator_watch.py` — CLI-Tool, liest `actuator_turns.log` und erkennt
Diskrepanzen (EXEC_DIFFERS, STATUS_PROBLEM). Dedupliziert via
`actuator_watch.jsonl`. Siehe `tools/wake_triage.py` für den Stil.

`voice_assistant/services/watcher.py` — Event-gesteuerter Daemon-Thread im
Voice-Assistant. `check_turn()` wird sofort nach jedem Aktuator-Turn
aufgerufen (nicht periodisch). Semantische Prüfung via LLM
(OpenAI-kompatibles API, System-Prompt ~200 Token): passt das Transkript
zum Intent? LLM_MISMATCH und EXEC_DIFFERS gehen in eine separate Telegram-
Gruppe (nicht den Voice-Spiegel). Stille Zeit 01:00–07:00: nichts wird
gesendet, nur gesammelt. STATUS_PROBLEM bleibt im JSONL-Archiv, nicht in
Telegram (zu laut). Bei LLM-Fehler: Meldung an Telegram
(„Überwachung konnte nicht erfolgen weil …"). Timeout 30s + 1 Retry.

Aktiviert per Profil-Block `watcher:` (Default `enabled: false`).
LLM-Felder: `llm_url`, `llm_model`, `llm_api_key`, `llm_timeout`.

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

Optionaler Profil-Block `wakewords:` (Multi-Wakeword + Routing, siehe
`Wakeword_Studio_Spec.md`): je Eintrag `bundle` (Name unter
`models/wakewords/<bundle>/` oder eingebautes openwakeword-Modell wie
`hey_jarvis`), plus optional `session`, `ack`, `tts_voice`, `threshold` —
fehlende Felder fallen auf die Profil-Defaults zurück. Fehlt der Block ganz,
verhält sich das Profil wie bisher (ein `hey_jarvis`-Eintrag). Beim Trigger
merkt sich `assistant.py`, welches Wakeword gefeuert hat, und nutzt dessen
`session`/`tts_voice` für den OpenClaw-Turn (Sprecher-Stimmenauflösung hat
weiterhin Vorrang vor der Wakeword-Stimme).

## Key External Dependencies

| Service | URL | Purpose |
|---|---|---|
| Speaches (GPU container) | `http://<speaches-host>:8000` | STT + TTS (OpenAI-compatible) |
| OpenClaw | `http://127.0.0.1:18789/v1/responses` | AI brain, SSE-streaming, session-based |
| WLED controller | `wled_controller.py` (repo-local) | LED-Status |
| Piper TTS | `~/.local/share/piper/*.onnx` | Lokaler TTS-Fallback |
| Telegram Bot API | `https://api.telegram.org/...` | Mirror queries and replies |
| ReSpeaker (ESPHome) | `<host>:6053` | Native API (Audio-Stream, Wakeword-Events, LED-Ring) — optional |

STT/TTS beide nutzen 60-Sekunden-Cooldown nach Fehler vor erneutem
Speaches-Versuch (`services/speaches.py:SpeachesState`).

## State Machine

Fünf Zustände in der Hauptschleife (`voice_assistant/assistant.py`):

1. **LISTENING** — WakewordEngine bekommt jeden 16-kHz-Chunk; triggert bei Score über
   dem Wakeword-Threshold (Config > manifest.yaml > Default 0.65)
2. **RECORDING** — Chunks werden gesammelt; endet bei Stille nach Sprache oder
   am Deckel. Zwei Parametersätze, siehe „Endpointing" unten
3. **PROCESSING** — wartet auf STT-Ergebnis aus `state.stt_queue`
4. **WAITING** — wartet auf `state.reply_done_event` (openclaw_worker setzt es)
5. **PAUSE** — 1 s Totzone bevor es zurück in LISTENING geht

### Endpointing: Dialog vs. Kommando

Wann eine Aufnahme endet, hängt davon ab, ob der Nutzer das „Ja?" abgewartet
hat. Wer durchspricht (Ein-Satz), meint fast immer einen kurzen Schaltbefehl
für den Aktuator — da zählt Tempo und Denkpausen kommen nicht vor. Wer wartet,
stellt meist etwas Komplexeres, das an den Brain geht.

| | Nachlauf (Stille bis Ende) | Deckel |
|---|---|---|
| Dialog (Ja? abgewartet, Follow-ups) | `silence_seconds` (2,0 s) | `RECORDING_MAX_SEC` (30 s) |
| Kommando (Ein-Satz) | `command_silence_seconds` (1,0 s) | `command_max_seconds` (8 s) |

Der Kommando-Modus wird **nicht** schon von der Ein-Satz-Einstufung scharf,
sondern erst nach `_COMMAND_MIN_SPEECH_SEC` (0,5 s) tatsächlicher Sprache. Der
Ein-Satz-Entscheid fällt 0,4 s nach dem Trigger und spricht auch auf den
Ausklang des Wakewords an — ohne diese Sperre stirbt eine Aufnahme in der
Denkpause direkt nach „Gaston" und das Kommando ist komplett weg (belegt an
`20260730_181211`; 30 von 37 protokollierten Entscheidungen lauten
„Ein-Satz", der Erkenner springt also leicht an).

**Warum überhaupt zwei Sätze:** Bei laufendem Fernseher endete die Aufnahme
nie — die Sprechpausen einer Störquelle sind ~1,7 s lang und setzen den
Stille-Zähler vor der 2-s-Schwelle zurück. Ein Turn lief so 21,9 s bis zum
Deckel (2026-08-01). Es braucht dafür keine Sprache im Hintergrund, nur
irgendein Geräusch alle ~1,5 s: `_chunk_speech_stats` verodert die vier
20-ms-VAD-Frames eines Chunks, ein einziger Frame genügt.

**Parameter nur gegen `tools/endpoint_replay.py` ändern.** Das Werkzeug spielt
die Endpointing-Logik über die archivierten `*_rec.wav` (siehe
`TRIGGER_AUDIO_DIR`) und weist per STT nach, ob ein Schnitt ein Kommando
zerschneidet. Gemessen am 2026-08-01 über die 30 Ein-Satz-Turns im Archiv:
Median 7,0 s → 5,8 s, kein einziges der 22 ausgeführten Kommandos beschädigt.
Zwei Fallen, die dabei beide zugeschlagen haben: den Pre-Roll muss das Replay
überspringen (der VAD sieht ihn im Betrieb nie, sonst zählt das Wakeword als
Sprache), und ein reiner Wortvergleich taugt nicht als Verlustkriterium —
STT-Varianten wie „Gastro-Monitor an" / „Gastro Monitoren" sehen aus wie ein
abgeschnittenes Kommando. Verlust wird deshalb aus der VAD-Spur bestimmt.

`endpoint.log` bekommt pro Turn `mode`, `rms_p10/median/max` und
`vad_frame_ratio` — der Rohstoff, um später zu entscheiden, ob eine
Pegelschwelle (`vad_voice_rms_min`) oder ein Frame-Anteil-Gate den Fernseher
vom Sprecher trennen kann. Geschrieben wird die Zeile auf **jedem** Ausgang,
auch bei Stopp-Wort und „keine Sprache" (`_flush_endpoint`); vorher fehlten
ausgerechnet die kaputten Aufnahmen im Log.

## Threading Model

- STT läuft in eigenem Thread, Ergebnis über `state.stt_queue`.
- Bestätigungs-TTS ("Ich habe verstanden: …") läuft in eigenem Thread (`ReplySpeaker`).
- `_openclaw_turn` in eigenem Thread: OpenClaw anfragen → Telegram spiegeln
  → Antwort satzweise vorlesen → `state.reply_done_event` setzen.
- `ThinkingWorker` feuert Lebenszeichen-Phrasen mit wachsendem Abstand
  (erster nach gesprochener Länge, dann ~25 s ×1.5 pro Wiederholung, max
  120 s), wenn OpenClaw zu langsam antwortet.
- `state.tts_lock` verhindert überlappende Audio-Wiedergabe.

## OpenClaw Request Format

Voice-Anfragen werden mit einer Prompt-Direktive umhüllt (Default:
`config.py:_DEFAULT_VOICE_INSTRUCTION`, pro Profil überschreibbar):

```
🎤 [Sprecher: jochen] {user_text}

[Hinweis zur Verarbeitung dieser Spracheingabe ...]
```

Die Direktive ist im Mandats-Stil formuliert (Ziel/Blickweise statt
Einzelregeln): gesprochene Antwort in Fließtext, Zahlen/Daten ausgeschrieben,
und "Du hütest den Sprachkanal" — bei Aufgaben, die (auch erst mitten in der
Arbeit erkennbar) länger dauern, sofort kurze Rückmeldung geben, im
Hintergrund weiterarbeiten und das Ergebnis per `voice_speak_text` ansagen.

Anfragen laufen per SSE-Streaming; der Read-Timeout
(`OPENCLAW_STREAM_TIMEOUT`, 600 s) überlebt lange Tool-Phasen ohne Deltas.
Bei Stream-Timeout wird der Auftrag NICHT erneut gepostet (Doppel-Ausführung),
sondern per `query_status()` nur das Ergebnis des laufenden Turns abgefragt.

Der `x-openclaw-session-key`-Header trägt die Session-Kennung (z.B.
`agent:main:telegram:group:-1003XXXXXXXXX`) und teilt die Session mit dem
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

- Workspace: `~/.openclaw/workspace`
- Piper "Ja?" pre-rendered WAV: `~/.openclaw/workspace/ja.wav`
- Piper models: `~/openclaw_voice_assist/models/piper/de_DE-thorsten_emotional-medium.onnx`,
  `de_DE-thorsten-low.onnx` (im Projekt, gitignored wegen Größe; je `.onnx` + Pflicht-Sidecar `.onnx.json`)
- Wakeword-Bundles (eigene Wakewords, Manifest + `.tflite`):
  `~/openclaw_voice_assist/models/wakewords/<name>/` (committed außer
  `samples/`; siehe `models/wakewords/README.md`). Eingebaute openwakeword-
  Modelle (`hey_jarvis`, `alexa`, …) kommen weiterhin aus den Package-
  Ressourcen, kein Env-Var-Override nötig.
- Venv (Python 3.11, openwakeword/tflite/piper/num2words): `~/openclaw_voice_assist/ow-venv`
- ESPHome venv (getrennt, nur fürs Flashen): `~/openclaw_voice_assist/esphome-venv`
