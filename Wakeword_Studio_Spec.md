# Wakeword-Studio — Spezifikation

Stand: 2026-07-06 · Status: M1+M2 live, Phase A umgesetzt

## Umsetzungsstand (2026-07-06)

- **M1 Runtime multi-wakeword + Routing**: fertig (`579e3cc`), gaston +
  hey_jarvis laufen parallel auf clawdpi1.
- **M2 erstes echtes Modell**: `gaston` trainiert und live (`87d3010`,
  Threshold 0.35). Pipeline-Skripte + Stolpersteine im ai-stack-Git
  (`~/ai-stack/wakeword-studio/`).
- **Phase A (Erfassen)**: umgesetzt als `wakeword_studio/`-Package
  (`232c4dc`, `17b9569`, `ab70fdf`) — `record` (geführte Aufnahmen,
  Grundpegel-Kalibrierung, Sofort-Scoring, LED-Anzeige), `review`
  (anhören/aussortieren), `score` (Test-Set-Regression, Multi-Offset).
  Erste 20 echte jochen-Takes als Test-Set; `samples/` ist ein eigenes
  privates Repo (gitlab.brokenpipe.de/jochen/wakeword-samples-gaston).
- **Trigger-Semantik erweitert**: `min_hits` pro Wakeword (Config >
  manifest.yaml > Default 3, `badfd81`). Kurzes "Gaston" (~0.5 s ≈ 6
  Frames) steht auf 2 — FP-geprüft 0.09 FP/h auf 10.7 h Validierung;
  Test-Set 11/20 statt 8/20 Trigger @0.35.
- **Erkenntnis fürs Studio**: rein TTS-trainierte Modelle liefern auf
  echter Sprache spitze Scores (oft nur 1–2 Frames hoch); Threshold-
  Senken hilft nicht. Schwächen von gaston Runde 2: langsam/überdeutlich,
  leise, fern.
- **Nächster Schritt ("Hebel b")**: Trainings-Runde 3 mit den echten
  Aufnahmen als zusätzliche Positives + Tempo-/Distanz-Augmentation.
  Danach M3 Verifier (Phase E) und Studio-CLI-Ausbau (Phasen B–D, F).

## Ziel

Eigene Wakewords erzeugen, verwalten und im Voice-Assistant einsetzen — als
wiederverwendbare Rundum-Lösung: für "Gaston" zuhause, für neue Wakewords im
Fablab und für Dritte, die das Repo von GitHub verwenden. Mehrere Wakewords
können parallel aktiv sein und unterschiedliche OpenClaw-Agents ansprechen
(Routing über `x-openclaw-session-key`).

**Bewusst außen vor:** microWakeWord / Erkennung auf dem ESP. openwakeword
läuft in beiden Modi (`local` und `respeaker`) auf dem Pi — eine
Trainings-Pipeline genügt, und das Routing braucht die Logik ohnehin auf dem Pi.

## Kernidee: Wie das Wakeword entsteht

Moderne openwakeword-Modelle werden **nicht** aus eingesprochenen Aufnahmen
trainiert, sondern aus synthetischen TTS-Daten (tausende Varianten, viele
Stimmen, Augmentierung mit Rauschen/Hall) plus großen Negativ-Datensätzen.
Die eingesprochenen Aufnahmen des Nutzers übernehmen drei andere Rollen:

1. **Aussprache-Referenz** — legt fest, wie das Wort wirklich klingt
   (z.B. "Gaston" als [ɡastɔ̃] mit deutschem Akzent).
2. **Validierungs-Set** — Goldstandard zum Messen der Trigger-Rate vor Deploy.
3. **Verifier-Training (optional)** — openwakeword kann als zweite Stufe einen
   kleinen sprecherspezifischen Verifier aus wenigen echten Aufnahmen
   trainieren (reduziert Fremd-Trigger durch TV/Besucher).

Das Aussprache-Problem wird nicht über Lautschrift gelöst, sondern über
**Spelling-Kandidaten**: mehrere Schreibweisen ("Gaston", "Gastong", "Gastoh",
"Gastón") werden per TTS synthetisiert und dem Nutzer vorgespielt; er wählt,
was der echten Aussprache am nächsten kommt. Ergänzend ranked das Studio die
Kandidaten automatisch über die Embedding-Distanz zwischen synthetischen
Samples und den echten Aufnahmen.

## Teil 1: Wakeword-Bundle (Artefakt-Format)

Ein Wakeword = ein Verzeichnis unter `models/wakewords/<name>/`:

