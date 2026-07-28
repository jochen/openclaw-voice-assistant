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

- `wake_triage.py` — sortiert in ECHTER RUF / RAUSCHEN / UNKLAR, aus **zwei
  Quellen in dieser Rangfolge**:
  1. **Selbst-Labels** aus Handlungen, die nur bei einem echten Ruf bzw. nur
     bei einem Fehltrigger vorkommen (siehe Abschnitt unten). Kein Mensch,
     keine STT, kein Schwellwert.
  2. **STT-Einstufung** für alles, was Regel 1 nicht erreicht — schwächer, das
     Wort „Gaston" wird regelmäßig verhört.
  Zeigt zusätzlich den Sprechfluss ([Ein-Satz]/[Pause], aus der
  protokollierten ack-Entscheidung) und Wiederkehrer. Dedupliziert via
  `wake_triage.jsonl`.
- `wakeword_studio record` — geführte echte Aufnahmen (eigenes Package).
- Trigger-Archiv: `~/.openclaw/workspace/voice/triggers/`
- Labels: `~/.openclaw/workspace/wake_triage.jsonl`

## Was sich von selbst labelt

Der Nutzer labelt beim Benutzen mit, ohne es zu merken. Drei Regeln, alle
gemessen am Bestand vom 2026-07-25..28:

| Beobachtung | Label | warum es trägt |
|---|---|---|
| Near-Miss, dem binnen 15 s ein Trigger folgt | echter Ruf, **verloren** | der Nutzer hat sich wiederholt, weil der erste Ruf nicht ankam |
| Trigger, aus dem ein ausgeführtes Schaltkommando wurde | echter Ruf | ein Fehltrigger erzeugt praktisch nie ein gültiges Intent |
| Trigger, den der Nutzer mit einem Stopp-Wort abbrach | Fehltrigger | der Abbruch ist sein ausdrückliches Urteil |

Die erste Regel ist die wertvollste: sie labelt genau das, was das Gate
**verpasst** hat, statt zu bestätigen, was es ohnehin durchlässt. Von 19 so
gefundenen Fällen hatte die STT-Einstufung 10 als UNKLAR liegen gelassen und
2 als RAUSCHEN falsch einsortiert.

Bewusst **kein** Label: „keine Sprache" oder leeres Transkript nach einem
Trigger. Das sieht nach Fehltrigger aus, deckt aber auch den Fall ab, dass der
Ruf echt war und der Nutzer dann unterbrochen wurde.

**Die Grenze, die bleibt:** ein verlorener Ruf, den der Nutzer *nicht*
wiederholt hat, taucht nirgends auf. Der Prozess misst nicht den wahren
Recall, sondern nur den beobachtbaren Teil — und schätzt ihn systematisch zu
gut. Für die Recall-Zahl der Spec zählt weiterhin nur das Validierungs-Set aus
`wakeword_studio record`.

## Ein-Satz gegen Pause

Seit dem Pre-Roll (2026-07-28) hält der Assistent je Trigger fest, ob
durchgesprochen wurde. Aus dem Audio ist das **nicht** rekonstruierbar — der
Wake-Clip endet, bevor das nächste Wort beginnt; die naheliegende
Tail-RMS-Heuristik traf gegen die echte Entscheidung nur 6 von 9 Fällen.

Das ist die Datenbasis für eine offene Frage: „Gaston" im Satzfluss wird
schneller und unbetont gesprochen, das Modell kennt nur die isolierte Form
(30 000 synthetische Einzelwort-Samples). Erste Zahlen: Ein-Satz-Trigger
landen bei 25 % auf einem 1-Frame-Streak, Rufe mit Pause bei 3 % — und kurze
Streaks müssen am Gate einen höheren Peak erreichen. Bestätigt sich das über
mehr Daten, gehören Ein-Satz-Aufnahmen ins Nachtraining, nicht nur isolierte
Takes.

## Prozess (wiederholend)

