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
- `review_audio.py` — Clips zum Anhören exportieren und die Sortierung als
  **harte Ohr-Labels** zurücklesen (`wake_review.jsonl`). Ein Ohr-Urteil
  sticht jede Regel und jede STT — es ist die stärkste Label-Quelle.
- `wake_rms_replay.py` — Pegel-Gate gegen das Archiv messen (siehe unten).
- Trigger-Archiv: `~/.openclaw/workspace/voice/triggers/`
- Labels (stärkste Quelle zuerst): `~/.openclaw/workspace/wake_review.jsonl`
  (Ohr) > Selbst-Labels (`wake_triage.py`) > `wake_triage.jsonl` (STT).

## Pegel-Gate (`wake_rms_min`)

Neben dem Score-Gate gibt es ein **Pegel-Gate**: der RMS des lautesten
300-ms-Fensters im `wake_ring` muss eine Schwelle erreichen, sonst feuert der
Trigger nicht — selbst wenn der Score das hergibt. Es blockt leise
Fehltrigger (Fernseher, Tastatur, ferne Gespräche), die am Score-Gate
vorbeikommen, weil das Modell auf das jeweilige Geräusch hoch scoret.

- Per Profil-Parameter `wake_rms_min` (Default `0.0` = **aus**). Ohne den
  Eintrag verhält sich ein Profil exakt wie bisher. Bewusst ein eigener
  Parameter, nicht `vad_voice_rms_min` wiederverwendet — der ist schon fürs
  VAD/Endpointing in Gebrauch.
- Die Schwelle gehört ins **Profil**, nicht ins Bundle (`manifest.yaml`): sie
  hängt an Mikrofon und Gain, nicht am Wakewort.
- Unterschreitet der Pegel die Schwelle, wird der Streak **nicht** getriggert,
  sondern als Near-Miss archiviert und geloggt mit `failed_on: "min_rms"` und
  dem gemessenen `rms`-Wert. Sonst verschwände genau das, was man beobachten
  müsste — und ein zu hoch gesetzter Wert wäre unsichtbar.
- **Änderungen an dieser Schwelle nur gegen `tools/wake_rms_replay.py`.** Das
  Replay spielt die Pegelregel über das Archiv und zeigt, was sie geändert
  hätte — analog zu `endpoint_replay.py` und `actuator_grammar_test.py`. Die
  Rechnung (lautestes 300-ms-Fenster) ist in Replay und Live identisch, beide
  importieren `loudest_window_rms` aus `voice_assistant/wake_rms.py`.
- Bekannte Schwäche: **absolute RMS-Werte sind gain-abhängig** (ReSpeaker
  verstärkt ×4). Ändert sich Hardware oder Gain, verschiebt sich die ganze
  Skala und die Schwelle stimmt nicht mehr. Woran man das merkt: steigt der
  Anteil geblockter echter Rufe im Near-Miss-Log (`failed_on: min_rms`), ist
  die Schwelle zu hoch für die aktuelle Verstärkung. Messreihe und
  Begründung für 300: Docstring von `tools/wake_rms_replay.py`.

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
(30 000 synthetische Einzelwort-Samples). Bestätigt sich das, gehören
Ein-Satz-Aufnahmen ins Nachtraining, nicht nur isolierte Takes.

**Es geht dabei ausschließlich um die Aussprache, nicht um eine Störung durch
das Folgewort.** openwakeword ist kausal — was nach „Gaston" gesagt wird, kann
den Score am Wakewort nicht mehr drücken; gemessen sind die Scores bis zum
Gipfel bitidentisch, ob Sprache oder Stille folgt. Wer eine Erklärung dafür
sucht, dass Ein-Satz-Rufe schlechter ankommen, muss sie in der Aussprache
suchen, nicht im Signalweg.

**Die Frage ist offen — und diese Tabelle kann sie nicht schließen.**
Gemessen am 2026-08-02 über alle 46 protokollierten ack-Entscheidungen:
durchgesprochen 13/35 kurze Streaks (37 %), mit Pause 7/11 (64 %),
Peak-Median beide 0.94, Fisher exakt p = 0.17. Kein Unterschied
nachgewiesen — und die Richtung zeigt, wenn überhaupt, gegen die Hypothese.

Zwei Gründe, warum das trotzdem kein Freispruch ist:

1. **Survivorship-Bias, prinzipiell.** Die Tabelle zählt nur TRIGGER, denn
   nur dort steht der Sprechfluss fest (ein Near-Miss erzeugt kein `ack`).
   Verliert Durchsprechen Rufe, fehlen genau diese Rufe im Nenner. Die
   Messung sieht die Überlebenden und schätzt Durchsprechen darum zu gut.
2. n = 11 in der Pause-Gruppe trägt keine Aussage in beide Richtungen.

**Die frühere Zahl „25 % gegen 3 % (n=12 bzw. 35)" ist ungültig** und war es
schon, als sie notiert wurde. Am 2026-07-28 existierten erst 5 `ack`-Zeilen —
aus denen konnte n=12/35 nicht stammen. Sie kam aus der Tail-RMS-Heuristik,
die im Absatz darüber mit 6 von 9 Treffern als untauglich verworfen wird;
ihre Gruppengrößen sind gegenüber den echten Labels gerade vertauscht (sie
las lauten Ausklang als Pause statt als Weitersprechen). Eine Zahl aus einem
im selben Dokument verworfenen Verfahren hat vier Tage lang als
Entscheidungsgrundlage gedient — deshalb steht sie hier als Warnung statt
gelöscht zu werden.

## Pegel als zweite Dimension (2026-08-02)

