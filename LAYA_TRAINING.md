# Laya-Training für den Aktuator — Ablauf, Fallen, Vorlage für die Automatisierung

Laya beantwortet für den Aktuator Tor, Ziel und Aktion eines Satzes
(`voice_assistant/services/laya_intent.py`). Das Modell ist auf die Ziele
**eines** Hauses trainiert und muss neu trainiert werden, sobald sich diese
Ziele ändern. Das kommt oft genug vor, dass der Lauf vollständig im Code
stehen muss. Heute ist er eine Folge von Handgriffen.

Diese Datei hält fest, was der erste Durchlauf (2026-09-28/29) dafür
gebraucht hat und was dabei schiefging. Sie ist die Vorlage für die
Automatisierung, keine Anleitung zum Abtippen. Jede Falle unten hat einen
Abschnitt „Was die Automatisierung tun muss“. Wo eine Falle schon im Code
abgefangen ist, steht das dabei.

> **Zum Weitermachen: erst „Stand“ ganz unten lesen.**

## Wann neu trainiert werden muss

| Auslöser | Warum | Heute erkannt? |
|---|---|---|
| capabilities-Version ändert sich (Ziel neu, umbenannt, entfernt, `namen` ergänzt) | Die ziel-Frage entsteht aus dem Digest. Ein Checkpoint kennt nur die Optionen, auf die er trainiert wurde; eine neue Option hat er nie gesehen | **Ja, seit 2026-10-06.** `/health` des Containers trägt `checkpoint.capabilities`; der Assistent meldet eine Abweichung, `tools/laya_nachtraining.py` trainiert nachts neu (siehe Falle 11) |
| Neue gelabelte echte Turns (Test-Set wächst) | Lücken der Vorlagen schließen (siehe „Synthetische Lücken“) | Nein |
| Neue Laya-Version | Basismodell, Sequenzbau oder Temperaturbehandlung können sich ändern | Nein, gepinnt auf 0.3.21 |

## Die Kette heute, Schritt für Schritt

```
1. tools/tor_trainset.py            (ow-venv)   capabilities + MASSIVE -> testsets/tor_train.jsonl
                                                 + testsets/tor_train.capabilities.json
2. git commit in testsets/          (privat)    Daten und Schnappschuss versionieren
3. VRAM frei machen                 (Hand)      2026-09-28: llamacpp-gemma gestoppt
4. tools/laya_aktuator_train.py     (Laya-venv) -> ~/laya-modelle/aktuator-vN
5. LAYA_CKPT in compose anpassen    (Hand)      openclaw-voice-stack
6. podman-compose up -d --force-recreate laya
7. tools/aktuator_vergleich.py      (ow-venv)   Test-Set: Gemma gegen Laya
8. Zahlen in die Docstrings         (Hand)      Messreihen
```

Zwei getrennte Umgebungen sind Absicht: torch gehört nicht in den
Sprachassistenten. `ow-venv` erzeugt die Daten und misst. Ein eigener venv
mit torch und Laya trainiert. Der Container bedient im Betrieb. Die drei
Fragen stehen in `laya_intent.py`, das nur die stdlib braucht. Training und
Betrieb importieren dieselbe Datei, damit beide garantiert dasselbe fragen.

## Was schiefging — und was die Automatisierung tun muss

### 1. torch: die CPU-Fassung bleibt still liegen

- **Symptom:** `torch.cuda.is_available()` ist `False`, obwohl die
  CUDA-Fassung installiert wurde.
- **Ursache:** Zwei Dinge zusammen.
  - pip hält ein installiertes `2.14.0+cpu` für eine Erfüllung von
    `torch==2.14.0` und tauscht nichts aus.
  - Der cu128-Index führt torch nur bis 2.11.
- **Lösung:** `--force-reinstall "torch==2.14.0+cu130"` gegen den
  cu130-Index (Treiber 580 kann CUDA 13).
- **Automatisierung:** Den venv aus einer festen Anforderungsliste mit
  vollständiger Versionsangabe (`+cu130`) bauen. Danach prüfen, dass
  `torch.cuda.is_available()` wahr ist, und sonst abbrechen. Ohne GPU
  rechnet das Training stundenlang auf der CPU und meldet keinen Fehler.

### 2. Laya ändert sich täglich

- 0.3.7 war am 23.09. aktuell und fünf Tage später nicht mehr auf PyPI;
  dazwischen lagen 14 Versionen.
- Die Version ist gepinnt (`laya[serve]==0.3.21`), im Trainings-venv wie
  im Container.