1. **Sammeln** — passiv aus dem Alltag. Tage bis Wochen.
2. **Triagieren** — `ow-venv/bin/python -m tools.wake_triage --seit N --auch-trigger`
   läuft über ungelabelte Files. Die Selbst-Labels stehen sofort, die STT
   klassifiziert nur den Rest, UNKLAR-Fälle bleiben übrig.
3. **Per Ohr entscheiden** — UNKLAR-Fälle mit `aplay` anhören (wake_triage
   schlägt den Pfad vor). Label in `wake_triage.jsonl` eintragen. Der Stapel
   ist klein: auf dem Bestand blieben nach den Selbst-Labels 2 von 56 übrig.
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

> Zahlen altern. Was hier steht, ist mit `tools/wake_triage.py` in Minuten neu
> zu erheben — die Datei sagt, **wo der Prozess steht und was als naechstes
> ansteht**, nicht was gerade in den Logs liegt.

**Wo der Prozess steht (2026-07-28):**

Schritt 1 (Sammeln) laeuft, Schritt 2 (Triagieren) ist automatisiert, soweit
es geht. Was noch fehlt, ist eine Entscheidungsgrundlage fuer Schritt 4
(Trainieren) — genauer: fuer die Frage, ob Ein-Satz-Aufnahmen ins Training
gehoeren.

Bestand zu diesem Zeitpunkt: ~250 archivierte Clips, 54 Trigger, 61
Near-Misses. Nach den Selbst-Labels: 22 verlorene echte Rufe, 33 Rauschen,
2 offen zum Anhoeren. Von den 22 echten kamen 17 aus den Selbst-Labels, nicht
aus der STT.

**Was als naechstes ansteht, in dieser Reihenfolge:**

1. **Warten und sammeln.** Der Sprechfluss (`ack`-Zeilen) wird erst seit dem
   2026-07-28 abends protokolliert — die Ein-Satz-Bilanz in wake_triage hat
   noch einstellige n und sagt nichts. Ein paar Tage Alltag reichen.
2. **Bilanz lesen.** `ow-venv/bin/python -m tools.wake_triage --auch-trigger`,
   Abschnitt „EIN-SATZ GEGEN PAUSE". Die Frage lautet: landen
   durchgesprochene Rufe deutlich haeufiger auf kurzen Streaks als Rufe mit
   Pause? Erste, noch nicht belastbare Zahl: 25 % gegen 3 % (n=12 bzw. 35).
3. **Wenn ja: Ein-Satz-Aufnahmen ins Training.** Das Modell wurde auf 30 000
   synthetischen Einzelwort-Samples trainiert (`piper-sample-generator`,
   siehe `models/wakewords/gaston/manifest.yaml`) und kennt „Gaston" nur
   isoliert, mit Endsilbenloesung und fallender Intonation. Im Satzfluss
   klingt es anders. `wakeword_studio record` nimmt heute NUR isolierte Takes
   auf — es muesste Ein-Satz-Takes fuehren („Gaston schalte das Tischlicht
   ein") und daraus den „Gaston"-Anteil schneiden.
4. **Wenn nein:** Recall-Problem liegt woanders, dann zaehlt der normale Weg
   (mehr Positivdaten, Negativ-Korpus aus den Wiederkehrern).

**Was NICHT mehr zu versuchen ist:** an den Schwellen drehen. Die Begruendung
mit Messwerten steht in `models/wakewords/gaston/manifest.yaml` an jedem
einzelnen Parameter. Zwei belegte echte Rufe kamen mit Peak 0.37/0.38 an, dort
liegt Rauschen gleichauf — die sind durch keine Schwelle zu retten.

Im MemPalace (Wing `clawdpi1-home-pi-openclaw-voice-assist`, Room `decisions`,
Drawer `...11c2e98f58cb...`) liegt der ausfuehrliche Prozess-Drawer mit
Session-Kontext und Fehlerdokumentation. Diese Datei ist die Repo-Seite
desselben — beide sind gegenseitig verlinkt.