```
models/wakewords/gaston/
  manifest.yaml       # Metadaten, siehe unten
  gaston.tflite       # trainiertes openwakeword-Modell (~einige 100 KB)
  verifier.pkl        # optional: sprecherspezifischer Verifier
  samples/            # echte Referenz-Aufnahmen (16 kHz mono WAV) = Test-Set
```

`manifest.yaml`:

```yaml
name: gaston
display: "Gaston"
model: gaston.tflite
threshold: 0.5            # empfohlener Trigger-Score (aus Validierung)
verifier:                  # optional
  model: verifier.pkl
  threshold: 0.3
spellings: ["Gastong"]     # gewählte TTS-Schreibweisen (Doku/Reproduzierbarkeit)
created: 2026-07-05
training:                  # Reproduzierbarkeits-Infos
  generator_voice: piper de_DE-thorsten (Checkpoint)
  positive_samples: 30000
  validation:
    recall: 0.97           # gegen samples/
    false_per_hour: 0.4    # gegen Negativ-Korpus
```

Das Bundle ist **portabel** (enthält kein Deployment-spezifisches Routing) und
klein genug, um `.tflite` + `manifest.yaml` zu committen. `samples/` bleibt
lokal (gitignore) — Stimmen der Familie gehören nicht auf GitHub.

## Teil 2: Runtime-Erweiterung (`voice_assistant/`)

Kleiner, rückwärtskompatibler Umbau:

### Engine

- `OpenWakewordEngine` lädt N Bundles gleichzeitig:
  `Model(wakeword_models=[pfad1, pfad2, ...])` liefert bei `predict()` einen
  Score je Modell.
- `feed()` gibt statt `float` künftig `(name, score) | None` des besten
  Kandidaten zurück; der Trigger-Vergleich nutzt den **Threshold aus dem
  Manifest** (statt global hart 0.65 in `assistant.py`; 0.65 bleibt der
  Default für Einträge ohne Manifest/Config-Wert).
- Verifier-Modelle werden, wenn im Manifest vorhanden, über
  `custom_verifier_models` / `custom_verifier_threshold` an openwakeword
  durchgereicht.
- Aufräumen: tote `OPENWAKEWORD_MODEL_PATH`-Env-Var und
  `/tmp/ow_models_min`-Referenzen entfernen (openwakeword ignoriert die
  Env-Var; Modelle kamen zuletzt aus den Package-Ressourcen).

### Profil-Config (`config.yaml`)

Neuer optionaler Block je Profil — das Routing lebt hier, nicht im Bundle:

```yaml
profiles:
  clawdpi:
    # ... wie bisher ...
    wakewords:
      - bundle: gaston                      # models/wakewords/gaston/
        session: "agent:main:telegram:group:-1003XXXXXXXXX"
        ack: "Ja?"                          # optional, default: wakeword_ack
        tts_voice: "..."                    # optional, default: speaches_tts_voice
      - bundle: hey_jarvis                  # eingebaute openwakeword-Modelle
        session: "agent:zweiter:..."        # per Namen weiter erlaubt
```

- Fehlt der Block: Verhalten wie heute (`hey_jarvis`, Profil-Session) —
  Rückwärtskompatibilität wie beim alten flachen YAML-Schema.
- Fehlt `session`/`ack`/`tts_voice` am Eintrag: Fallback auf die
  Profil-Defaults (`openclaw_session`, `locale.wakeword_ack`,
  `speaches_tts_voice`).
- `assistant.py` merkt sich beim Trigger das aktive Wakeword und reicht dessen
  Session-Key an `workers.start_openclaw_turn` → `services/openclaw.py`
  durch (dort wird `session` bereits pro Request gesetzt, Header
  `x-openclaw-session-key`). Follow-ups innerhalb eines Dialogs bleiben beim
  zuletzt getriggerten Wakeword.
- Telegram-Spiegelung nutzt weiterhin die Profil-Einstellungen; ein optionales
  `telegram_chat_id` je Wakeword ist als spätere Erweiterung offen.

## Teil 3: Das Studio (`wakeword_studio/` — neues, eigenständiges Package)

