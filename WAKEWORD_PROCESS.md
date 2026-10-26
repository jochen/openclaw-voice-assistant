# Wakeword-Verbesserungsprozess

Wiederholender Prozess um die Treffer-Quote des Wakeword-Modells zu erhöhen:
weniger verlorene Rufe (Recall) UND weniger Fehltrigger (Precision).

> **Zum Weitermachen: erst den Abschnitt „Stand" ganz unten lesen.** Dort steht,
> welche Fäden offen sind, welcher gerade der aktive ist und was ausdrücklich
> NICHT als nächstes ansteht. Alles davor ist das Wie und Warum — bleibt gültig,
> ändert sich selten. Der Rest dieser Datei erklärt es; der „Stand" sagt, wo wir
> stehen.

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
  **Vor dem Anhören das Mikro stumm schalten**, sonst lösen die
  abgespielten Clips selbst Trigger aus und landen als neue Fehltrigger im
  Archiv (Jochen, 2026-10-19): `curl -X POST 127.0.0.1:18792/mikro -d
  '{"stumm": true, "minuten": 30, "ansage": false}'`, danach
  `{"stumm": false}` (oder „Gaston, stell dich taub“; siehe `mikro:` in
  CLAUDE.md).
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
  Begründung für den aktuellen Wert (seit 2026-08-22: **400**, vorher 300):
  Docstring von `tools/wake_rms_replay.py`.
- **Die gelabelten Clips gehören gesichert, bevor das Archiv sie löscht.**
  `TRIGGER_AUDIO_DIR` räumt beim Service-Start alles älter als 30 Tage ab, die
  Labels dazu leben unbegrenzt weiter — am 2026-08-22 kostete das 6 Ohr-Urteile
  (siehe „Was am 2026-08-22 verloren ging"). `tools/wake_corpus.py sichern`
  hebt gelabelte Clips heraus, `bilanz` meldet Erosion.

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

### Nachtrag 2026-10-01: zum ersten Mal verpasste Rufe im Nenner — VORLÄUFIG

Der Rückspul-Puffer (`rewind:`, Marker-Taster) liefert, was der Tabelle oben
fehlte: Rufe, die **nicht** ausgelöst haben, samt Audio. Jochens Eindruck dazu:
je flüssiger „Gaston" in den Satz übergeht, desto eher wird er verpasst.

Gemessen (Skripte im Session-Scratchpad, noch nicht im Repo; Pause = längste
stille Strecke im Pegel zwischen „Gaston" und dem nächsten Wort, Wortgrenzen
aus Whisper-Wortzeitstempeln), alles seit v3 (2026-09-16):

| | n | Pause median | Pause ≥ 100 ms |
|---|---|---|---|
| Treffer (`*_rec.wav`) | 54 | 95 ms | 27 |
| verpasst (Marker-Clips, Live-Score < 0,35) | 12 | **0 ms** | 1 |

Unter 50 ms Pause: 24 Treffer, 11 verpasst. Ab 50 ms: 30 Treffer, 1 verpasst.

Gegenprobe zur Kausalität (bestätigt den Absatz oben, nichts Neues): bei 27
Treffern die Pause herausgeschnitten → 25/27 lösen weiter aus, Score-Median
0,97 → 0,97; bei 14 verpassten 300 ms Stille nach „Gaston" eingefügt → 2/14
vorher, 2/14 nachher. **Was folgt, ist egal — falls es am Fluss liegt, dann an
der Aussprache des Worts selbst.** Die Wortdauer trennt nicht deutlich
(Median 760 ms Treffer, 680 ms verpasst), der Pegel auch nicht (verpasst eher
lauter).

**Warum das noch kein Befund ist:** Die 12 verpassten Rufe stammen aus drei
Episoden, 8 davon aus denselben 90 Sekunden (29.09. 14:16) — ein Sprecher, ein
Platz, eine Situation. Die Treffer kommen aus zwei Wochen und von allen. Die
Pause ist aus Whisper-Grenzen und Pegel abgeleitet, nicht gehört. Nach Jochens
Vorgabe: gemeinsam anhören, dann bewerten.

**Was es sauber klären würde:** dieselbe Person, derselbe Platz, je 10 Takes
„Gaston, schalte …" flüssig und mit Pause (`wakeword_studio record`, Scores
werden sofort mitgeschrieben). Und unabhängig vom Ausgang: die verpassten
Marker-Rufe sind genau die Positiv-Beispiele, die dem Modell fehlen — echte,
im Satz gesprochene „Gaston" — und gehören in die nächste Trainingsrunde
(dort aber erst auf die letzten ~1,8 s schneiden, siehe Runde 3).


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

**Gewählt war 300, nicht 400.** Bei 400 stünde die Schwelle zwei Zähler über
dem leisesten je beobachteten Ruf; das ist an die Stichprobe angepasst und der
nächste leise Ruf fällt durch. Der Sweep im Werkzeug zeigt den Kipppunkt: bei
450 kostet es die ersten fünf Rufe.

### Nachtrag 2026-08-22: erhöht auf 400

Die Familie meldete auffällig viele Fehltrigger. Der Befund aus
`wake_events.log`: am 21.08. drei Trigger, alle falsch, dazu zwei weitere in
der Nacht — und seit dem 19.08. abends **kein einziger echter Ruf** mehr
darunter. An Code oder Config lag es nicht, der Prozess lief da seit 19 Tagen
unverändert; die Umgebung war lauter geworden (Median des lautesten Fensters
je Tag: 341 → 549 → 592 → 909).

Damit war die oben notierte Beobachtungswette entschieden — und zwar in die
Richtung „Schwelle zu niedrig für diesen Raum": **16 geblockte Streaks in 20
Tagen Betrieb, kein einziger belegter echter Ruf darunter**, während die
Fehltrigger weiterliefen.

| Schwelle | echte Rufe verloren | Fehltrigger geblockt |
|---|---|---|
| 300 | 0 / 88 | 7 / 20 |
| 350 | 1 / 88 | 9 / 20 |
| **400** | **1 / 88** | **14 / 20** |
| 450 | 6 / 88 | 16 / 20 |

**Was 400 kostet, ausdrücklich benannt:** einen belegten echten Ruf
(`20260805_065303`, RMS 336) — ein frühmorgens leise gesprochenes
Rollo-Kommando, hart belegt durch die Aktuator-Ausführung. Der leiseste echte
Ruf liegt damit nicht mehr bei 402, sondern bei 336; die Faustregel „25 % unter
dem leisesten Ruf" ergäbe heute **252**, also eine Senkung. Die beiden Regeln
zeigen in verschiedene Richtungen. Gewählt ist die gemessene Wirkung, nicht die
Faustregel.

**Was 400 nicht löst:** von den fünf Fehltriggern des 21./22.08. hätte es
genau einen geblockt (RMS 316). Die anderen lagen bei 588–1691 und kamen mit
Score 0.93–0.98 durch — für jedes Gate ununterscheidbar von einem echten Ruf.
Der eigentliche Hebel bleibt das Nachtraining.

**Geprüft und verworfen: ein Sprach-Gate.** Die Triage meldete fünf der sechs
letzten Fehltrigger als „kein Sprachanteil", das klang nach einem billigen
Filter. Über alle 114 gelabelten Trigger-Clips gerechnet trennt WebRTC-VAD
aber nicht: Sprachanteil im Wake-Fenster bei echten Rufen Median 0,36, bei
Fehltriggern 0,35. Bei einer Schwelle, die 4 von 41 Fehltriggern blockt, ist
der Gewinn Rauschen. Nicht einbauen.

### Was am 2026-08-22 verloren ging

Der Neustart nach der Änderung hat den Archiv-Cleanup ausgelöst: **56 Dateien
älter als 30 Tage gelöscht, darunter das Audio zu 6 per Ohr entschiedenen
Fehltriggern.** Die Labels stehen weiter in `wake_review.jsonl`, das Audio
dazu ist weg. Die Negativseite des Sweeps fiel dadurch im selben Lauf von 26
auf 20 belegte Fehltrigger — ohne dass ein Werkzeug etwas gemeldet hätte, es
rechnete einfach mit weniger. Ohr-Urteile sind das teuerste Label des
Verfahrens und waren am schlechtesten geschützt.

Behoben:
- `_cleanup_trigger_audio` verschont ungesicherte Ohr-Urteile
  (`voice_assistant/assistant.py:_geschuetzte_clips`).
- `tools/wake_corpus.py` hebt gelabelte Clips in einen Dauer-Korpus außerhalb
  des selbstlöschenden Verzeichnisses und meldet Erosion.

Nicht behebbar: die 6 Clips sind fort, Messreihen von vor dem 2026-08-22 sind
nicht mehr exakt reproduzierbar.

Wie das Gate arbeitet und was bei Änderungen zu beachten ist, steht oben unter
„Pegel-Gate (`wake_rms_min`)" — hier nur die Messung dahinter.

Dieser Weg ist **unabhängig vom Verifier** (siehe `tools/verifier_probe.py`)
und deutlich einfacher: ein Parameter statt eines sprecherspezifischen Modells,
kein Training, keine Sprecherbindung. Beide lassen sich kombinieren — Pegel
davor, Verifier dahinter. Ob der Verifier daneben noch etwas beiträgt, ist
offen und erst zu messen, wenn das Pegel-Gate scharf ist.

### Mehr Mikrofon-Verstärkung bringt dem Modell nichts (2026-10-02)

Frage: hilft eine höhere Eingangsverstärkung, wenn man das Pegel-Gate
entsprechend mit anhebt? Anlass war der Eindruck, die Aufnahmen seien leise,
und die Erinnerung an eine frühere Absenkung — die war aber im **Fablab**
(`fcd4cb9`, `respeaker-fablab.yaml`), nicht hier. Und die dort gesenkten
Firmware-Werte `auto_gain`/`volume_multiplier`/`noise_suppression_level`
wirken in diesem Aufbau gar nicht: ESPHome schickt sie als `audio_settings`
an den Server, `RespeakerSource` nimmt sie entgegen und ignoriert sie. Die
einzige Verstärkung ist das digitale `samples * 4` in `audio/respeaker.py`.