- **Automatisierung:** Den Pin nie automatisch heben. Eine neue Version ist
  ein eigener Lauf mit eigenem Vergleich, weil Sequenzbau und Temperaturen
  sich zwischen Versionen geändert haben (siehe Laya-README „Calibration“).
  Das Basismodell kommt vom Hub (`convaiinnovations/laya`, Unterordner
  `multilingual`). Die Revision muss mit in den Checkpoint
  (`--revision`, heute nicht gespeichert).

### 3. Der Speicher reicht nur knapp — und die Grenze wandert mit der Zielliste

- **Lage:** Auf der 3060 Ti (8 GB) belegen Speaches 2,4 GB und ser 1,7 GB
  fest. Frei sind knapp 4 GB, und auch die erst, seit `llamacpp-gemma`
  gestoppt ist.
- **Zwei OOM-Abbrüche:**
  - bei 8.192 Token je Batch;
  - bei 2.048 Token, erst nach 1.000 Schritten. Ein Batch mit zwei langen
    ziel-Sequenzen hat ihn ausgelöst.
- **Was am Ende trug** (Spitze 2,93 GB):
  - Die Einbettungstabelle ist eingefroren. Sie umfasst 197 der 322
    Mio. Parameter.
  - Sie liegt in bf16 statt fp32 und spart damit ~400 MB.
  - Batches werden nach Tokenbudget (1.536) geschnitten, nicht nach Anzahl.
- **Warum die Grenze wandert:** Eine ziel-Sequenz ist lang, weil sie alle
  Optionen trägt (68 Ziele → `head_max_len` 896). Sie wächst mit jedem
  Ziel. Eine Einstellung, die heute passt, kann mit zehn neuen Zielen
  wieder platzen.
- **Automatisierung:**
  - Vor dem Start freien VRAM messen und das Tokenbudget daraus ableiten,
    nicht fest vorgeben.
  - Bei OOM mit halbem Budget neu starten statt aufgeben.
  - Den Lauf nie über den Platz der laufenden Dienste stellen. **Und
    „Speaches bleibt an“ reicht nicht** — siehe Falle 12: Speaches braucht
    über seine ruhenden 2,4 GB hinaus Arbeitsspeicher für jede Anfrage.
  - Dass der Laya-Container selbst 1,4 GB belegt, gilt es zu beachten: Läuft
    er während des Trainings, fehlen diese 1,4 GB. Entweder vorher stoppen
    (der Schatten fällt dann kurz aus, das ist harmlos) oder auf eine Zeit
    ohne Turns legen.

### 4. Der Container antwortete auf jede Anfrage mit HTTP 500

- **Ursache:** torch 2.14 leitet ein `bmm` in der RoPE von ModernBERT auf
  einen Triton-Kernel um. Triton baut beim ersten Aufruf ein Hilfsmodul mit
  dem C-Compiler, und das schlanke Image hatte keinen.
- **Warum es im Training nicht auffiel:** Auf dem Host ist gcc da.
- **Lösung:** gcc im Image (`openclaw-voice-stack/laya/Dockerfile`).
- **Automatisierung:** Nach jedem Deploy einen Rauchtest mit Sätzen, deren
  Antwort feststeht. „Mach das Küchenlicht an“ muss ein bestimmtes Ziel
  liefern, „Trag Gemüsesuppe in die Essensliste ein“ muss nein sein. Ein
  `/health` reicht nicht: Der meldete die ganze Zeit `ok`.

### 5. Ein neues Image ersetzt den laufenden Container nicht

- `podman-compose up -d laya` nach einem `build` lässt den alten Container
  laufen.
- **Lösung:** `--force-recreate`.
- **Automatisierung:** Danach prüfen, ob die Image-ID des Containers der
  frisch gebauten entspricht.

### 6. Die erste Anfrage dauert über eine Sekunde

- Das ist das Triton-Kompilat aus Falle 4.
- Der Assistent wärmt beim Start vor (`aktuator_schatten.aufwaermen`).
  Nach einem Container-Neustart ohne Assistenten-Neustart fehlt das.
- **Automatisierung:** Nach dem Deploy selbst eine Anfrage schicken (das
  erledigt der Rauchtest gleich mit).

### 7. Ein Ausfall sah aus wie ein Ergebnis

- **Was passiert war:** Der erste Vergleichslauf zählte 330 HTTP-500-Antworten
  als „226 richtig / 104 verpasst“. Jeder Satz, der nicht schalten sollte,
  war per Ausfall „richtig nicht geschaltet“.