Geführter **CLI**-Prozess (`ow-venv/bin/python -m wakeword_studio`), kein
Umbau von `voice_assistant/`. Sprachgeführte Bedienung ("Gaston, lern ein
neues Wort") ist bewusst spätere Ausbaustufe.

### Phasen

**A — Erfassen**
Name eintippen; Wakeword mehrfach einsprechen (geführt: verschiedene
Distanzen, normale/schnelle Sprechweise, gern mehrere Personen; Ziel ≥ 10–20
Aufnahmen). Nutzt denselben Mic-Pfad wie der Assistant (Profil-Config).
Ablage in `samples/`.

**B — Spelling-Runde (interaktive Rückfragen)**
Kandidaten-Schreibweisen generieren (Heuristik + optional LLM auf dem
ai-stack), je Kandidat kurze TTS-Hörprobe synthetisieren und über den
Assistant-Lautsprecher vorspielen: "Klingt das wie dein Wakeword? [j/n]".
Parallel automatisches Ranking per Embedding-Distanz (openwakeword-Embedding
der synthetischen Samples vs. der echten Aufnahmen). Ergebnis: 1–3 gewählte
Spellings.

**C — Training (remote auf dem GPU-Host)**
- Host: ai-stack `user@<speaches-host>` (Fablab-Server, 2× RTX 5060 Ti) —
  **derselbe Host für Heim- und Fablab-Szenario**.
- Eigener Podman-Container `wakeword-trainer` im ai-stack-Compose
  (piper-sample-generator + openwakeword-Training + gecachte Negativ-Daten /
  vorgerechnete Features; Cache einmalig ~10–20 GB Download).
- Positive Samples mit **deutschem Piper-Checkpoint** (z.B. Thorsten)
  generieren, damit die gewählten Spellings deutsch phonemisiert werden;
  Augmentierung (Rauschen, RIR) wie im openwakeword-Standard-Training.
- Ansteuerung vom Pi per SSH: Studio kopiert Job-Config + Samples hoch,
  startet Training (~unter 1 h), holt `.tflite` zurück.
- **Fallback ohne eigenen GPU-Host** (GitHub-Nutzer): dokumentierter Weg über
  das offizielle openwakeword-Colab-Notebook; das Studio kann ein fertiges
  `.tflite` importieren und ab Phase D weitermachen.

**D — Validieren (Gate vor Deploy)**
- **Recall**: alle echten Aufnahmen aus `samples/` durchs Modell, Trigger-Rate
  messen; Threshold-Sweep → empfohlenen Threshold ins Manifest.
- **False-Positives**: mehrere Stunden Negativ-Audio (Podcast/Radio/
  Fablab-Raumgeräusche) durchs Modell, Fehltrigger/Stunde messen.
- Zielwerte (Startvorschlag): Recall ≥ 0.9, < 1 Fehltrigger/Stunde. Werden
  sie verfehlt: zurück zu B (andere Spellings) oder C (mehr Daten).

**E — Verifier (optional)**
`openwakeword.train_custom_verifier` aus den echten Aufnahmen (positiv) und
Negativ-Clips; Ergebnis + Threshold ins Bundle.

**F — Deploy**
Bundle nach `models/wakewords/<name>/`, Eintrag im Profil (`wakewords:`-Block,
interaktiv abgefragt: welche Session/Agent?), Hinweis zum Neustart des
Assistants.

## Meilensteine

1. **Runtime multi-wakeword + Routing** — Engine/Config/assistant.py-Umbau,
   testbar sofort mit den eingebauten Modellen (`hey_jarvis` → Session A,
   `alexa` → Session B), ohne dass schon trainiert werden muss.
2. **Trainer-Container + Studio-Happy-Path** — Phasen A–C + F, erstes echtes
   "Gaston"-Modell entsteht. Enthält den Spike zum deutschen
   Piper-Checkpoint (größtes fachliches Risiko, s.u.).
3. **Validierung + Verifier** — Phasen D–E, Zahlen-Gate, Threshold-Sweep.
4. **Doku für Dritte** — README-Abschnitt (en/de), Colab-Fallback-Anleitung,
   `config.example.yaml`-Erweiterung.

## Risiken / offene Punkte

- **Deutsche Aussprache im Sample-Generator** (Hauptrisiko): Der
  Standard-Generator von openwakeword nutzt ein englisches
  LibriTTS-Multispeaker-Modell. Plan: deutschen Piper-Checkpoint verwenden;
  liefert der zu wenig Stimm-Varianz, Mischung aus deutschem + englischem
  Generator mit angepassten Spellings. Wird in Meilenstein 2 als Erstes
  gespikt, bevor der Rest des Studios gebaut wird.
- **Embedding-Modell ist englisch trainiert**: Community-Erfahrung zeigt, dass
  nicht-englische Wakewords trotzdem funktionieren; die Validierungs-Phase D
  ist genau dafür das Sicherheitsnetz.
- **Follow-up-Semantik bei mehreren Wakewords**: Kurz-Follow-ups ohne neues
  Wakeword (bestehende Logik) bleiben bei der zuletzt aktiven Session —
  bewusst simpel; alles Weitere erst bei realem Bedarf.
- **GPU-Host-Verfügbarkeit**: Fablab-Server ist nicht überwacht und kann
  wegfallen; der Colab-Fallback ist deshalb nicht nur für GitHub-Nutzer da.