Gemessen mit `wake_corpus messen --gain G --rms-min 400` (Clips × G mit
Übersteuerung wie live, Pegel-Gate 400 × G) über den Dauer-Korpus:

| | ×1 | ×1,5 | ×2 |
|---|---|---|---|
| v4, ganzer Korpus — echte Rufe | 152/180 | 153/180 | 153/180 |
| v4, ganzer Korpus — Fehltrigger | 24/79 | 24/79 | 24/79 |
| v3, frisch (nach 2026-09-16) — echte Rufe | 70/100 | 70/100 | 70/100 |
| v3, frisch — Fehltrigger | 25/50 | 25/50 | 25/50 |

v4 hat den Korpus im Training gesehen (Pegel ×1); v3 auf den frischen Clips
ist die ehrliche Gegenprobe. Beide Modelle bleiben auf ±1 Clip gleich: der
Score ist praktisch pegelunabhängig, und mit mitskaliertem Gate ändert sich
nichts. Die STT normalisiert ohnehin auf Peak. Damit ist ein Mehr an
**digitaler** Verstärkung erledigt.

**Grenze dieser Messung (Jochen):** im Korpus steht nur, was schon einmal
aufgefallen ist — als Trigger, Near-Miss oder im Rückspul-Puffer. Ein Ruf,
der zu leise war, um überhaupt aufzufallen, fehlt; genau um den ginge es bei
mehr Empfindlichkeit. Beantwortbar ist das nur mit neuen, geführten
Aufnahmen (feste Positionen, vorher/nachher). Physikalisch bestätigt die
Messung nur, was ohnehin gilt: digitale Verstärkung verbessert den
Rauschabstand nicht.

Und der Pegel ist nicht abgesenkt, er ist geregelt: der XVF3800 hat eine
eigene AGC (`PP_AGCONOFF`, Default an) mit Zielpegel `PP_AGCDESIREDLEVEL`
0,0045 ≈ −23,5 dBFS RMS — passt zu den gemessenen Spitzen von −16 bis
−21 dBFS. Die Stellschrauben dort (AGC-Maximum, Rauschunterdrückung,
Beam) sind der nächste Schritt, siehe `esphome/respeaker.yaml`. Offen bleibt nur die analoge Seite
(Mikrofon-Gain/AGC im XVF3800 selbst) — die ist nicht über die Firmware
dieses Repos eingestellt und ungemessen.

## Prozess (wiederholend)

1. **Sammeln** — passiv aus dem Alltag. Tage bis Wochen.
2. **Triagieren** — `ow-venv/bin/python -m tools.wake_triage --seit N --auch-trigger`
   läuft über ungelabelte Files. Die Selbst-Labels stehen sofort, die STT
   klassifiziert nur den Rest, UNKLAR-Fälle bleiben übrig.
3. **Per Ohr entscheiden** — `tools/review_audio.py`: `export` legt die Clips
   bereit (Wake + Folgeaufnahme in einer Datei, Pre-Roll entfernt), man
   sortiert sie in `positiv/` bzw. `negativ/`, `import` liest das als harte
   Labels nach `wake_review.jsonl` zurück und meldet jede Abweichung vom
   automatischen Urteil. Ein Ohr-Urteil sticht Selbst-Label und STT.
   Mit `--liste` nur die Clips, an denen eine konkrete Messung hängt — das
   sind meist ein paar Dutzend statt hundert.
4. **Trainieren** — synthetische TTS-Samples + echtes Validierungs-Set +
   Negativ-Korpus → neues Modell. Siehe `Wakeword_Studio_Spec.md`.
5. **Validieren** — gegen Validierungs-Gate prüfen: Recall ≥ 0.9 gegen echte
   Aufnahmen, < 1 FP/Stunde gegen Negativ-Korpus. Nicht bestanden → zurück
   zu Schritt 4.
6. **Deployen** — neues `.tflite` ins Bundle, Service neustarten.
7. **Weiter sammeln** — der Kreislauf beginnt von vorn.

## Das Modell erkennt Gastons eigene Stimme (2026-09-20)

Beim Einbau des Abbruchs mitten im Turn (`barge_in`, siehe CLAUDE.md und
`tools/bargein_echo_test.py`) kam ein Befund heraus, der fürs **Training**
wichtiger ist als für den Abbruch:

> Das gaston-Modell erkennt die TTS-Stimme, mit der Gaston selbst spricht.
> Gemessen am reinen TTS-Signal: **5 Selbst-Trigger auf 40 Renderings (12,5 %),
> höchster Score 0,97.**

Das ist kein Zufall, sondern eine Lücke in der Trainingsverteilung. Trainiert
wird synthetisch auf Piper-Stimmen (`manifest.yaml`, `voices:` — thorsten-medium
/ high / emotional, karlsson, pavoque, …), und der Assistent spricht mit
`de_DE-thorsten-medium`. Seine eigene Stimme ist für das Modell nicht „jemand
anders", sie ist **Trainingsmaterial für die positive Klasse**.

Belege, die zeigen, dass es wirklich die Stimme ist und nicht bloß das Wort:

- Getroffen wurden alle drei Gate-Pfade (1 Frame/0,76 und 0,81;
  2 Frames/0,93; 3 Frames/0,97) und vier verschiedene Sätze.
- Darunter **„Das dauert noch einen Augenblick"** — eine Denk-Phrase, in der das
  Wakewort überhaupt nicht vorkommt.
- Umgekehrt: das korrekt ausgesprochene „Gaston" blieb in einem Lauf bei 0,58,
  während der STT-Verhörer „Gastau" 0,93 erreichte — dieselbe Nasal-Eigenschaft,
  die unter `spellings:` im Manifest steht.

**Was daraus für die nächste Trainingsrunde folgt:** Gastons eigene
TTS-Renderings gehören als **adversariale Negative** in den Datensatz. Sie sind
in beliebiger Menge und ohne Aufnahmesession herstellbar (jeder Satz, den er
je gesagt hat, plus die Bestätigungs- und Denk-Phrasen aus der Profil-Config)
und treffen eine Klasse von Fehltriggern, die im Alltagsarchiv kaum auftaucht —
weil das Mikro sie bisher nie zu hören bekam. Ab jetzt bekommt es sie: mit
`barge_in.while_speaking: true` hört der Assistent durch seine eigene Ansage
hindurch.

**Warum es trotzdem live funktioniert:** akustisch fällt derselbe Satz auf
Score **0,07** — die Echo-Unterdrückung des XVF3800 nimmt dem Signal die
Wakeword-Eigenschaft (Messreihe im Docstring von
`tools/bargein_echo_test.py`). Diese Rettung hängt vollständig an der Hardware:
ohne Echo-Unterdrückung im Audio-Pfad (`use_speaker: false`, ALSA-Lautsprecher)
gilt die digitale Zahl. Ein Modell, das die eigene Stimme nicht kennt, wäre
unabhängig davon robust — deshalb der Eintrag hier und nicht nur in der
Barge-in-Doku.

## Was NICHT zu tun ist

- An den **Score**-Schwellen weiter justieren — ausgereizt, die Begründung mit
  Messwerten steht an jedem Parameter in `models/wakewords/gaston/manifest.yaml`.
  (Das **Pegel**-Gate `wake_rms_min` ist davon ausgenommen: eine andere
  Dimension, siehe „Pegel als zweite Dimension". Es wird gegen
  `tools/wake_rms_replay.py` geändert, nicht nach Gefühl.)
- FPs als „egal" abtun — sie sind der wertvollste Teil des Datensatzes.
- Die Folgeaufnahme als Fehltrigger-Indikator fehlinterpretieren — sie ist der
  BELEG für einen echten Ruf, nicht der Fehltrigger selbst.
- Echte Aufnahmen als Trainingsdaten missverstehen — sie sind das
  Validierungs-Set. Trainiert wird synthetisch.
