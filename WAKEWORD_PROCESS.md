# Wakeword-Verbesserungsprozess

Wiederholender Prozess um die Treffer-Quote des Wakeword-Modells zu erhöhen:
weniger verlorene Rufe (Recall) UND weniger Fehltrigger (Precision).

## Ziel

Der Schwellen-Weg ist ausgereizt — FPs und echte Rufe überlappen im Peak-Bereich
(FPs bis 0.99, echte Rufe ab 0.80). Peak allein trennt sie nicht. Der nächste
Schritt ist Nachtrainieren mit einem gelabelten Datensatz aus dem Alltag.

## Wie die Logdaten entstehen

1. Jemand spricht im Raum. Das Wakeword-Modell scoret jeden Frame.
2. Feuert das Gate (`min_peak_single 0.75`): das **Wake-Clip** (`*_wake.wav`)
   wird gespeichert — die Audio die den Peak ausgelöst hat. Evtl. „Gaston",
   evtl. etwas ganz anderes.
3. Gleichzeitig startet die **Folgeaufnahme** (`*_rec.wav`) — was NACH dem
   Trigger gesagt wurde.
4. `wake_events.log` schreibt peak, hits, failed_on, audio-Dateiname.

## Trigger klassifizieren

Das Wake-Clip ist NICHT zuverlässig. Die STT verhört „Gaston" regelmäßig als
„Gestalt.", „Gastow.", „Gasthof.", „Kastoff.", „Gestern?", „Das ist toll",
„Das war's". Bekannt — siehe `tools/wake_triage.py`.

Der zuverlässige Indikator ist die Folgeaufnahme (`*_rec.wav`):

| Folgeaufnahme | Klassifikation |
|---|---|
| Klares Kommando („Schalt das Küchenlicht ein") | **ECHTER RUF** — Jochen hat „Gaston" gesagt, dann sein Kommando |
| „Stopp Stopp!" | **FEHLTRIGGER** — niemand hat „Gaston" gesagt, Jochen bricht ab |
| Nichts | wahrscheinlich **FEHLTRIGGER** |
| Wirres | wahrscheinlich **FEHLTRIGGER** |

**Ausnahme:** „Stopp" kann auch in einer Selbstkorrektur stehen
(„Schalt das Arbeitsplattentischlicht an. Arbeitsplatten? Nein. Warte. Stopp.")
— das ist ein **ECHTER RUF**. Die Heuristik muss den Kontext prüfen, nicht
bloß das Wort „Stopp".

## Zwei Zwecke der Sammlung

1. **Echte Rufe** (Positiv-Set): Aussprache-Referenz + Validierungs-Set fürs
   Training. Spec: Recall ≥ 0.9. Trainiert wird aus synthetischen TTS-Samples
   (piper-sample-generator), NICHT aus diesen Aufnahmen.
2. **Fehltrigger** (Negativ-Set / harte Negativbeispiele): GEGENCHECK des
   trainierten Modells. Spec: < 1 FP/Stunde. Mindestens so wichtig wie die
   echten Rufe — das Modell muss gegen sie getestet werden vor Deploy.

## Werkzeuge

- `wake_triage.py` — klassifiziert Wake/Nearmiss-Files per STT in ECHTER RUF /
  RAUSCHEN / UNKLAR, nutzt `danach` (Folgeaufnahme) als Beleg. Dedupliziert
  via `wake_triage.jsonl`.
- `wakeword_studio record` — geführte echte Aufnahmen (eigenes Package).
- Trigger-Archiv: `~/.openclaw/workspace/voice/triggers/`
- Labels: `~/.openclaw/workspace/wake_triage.jsonl`

## Prozess (wiederholend)

1. **Sammeln** — passiv aus dem Alltag. Tage bis Wochen.
2. **Triagieren** — `ow-venv/bin/python -m tools.wake_triage --seit N --auch-trigger`
   läuft über ungelabelte Files, STT klassifiziert, UNKLAR-Fälle listen.
3. **Per Ohr entscheiden** — UNKLAR-Fälle mit `aplay` anhören (wake_triage
   schlägt den Pfad vor). Label in `wake_triage.jsonl` eintragen.
4. **Trainieren** — synthetische TTS-Samples + echtes Validierungs-Set +
   Negativ-Korpus → neues Modell. Siehe `Wakeword_Studio_Spec.md`.
5. **Validieren** — gegen Validierungs-Gate prüfen: Recall ≥ 0.9 gegen echte
   Aufnahmen, < 1 FP/Stunde gegen Negativ-Korpus. Nicht bestanden → zurück
   zu Schritt 4.
6. **Deployen** — neues `.tflite` ins Bundle, Service neustarten.
7. **Weiter sammeln** — der Kreislauf beginnt von vorn.

## Was NICHT zu tun ist

- Schwellen weiter justieren — ausgereizt.
- FPs als „egal" abtun — sie sind der wertvollste Teil des Datensatzes.
- Die Folgeaufnahme als Fehltrigger-Indikator fehlinterpretieren — sie ist der
  BELEG für einen echten Ruf, nicht der Fehltrigger selbst.
- Echte Aufnahmen als Trainingsdaten missverstehen — sie sind das
  Validierungs-Set. Trainiert wird synthetisch.
- Ohne Prozess-Verständnis Klassifizierungen vornehmen.

## Stand

Siehe MemPalace (Wing `clawdpi1-home-pi-openclaw-voice-assist`, Room
`decisions`) für den aktuellen Sammlungsstand und detaillierte Drawer.
