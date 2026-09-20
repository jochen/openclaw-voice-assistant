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

  Quer dazu: Barge-in ("Stopp Gaston") bricht den laufenden Turn ab —
  Wiedergabe, Denk-Phrasen, OpenClaw-Run, Spiegel, Follow-up (optional).
```

### Package-Struktur

```
voice_assistant/
  __main__.py            entry: python -m voice_assistant (venv-Re-Exec)
  assistant.py           run() — Hauptloop + State-Machine
  state.py               STATE_*, tts_lock, reply_done_event, TurnControl
  bargein.py             Abbruch mitten im Turn ("Stopp Gaston")
  wake_gate.py           Score-Gate, geteilt von Wakeword und Barge-in
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
    speaker_state.py     current_speaker.json — Grundlage der Sprecher-Schranke
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
capabilities-Version. Stand: **31/32 bei capabilities `9b429c57`**, Messreihe
im Docstring des Werkzeugs.

**Ein Gruppen-Ziel braucht seinen Beleg im Satz (Regel A, 2026-08-02).** Wählt
das Klassifikations-Modell eine Gruppe, muss das Gruppenwort auch gesagt
worden sein — sonst Rückfrage statt Schalten. Die Belege entstehen bei jedem
`refresh()` aus den `namen` der Gruppe, im Code steht keine id.

Daraus folgt eine Pflicht bei der Datenpflege: **eine Gruppe braucht alle
Formen, in denen Menschen sie aussprechen** (`["Küchenrollos", "die Rollos in
der Küche", "alle Rollos in der Küche"]`), nicht nur ihr Kompositum. Mit
einem einzigen Namen lehnt Regel A völlig legitime Sätze ab — gemessen, siehe
die Messreihe. Zweites Werkzeug dafür: `tools/gruppenbeleg_replay.py` spielt
die Regel über die echten Turns in `actuator_turns.log`.

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

## Sprecher-Zustand und die Schranke (`current_speaker.json`)

Der erkannte Sprecher war bis zum 2026-09-19 **ausschliesslich ein Label im
Prompt** (`🎤 [Sprecher: jochen]`). Kein Gate im Code, keins in den Werkzeugen
des Brains — die gesamte Absicherung war Prosa in der ausgerollten `AGENTS.md`,
inklusive einer Fortsetzungs-Ausnahme, die eine `unbekannt`e Eingabe als
Fortsetzung des zuletzt erkannten Sprechers erlaubte.

Am **2026-09-18** hat das im Fablab genau so versagt: Speaches-Diarization gab
ab 20:01 HTTP 500, jeder Turn wurde dadurch zu „unbekannt", und Mister Handy
hat Desktop-Rechner ausgeschaltet. Nachlesbar im Journal des Pi und in seiner
eigenen Antwort um 20:05:49.

Zwei Dinge sind daraus entstanden:

**1. Vier Status statt zwei** (`services/diarization.py`). `diarize()` liefert
ein `SpeakerVerdict` mit `bekannt` · `unbekannt` · `ausgefallen` ·
`nicht_eingerichtet`. Vorher wurde **jeder** Fehler zu `None` und damit zum
Label „unbekannt" — ein toter Dienst war nicht von einem Fremden zu
unterscheiden, die Identitaet fiel still nach aussen offen aus. `name` ist nur
bei `bekannt` gesetzt, damit alles Identitaetsgebundene (Sprecher-Stimme,
`last_speaker`) unveraendert weiterlaufen kann. Auch ein Join-Timeout in der
State-Machine zaehlt als `ausgefallen`, sonst waere das Gate per Timeout
aushebelbar.

**2. Eine Datei statt eines Prompt-Hinweises** (`services/speaker_state.py`).
Nach **jedem** Turn — auch einem gescheiterten — wird
`~/.openclaw/workspace/voice/current_speaker.json` atomar geschrieben. Wer
etwas Folgenreiches tut, liest sie selbst. Vollstaendiger Vertrag samt
Mindest-Implementierung und Abnahme-Pruefung: **`SPEAKER_STATE.md`**.