- Gastons **eigene** TTS-Stimme bei den Negativen vergessen — sie ist die eine
  Stimme, die garantiert täglich vor dem Mikro steht, und das Modell erkennt sie
  derzeit als Wakewort (siehe „Das Modell erkennt Gastons eigene Stimme").
- Ohne Prozess-Verständnis Klassifizierungen vornehmen.

## Stand

> Zahlen altern. Was hier steht, ist mit `tools/wake_triage.py` in Minuten neu
> zu erheben — die Datei sagt, **wo der Prozess steht und was als naechstes
> ansteht**, nicht was gerade in den Logs liegt.

**Wo der Prozess steht (2026-08-22, nachts):**

Es laufen drei Faeden nebeneinander. Der aktive ist Nummer 1.

**Faden 1 — Pegel-Gate ausgewertet, Schwelle auf 400, Wette geschlossen.**
Die am 2026-08-02 formulierte Frage („stimmt die Schwelle im Alltag?") ist
nach 20 Tagen Betrieb beantwortet: 16 geblockte Streaks, kein belegter echter
Ruf darunter, Fehltrigger liefen weiter durch — der Fall „Schwelle zu niedrig
fuer diesen Raum". `wake_rms_min: 400` steht seit dem 2026-08-22, 01:03 im
Profil `gastonllm`. Messung, Preis (ein belegter echter Ruf) und die verworfene
Sprach-Gate-Idee: Abschnitt „Nachtrag 2026-08-22" oben.

Die neue offene Frage ist dieselbe wie vorher, nur in die andere Richtung:

    grep min_rms ~/.openclaw/workspace/wake_events.log

- Tauchen dort jetzt Rufe auf, die jemand gemeint hat? → 400 ist zu hoch,
  zurueck auf 350 (kostet denselben einen Ruf, blockt 9 statt 14).
- Kommen Fehltrigger weiter durch, ohne dass echte Rufe verloren gehen? → das
  Gate ist ausgereizt, der Rest ist Modellarbeit (Faden 4).
- **Erst mit ein paar Tagen Betrieb ist das entscheidbar.** Vorher nicht am
  Wert drehen.

Bekannte Schwaeche, die dieser Fall offengelegt hat: eine ABSOLUTE Schwelle
muss zugleich fuer den leisen Morgen (Ruf bei 336) und den lauten Abend
(Fehltrigger bei 1691) passen — das kann sie nicht. Der naheliegende naechste
Entwurf ist ein Abstand zum gleitenden Grundpegel statt eines festen Werts;
`wakeword_studio/recorder.py:331` rechnet bereits so. Nicht gebaut, nicht
gemessen — notiert als Idee, nicht als Plan.

**Faden 4 — Nachtraining: Material und Ausgangsmessung liegen bereit.**
Der Grund steht im Nachtrag: die starken Fehltrigger (Score 0.93–0.98) sind
score- und pegelseitig nicht trennbar. `tools/wake_corpus.py` sichert die
gelabelten Clips dauerhaft (88 Stueck: 68 echte Rufe, 20 Fehltrigger) und
misst das laufende Bundle dagegen.

**Ausgangswert 2026-08-22, gaston @ threshold 0.35 (`wake_corpus messen`):**

    positiv   51/68  loesen aus  (75 %)   ← darf NICHT fallen
    negativ   19/20  loesen aus  (95 %)   ← soll fallen

Die 95 % sind fast tautologisch — der Negativ-Korpus besteht aus Clips, die
live getriggert HABEN. Der Wert taugt nicht als Guete des Modells, nur als
Vorher-Zahl fuer ein Nachher. Was noch fehlt, bevor trainiert wird:
- Die Negativseite ist mit 20 harten Labels duenn. 254 weitere `rauschen`-Clips
  liegen STT-gelabelt bereit; sie gehoeren per Ohr bestaetigt
  (`tools/review_audio.py`), bevor sie als Negativbeispiele taugen —
  ein faelschlich als Rauschen trainierter echter Ruf bringt genau das Wort
  bei, das nicht erkannt werden soll.
- Trainiert wird auf dem GPU-Host (`~/ai-stack/wakeword-studio/`, Spec Phase C);
  von diesem Pi aus ist das nicht erreichbar.

**Faden 2 — Verifier: gemessen, NICHT deploy-reif, liegt bewusst still.**
`tools/verifier_probe.py`, Messreihe im Docstring. FP-Achse traegt
hoch-signifikant (44 → 9 ueber 104 Clips, p < 0,001), Recall-Achse nicht
(12/16 → 15/16, McNemar p = 0,25). Eine Achse von zweien reicht nicht.

**Dieser Faden wird erst wieder angefasst, wenn Faden 1 ausgewertet ist** —
gut moeglich, dass das Pegel-Gate den Verifier ueberfluessig macht: ein
Parameter statt eines sprecherspezifischen Modells. Wenn doch weiter:
(a) Kreuzvalidierung ueber die Tagespartitionen, damit alle 55 harten Rufe
Testfall werden statt 16; (b) die Ohr-Labels einhaengen — `verifier_probe`
liest `wake_review.jsonl` noch NICHT, obwohl zwei der 24 Urteile die Messung
direkt betreffen (ein geretteter und ein unterdrueckter echter Ruf).

**Faden 3 — Ein-Satz-Aufnahmen: offen, ruht.** Siehe „Ein-Satz gegen Pause".
Die Alltagsstatistik kann die Frage prinzipiell nicht beantworten
(Survivorship-Bias). Es braucht einen kontrollierten Vergleich: beide Formen
vom selben Sprecher in derselben Session. `wakeword_studio record` nimmt
heute NUR isolierte Takes auf (`VARIATIONS` in `recorder.py`, alle 10
Eintraege isoliert) — es muesste Ein-Satz-Takes fuehren („Gaston, schalte das
Tischlicht ein") und im Scoring beide Gruppen trennen. Dieselbe Aenderung
liefert bei Bedarf gleich die Trainingsdaten.

**Bestand 2026-08-22 nachts** (2026-08-02 in Klammern): 580 (391) archivierte
Clips. Labels: 119 (106) echter_ruf, 280 (104) rauschen, 86 (54) unklar. Davon
**24 per Ohr entschieden** (`wake_review.jsonl`, 2 echter_ruf / 22 rauschen) —
die staerkste Quelle, aber **zu 6 davon existiert kein Audio mehr**, sie zaehlen
in keiner Messung mehr mit. Dauerhaft gesichert sind 88 Clips
(`tools/wake_corpus.py bilanz`). Fuer Schritt 4/5 (Trainieren/Validieren) ist
genug Material da; es fehlt nicht an Daten, sondern an der Entscheidung, was
trainiert werden soll.

**Kleinere offene Punkte:**
- Die UNKLAR-Faelle per Ohr entscheiden — jetzt mit
  `tools/review_audio.py --klasse unklar` statt `aplay` von Hand.
- Die ~78 weichen `rauschen`-Labels (STT-geraten, „kein Text erkannt") per
  Ohr pruefen, falls die FP-Zahlen belastbarer werden sollen. 16 Minuten
  Hoerzeit fuer alle.
- `--nur-studio` schlaegt die Schwelle aus dem leisesten Take vor; bei uns
  haben die kritischen Stile `leise` und `fern` nur je EINEN Take. Fuer die
  eigene Anlage war das durch 57 Alltagsrufe abgesichert, fuer einen Fremden
  ist es duenn — eine Warnung bei n=1 je kritischem Stil waere sinnvoll.

**Was NICHT als naechstes ansteht:** weiter passiv sammeln und die
Ein-Satz-Bilanz nochmal lesen. Das war die Empfehlung vom 2026-07-28 und ist
mit der Messung vom 2026-08-02 erledigt.

**Was NICHT mehr zu versuchen ist:** an den Schwellen drehen. Die Begruendung
mit Messwerten steht in `models/wakewords/gaston/manifest.yaml` an jedem
einzelnen Parameter. Zwei belegte echte Rufe kamen mit Peak 0.37/0.38 an, dort
liegt Rauschen gleichauf — die sind durch keine Schwelle zu retten.

Im MemPalace (Wing `clawdpi1-home-pi-openclaw-voice-assist`, Room `decisions`,
Drawer `...11c2e98f58cb...`) liegt der ausfuehrliche Prozess-Drawer mit
Session-Kontext und Fehlerdokumentation. Diese Datei ist die Repo-Seite
desselben — beide sind gegenseitig verlinkt.

## Nachtraining Runde 3 — Kandidat gaston_v3 (2026-09-16)

Erster Lauf mit den echten Clips ("Hebel b"): 47 echte Rufe + 19 belegte
Fehltrigger (×10 dupliziert) zu den 30k synthetischen, Paket und Split siehe
`wake_corpus paket` (Seed 20260916).

**Falle, an der der erste Lauf scheiterte:** `augment_clips` schneidet Clips
über `total_length` (hier 2 s) per **Münzwurf** vorn ODER hinten ab
(`data.py:create_fixed_size_clip`). Die 3-s-Archiv-Clips tragen das Wakewort
am Ende — die Hälfte der echten Positives ging als "Gaston"-gelabeltes
Raum-Audio OHNE Wakewort ins Training. Ergebnis: Recall brach ein (Studio
50→25 %). **Wer echte Archiv-Clips einspeist, schneidet sie vorher auf die
letzten 1,8 s.** Mit dem Fix (Runde 2 desselben Tages):

| Messung | gaston (live) | gaston_v3 |
|---|---|---|
| val/positive (27 echte Rufe, Val-Tage) | 74 % | **93 %** |
| val/positive_studio (20 Takes leise/fern) | 50 % | **60 %** |
| val/negative (6 belegte FP, Val-Tage) | 6 lösen aus | **4** |
| train/negative (19 belegte FP, Training) | 17 lösen aus | 8 (Selbstmessung) |
| FP/h generisch, 3-Frame-Streak @0.35 | 0,00 | 0,00 |

Gepaart (alt→neu): val/positive **5 Gewinne, 0 Verluste** (darunter der
komplette Wiederholungs-Cluster 20260731_1754xx; McNemar einseitig p=0,031,
zweiseitig 0,0625 — Richtung klar, knapp an der Schwelle). Studio 3:1
(verloren: der "schnell"-Take). val/negative: 2 der 6 bekannten Fehltrigger
eliminiert, **darunter 20260822_003128 — der "Stopp und Stopp"-Fehltrigger,
mit dem die Familienbeschwerde anfing**. Keine neuen FP im generischen Set.

**Deployt am 2026-09-16** (Jochens Entscheid): v3 ersetzt das Modell im
`gaston`-Bundle, der Vorgänger liegt in der Git-Historie. Die Gate-Parameter
(`min_peak_short` 0.9, `min_peak_single` 0.75) sind an den Score-Verteilungen
des **Vorgängers** geeicht und wurden bewusst nicht angefasst.

### Beobachtungswette v3 (formuliert VOR den Daten, wie beim Pegel-Gate)

Die Offline-Zahlen sagen 93 % Recall und weniger bekannte Fehltrigger voraus.
Live können Frame-Phasen anders fallen (Methoden-Warnung 2026-07-26). Nach
**~3 Wochen Betrieb** entscheidet `wake_events.log` + Triage:

1. **FP-Rate** (`Trigger mit Fehltrigger-Outcome pro Tag`): vorher 1,16/Tag.
   Fällt sie deutlich (< ~0,6/Tag), hat das Nachtraining geliefert. Bleibt
   sie gleich, war der Val-Gewinn Offline-Artefakt → zurück zu Schritt 4.
2. **Verlustquote** (Selbst-Label „wiederholt" unter den Near-Misses):
   zuletzt 5 von 16 Rufversuchen (~31 %). Soll spürbar fallen. Steigt sie,
   ist v3 live schlechter als gemessen → Rollback (Git-Historie) und Befund.
3. **1-Frame-/Kurz-Streak-Pfade**: die Peaks von v3 sind anders verteilt —
   häufen sich echte Rufe als Near-Miss mit `failed_on: min_peak` oder
   knapp unter `min_peak_single`, sind die Kurz-Streak-Schwellen für v3 neu
   zu messen (nur gegen die live geloggten Score-Verläufe, nicht offline).

Rollback-Weg: die drei Modell-Dateien aus der Git-Historie
(`git checkout <alt> -- models/wakewords/gaston/`), Service-Neustart.

### Zwischenlesung der Wette nach 4 Tagen (2026-09-20) — NICHT geschlossen

Die Wette will ~3 Wochen. Nach 4 Tagen ist sie nicht entscheidbar, aber drei
Dinge sind schon messbar, und eines davon zeigt in eine unerwartete Richtung.
Alle Zahlen sind mit `tools/wake_triage.py --seit N --auch-trigger`,
`tools/wake_corpus.py messen --split` und `wake_events.log` reproduzierbar.

**Das Modell ist besser — gemessen auf Material, das es nie gesehen hat.**
v2 gegen v3 auf denselben Korpus-Clips, identische Gate-Parameter (nur die
tflite getauscht, v2 aus `daf5518^`), und getrennt nach dem Tages-Split des
damaligen Trainingspakets:

| auf FRISCHEN Clips (32 positiv / 7 negativ) | echte Rufe | Fehltrigger durchgelassen |
|---|---|---|
| v2 | 24/32 = 75 % | 6/7 = 86 % |
| v3 | 30/32 = **94 %** | 4/7 = **57 %** |

v3 ist auf beiden Achsen besser, und zwar auf Clips, die **keines** der beiden
Modelle im Training hatte. Der Verdacht „beim Nachtraining ist etwas Falsches
hineingerutscht" ist damit ausgeräumt. Nebenbefund gegen Überanpassung: v3
erkennt auf den frischen Clips (94 %) *mehr* als auf den Trainingsclips (85 %).

**Die FP-Rate im Betrieb ist unverändert — Kriterium 1 zeigt nach unten.**
Mit derselben Triage-Methode über beide Zeiträume: **1,33 Fehltrigger/Tag vorher,
1,40/Tag nachher.** Die Wette verlangt „< ~0,6/Tag, sonst war der Val-Gewinn
Offline-Artefakt". Bei n=7 Fehltriggern in 4 Tagen ist das keine Entscheidung,
aber die Richtung ist nicht die erhoffte. Beides zugleich wahr zu haben ist
kein Widerspruch: der Korpus misst, ob v3 die **bekannten** Fehltrigger
abstellt; der Alltag produziert **neue** — Gesprächsfetzen, die kein Modell
gesehen hat. Genau diese Grenze steht im Docstring von `wake_corpus`.

**Kriterium 3 ist das eigentliche Ergebnis: der 1-Frame-Pfad kauft nichts mehr.**
Trigger nach Gate-Pfad, gegen die Labels:

| Pfad | vor v3: echt / Fehltrigger | nach v3: echt / Fehltrigger |
|---|---|---|
| 1 Frame (`min_peak_single`) | 12 / **29** | **0** / **4** |
| 2 Frames (`min_peak_short`) | 9 / 14 | 1 / 2 |
| 3+ Frames (`min_peak`) | 55 / 17 | 1 / 1 |

Der 1-Frame-Pfad war immer FP-lastig (29 von 73 Fehltriggern vor v3, also 66 %
seiner eigenen Trigger), aber er hatte eine Rechtfertigung: er holte 12 belegte
echte Rufe herein, und eingebaut wurde er am 2026-07-26, weil 4 von 6
verlorenen Rufen nur so zurückkamen. **Mit v3 holt er keinen einzigen mehr** —
v3 erreicht echte Rufe mit richtigen Streaks und braucht die Rettung nicht.
Übrig bleibt die Fehltrigger-Seite: 4 von 7 Fehltriggern der letzten 4 Tage
kamen über diesen Pfad.

Das ist die plausibelste Erklärung für „v3 ist offline besser, der Alltag fühlt
sich gleich an": das Modell wurde besser, aber ein Gate-Pfad, der auf die
Score-Verteilung des VORGÄNGERS geeicht war, lässt weiter Gesprächsfetzen
durch. Genau die Eichung, die `manifest.yaml` beim Deploy als offen markiert
hat.

**Methodisch wichtig, und es bestätigt die Wette:** das ist offline NICHT
messbar. Ein Korpus-A/B „v3 mit gegen v3 ohne `min_peak_single`" ergab exakt
dieselben Zahlen (94 % / 57 % in beiden Fällen), weil der Offline-Scorer
mehrere Frame-Phasen probiert und immer den besten Streak findet — ein
1-Frame-Trigger entsteht dort praktisch nie, live liegt die Phase fest. Die
Wette hat das vorausgesagt („nur gegen die live geloggten Score-Verläufe, nicht
offline"). Die Tabelle oben ist deshalb jetzt Teil von
`tools/wake_triage.py` (Abschnitt „TRIGGER NACH GATE-PFAD"), damit die Wette
mit einem Befehl und nicht mit einem Wegwerf-Skript gelesen wird.

**Was daraus NICHT folgt:** `min_peak_single` jetzt abzuschalten. Vier Tage mit
5 belegten echten Rufen sind zu wenig — ein kurzes Fenster mit wenigen Rufen
sieht immer so aus, als kaufe der Pfad nichts. Die Wette läuft bis ~3 Wochen;
bleibt die Tabelle dann so, ist das Abschalten (oder eine Anhebung auf die neue
Score-Verteilung) der erste Schritt, und zwar mit eigener, vorab formulierter
Wette.

**Nebenbefund, der eigene Aufmerksamkeit verdient:** von 792 Labels haben
**307 kein Audio mehr**, darunter 6 Ohr-Urteile aus dem Juli. `wake_corpus
sichern` hat am 2026-09-20 nichts Neues gefunden (alles Haltbare ist im Korpus),
die Erosion ist also Altlast von vor der Schutzregel — aber sie verkleinert
jede künftige Messbasis dauerhaft.

### Bilanz der Beobachtungswette v3 (2026-10-01) — nicht bestanden, aber kein Rollback

Nach 15 Tagen statt der angepeilten drei Wochen, weil die Kriterien schon klar
ausfallen. Zeiträume getrennt, weil am 2026-09-25 21:38 der 1-Frame-Pfad
abgeschaltet wurde (siehe Wette unten). Methode wie in der Zwischenlesung:
`tools/wake_triage.py --seit 16 --auch-trigger`, Labels aus
`wake_triage.jsonl`, Verlustquote = Near-Misses mit Selbst-Label
„wiederholt“ / (diese + echte Trigger).

| | vorher | A 16.–25.09. (1-Frame an) | B 25.09.–01.10. (1-Frame aus) |
|---|---|---|---|
| 1. Fehltrigger/Tag | 1,16 (Zwischenlesung: 1,33) | **2,26** | **1,99** |
| 2. Verlustquote | 31 % (5/16) | **47 %** (9/19; mit STT-Labels 60 %) | **35 %** (18/51; 37 %) |
| 3. 1-Frame-Pfad | — | 0 echt / 12 Fehltrigger | abgeschaltet |

**Nach den eigenen Regeln nicht bestanden:** Kriterium 1 verlangte < ~0,6
Fehltrigger/Tag — die Rate ist eher gestiegen. Kriterium 2 verlangte, dass
die Verlustquote fällt; „steigt sie → Rollback“.

**Warum trotzdem kein Rollback — direkter Vergleich v2/v3 (2026-10-01):**
alle Korpus-Clips seit dem v3-Deploy (16.09. 14:37), für BEIDE Modelle
unbekannt, je mit dem eigenen Manifest gescort (v2 aus `daf5518^`):

| 62 echte Rufe | ausgelöst |
|---|---|
| v2 | 47 (76 %) |
| v3 | 52 (84 %) |

14 Rufe gehen verschieden aus: 9 fängt nur v3, 4 nur v2 (McNemar nicht
signifikant). Drei der vier v2-Treffer sind vom lauten Abend des 01.10.
(Pegel-Median ~10× höher als am 29.09.) — Hinweis, kein Befund. Für die
Fehltrigger-Seite gibt der Korpus nichts her: seit dem 16.09. nur 5 hart
gelabelte Negative (die STT-gelabelten werden nicht gesichert).

**Einschränkungen der Wette selbst:** die Basis „31 %“ stammt aus 16
Rufversuchen; die Fehltrigger sind fast alle STT-gelabelt (hart: 3 in A,
1 in B); die Nutzung hat sich geändert (B: fünfmal so viele echte Rufe/Tag,
Aktuator-Tests, laute Abende).

**Nachtrag 2026-10-01 nachts — mit Ohr-Labels neu gerechnet.** Jochen hat
95 Clips seit v3 angehört (`tools/review_audio.py`, Stapel A: Trigger mit
STT-Label „rauschen/unklar“, B: Near-Misses, C: Marker-Rufe). Von den
Triggern in Stapel A waren **18 echte Rufe**, die die STT als Rauschen oder
unklar geführt hatte. Mit Ohr > Selbst > STT:

| | vorher | A 16.–25.09. | B 25.09.–01.10. |
|---|---|---|---|
| Fehltrigger/Tag (jetzt alle hart) | 1,16 | 2,37 | **1,16** |
| Verlustquote | 31 % | 52 % | **28 %** |

Seit der 1-Frame-Pfad aus ist, liegt v3 bei den Fehltriggern genau auf dem
alten Stand und bei der Verlustquote leicht darunter — Kriterium 1 bleibt
verfehlt (< 0,6), Kriterium 2 ist nicht verletzt. Die erste Rechnung oben
(1,99/Tag, 35 %) beruhte auf STT-Labels und war zu schlecht. Das Abschalten
des 1-Frame-Pfads hat die Fehltrigger halbiert (2,37 → 1,16/Tag).

**Lesart:** v3 ist auf frischen Rufen nicht schlechter als v2, eher besser —
aber es hat im Betrieb nicht geliefert, was der Offline-Gewinn versprach.
Die Wette sagt für diesen Fall „zurück zu Schritt 4“: Runde 4 mit den echten
Fehlschlägen seit v3 (Near-Miss-Mitschnitte, Marker-Clips, siehe Nachtrag
„zum ersten Mal verpasste Rufe im Nenner“), und vorher die Fehltrigger-Seite
belastbarer machen (Ohr-Labels für eine Stichprobe der STT-„rauschen“).
Entscheidung über Merge von `feature/wakeword-nachtraining`: Jochen.

## Nachtraining Runde 4 — Schranke, formuliert VOR dem Training (2026-10-02 nachts)

Jochen hat das Training freigegeben und ist schlafen gegangen („mach alles
komplett fertig“). Deshalb steht hier VORHER, was v4 erfüllen muss, um
ohne Rückfrage deployt zu werden — und was sonst passiert.

Paket: `wake_corpus paket`, Seed 20260916 (wie Runde 3), Positive selbst
geschnitten (1,8 s), Marker-Rufe je Wakewort, Ohr-Labels aus dem Review
vom 2026-10-01. Einspeisung wie Runde 3: `real_*`, je ×10, die
Runde-3-Clips raus (Sicherung daneben), 30k synthetische unverändert.

Vergleich auf den FRISCHEN Val-Clips des Pakets (11 Tage). Drei davon
(20260801, 20260802, 20260906) waren Trainingstage von v3 — dort misst
sich v3 selbst, der Vergleich wird für v4 strenger. Zusätzlich wird ohne
diese drei Tage gerechnet.

| | v3 (Vorher) | v4 muss |
|---|---|---|
| val/positive (48) | 37 lösen aus | **≥ 37**, gepaart nicht mehr Verluste als Gewinne |
| val/negative (36, harte Fehltrigger) | 21 lösen aus | **≤ 21** |
| val/positive_studio (20 Takes leise/fern) | wird gemessen | **≥ v3 − 1** |
| FP/h generisch (`eval_debounce.py`, 3-Frame @0,35) | 0,00 (Runde 3) | **≤ 0,5** |

**Alle vier erfüllt →** Deploy (Modell im `gaston`-Bundle ersetzen,
Gate-Parameter unverändert, v3 bleibt in der Git-Historie), neue
Beobachtungswette, Morgen-Bericht an Jochen mit Rollback-Befehl.
**Eins verfehlt →** kein Deploy; Kandidat als Bundle `gaston_v4` daneben
ablegen, Befund notieren, Entscheidung bei Jochen.

### Ergebnis Runde 4 (2026-10-02 00:40) — alle vier erfüllt, deployt

Training auf dem ai-stack 23:59–00:36 (Augment 26 min, Training 11 min;
`llm` gestoppt und per `trap` wieder gestartet; der Fehlercode von
`train.py` kommt nur von der eingebauten tflite-Umwandlung — `onnx_tf`
fehlt dort, wie in Runde 3 —, Umwandlung separat mit `onnx2tf -kat x`).
Eingespeist: 131 Positiv-Stücke + 43 Negative aus dem Paket, je ×10, statt
der 47/19 aus Runde 3 (die liegen in `train_out/backup_r3_20261002/` samt
v3-Modell und Feature-Dateien).

| frische Val-Tage | v3 | v4 | Schranke |
|---|---|---|---|
| echte Rufe (48) | 37 | **41** — gepaart 8 Gewinne / 4 Verluste | ✓ |
| harte Fehltrigger (36) | 21 | **17** — 8 weg, 4 neu | ✓ |
| ohne die 3 v3-Trainingstage | 32/43, 18/29 | **37/43, 13/29** | — |
| Studio (20) | 12 | **14** | ✓ |
| FP/h generisch, 3-Frame @0,35 | 0,00 | **0,09** | ✓ |

v4 erkennt u. a. die verpassten Rufe vom Abend des 01.10. (21:54:37, 22:06:51,
22:07:09, 22:07:31) und lässt A039 fallen (der Fehltrigger, den Jochen per
Ohr zweimal geprüft hat). Verloren: drei Trigger-Rufe, bei denen v4 auf
0,60–0,68 fällt — knapp unter `min_peak` 0,7. Offline-Messung (beste von
mehreren Frame-Phasen) — live entscheidet die Beobachtung.

Deploy nach der vorab festgelegten Regel: `gaston.tflite/.onnx/.onnx.data`
im Bundle ersetzt, Gate-Parameter unverändert. **Rollback:**
`git checkout <Deploy-Commit>^ -- models/wakewords/gaston/` und
`systemctl --user restart openclaw-voice-assist`.

### Beobachtungswette v4 (formuliert VOR den Daten)

Bezug ist der Zeitraum B der v3-Bilanz (25.09.–01.10., 1-Frame aus,
Ohr-Labels): **1,16 Fehltrigger/Tag, Verlustquote 28 %.** Nach ~2 Wochen,
gelesen wie die v3-Bilanz (Triage + Ohr-Review der STT-„rauschen“, Ausschluss-
Fenster beachten):

1. **Fehltrigger/Tag** soll unter 1,16 fallen. Steigt sie deutlich (> 1,5),
   ist der Offline-Gewinn auf der FP-Seite ein Artefakt → Rollback prüfen.
2. **Verlustquote** soll unter 28 % fallen. Steigt sie über 35 % → Rollback.
3. **Gate-Parameter**: liegen echte Rufe gehäuft als Near-Miss bei
   0,6–0,7 (`failed_on: min_peak`) — das Verlustmuster der 3 Offline-Verluste —,
   ist `min_peak` für v4 neu zu messen, nur gegen live geloggte Verläufe.

### Wette 1-Frame-Pfad aus (2026-09-25, formuliert VOR den Daten)

`min_peak_single` 0.75 → 0.0, vorzeitig vor dem Ende der v3-Wette. Stand beim
Abschalten (`wake_triage --seit 10 --auch-trigger`, seit v3-Deploy):

| Gate-Pfad | echt | Fehltrigger | unklar |
|---|---|---|---|
| 1 Frame | 0 | 12 | 2 |
| 2 Frames | 3 | 4 | 1 |
| 3+ Frames | 9 | 5 | 5 |

Allein am 25.09. kamen 5 von 10 Triggern über diesen Pfad, alle mit leerer
oder sinnloser Transkription.

**Messbar bleibt es:** ein Einzel-Frame ≥ 0,75 wird jetzt als Near-Miss mit
`failed_on: min_hits` archiviert — genau die Ereignisse, die vorher getriggert
hätten. Nach **~3 Wochen (bis ~2026-10-16)** diese Klasse triagieren:

1. **Geliefert**, wenn die Fehltrigger-Rate pro Tag deutlich fällt (Bezug:
   12 FP über den Pfad in 10 Tagen) und unter den 1-Frame-Near-Misses ≥ 0,75
   **höchstens 1** belegter echter Ruf liegt.
2. **Zurück auf 0,75**, wenn dort mehr belegte echte Rufe als Fehltrigger
   liegen — dann war der 10-Tage-Stand ein kurzes Fenster.
3. Dazwischen: Wiederholungsrate (Selbst-Label „wiederholt") vorher/nachher
   vergleichen. Ein Ruf, der sofort wiederholt wird, kostet Sekunden, ein
   Fehltrigger einen falschen Turn.

**Zwischenstand 2026-10-01, VORLÄUFIG (aus den Marker-Clips, nicht aus der
Near-Miss-Triage):** zwei Rufe am 30.09. 07:18, beide „Gaston, schalt(e) das
Abendlicht aus", erreichten je genau EINEN Frame über der Schwelle (0,83 und
0,90) und lösten deshalb nicht aus; mit `min_peak_single` 0,75 hätten beide
ausgelöst. Das wären schon zwei belegte echte Rufe in der Klasse, für die
Kriterium 1 höchstens einen erlaubt. Gezählt wird bei der Auswertung.

**Zwischenzählung 2026-10-01 22:10, VORLÄUFIG (nach STT-Text, nicht nach
Ohr):** seit dem Abschalten 18 Ein-Frame-Near-Misses ≥ 0,75 — 3 davon echte
Rufe (30.09. 07:18 ×2 mit 0,83/0,90; 01.10. 22:08 mit 0,96, „Gastau, schalte
das Tischlicht aus“), 15 vermutlich Fehltrigger (Fernsehen, Gespräch). Zurück
auf 0,75 hieße also +3 Rufe gegen +15 falsche Turns in ~6,5 Tagen. Alle drei
Rufe kamen beim nächsten Versuch binnen Sekunden durch. Nach den Kriterien
oben: weder 1 (≤ 1 echter Ruf) noch 2 (mehr echte als falsche) — Fall 3, und
der spricht fürs Ausgeschaltet-Lassen.

Derselbe Abend zeigte, wo der größere Verlust liegt: „Gaston. Gaston.“ und
„Gaston? Gaston?“ — einzeln, mit Pause gesprochen — kamen auf 0,65 und 0,42.
Das ist keine Gate-Frage, sondern eine des Modells. Material für die nächste
Trainingsrunde liegt an zwei Stellen, die sich ergänzen: die Marker-Clips
(`*_marker_rueckspul.wav`, 120 s VOR jedem Tastendruck — auch Rufe mit Score
≈ 0, die nie ein Near-Miss wurden) und die Near-Miss-Mitschnitte
(`*_nearmiss_folge.wav`, 6 s nach jedem Near-Miss — auch Rufe NACH dem
letzten Tastendruck).

**Falle beim Auswerten der Marker-Clips:** Sprachabschnitte über „Pegel >
3 × Median" zu suchen, versagt in lauter Umgebung. Am 2026-10-01 abends lag
der Median bei 285 (am 29.09.: ~25) — die Schwelle damit bei ~855, die Rufe
bei 730–770. Die Rufe waren im Clip, die Suche sah sie nicht. Abschnitte am
Grundrauschen (unteres Perzentil) oder an der Score-Spur festmachen, nicht am
Median.

Wichtig für die v3-Wette: deren Kriterium 2 (Verlustquote) wird ab heute
durch diese Änderung mitbewegt — Zeiträume vor und nach dem 25.09. getrennt
lesen. Offline nicht messbar (der Korpus-Scorer findet über mehrere
Frame-Phasen immer den besten Streak).

## Nachtraining Runde 5 — Schranke, formuliert VOR dem Training (2026-10-11 nachts)

Anlass (Jochen, 2026-10-10): nach „Gaston“ muss man eine spürbare Lücke
lassen, sonst kommt der Ruf nicht an. Gemessen bestätigt: von 54 flüssig
gesprochenen Timer-Sätzen („Gaston, stell …“, Jochen, 2026-10-10) löst v4
nur 12 aus. Die Wette v4 wird dafür vorzeitig beendet (Jochen hat ab Montag
keine Zeit); Bezugszahlen der Wette bleiben für die Beobachtung von v5.

**Labels mit weniger Hören.** Statt eines Voll-Reviews nur 50 Clips per Ohr,
ausgewählt nach „Signale widersprechen sich und der Clip wäre wertvoll“
(Jochen: lieber einen Clip weglassen als falsch einsortieren). Ergebnis:
**42 von 50 waren echte Rufe**, fast alle Trigger, deren Wake-Clip die STT
als leer oder Verhörer las („Gestalt“, „Das ist toll“, „Rastun“). Die
STT-Triage taugt auf dem kurzen Wake-Clip also nicht als Negativ-Label für
TRIGGER. Automatisch übernommen wurden nur Beinahe-Treffer mit Peak ≥ 0,5,
bei denen medium UND Qwen weder im Clip noch im 6-s-Mitschnitt danach eine
Gaston-Form hören, die Torfrage „kein Befehl“ sagt und kein Selbst-Label
(Wiederholung) existiert: 128 Negative (7 weitere fielen durch Qwen heraus).
Im Paket getrennt geführt (`auto_*`), in der Validierung eigene Gruppen.

Paket: `wake_corpus paket`, Seed 20260916, 8 Val-Tage (die Aufteilung ist
mit mehr Material eine andere als in Runde 4 — einige Val-Tage waren
Trainingstage von v4, der Vergleich ist also für v5 strenger). Dazu die
Timer-Sätze: ungerade Nummern ins Training (auf „Gaston“ geschnitten), gerade
(27, ganz) als Prüfsatz `val/positive_timer`.

| Validierung | v4 (Vorher) | v5 muss |
|---|---|---|
| flüssige Sätze `positive_timer` (27) | 5 | **≥ 10** |
| echte Rufe `positive` (130) | 113 | **≥ 111** |
| Fehltrigger `negative` + `negative_auto` (21 + 25) | 12 + 10 = 22 | **≤ 22** |
| Studio-Takes (20) | 14 | **≥ 13** |
| FP/h generisch (`eval_debounce.py`, 3-Frame @0,35) | 0,09 | **≤ 0,5** |

**Alle erfüllt →** Deploy wie Runde 4 (Modell im `gaston`-Bundle ersetzen,
Gate-Parameter unverändert), Korpus vorher sichern, Bericht mit
Rollback-Befehl. **Eins verfehlt →** kein Deploy, Kandidat daneben,
Entscheidung bei Jochen.

### Ergebnis Runde 5 (2026-10-11 01:05) — alle fünf erfüllt, deployt

Training im Fablab 00:24–01:03 (Augment 27 min, Training 11 min; `llm`
gestoppt und per `trap` im eigenen Scope wieder gestartet; tflite wie in
Runde 4 separat per `onnx2tf -kat x`, float32). Eingespeist: 300
Positiv-Stücke (davon 26 Timer-Sätze, 14 auto) + 175 Negative (davon 103
auto), je ×10. Runde-4-Clips samt v4-Modell in
`train_out/backup_r4_20261011/`.

| Validierung | v4 | v5 | gepaart | Schranke |
|---|---|---|---|---|
| flüssige Sätze (27) | 5 | **10** | +5 / −0 | ≥ 10 ✓ |
| echte Rufe (130) | 113 | **116** | +8 / −5 | ≥ 111 ✓ |
| Fehltrigger (21 + 25) | 22 | **16** | 2 neu / 8 weg | ≤ 22 ✓ |
| Studio (20) | 14 | **19** | +5 | ≥ 13 ✓ |
| FP/h generisch | 0,09 | **0,09** | | ≤ 0,5 ✓ |

Alle 54 Timer-Sätze (Hälfte im Training): v4 12, v5 25. **Ehrlich gelesen:
die Lücke ist kleiner, nicht weg** — 17 von 27 ungesehenen flüssigen Sätzen
lösen weiter nicht aus. Material dafür ist jetzt leicht zu bekommen:
flüssige Sätze wie die Timer-Aufnahmen, auch von anderen Stimmen.

**Rollback:** `git checkout 42797ef^ -- models/wakewords/gaston/` und
`systemctl --user restart openclaw-voice-assist`.

### Beobachtungswette v5 (formuliert VOR den Daten)

Bezug ist die (vorläufige, STT-gelesene) Zwischenzählung v4 vom 07.10.:
~3 Fehltrigger/Tag, ~37 % Verlust — gelesen wird v5 genauso, damit der
Vergleich gleich schief ist. Nach ~2 Wochen:

1. **Fehltrigger/Tag** nicht über dem v4-Wert. Deutlich darüber → Rollback prüfen.
2. **Verlustquote** soll fallen; steigt sie, war der Gewinn bei den flüssigen
   Sätzen ein Artefakt des einen Sprechers.
3. **Ein-Satz-Anteil** der Trigger (`ack`-Entscheidung im `wake_events.log`)
   soll steigen — das ist die Wirkung, um die es ging.

### Bilanz der Beobachtungswette v5 (2026-10-25): nach Wortlaut nicht bestanden, kein Rollback

Gezählt mit `labels_fuer_clips` (Ohr > Selbst > STT), v4 = 02.–11.10. (9 Tage),
v5 = 11.–25.10. (14,5 Tage):

| | v4 | v5 | Kriterium |
|---|---|---|---|
| Fehltrigger/Tag, nur Selbst-Labels | 0,78 | 1,79 | 1 ✗ |
| Fehltrigger/Tag, Ohr + Selbst | 1,33 | 2,34 | |
| Verlustquote | 38 % | 44 % | 2 ✗ |
| Ein-Satz-Anteil der Trigger | 85 % | 73 % | 3 ✗ |

Die Bezugszahl der Wette (~3/Tag, ~37 %, STT-Lesung vom 07.10.) ist nicht
nachvollziehbar gezählt; verglichen wird deshalb mit derselben Zählung heute.
Die Labels sind trotzdem ungleich: die v4-Trigger sind nach dem Review für
Runde 5 fast ganz Ohr/Selbst, bei v5 sind 112 von 279 nur STT. Das
Umgebungspegel-Experiment (ab 17.10.) erklärt es nicht: v5 vor dem 17.10.
schon 42 % Verlust, 69 % Ein-Satz.

**Offline-Gegenprobe** (v4 aus `42797ef^`, beide Modelle auf denselben Clips
seit dem Deploy; Raum = gleitender Median aus dem Log bzw. aus `*_vorlauf.wav`,
„ruhig“ unter 96 = 4 × Ruhe):

- **Ruhiger Raum:** von 13 hart belegten Fehltriggern hätte v4 **9 nicht**
  ausgelöst (v4 0,03–0,57, v5 0,77–0,93), 7 davon ohne Sprache im Wake-Clip,
  viermal danach „Stopp, Stopp!“. Per Ohr am 21.10.: Klappern, Aufräumen,
  Gegenstand auf der Küchenplatte („Lautstärkeform wie Gaston“). Das ist eine
  **neue Fehlerklasse von v5**, Jochens Eindruck stimmt.
- **Lauter Raum:** 16 von 21 harten Fehltriggern hätte v4 auch ausgelöst; der
  Anstieg dort (0,89 → 1,45/Tag) liegt eher an der Umgebung als am Modell.
- **Echte Rufe:** von 90 hart belegten v5-Triggern hätte v4 11 nicht ausgelöst.
- **Fernseh-Fehltrigger seltener** (Jochens Eindruck): mit dem Archiv nicht
  belegbar. Archiviert wird nur, worauf das laufende Modell anspringt; die
  zwei sauberen Val-Tage der v4-Zeit hatten zu wenige Fehltrigger. Der Scan
  über die 485 Vorläufe taugt nicht als FP-Rate: die 30 s vor einem Trigger
  enthalten meist eigene frühere Rufe (beide Modelle ~30 Auslöser/h).

Vermutung, nicht gemessen: die flüssigen Sätze machen den Anlaut
empfindlicher, und ein Klopfen ähnelt einem harten G/K. Unter den 14
Auto-Positiven von Runde 5 sind 2 Gerede („Gastraum“, „Gastein“), keine
Klopfer. Kein Rollback: v4 verlöre die flüssigen Rufe wieder. Stattdessen
Runde 6 mit Geräusch-Negativen.

## Nachtraining Runde 6 — Schranke, formuliert VOR dem Training (2026-10-25)

Ziel: die Geräusch-Fehltrigger im ruhigen Raum weg, ohne den Gewinn von
Runde 5 (flüssige Sätze) zu verlieren.

**Material.** Alles Hart-Gelabelte seit dem Paket von Runde 5 (11.10.
00:19), dazu das Ohr-Review der 121 v5-Trigger, die nur STT-Labels haben
(Hörseite, Reihenfolge: nur-v5-Fälle zuerst), und Auto-Negative nach der
Regel von Runde 5 (Near-Miss Peak ≥ 0,5, medium UND Qwen ohne Gaston-Form in
Clip und Folge, Torfrage nein, kein Selbst-Label). Neu: bei den Auto-Negativen
wird der Raum mitgeschrieben (`umgebung.laut`), Kandidaten aus dem ruhigen
Raum sind das Klapper-Material. Keine gestellten Geräusch-Aufnahmen (Jochen).

**Ohr-Review 2026-10-25 (121 v5-Trigger mit STT-Label):** 100 echte Rufe,
17 Fremdes (12 Person im Raum, 6 Geräusch, 1 Musik; Husten, Schnarchen),
2 unklar. Von 53 STT-„rauschen“ waren 41 echte Rufe — wie in Runde 5 taugt
die STT auf Trigger-Clips nicht als Negativ-Label. Ein Selbst-Label lag
falsch: `20261018_151852` (nach „Stopp“ als Fehltrigger gezählt) war ein
„Stopp Gaston“; im ruhigen Raum bleiben damit 8 von 12 harten Fehltriggern,
die v4 nicht ausgelöst hätte. `20261011_113145` („Gastoncode“, gesagt, aber
nicht gemeint) bleibt als Sonderfall draußen.

**Jochens Hinweis zur Qualität der echten Rufe:** viele sind zweifelhaft,
oft mit Geräuschen davor und danach; kommentiert hat er nur die
auffälligsten („klopfen und dann gaston“, „mit Musik“). Folge für die
Auswahl, festgelegt vor dem Training: ins **Training** kommen nur Positive,
bei denen im geschnittenen 1,8-s-Stück medium ODER Qwen eine Gaston-Form
hört, und keine mit Geräusch- oder Musik-Notiz. Die übrigen echten Rufe
gehen nur in die Validierung (eigene Gruppe `positive_zweifel`, gemessen,
nicht Teil der Schranke). Grund: ein Positiv mit einem Klopfer im
Schnittfenster lehrt das Modell, dass Klopfen zu „Gaston“ gehört — genau
die Fehlerklasse, um die es geht. Ob das schon in Runde 5 passiert ist (42
per Ohr gerettete Rufe, deren Clip die STT nicht verstand, gingen dort ins
Training), ist nicht gemessen; der Vergleich der Gruppe
`negative_geraeusch` zwischen v5 und v6 zeigt es indirekt.

**Split.** Die Val-Tage von Runde 5 bleiben Val (sonst ist die v5-Zahl
nicht mehr gültig, v5 hätte diese Tage im Training nie gesehen, v6 schon).
Von den Tagen ab 12.10. kommen nach Seed 20260916 ganze Tage dazu,
Ziel 30 %. Neue Val-Gruppe `negative_geraeusch`: Fehltrigger mit
Ohr-`herkunft` geraeusch und harte Fehltrigger im ruhigen Raum, je nach Tag
Train oder Val wie alles andere.

| Validierung | v5 (Vorher) | v6 muss |
|---|---|---|
| flüssige Sätze `positive_timer` (27) | 10 | **≥ 10** |
| echte Rufe R5-Val `positive` (130) | 116 | **≥ 114** |
| Fehltrigger R5-Val (21 + 25) | 16 | **≤ 16** |
| Studio-Takes (20) | 19 | **≥ 18** |
| FP/h generisch (`eval_debounce.py`) | 0,09 | **≤ 0,5** |
| echte Rufe neue Val-Tage (94) | 82 | **≥ v5 − 2 % → ≥ 80** |
| Fehltrigger neue Val-Tage (hart, 20) | 20 | **≤ v5 → ≤ 20** |
| `negative_geraeusch` Val (4) | 4 | **höchstens halb so viele wie v5, mindestens 1 weniger → ≤ 2** |

Die drei offenen Vorher-Zahlen misst `wake_corpus messen` mit v5 auf dem
fertigen Paket, eingetragen werden sie hier, bevor das Training startet. Die
Regeln stehen jetzt und werden danach nicht angepasst. Die Geräusch-Gruppe
ist klein; ihre Zahl wird gepaart gelesen (welche Clips kippen, in welche
Richtung), nicht nur als Quote.

**Alle erfüllt →** Deploy wie Runde 5, Korpus vorher sichern, Bericht mit
Rollback-Befehl. **Eins verfehlt →** kein Deploy, Kandidat daneben,
Entscheidung bei Jochen.

### Paket und Vorher-Zahlen Runde 6 (2026-10-25, vor dem Training)

Gebaut mit `tools/wake_paket.py bauen` (neu, hält die Handarbeit von Runde 5
fest), Paket `~/wake-pakete/gaston_r6_paket` (Familienstimmen, nicht im
Repo). Neue Val-Tage: 11., 12., 16., 17., 19., 23.10. Training: 394
Positive (368 echte Rufe, 26 Timer-Sätze), 282 Negative (169 Auto, davon
103 aus Runde 5; 92 hart; 21 mit Ohr-Herkunft, darunter 8 Geräusch). Die
Qualitätsregel nahm 76 von 444 echten Rufen der Trainings-Tage heraus
(`val/positive_zweifel`), 27 davon Ohr-Rufe. 151 Clips, die seit Runde 5 an
deren Val-Tagen dazukamen, gehen absichtlich nirgendwohin.

`wake_paket messen`, alle Modelle mit dem heutigen Manifest (Gate gleich,
nur das Modell verschieden); v2 aus `daf5518^`, v3 aus `16ae2ee^`, v4 aus
`42797ef^`:

| Gruppe | n | v2 | v3 | v4 | v5 |
|---|---|---|---|---|---|
| Vorrunde: echte Rufe | 130 | 62 | 73 | 113 | 116 |
| Vorrunde: Fehltrigger + auto | 21 + 25 | 7 + 2 | 12 + 4 | 12 + 10 | 10 + 6 |
| Vorrunde: Studio | 20 | 10 | 12 | 14 | 19 |
| Vorrunde: flüssige Sätze | 27 | 2 | 3 | 5 | 10 |
| neu: echte Rufe | 94 | 30 | 41 | 61 | 82 |
| neu: Fehltrigger hart | 20 | 8 | 9 | 14 | 20 |
| neu: Geräusch | 4 | 1 | 0 | 2 | 4 |
| neu: auto | 31 | 0 | 2 | 2 | 16 |
| `positive_zweifel` | 76 | 20 | 30 | 44 | 62 |

Die Vorrunden-Zeilen von v4 und v5 stimmen exakt mit dem Ergebnis von
Runde 5 überein, das Werkzeug misst also dasselbe. **Lesart der neuen
Zeilen:** die Clips stammen aus der v5-Zeit und sind damit von v5
ausgewählt (Trigger und Near-Misses von v5). Auf den Negativ-Zeilen sieht
v5 deshalb zwangsläufig schlecht aus, „≤ 20 von 20“ ist als Kriterium
leer, und der Vergleich mit v2–v4 ist dort schief. Für v6 gegen v5 gilt
das nicht: v6 hat diese Tage nicht gesehen und muss genau v5s Fehler
abstellen. Die FP-Seite tragen die Vorrunden-Fehltrigger und FP/h. v3 hatte
Tage der Vorrunden-Val vermutlich im Training (Runde 3, echte Clips bis
16.09.); seine Vorrunden-Zahlen sind teils Selbstmessung.

**Wette v6, falls deployt** (formuliert jetzt): nach ~2 Wochen, gleiche
Zählung wie die v5-Bilanz. (1) Harte Fehltrigger im ruhigen Raum unter
v5s 0,90/Tag. (2) Verlustquote nicht über 44 %. (3) Harte Fehltrigger im
lauten Raum nicht über 1,45/Tag.

### Ergebnis Runde 6 (2026-10-25 17:40) — FP/h verfehlt, kein Deploy

Training im Fablab 17:00–17:40 (Augment 28 min, Training 11 min; `llm`
gestoppt, per `trap` im eigenen Scope wieder gestartet, SSH-Sitzung bis dahin
offen gehalten, weil dort kein Linger gesetzt ist). Runde-5-Clips samt
v5-Modell in `train_out/backup_r5_20261025/`, Kandidat in
`train_out/gaston_v6_tflite/` (`onnx2tf -kat x`, float32). Eingespeist 394
Positive + 282 Negative, je ×10.

| Validierung | v5 | v6 | gepaart | Schranke |
|---|---|---|---|---|
| flüssige Sätze (27) | 10 | **19** | +10 / −1 | ≥ 10 ✓ |
| echte Rufe Vorrunde (130) | 116 | **120** | +6 / −2 | ≥ 114 ✓ |
| Fehltrigger Vorrunde (21 + 25) | 16 | **16** | 2 neu / 2 weg | ≤ 16 ✓ |
| Studio (20) | 19 | **18** | −1 („beiläufig“) | ≥ 18 ✓ |
| FP/h generisch (`eval_debounce.py`, 3-Frame @0,35) | 0,09 | **0,84** | | ≤ 0,5 ✗ |
| echte Rufe neu (94) | 82 | **80** | +3 / −5 | ≥ 80 ✓ |
| Fehltrigger neu (20) | 20 | **15** | 0 neu / 5 weg | ≤ 20 ✓ |
| Geräusch neu (4) | 4 | **2** | 0 neu / 2 weg | ≤ 2 ✓ |
| `positive_zweifel` (76, nicht Schranke) | 62 | 65 | +5 / −2 | |

Sieben von acht erfüllt, die FP/h nicht: auf dem generischen Fremd-Audio
löst v6 rund neunmal so oft aus wie v5 (bei jeder Schwelle 0,25–0,5
zwischen 0,56 und 1,03/h, v5 durchgehend 0,09). Die Trainingsmetrik hatte
es angekündigt (intern FP/h 3,81 gegen 0,97 in Runde 5 und 0,53 in Runde 4;
Recall 0,17 gegen 0,20 und 0,24). Nach der Regel: kein Deploy, Kandidat
daneben, Entscheidung bei Jochen. v5 läuft unverändert.

Ursache offen. Nicht unterschieden: Streuung des Trainings (ein Lauf je
Runde, nie wiederholt) gegen eine Wirkung der neuen Auswahl (Qualitätsregel
nimmt 76 schwierige Positive heraus, 66 neue Auto-Negative).

### Wiederholung Runde 6 (2026-10-25 20:50) — Streuung groß, kein Lauf besteht

Drei weitere Läufe nur mit `--train_model` auf denselben Features (train.py
setzt keinen Seed). Schranke wie oben, Live-Gate, FP/h 3-Frame @0,35:

| | v5 | L1 (v6) | L2 | L3 | L4 | Schranke |
|---|---|---|---|---|---|---|
| FP/h generisch | 0,09 | 0,84 | 0,56 | 0,56 | 0,37 | ≤ 0,5 |
| interne FP/h | 0,97 | 3,81 | 3,10 | 3,27 | 1,77 | – |
| echte Rufe Vorrunde /130 | 116 | 120 | 119 | 119 | 119 | ≥ 114 |
| FT Vorrunde /46 | 16 | 16 | 17 | 17 | 15 | ≤ 16 |
| Studio /20 | 19 | 18 | 17 | 20 | 15 | ≥ 18 |
| flüssige Sätze /27 | 10 | 19 | 16 | 19 | 11 | ≥ 10 |
| echte Rufe neu /94 | 82 | 80 | 86 | 82 | 80 | ≥ 80 |
| FT neu /20 | 20 | 15 | 14 | 12 | 12 | ≤ 20 |
| Geräusch /4 | 4 | 2 | 2 | 2 | 2 | ≤ 2 |

Kein Lauf erfüllt alles: L4 scheitert am Studio (15), L2 an FT und Studio,
L3 an FP/h und FT. Die Streuung allein eines Laufs ist größer als die meisten
Abstände, auf die die Schranke prüft (Studio 15–20, flüssige Sätze 11–19,
FP/h 0,37–0,84). Alle vier liegen bei FP/h über v5 — für Runde 5 ist die
Streuung nie gemessen, ob v5 ein Glückslauf war, ist offen. Konsequenz: ein
Einzellauf ist keine Messung. Weiter so lange Läufe würfeln, bis einer
besteht, wäre Auswahl am Val-Set (die Schranke würde zum Trainingskriterium).

### Wiederholung Runde 5 (2026-10-25 21:55) — v5 war kein Glückslauf

Gleicher Versuch mit den gesicherten R5-Features (`backup_r5_20261025`),
3× `--train_model`. FP/h 3-Frame @0,35:

| | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| Runde 5 | 0,09 (v5) | 0,19 | 0,09 | 0,00 |
| Runde 6 | 0,84 | 0,56 | 0,56 | 0,37 |

Interne FP/h Runde 5: 0,97 / 2,12 / 1,24 / 0,53, Runde 6: 3,81 / 3,10 /
3,27 / 1,77. Die Reihen überlappen nicht: der Anstieg kommt aus der Auswahl
von Runde 6, nicht aus der Streuung. Verdacht zuerst auf die Qualitätsregel
(76 schwierige Rufe nicht trainiert, das Modell zieht die Grenze weiter);
die 66 neuen Auto-Negative sollten FP/h eher senken. Nächster Versuch:
Runde 6 ohne Qualitätsregel (die 76 aus `val/positive_zweifel` ×10 in
positive_train), sonst unverändert.

Haus-Clips der Runde-5-Läufe (Paket R6, Live-Gate):

| | v5 (L1) | L2 | L3 | L4 |
|---|---|---|---|---|
| FP/h | 0,09 | 0,19 | 0,09 | 0,00 |
| echte Rufe Vorrunde /130 | 116 | 120 | 112 | 104 |
| FT Vorrunde /46 | 16 | 18 | 14 | 14 |
| Studio /20 | 19 | 20 | 17 | 16 |
| flüssige Sätze /27 | 10 | 22 | 9 | 9 |
| echte Rufe neu /94 | 82 | 87 | 77 | 70 |
| FT neu /20 | 20 | 18 | 19 | 19 |
| Geräusch /4 | 4 | 4 | 2 | 2 |

Auch mit den alten Daten wandert jeder Lauf auf einer Linie zwischen
„hört mehr“ (L2: 120 Rufe, 22 Sätze) und „ist strenger“ (L4: 104 Rufe,
FP/h 0). v5 liegt in der Mitte. Runde 6 hält dagegen die Rufe stabil bei
119–120 und senkt die neuen Fehltrigger (12–15 statt 18–20) — bezahlt mit FP/h
auf Fremd-Audio. Die Geräusch-Zahl (2 von 4) schafft auch Runde 5 in zwei von
vier Läufen, sie ist bei n=4 kein Beleg für die neuen Daten.

### Runde 6 ohne Qualitätsregel (2026-10-25 23:19) — nicht die Ursache

Die 76 Zweifel-Rufe ×10 zusätzlich in positive_train, augment neu, 3×
`--train_model`. FP/h 3-Frame @0,35: 0,47 / 0,84 / 0,37 (interne FP/h
4,16 / 7,08 / 4,96). Gleicher Bereich wie Runde 6 mit Regel (0,37–0,84),
klar über Runde 5 (0,00–0,19). Die Qualitätsregel erklärt den Anstieg also
nicht. Was Runde 6 sonst von Runde 5 unterscheidet: die neuen
Trainings-Positiven der Tage ab 11.10., die 66 neuen Auto-Negative, die
weggelassenen Auto-Positive der Vorrunde.

Haus-Clips ohne Qualitätsregel (3 Läufe, `val/positive_zweifel` ist hier
trainiert und zählt nicht): echte Rufe Vorrunde 126–127/130, neu 90–93/94,
flüssige Sätze 16–24, Studio 18–19, FT Vorrunde 20–21/46, FT neu 16/20,
Geräusch 1–2/4. Die Zweifel-Rufe bringen also viel Recall und kosten etwa
fünf Fehltrigger auf den alten Tagen; an FP/h ändern sie nichts.

### Nur sichere Labels (2026-10-26 00:28) — FP/h weg, Recall auch

Jochens Frage: steckt Fehlerhaftes aus frühen Runden „immer noch drin“?
Antwort vorab: in den Gewichten nicht — `train.py` lädt kein Vorgänger-Modell,
jede Runde startet zufällig und trainiert auf 30k synthetisch + den echten
Clips des aktuellen Pakets (3 940 = 394 × 10). In den Daten schon: jedes Paket
übernimmt den Train-Teil der Vorrunde. Herkunft der 394 Train-Positiven von
Runde 6: 111 Ohr, 257 Selbst (aus Handlungen: wiederholt nach Near-Miss,
Befehl ausgeführt), 26 Timer. Negative: 78 Ohr, 35 Selbst (Stopp-Abbruch),
169 Auto (nur STT, kein Gaston gehört — dieselbe STT, die 41 von 53 echten
Rufen „Rauschen“ nannte).

Versuch: nur Ohr-Positive + Timer (137) und Ohr-Negative (78), je ×10, 30k
synthetisch unverändert, augment einmal, 3× `--train_model`. Nulllinie das
rein synthetische Modell vom 05.07. (in den Tabellen „v2“, Git `1702b34`):

| | synth. (v2) | nur sicher L1–L3 | Runde 6 | v5 |
|---|---|---|---|---|
| FP/h @0,35 | 0,00 | 0,00 / 0,00 / 0,00 | 0,37–0,84 | 0,09 |
| echte Rufe Vorrunde /130 | 62 | 81 / 89 / 82 | 119–120 | 116 |
| echte Rufe neu /94 | 30 | 46 / 45 / 48 | 80–86 | 82 |
| Studio /20 | 10 | 12 / 11 / 10 | 15–20 | 19 |
| flüssige Sätze /27 | 2 | 4 / 5 / 6 | 11–19 | 10 |
| FT Vorrunde /46 | 9 | 11 / 9 / 13 | 15–17 | 16 |
| FT neu /20 | 8 | 10 / 10 / 7 | 12–15 | 20 |
| Geräusch /4 | 1 | 1 / 1 / 1 | 2 | 4 |

Lesart: kein sauberer Beleg für „schlechte Labels“. Weniger echte Positive
ergeben ein strengeres Modell — es liegt auf derselben Linie Recall gegen
Fehlauslöser wie alle anderen, nur weit am strengen Ende (zwischen synth.
und v4). Die 257 Selbst-Positiven bringen den Großteil des Recalls. Was FP/h
treibt, ist damit eingegrenzt, aber nicht getrennt: Selbst-Positive,
Auto-Negative oder schlicht die Menge. Trennen ginge mit zwei Läufen „sicher
+ Selbst-Positive“ und „sicher + Auto-Negative“.
