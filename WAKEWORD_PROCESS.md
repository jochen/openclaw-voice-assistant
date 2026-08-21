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