Die ausfuehrende Seite liegt **nicht in diesem Repo**: im Fablab ist es
`~/.openclaw/inventory/claw-power`, das seit 2026-09-19 `shutdown`/`reboot`
ablehnt, wenn der letzte frische Sprach-Turn (Fenster 120 s) nicht `bekannt`
war. Absichtlich ohne Abschalt-Option — was man abschalten kann, wird
wegargumentiert. Ist der letzte Turn aelter oder fehlt die Datei, greift die
Schranke nicht; eine Anweisung per Chat laeuft also normal durch.

**Ehrliche Grenze:** das ist kein Schutz gegen ein Modell mit Shell-Zugang als
derselbe Benutzer — es kann die Datei schreiben. Es ist ein Schutz gegen das,
was tatsaechlich passiert ist: eine Rationalisierung. Die echte Grenze waere,
Power-Aktionen hinter einen Dienst zu legen, den der Agent nicht als derselbe
Benutzer erreicht. Offen.

**Aenderungen hier nur gegen `tests/test_speaker_verdict.py`** (14 Tests, ohne
Netz). Der Test haelt genau die Bruchlinie fest, an der es schiefging: jeder
Fehlerweg muss `ausgefallen` ergeben, jeder gemessene Nicht-Treffer
`unbekannt`. Verschmelzen die beiden wieder, ist das Loch lautlos zurueck.

## Abbruch mitten im Turn (`barge_in`, optional pro Profil)

Bis zum 2026-09-20 gab es genau ein Zeitfenster für einen Abbruch: das
Stopp-Wort **in der laufenden Aufnahme**, geprüft am Transkript in
STATE_PROCESSING (`_is_stop_command`). War die Aufnahme vorbei, lief der Turn
zu Ende — Bestätigung, Denken, Antwort, und was der Brain dabei tut.

Diese Lücke wurde durch die Ein-Satz-Optimierung größer: wer durchspricht,
bekommt kein „Ja?" mehr (`_ACK_DELAY_SEC`), merkt einen Fehltrigger also oft
erst, wenn der Assistent schon antwortet.

Das Fenster ist jetzt **STATE_WAITING** — dort las die Hauptschleife die
Chunks und warf sie weg. Der Abbruch benutzt dieselbe Wakeword-Erkennung wie
der Leerlauf, mit denselben Gates: `wake_gate.gate_passed` (aus `assistant.py`
herausgezogen, damit es die Regel genau einmal gibt) und
`wake_rms.loudest_window_rms`. Ein Abbruch soll weder leichter noch schwerer
auslösbar sein als der Ruf selbst.

**Das Stopp-Wort wird nicht am Wakeword-Ereignis erkannt, sondern danach am
Transkript.** Der Trigger feuert auf „Gaston", das „Stopp" davor steckt im
Pre-Roll (`_PRE_ROLL_SEC`, dieselbe Begründung wie beim durchgesprochenen
Kommando). Folge: „Stopp Gaston" und „Gaston, Stopp" wirken gleich, und ein
Barge-in **ohne** Stopp-Wort ist einfach ein neuer Auftrag. Deshalb braucht der
Fall auch keinen neuen State: WAITING → RECORDING → PROCESSING, und dort
entscheidet das vorhandene Stopp-Muster (mit `bargein_round` gilt das
mildere Ein-Wort-Muster wie bei Follow-up und Klärungs-Rückfrage).