- **Heute:** `aktuator_vergleich.py` führt Ausfälle als eigene Klasse und
  warnt.
- **Automatisierung:** Jede Auswertung bricht ab bzw. markiert sich als
  ungültig, sobald ein Ausfall darin steht. Nie eine Zahl veröffentlichen,
  in der Ausfälle stecken.

### 8. Die choice-Temperatur ist 1,000 — gefittet wurde nichts

- **Ursache:** Die zurückgehaltenen synthetischen Sätze waren zu 100 %
  richtig, also hatte der Temperatur-Fit nichts zu tun. Für noul ergab sich
  2,4.
- **Folge:** Die P-Werte für Ziel und Aktion sind vermutlich zu
  selbstsicher. Im Schatten-Log stehen fast überall 1,0.
- **Automatisierung:** Die Temperatur auf **echten** gelabelten Turns
  fitten, nicht auf synthetischen. Diese Turns dürfen dann nicht mehr ins
  Test-Set, deshalb braucht es eine dritte Menge (Kalibrierung) neben
  Training und Test.

### 9. Die Zahlen auf dem Test-Set sind optimistisch

- Die Vorlagen des Generators sind entstanden, nachdem die 334 Testsätze von
  Hand gelesen waren. Anreden, Essensliste und Arbeitszeiten stammen aus
  denselben Logs.
- Wörtliche Dubletten werden entfernt (`tor_trainset.py`, normalisierter
  Vergleich). Der abgeschaute Stil bleibt trotzdem drin.