„An den Schwellen ist nichts mehr zu holen" galt immer für den **Score**. Der
**Pegel** ist davon unabhängig — und er trennt.

Anlass war das Anhören der Fehltrigger: sie waren durchweg leise, die echten
Rufe darunter hörbar lauter. Gemessen (RMS des lautesten 300-ms-Fensters):

| Schwelle | echte Rufe verloren | Fehltrigger geblockt | Fisher |
|---|---|---|---|
| 300 | **0 / 77** | 10 / 24 | p = 1,0e-07 |
| 400 | **0 / 77** | 16 / 24 | p = 4,6e-13 |
| 450 | 5 / 77 | 18 / 24 | — |

Die 77 sind 57 belegte Rufe aus dem Archiv plus die 20 geführten Studio-Takes.
Letztere sind der eigentliche Beleg, weil dort absichtlich schwierige Fälle
drin sind: „leise" (427), „abgewandt" (675), „fern" (1129) — keiner fällt unter
400. Der leiseste echte Ruf überhaupt liegt bei 402.

**Gewählt ist 300, nicht 400.** Bei 400 stünde die Schwelle zwei Zähler über
dem leisesten je beobachteten Ruf; das ist an die Stichprobe angepasst und der
nächste leise Ruf fällt durch. Der Sweep im Werkzeug zeigt den Kipppunkt: bei
450 kostet es die ersten fünf Rufe.

Wie das Gate arbeitet und was bei Änderungen zu beachten ist, steht oben unter
„Pegel-Gate (`wake_rms_min`)" — hier nur die Messung dahinter.

Dieser Weg ist **unabhängig vom Verifier** (siehe `tools/verifier_probe.py`)
und deutlich einfacher: ein Parameter statt eines sprecherspezifischen Modells,
kein Training, keine Sprecherbindung. Beide lassen sich kombinieren — Pegel
davor, Verifier dahinter. Ob der Verifier daneben noch etwas beiträgt, ist
offen und erst zu messen, wenn das Pegel-Gate scharf ist.

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

**Wo der Prozess steht (2026-08-02):**

Schritt 1 (Sammeln) hat geliefert, Schritt 2 (Triagieren) ist automatisiert.
Schritt 3 der alten Reihenfolge — „Bilanz lesen" — ist **abgearbeitet und
hat die Frage nicht beantwortet**, siehe „Ein-Satz gegen Pause" oben: p =
0.17, und die Messung ist per Konstruktion auf die durchgekommenen Rufe
verzerrt. Passives Weitersammeln behebt das nicht, es vergrößert nur n auf
einer Größe, die die Frage ohnehin nicht trennscharf beantworten kann.

Bestand 2026-08-02 (Vergleich 2026-07-28 in Klammern): 384 (~250) archivierte
Clips, 137 (54) Trigger, 110 (61) Near-Misses, 46 (5) `ack`-Entscheidungen.
Nach den Selbst-Labels: **36 (22) verlorene echte Rufe, 66 (33) Rauschen,
8 (2) offen zum Anhoeren.** Positiv- und Negativ-Korpus haben sich damit
beide ungefaehr verdoppelt — fuer Schritt 4/5 ist genug Material da.

**Was als naechstes ansteht, in dieser Reihenfolge:**

1. **Kontrollierter Vergleich statt Alltagsstatistik.** Die Frage
   „triggert Ein-Satz schlechter?" braucht beide Formen vom selben Sprecher
   in derselben Session, gegen dasselbe Modell gescort — dann faellt der
   Survivorship-Bias weg, weil auch die Nicht-Trigger gezaehlt werden.
   `wakeword_studio record` nimmt heute NUR isolierte Takes auf
   (`VARIATIONS` in `recorder.py`, alle 10 Eintraege isoliert). Es braucht
   Ein-Satz-Takes („Gaston, schalte das Tischlicht ein") als eigene
   Variationen und im Scoring die Trennung beider Gruppen. Das ist die
   kleinste Aenderung, die die Frage wirklich entscheidet — und dieselbe
   Aenderung liefert bei Bedarf gleich die Trainingsdaten.
2. **Die 8 UNKLAR-Faelle anhoeren** (`wake_triage` schlaegt den `aplay`-Pfad
   vor). Kleiner Stapel, macht den Datensatz vollstaendig.
3. **Danach erst Schritt 4 (Trainieren).** Das Modell wurde auf 30 000
   synthetischen Einzelwort-Samples trainiert (`piper-sample-generator`,
   siehe `models/wakewords/gaston/manifest.yaml`) und kennt „Gaston" nur
   isoliert, mit Endsilbenloesung und fallender Intonation. Ob der Satzfluss
   wirklich das Problem ist, sagt Schritt 1 — vorher nicht danach trainieren.

**Was NICHT als naechstes ansteht:** weiter passiv sammeln und die
Ein-Satz-Bilanz nochmal lesen. Das war die Empfehlung vom 2026-07-28, sie ist
mit dieser Messung erledigt.

**Was NICHT mehr zu versuchen ist:** an den Schwellen drehen. Die Begruendung
mit Messwerten steht in `models/wakewords/gaston/manifest.yaml` an jedem
einzelnen Parameter. Zwei belegte echte Rufe kamen mit Peak 0.37/0.38 an, dort
liegt Rauschen gleichauf — die sind durch keine Schwelle zu retten.

Im MemPalace (Wing `clawdpi1-home-pi-openclaw-voice-assist`, Room `decisions`,
Drawer `...11c2e98f58cb...`) liegt der ausfuehrliche Prozess-Drawer mit
Session-Kontext und Fehlerdokumentation. Diese Datei ist die Repo-Seite
desselben — beide sind gegenseitig verlinkt.