**Was „abbrechen" umfasst** (`state.TurnControl`, `workers._finish_cancelled`):
Wiedergabe sofort aus (`AudioSink.stop()`), ThinkingWorker aus, SSE-Verbindung
zu OpenClaw zu — letzteres bricht den Agent-Run **serverseitig** ab
(dokumentiert in `docs/gateway/openresponses-http-api.md` des Gateways: „Disconnecting
the HTTP client cancels … the agent run"), kein Telegram-Spiegel, kein
Follow-up, und mit `notify_brain` eine Systemnachricht in dieselbe Session,
damit der Brain den Abbruch im Verlauf sieht.

**Die Turn-Nummer ist der Kern, nicht ein Flag.** `TurnControl.begin()` vergibt
sie in STATE_PROCESSING, bevor irgendetwas gesprochen wird; jeder Worker
fragt `turn_stopped(nr)`. Ein verwaister Worker des abgebrochenen Turns läuft
noch Millisekunden weiter, während die Hauptschleife schon den nächsten Turn
aufnimmt — ohne Nummer würde er in den neuen hineinsprechen. `turn=None` heißt
ausdrücklich „gehört zu keinem abbrechbaren Turn" und ist nie gestoppt: sonst
verstummen Ansagen von außen (`voice_speak_text`), Aktuator-Antworten und
Quittungen nach einem Abbruch, auf den kein neuer Turn folgt — lautlos.

### Gaston erkennt seine eigene Stimme — aber die Echo-Unterdrückung nimmt sie weg

Zwei Läufe von `tools/bargein_echo_test.py` am 2026-09-20 (Messreihe im
Docstring des Werkzeugs):

| Lauf | Sätze | Selbst-Trigger | höchster Score |
|---|---|---|---|
| digital (reines TTS-Signal) | 40 | **5 (12,5 %)** | **0,97** |
| akustisch (Lautsprecher → XVF3800-AEC → Mikro) | 24 | **0** | **0,07** |

**Digital ist der Befund eindeutig und strukturell:** das Modell ist auf
synthetischen *thorsten*-Stimmen trainiert, und mit genau so einer spricht der
Assistent — die eigene Stimme liegt IN der Trainingsverteilung. Die
Selbst-Trigger verteilten sich über alle drei Gate-Pfade (1 Frame/0,76 und
0,81; 2 Frames/0,93; 3 Frames/0,97) und über vier verschiedene Sätze, darunter
„Das dauert noch einen Augenblick" — eine Denk-Phrase **ohne** Wakewort darin.
Es ist also nicht bloß die Bestätigung, die das Wakewort wiederholt; das Modell
springt auf diese Stimme an sich an.

**Akustisch fällt derselbe Satz von 0,97 auf 0,07.** Dass Ton ankam, steht in
denselben Zeilen: Raum-Grundpegel 36, Echo am Mikro 96–479, Wiedergabedauer
passend zur Dateilänge. Die Echo-Unterdrückung nimmt dem Signal nicht nur
Pegel, sondern die Wakeword-Eigenschaft — Abstand zur niedrigsten Schwelle
(`min_peak_single` 0,75) rund zehnfach.

**Die Vorab-Erwartung war falsch, und das steht dort so.** Vor dem Lauf war
notiert: „eher nicht — AEC dämpft den Pegel, aber das Gate hängt am Score."
Gemessen ist das Gegenteil. Deshalb steht in dieser Installation
`while_speaking: true`, der **Code-Default bleibt aber False**: die Zahl gilt
nur für respeaker-Modus MIT `use_speaker` (nur dann hat der XVF3800 die
Referenz auf dem I2S-Ausgang) und für `volume 0.8`. Bei `use_speaker: false`
geht der Ton über ALSA, es gibt keine Echo-Unterdrückung — dort ist die
digitale Zahl die zutreffende.

Neu messen nach: Wechsel der TTS-Stimme, der Lautstärke, des Wakeword-Modells
oder der Audio-Hardware.

**Die Folge fürs Training steht in `WAKEWORD_PROCESS.md`** („Das Modell erkennt
Gastons eigene Stimme"): die eigene TTS-Ausgabe gehört als adversariales
Negativ in die nächste Runde. Sie ist ohne Aufnahmesession in beliebiger Menge
herstellbar und trifft eine Fehltrigger-Klasse, die im Archiv fehlt, weil das
Mikro sie bisher nie zu hören bekam — mit `while_speaking: true` bekommt es sie
ab jetzt. Ein Modell, das die eigene Stimme kennt, wäre auch ohne
Echo-Unterdrückung robust; die aktuelle Rettung hängt allein an der Hardware.

Für `while_speaking: false` bleibt ein Detail im Code, das man nicht wegkürzen
darf: beim Übergang „eigene Ansage zu Ende → wieder zuhören" werden
`audio_source.flush()` **und** `bargein.reset()` gerufen. openwakeword
entscheidet aus einem Fenster von rund 1,4 s, die ersten Predictions danach
liefen also sonst über die eigene Stimme — messbar genau die, die digital zu
12,5 % triggert.

### Zwei Fallen, die der erste Messlauf aufgedeckt hat

Beide kosteten je einen kompletten Lauf. Festgehalten sind sie **dort, wo man
hineinläuft** — nicht nur im Messwerkzeug: Falle 1 an
`RespeakerClient._BUSY_STATES` und in der ReSpeaker-Sektion beider READMEs
(wer die Hardware nachbaut, stolpert sonst genauso), Falle 2 im Docstring von
`SpeachesTts.synth()`, also an der Quelle dieser Bytes, plus eine Warnung an
`RespeakerSink._wav_seconds()`, der einzigen Stelle im Repo, die mit
`getnframes()` rechnet. Die verallgemeinerbaren Lehren (geratenes
Gültigkeitskriterium, nicht-deterministisches TTS) stehen bei den
Mess-Werkzeugen in beiden READMEs, neben den zwei älteren.

1. **Eine Ansage läuft als `PLAYING`, nicht als `ANNOUNCING`.** Die erste
   Fassung der Senke wartete auf `ANNOUNCING`, das nie kommt — jeder Satz lief
   in die 5-Sekunden-Startschranke und wurde dann „blind" abgewartet
   (5 s Zusatzlatenz pro Satz!). Nebenwirkung für die Messung: der Hörrahmen
   war 5–9 s länger als die Ansage, und was in dieser Zeit im Raum passierte,
   landete als Score im Ergebnis. Die Werte 0,42–0,70 des ersten Laufs kamen
   daher — nicht aus dem Echo. Siehe `RespeakerClient._BUSY_STATES`.
2. **Speaches-WAVs lügen im Header:** `nframes` steht auf dem
   Streaming-Platzhalter 2147483647, bei 22050 Hz also 97391 Sekunden. Jede
   Längenrechnung muss aus den gelesenen Bytes kommen. Die Senke ist davon
   nicht betroffen (sie rechnet auf der selbst geschriebenen 48-kHz-Datei), das
   Messwerkzeug war es.

Dazu eine Lehre über Messwerkzeuge in diesem Repo: das erste
Gültigkeitskriterium des Werkzeugs war ein **geratener** Mikrofon-Pegel (200).
Gemessen liegt das Echo nach AEC bei 96–479 und der Raum bei 36 — die geratene
Schwelle lag mitten im Messbereich und erklärte gültige Läufe für ungültig.
Jetzt entscheidet, ob die Wiedergabe stattgefunden hat (Dauer gegen
Dateilänge), und der Raum-Grundpegel wird gemessen statt angenommen.

### Nebenwirkung: ReSpeaker-Wiedergabe läuft jetzt über den Media-Player

`RespeakerSink.play_wav` benutzte die ESPHome-**Announce-API**. Die beendet die
`voice_assistant`-Session des ESP (`handle_stop` → EOS), weshalb am Ende der
Methode `press_start_button()` stand. Folge: der Pi war während **jeder**
Ansage taub — kein Wakeword, kein Barge-in, nichts. Jetzt geht die Wiedergabe
direkt über die `media_player`-Entity (`media_player_command(media_url=…,
announcement=True)`); die VA-Session bleibt unberührt, der Mic-Strom läuft
durch die Ansage hindurch, und das Echo nimmt der XVF3800 per AEC weg (er hat
die Referenz auf dem I2S-Ausgang). Zweiter Gewinn: eine Announce-Wiedergabe war
nicht abbrechbar, ein Media-Player-STOP ist es.

Kein Firmware-Wechsel nötig — die Entity samt `announcement_pipeline` steht
schon in `esphome/respeaker.yaml`. Das **Ende** der Wiedergabe wird seither am
Zustand des Players erkannt (ANNOUNCING → nicht mehr ANNOUNCING) statt an der
Announce-Antwort; bleibt das Zustands-Event aus, fällt die Senke auf die Länge
der WAV-Datei zurück, damit ein Turn nicht hängt. Die Player-Befehle gehen über
`loop.call_soon_threadsafe` und nicht wie die LED-Befehle direkt in den
asyncio-Client: ein verschluckter Wiedergabe-Befehl ließe einen Turn hängen,
eine verschluckte LED-Farbe nicht.

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
4. **WAITING** — wartet auf `state.reply_done_event` (openclaw_worker setzt es).
   Hier läuft zugleich das Barge-in-Fenster: mit `barge_in.enabled` gehen die
   Chunks durch eine ZWEITE Wakeword-Instanz, statt verworfen zu werden
   (siehe „Abbruch mitten im Turn")
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

### Pegel-Gate (`wake_rms_min`)

Neben dem Score-Gate gibt es ein Pegel-Gate: der RMS des lautesten 300-ms-
Fensters im `wake_ring` muss eine Schwelle erreichen, sonst feuert der
Trigger nicht. Blockt leise Fehltrigger, die am Score-Gate vorbeikommen. Per
Profil-Parameter `wake_rms_min`, Default `0.0` = **aus** — ohne den Eintrag
verhält sich ein Profil wie bisher. Bewusst ein eigener Parameter, nicht
`vad_voice_rms_min` (der ist fürs VAD/Endpointing in Gebrauch). Unterschreitet
der Pegel die Schwelle, wird der Streak als Near-Miss mit `failed_on: "min_rms"`
und dem gemessenen `rms` archiviert, nicht getriggert — sonst verschwände
genau das, was man beobachten müsste. Die Schwelle gehört ins Profil, nicht
ins Bundle (`manifest.yaml`), denn sie hängt an Mikrofon und Gain.

**Änderungen an dieser Schwelle nur gegen `tools/wake_rms_replay.py`.** Das
Replay spielt die Pegelregel über das Archiv und zeigt, was sie geändert
hätte — analog zu `endpoint_replay.py` und `actuator_grammar_test.py`. Die
Rechnung ist in Replay und Live identisch (beide importieren
`loudest_window_rms` aus `voice_assistant/wake_rms.py`). Messreihe und
Begründung für den aktuellen Wert (seit 2026-08-22: **400**, davor 300):
Docstring des Werkzeugs. Bekannte Schwäche: absolute RMS-Werte sind
gain-abhängig (ReSpeaker ×4) — ändert sich Hardware/Gain, verschiebt sich die
Skala. Zweite, am 2026-08-22 sichtbar gewordene Schwäche: **eine absolute
Schwelle muss zugleich für den leisen Morgen und den lauten Abend passen** —
der teuerste Fehltrigger lag bei RMS 1691, der verlorene echte Ruf bei 336.

**Gelabelte Clips gehören gesichert, bevor das Archiv sie löscht.**
`TRIGGER_AUDIO_DIR` räumt beim Service-Start alles älter als
`TRIGGER_AUDIO_MAX_AGE_DAYS` ab; die Labels dazu leben unbegrenzt weiter. Am
2026-08-22 hat ein Neustart 56 Dateien gelöscht, darunter das Audio zu 6 per
Ohr gefällten Urteilen — die Messbasis des Replays fiel still von 26 auf 20
belegte Fehltrigger. Seither verschont `_cleanup_trigger_audio` ungesicherte
Ohr-Urteile, und `tools/wake_corpus.py` hebt gelabelte Clips in einen
Dauer-Korpus (`sichern`), meldet Erosion (`bilanz`) und misst das laufende
Bundle gegen den Korpus (`messen` — die Vorher-Zahl fürs Nachtraining).

## Threading Model

- STT läuft in eigenem Thread, Ergebnis über `state.stt_queue`.
- Bestätigungs-TTS ("Ich habe verstanden: …") läuft in eigenem Thread (`ReplySpeaker`).
- `_openclaw_turn` in eigenem Thread: OpenClaw anfragen → Telegram spiegeln
  → Antwort satzweise vorlesen → `state.reply_done_event` setzen.
- `ThinkingWorker` feuert Lebenszeichen-Phrasen mit wachsendem Abstand
  (erster nach gesprochener Länge, dann ~25 s ×1.5 pro Wiederholung, max
  120 s), wenn OpenClaw zu langsam antwortet.
- `state.tts_lock` verhindert überlappende Audio-Wiedergabe. Er ist zugleich die
  Auskunft „es spricht gerade jemand von uns" — daran hängt das Barge-in-Fenster
  bei `while_speaking: false`.
- `state.turn_control` (`TurnControl`) trägt die Nummer des laufenden Turns.
  Jeder Worker fragt `turn_stopped(nr)` vor folgenreichen Schritten; ein
  Abbruch schließt registrierte Closer (SSE-Verbindung, Wiedergabe-Prozess).
  Siehe „Abbruch mitten im Turn".

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