- **Automatisierung:**
  - Ein Checkpoint wird an Turns gemessen, die **nach** seinem Trainingstag
    gesprochen wurden. Das sind die Schatten-Turns, und dafür gibt es sie.
  - Das Test-Set gilt nur als Regressionsschranke ("nicht schlechter als
    vorher"), nicht als Beleg.

### 10. Synthetische Lücken: getrennte und verhörte Namen

- **Symptom:** Getrennt geschriebene Raumnamen („… Zimmer Rollo“),
  „Wohnzimmerverlauf“ und im Schatten „Lohnsimmerrolle“ ergeben
  `ziel: keins`. Das Tor sagt dabei sicher ja
  (P 0,99).
- **Ursache:** Die Vorlagen kennen nur die `namen` aus den capabilities, und
  das sind fast immer Komposita. Die STT liefert aber getrennte und verhörte
  Formen.
- **Automatisierung**, zwei Hebel:
  - **Der Generator zerlegt Komposita** (Wohnzimmerrollo → Wohnzimmer
    Rollo, Wohnzimmer-Rollo) und baut STT-artige Verhörer ein. Seit
    2026-10-02 (Anlass „Gaston macht alle Wolos auf.“): `_VERHOERER` in
    `tor_trainset.py` tauscht in rund 20 % der Sätze das Gerätewort gegen
    eine **in den Logs belegte** Form (Rollo → Roller, Rolle, Wolle, Wallo,
    Rolli, Rolo; Rollos → Roller, Rollen, Rohlos, Wolos, Rolos; Licht →
    lich nur im Kompositum), bei Befehlen und schweren Negativen gleich oft.
    `--verhoert 0` erzeugt Byte für Byte die Daten von vorher. Verhörte
    Raumnamen („Lohnsimmer“) bleiben offen. Noch nicht trainiert.
  - **Ausgeführte echte Turns** (aus `actuator_turns.log`, nach Urteil von
    Hand) **gehen als Trainingsdaten ein.** Dann muss die Trennung von
    Training und Test über die Zeit laufen (Punkt 9), nicht über die
    Herkunft.

### 11. Der Checkpoint hängt an einer Zielliste — und niemand prüft das

**Am 2026-10-01 um 19:15 eingetreten:** ein neues Ziel kam dazu
(capabilities `f07c67d0` → `e93fcc67`). Seitdem fragt der Schatten mit 70
Optionen, eine davon hat aktuator-v1 nie gesehen. Auf dem Test-Set fiel v1
von 313/16/1 auf 310/19/1 (richtig/verpasst/FALSCH), ohne dass sich am
Modell etwas geändert hätte. Im Journal steht davon nichts.

- Die ziel-Frage baut das Training aus dem Schnappschuss
  `testsets/tor_train.capabilities.json`, der Betrieb aus dem Live-Digest.
- Ändern sich die Ziele, fragt der Schatten mit Optionen, die das Modell nie
  gesehen hat. Er antwortet weiter, nur schlechter, und kein Fehler weist
  darauf hin.
- **Automatisierung:** Der Assistent vergleicht beim Start und bei jedem
  `refresh()` `capabilities` des Checkpoints (über `/health` oder eine
  eigene Abfrage) mit der Live-Version. Weichen sie ab, meldet er das und
  startet den Trainingslauf, bzw. stößt ihn an.
- **Gebaut 2026-10-06:**
  - `laya/serve.py` (openclaw-voice-stack) hängt `checkpoint` {name,
    capabilities, seed} an `/health`.
  - `aktuator_schatten.pruefe_checkpoint` vergleicht nach dem Aufwärmen und
    nach jedem `refresh()` aus MQTT/Poll (`Actuator.nach_refresh`). Eine
    Abweichung steht im Journal und geht einmal je (Checkpoint, Live-Version)
    an die Argus-Gruppe. Tests: `CheckpointAbgleichTest`.
  - `tools/laya_nachtraining.py` + `systemd/laya-nachtraining.{service,timer}`,
    jede Nacht 3:00, endet sofort, wenn die Versionen passen. Ablauf und
    Schranken im Docstring; Entscheidungen Jochen 2026-10-06: hier trainieren
    (laya und Qwen aus, Gemma und Parakeet springen ein), automatisch
    umschalten mit Schranken. Installationsspezifisches in
    `~/.config/openclaw/laya-nachtraining.env`. Ergebnisse je Lauf:
    `~/.openclaw/workspace/laya_nachtraining.jsonl`, Bericht leise in die
    Argus-Gruppe.

### 12. Das Training hat Speaches den Speicher weggenommen

- **Was passiert war (2026-10-01, 19:0x–19:3x):** Während das Training
  lief, antwortete Speaches mit `CUDA failed with error out of memory`. Der
  Assistent fiel auf den lokalen faster-whisper (CPU) zurück. Ein echter
  Ruf („Gaston macht mir den Wohnzimmer Rollo zu“) ging dadurch langsamer
  durch, aber richtig. Auch eine Auswertung, die nebenher die STT brauchte,
  scheiterte.
- **Ursache:** Speaches belegt im Ruhezustand 2,4 GB, braucht aber bei jeder
  Transkription mehr. Das Training (Spitze 2,9 GB) und der
  Speicher-Cache von torch nahmen genau diesen Spielraum weg. Gesamt lag
  bei 7,6 von 8 GB.
- **Was es nicht ist:** kein Absturz. Der Rückfall ist vorgesehen und hat
  gegriffen. Nur langsam.
- **Automatisierung**, eine von beiden:
  - Den Anteil des Trainings hart begrenzen
    (`torch.cuda.set_per_process_memory_fraction`), sodass für Speaches
    mindestens ~1 GB über seinem Ruhewert frei bleibt.
  - Oder in einem Zeitfenster ohne Turns trainieren (nachts) und dann
    gern auch Dienste stoppen, die gerade niemand braucht (laya, ser).
  - In beiden Fällen nach dem Lauf eine Probe-Transkription gegen Speaches,
    bevor „fertig“ gemeldet wird.

### 13. Kleinere Dinge, die man wissen muss

- **MASSIVE** (de-DE, CC BY 4.0) liegt nicht im Repo. Quelle:
  `amazon-massive-nlu-dataset.s3.amazonaws.com/amazon-massive-dataset-1.1.tar.gz`.
  Die Zuordnung der Intents (welche als ja, welche ausgelassen) steht im
  Docstring von `tor_trainset.py`. Sie ist eine Entscheidung, keine
  Ableitung.
- **noul-Labels** (Laya-Issue #156): Mit `false:`/`true:` kann das Modell am
  Wortpaar hängen statt am Satz. Trainiert ist die schlichte Frage ohne
  eigene Labels. Ob #156 nach dem Fine-Tuning noch eine Rolle spielt, ist
  nicht gemessen.
- **Optionsreihenfolge** wird im Training je Satz gemischt, im Betrieb steht
  sie in Digest-Reihenfolge. Ohne das Mischen lernt das Modell Positionen.
- **Zurückgehalten wird je Satz**, nicht je Frage. Sonst sieht das Training
  die tor-Frage eines Satzes, dessen ziel-Frage die Temperatur fittet.
- **Die Daten sind privat:**
  - `testsets/` hat ein eigenes Git ohne Remote.
  - Checkpoints liegen unter `~/laya-modelle/`.
  - Beides trägt die Gerätenamen des Hauses und gehört weder ins
    öffentliche Repo noch ins Image.
- **Dauer:** Daten ~10 s, Training 7,5 min (2 Epochen, 3.778 Schritte),
  Image-Bau ohne Cache mehrere Minuten (6 GB, fast alles CUDA).

### 14. Ein Container-Start aus der Unit stirbt mit der Unit

Erster erzwungener Probelauf (2026-10-06 03:05, `--erzwingen`, transiente
Unit): Training und Messung liefen durch (v4 351/18/2, v5a 345/21/5, v5b
345/22/4 — korrekt **nicht** umgeschaltet). Danach starb aber, was die Unit
gestartet hatte: rootless Podman lässt die Port-Weiterleitung
(`rootlessport`) im cgroup des Aufrufers, die Unit lief beim Beenden in den
Timeout, und systemd tötete per SIGKILL conmon und Weiterleitung von Laya
und Qwen mit. Die Container liefen innen gesund weiter, 8094/8096 waren vom
Host aus bis 18:50 tot, jeder Turn lief über Parakeet und Gemma — gemerkt
hat es niemand. Seither startet das Werkzeug Container über
`voice_assistant/services/container.py` (eigener Scope), und der
Dienst-Wächter (`dienstwaechter:`) meldet und heilt einen solchen Ausfall.

## Was die Automatisierung als Ganzes leisten muss

1. **Auslöser erkennen** (capabilities-Version ungleich der des
   Checkpoints). Nicht bei jeder Änderung sofort, sondern gesammelt:
   capabilities ändern sich oft in Schüben.
2. **Daten erzeugen** und mit Schnappschuss im privaten Git versionieren.
3. **In ein neues Verzeichnis trainieren** (`aktuator-vN`). Nie über den
   laufenden Checkpoint.
4. **Schranken vor dem Umschalten:**
   - Rauchtest grün.
   - Test-Set: nicht mehr FALSCH als der laufende Checkpoint.
   - Keine Ausfälle.
   - Die capabilities-Version stimmt mit der Live-Version überein.
5. **Umschalten:** `LAYA_CKPT` auf das neue Verzeichnis, `--force-recreate`,
   Image-ID prüfen, aufwärmen, Rauchtest wiederholen.
6. **Alten Checkpoint behalten** fürs Zurückschalten.
7. **Messreihe fortschreiben:** die Zahlen maschinell an einen Ort, nicht
   von Hand in Docstrings.

Offene Entscheidungen dafür (nicht von der Automatisierung zu treffen):

- Gehen echte Turns ins Training (Punkt 10)?
- Welche Turns werden zur Kalibrierungsmenge (Punkt 8)?
- Wo und wann läuft das Training (GPU-Platz, Punkt 3)?
- Soll „Tor ja, aber ziel keins“ eine Rückfrage auslösen statt den Brain?
  Heute geht es an den Brain, wie Gemmas Regel „Rollo ohne Raum“. Siehe
  Schatten-Turn „Lohnsimmerrolle“ unten: Gemma fragte zurück, und das war
  dort der bessere Weg. Gemessen mit aktuator-v1 auf dem Test-Set
  (2026-10-01): Diese Regel würde bei 15 Befehlen ohne bestimmbares Ziel
  („Rollo zu“), bei 5 verhörten Befehlen mit Ziel und bei 3
  Nicht-Befehlen (Kauderwelsch) nachfragen.

## Stand

**2026-10-01.** Checkpoint `aktuator-v1` (capabilities `f07c67d0`) läuft
seit 2026-09-29 00:37 im Schatten. Nichts davon ist automatisiert.

Erster Schattenvergleich (`tools/aktuator_vergleich.py --schatten`):

- **Turns:** 15 in gut zwei Tagen, 14 gleich ausgegangen.
- **Kommandos:** Alle 11 ausgeführten haben beide identisch entschieden,
  inklusive Wert. Falsch geschaltet hat keiner.
- **Latenz je Kommando:** Gemma (Tor + Klassifikation, Vega) median 1,8 s,
  max 2,0 s. Laya median 92 ms, max 104 ms. Das ist etwa Faktor 20.
- **Einzige Abweichung:** „Gastau, Lohnsimmerrolle auf 50 Prozent.“ Gemma
  fragte zurück, Laya ging an den Brain (Tor 0,99, ziel keins). Der
  Sprecher wiederholte eine Minute später, beide schalteten richtig. Das ist
  Lücke 10 und die offene Entscheidung zur Rückfrage.
- **Beide verpasst:** „Gas doch abendlich aus!“ (Abendlicht aus). Laya
  sagte Tor 0,99, ziel keins. Gemma sagte Tor 0,0.

15 Turns sind zu wenig, um zu entscheiden. Weiter sammeln.

**Testlauf „vereinte Frage“ (2026-10-01).** Statt Tor + Ziel eine einzige
Auswahlfrage über alle Ziele plus „keins“ (Befehl, Gerät unklar) und
„kein_befehl“ (`laya_intent.VARIANTEN`, `--variante vereint`, Checkpoint
`aktuator-v2-vereint`, gleiche Daten). Training 24 min statt 7,5, weil
jeder Satz die lange ziel-Sequenz trägt. Beide gegen capabilities
`e93fcc67` gemessen:

| | richtig / verpasst / FALSCH | Tor-AUROC | Befehle durch bei 0 / 1 / 3 FALSCH |
|---|---|---|---|
| v1 getrennt | 310 / 19 / 1 | 0,989 | 103 / 106 / 114 |
| v2 vereint | 308 / 20 / 2 | 0,983 | 77 / 81 / 108 |

- **Nicht besser.** Die Schaltabsicht trennt v2 etwas schlechter. Mehrere
  verhörte Befehle („Esstischrohlos“, „Wohnzimmerverlauf“), die v1 noch
  als Rückfrage erkannte, hält v2 für „kein Befehl“. Neu falsch:
  „Gastostop.“ → `rollostop`.
- **Aber eins kann v2:** Es trennt „Gerät unklar“ von „kein Befehl“
  sauber (206 von 209 Nicht-Befehlen sagen `kein_befehl`, 12 von 17
  Befehlen ohne Ziel `keins`). Das ist die Information, die die
  Rückfrage-Entscheidung braucht.
- **Entscheidung:** bei „getrennt“ bleiben. Ein einzelner Lauf je Variante;
  Unterschiede von 2–3 Sätzen liegen im Rauschen, der Abstand bei „0
  FALSCH“ (103 gegen 77) nicht.

**aktuator-v3 (2026-10-01 abends).** Neu trainiert für capabilities
`e93fcc67` (neues Ziel `fernsehelektronik`), mit getrennten Schreibvarianten
der Namen im Generator (Nr. 10). Training mit gestopptem `laya` und `ser` —
Speaches lief diesmal ungestört mit. Gleiche Daten sonst, 11 min.

| Test-Set, ohne Rückfrage-Regel | richtig / verpasst / FALSCH |
|---|---|
| v1 (kennt das neue Ziel nicht) | 310 / 19 / 1 |
| v3 | 306 / 20 / 4 |
| v3 + „setzen ohne Zahl → Rückfrage“ | 306 / 21 / 3 |
| Gemma (live) | 315 / 11 / 4 |

- Die getrennten Namen wirken: beide „rosa Zimmer Rollo“ jetzt richtig, das
  neue Ziel wird erkannt („Zeug hinterm Fernseher“).
- Dafür verloren: vier verhörte Formen, die v1 noch traf („Wohnzimmerwallo“,
  „Türboden“, „schau dir das Abendlicht an“). Ob das Rauschen zwischen zwei
  Läufen ist oder ein echter Tausch, zeigt erst ein zweiter Lauf mit anderem
  Seed — **die Streuung zwischen Läufen ist bisher nicht gemessen**, und
  Unterschiede von 3–4 Sätzen sind ohne sie nicht deutbar.
- Von den 3 FALSCH ist einer vermutlich ein Label-Fehler („Rollos überall
  auf“ → `rollos_ganzes_haus`, dessen Name genau so lautet), einer teilt v1
  („Mondzimmer“), einer ist geraten („Tyrolo“ → `tuerrollo`).
- `setzen` ohne Zahl im Satz war bei Laya ausführbar — jetzt Rückfrage
  (`laya_intent.als_intent`).

**Rollentausch geschaltet: 2026-10-01 20:50** (Jochen: „go zum tausch“).
Laya v3 entscheidet, Gemma urteilt im Schatten und springt ein, wenn Laya
ausfällt. Die selbst gesetzte Schranke gegen v1 war nicht bestanden — sie
stützte sich auf Unterschiede von 3–4 Sätzen, und die liegen, wie sich
danach zeigte, im Rauschen (unten). Gegen Gemma (alte Labels): gleich viele FALSCH
(3–4), mehr Verpasste (meist harmlos: Rückfrage oder Brain), ~5x schneller
auf dem Test-Set (72 gegen 379 ms) und ~20x im Betrieb (92 ms gegen 1,8 s).

**Streuung zwischen Läufen gemessen (v3b, 2026-10-01).** Gleiches Rezept,
gleiche Daten, nur `--seed 7` statt 20260928: **9 von 330 Sätzen** gehen
verschieden aus (alte Labels: v3 306/20/4, v3b 308/21/1). Unterschiede
zwischen Modellen von wenigen Sätzen sind ohne mehrere Läufe je Modell nicht
deutbar — eine Automatisierung muss das berücksichtigen (mehrere Seeds,
oder eine Schranke mit Abstand statt „nicht schlechter“).

**Gemeinsam gelabelt (2026-10-01, `label_von: jochen-2026-10-01`).**
41 neue Sätze aus Schatten-Log, Brain-Turns und den Rückspul-Clips (Rufe,
die das Wakeword verpasst hatte), dazu Korrekturen. Zwei Regeln von Jochen:

- **Verhörte Namen: „je nach Ähnlichkeit“.** Ist Raum oder Gerät noch
  erkennbar („Lohnsimmerrolle“, „Wohnzimmerwallo“, „Türboden“, „Tyrolo“,
  „Kickenlicht“, „abendlich“, „Mondzimmer“ = Wohnzimmer), soll geschaltet
  werden; ist er es nicht („Atemgericht“, „Zwiebel-Rolo“), nachgefragt.
  Achtung: das widerspricht der Erwartung in `tools/actuator_grammar_test.py`
  für „Gastau Tyrol(o) auf 40%“ (dort: nicht schalten). Die stammt aus dem
  Vorfall, bei dem Tyrolo zu ALLEN Rollos wurde — Gruppen schützt Regel A
  weiter; der Einzelfall Türrollo ist jetzt erwünscht. Noch nicht angeglichen.
- **Ein Rollostop ist immer unkritisch** („lieber zu früh als zu
  schlecht“). Eine falsch ausgelöste `rollostop`-Routine zählt nicht als
  teurer Fehler.

Mit den neuen Labels (370 Sätze, mit Rückfrage-Regel):

| | richtig / verpasst / FALSCH | davon 40 Sätze nach dem Training |
|---|---|---|
| v3 (live) | 343 / 26 / 1 | 35 / 4 / 1 |
| v3b | 339 / 29 / 2 | 32 / 6 / 2 |
| beide, schalten nur bei Einigkeit | 338 / 32 / **0** | — |

Das eine FALSCH von v3: „Gastau, Lohnsimmerrolle auf 50 Prozent.“ →
**Rosazimmer**rollo statt Wohnzimmer — die Kehrseite von „je nach
Ähnlichkeit“. Jochen: „mit dem 1 falsch kann ich erst mal leben“.

**Idee, nicht gebaut:** zwei Checkpoints (verschiedene Seeds) fragen und nur
bei Einigkeit schalten, sonst nachfragen — auf diesem Set 0 FALSCH für 6
Rückfragen mehr. Kostet einen zweiten Container (+1,4 GB VRAM) und etwas
Latenz.

**Nachtraining für einen STT-Wechsel geprüft (2026-10-05).** Frage: holt
Laya die Lücke von Parakeet (12 statt 7 verpasst) auf, wenn es dessen
Verhörer kennt? Vier Checkpoints für capabilities `70866bd6`, trainiert auf
dem Fablab-Server (2× RTX 5060 Ti, ~13 min je Lauf, Speaches daheim
unberührt): `ref` = heutiges Rezept, `pk` = zusätzlich „rollus“, „rolls“
für „Rollos“ (belegt in Parakeet-Transkripten), je Seed 20260928 (a) und 7
(b). Gemessen über 169 Aufnahmen mit 96 Labels (`tools/stt_vergleich.py
--transkripte … --laya-url`), richtig / verpasst / FALSCH / FALSCH?:

| Transkripte von | v3 (live) | ref-a | ref-b | pk-a | pk-b |
|---|---|---|---|---|---|
| medium (Speaches) | 88/7/1/0 | 87/8/1/0 | 90/6/0/0 | 87/6/3/0 | 90/4/2/0 |
| Parakeet (NeMo) | 83/12/0/1 | 83/12/0/1 | 84/10/0/2 | 82/10/1/3 | 84/10/1/1 |
| Parakeet (ONNX, Speaches) | 83/11/1/1 | 80/13/1/2 | 83/11/0/2 | 80/11/2/3 | 82/12/1/1 |
| Qwen3-ASR-1.7B + Kontext | 92/3/0/1 | 88/6/1/1 | 91/3/0/2 | 89/3/2/2 | 90/3/1/2 |
| Voxtral-Mini-3B | 90/5/0/1 | 88/7/0/1 | 91/4/0/1 | 89/5/0/2 | 91/4/0/1 |

- **Die Verhörer-Ergänzung bringt nichts.** Laya erkennt „alle Rollus zu“
  schon mit v3 richtig als `alle_rollos/zu` — die Rückfrage kommt von
  **Regel A** (Gruppenwort exakt im Satz, `Actuator.verdict`). Dasselbe gilt
  für medium mit „alle Wolos“. pk liegt im Rauschen der Seeds, eher mit mehr
  FALSCH; die Ergänzung ist deshalb nicht übernommen. Wer Verhörer von
  Gruppenwörtern durchlassen will, muss an Regel A (eine Tabelle belegter
  Formen statt Ähnlichkeit) — das ist eine Sicherheitsentscheidung, offen.
- **Seed-Streuung auf diesen 96 Labels:** ref-a gegen ref-b 3 Sätze auf
  medium. Unterschiede dieser Größe zwischen Checkpoints sind nicht deutbar.
- **Neu bei capabilities `70866bd6`:** „Badewasser“/„Brauwasser“ →
  `regenwasser_weiche` (ref-a, pk-a); v3 kennt das Ziel nicht und fällt
  dort nicht hinein. „Kükenarbeitsplanlicht“ → `kuechenlicht` (pk-a, pk-b).
- **v3 ist veraltet** (Falle 11: trainiert für `e93fcc67`). ref-a/ref-b
  lagen als `~/laya-modelle/aktuator-v4-kandidat-{a,b}` bereit.

**aktuator-v4 live seit 2026-10-06 00:10** (Jochen: Kandidat b). ref-b
heißt jetzt `~/laya-modelle/aktuator-v4`, ref-a `aktuator-v4a`. Test-Set
(371 Sätze, mit Rückfrage-Regel, capabilities `70866bd6`):

| | richtig / verpasst / FALSCH |
|---|---|
| v3 (kennt `70866bd6` nicht) | 340 / 28 / 3 |
| v4a (Seed 20260928) | 347 / 21 / 3 |
| **v4** (Seed 7) | **351 / 18 / 2** |

- Label korrigiert (Jochen 2026-10-06, testsets `541961c`): „Mach die
  Rollos bitte wieder überall auf“ ist `alle_rollos`, nicht
  `rollos_ganzes_haus` — „im ganzen Haus“ fehlt im Satz. Das Label vom
  2026-10-01 sagte das Gegenteil; v4 und v4a hatten recht.
- FALSCH v4: „Wohnzimmer Rollo etwas nach unten“ → **auf** (falsche
  Richtung), „Zwiebel-Rolo auf 50 Prozent“ → Rosazimmer (geraten). v3s
  „Lohnsimmerrolle“ → Rosazimmer ist bei v4 weg.
- „Braubwasser“/„Badewasser“ → `regenwasser_weiche` (v3, v4a) bleibt
  FALSCH: Jochen meinte damals ein Gerät, das es in Node-RED nicht gab, nicht
  die Regenwasser-Weiche. Labels unverändert.
- **Gemessen auf der eigenen GPU:** `laya` gestoppt (Gemma entscheidet in
  der Zeit), Kandidat als zweiter Container auf Port **8097** — nicht 8096,
  sonst entscheidet der Assistent live mit dem Kandidaten. 30 s je Lauf.
  Dieselbe Messung auf der CPU lief über 20 min ohne Ergebnis (die
  ziel-Sequenz trägt alle 70 Optionen); das Kopieren zum Fablab-Server
  kroch mit ~1 MB/s. Fürs Training gilt das nicht (Falle 12).
- Rauchtest nach dem Umschalten: Test-Set gegen den Live-Port, 350/18/3
  (altes Label) reproduziert, keine Ausfälle.

**Abgleich und Nachtraining gebaut (2026-10-06, Falle 11).** Live-Container
neu erzeugt (Image mit `checkpoint` in `/health`), Assistent neu gestartet:
„Checkpoint aktuator-v4 passt zu capabilities 70866bd6“. Weil die Versionen
passen, täte der Timer nichts — der Trainingsweg wäre bis zur nächsten
capabilities-Änderung ungeprüft. Deshalb einmalig `laya-nachtraining-probe`
am 2026-10-06 03:05 mit `--erzwingen` (transienter Timer, kein Repo-Stand).
Die Unit läuft mit `HF_HUB_OFFLINE=1`: sonst zöge `snapshot_download` nachts
still eine neue Basis-Revision (Falle 2); im Cache liegt `55cf4c4e`.
