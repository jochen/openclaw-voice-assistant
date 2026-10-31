# Ein eigenes Wakewort trainieren

*English version: [WAKEWORD_TRAINING.md](WAKEWORD_TRAINING.md)*

Wie das Wakewort dieses Assistenten entstanden ist, so aufgeschrieben, dass
man es für ein eigenes Wort nachmachen kann, in der eigenen Sprache, auf der
eigenen Hardware:

- **Teil 1, das erste Modell.** Rein aus synthetischer Sprache: Piper-Stimmen
  sprechen das Wort tausendfach, dazu ähnlich klingende Wörter als Negative.
  Ein Nachmittag, das meiste davon Downloads und Warten.
- **Teil 2, der Nachtrainings-Kreislauf.** Das laufende Modell verpasst Rufe
  und löst bei Dingen aus, die keine Rufe sind. Beides wird zu gelabelten
  Clips, daraus ein neues Modell, das eine vor dem Training aufgeschriebene
  Schranke schlagen muss. Hier wird ein Modell gut im *eigenen* Raum, und das
  dauert Wochen normalen Betriebs, keinen Nachmittag.

Wer mit einem Coding-Agenten arbeitet (Claude Code oder ein anderer Agent, der
Agent Skills liest): der Skill in `.claude/skills/wakeword-training/` führt
Schritt für Schritt durch diese Anleitung und hält an den Entscheidungen an,
die beim Menschen liegen.

Das Beispiel-Wort ist durchgehend `gaston`, ein deutscher Haushalt. Alles
Wortspezifische steht in zwei YAML-Dateien fürs Training und einem Block im
Manifest des Bundles (siehe
[Die Kreislauf-Werkzeuge an ein anderes Wort anpassen](#die-kreislauf-werkzeuge-an-ein-anderes-wort-anpassen)).
Das Messtagebuch dieser Installation, Runde für Runde mit allen Zahlen, ist
`WAKEWORD_PROCESS.md`; diese Anleitung zitiert daraus nur, wo eine Zahl eine
Regel begründet.

## Begriffe

| Begriff | Bedeutung |
|---|---|
| Trigger | Das Modell hat ausgelöst, ein Turn begann. Audio: `*_wake.wav` (die 3 s bis zum Auslösen), dazu die Folgeaufnahme `*_rec.wav` |
| Near-Miss | Score hoch, aber ein Gate (Frames, Peak, Pegel, Umgebung) hat abgelehnt. Audio: `*_nearmiss.wav`. Hier stecken die verpassten Rufe |
| Label | `echter_ruf`, `rauschen` (= Fehlauslöser oder kein Ruf) oder `unklar` |
| Labelquelle | **Ohr** (`wake_review.jsonl`) vor **Selbst** (aus dem Verhalten, `wake_triage._selbst_labels`) vor **STT** (`wake_triage.jsonl`). Die stärkere Quelle gewinnt immer |
| Korpus | Gelabelte Clips, dauerhaft gesichert (`voice/corpus/`). Das Trigger-Archiv selbst löscht alte Dateien |
| Paket | Ein Trainingsstand: `train/{positive,negative}` und Val-Gruppen, nach ganzen Tagen geteilt (`tools/wake_paket.py`) |
| Rezept | Welche Clips ins Training gehen: `synth` (nur synthetisch), `voll` (alles aus einem Paket) oder eine Liste |
| Lauf | Ein `--train_model` auf den Merkmalen eines Rezepts. train.py setzt keinen Seed, jeder Lauf ist anders |
| Schranke | Die Zahlen, die ein Kandidat erfüllen muss, **vor** dem Training aufgeschrieben und committet |

## Was man braucht

- **Den Assistenten aus diesem Repo**, der Trigger und Near-Misses archiviert
  (von Haus aus an: `~/.openclaw/workspace/voice/triggers/` und
  `wake_events.log`).
- **Einen Rechner mit NVIDIA-GPU** fürs Training, gern derselbe. Hier: 12
  Kerne, 32 GB RAM, zwei GPUs mit je 16 GB; das Modell selbst ist winzig, eine
  kleinere GPU reicht. Training nur auf der CPU ist hier nicht ausprobiert.
- **Rund 25 GB Platz** für die Trainingsumgebung, das meiste davon die
  vorgerechneten Negativ-Merkmale (17 GB).
- **32 GB RAM** sind bequem: ein Training hält das Validierungs-Set als
  Fenster im Speicher, rund 3 GB, zwei gleichzeitig passen.
- **Die eigenen Ohren.** Zweimal muss ein Mensch hinhören: bei den
  synthetischen Stimmen vor dem ersten Modell und bei Clips vor jedem
  Nachtraining. Keins von beidem lässt sich an eine Maschine abgeben; Teil 2
  sagt, warum.

## Teil 1: das erste Modell

### 1.1 Trainingsumgebung einrichten

Auf dem GPU-Rechner, mit diesem Repo (oder nur dem Ordner `wakeword_studio/`):

```bash
TORCH_INDEX=https://download.pytorch.org/whl/cu130 \
  wakeword_studio/gpu_einrichten.sh ~/wakeword-studio
```

`TORCH_INDEX` passend zur CUDA-Version des Treibers wählen (`nvidia-smi`;
`cu126`, `cu128`, `cu130`, …). Das Skript legt einen venv mit den Versionen
aus `wakeword_studio/training_requirements.txt` an, klont openWakeWord und
piper-sample-generator auf den hier benutzten Ständen, lädt die
vorgerechneten Merkmale, Raumhall und Hintergrund-Audio und kopiert die
Hilfsskripte in den Arbeitsordner. Was schon da ist, überspringt es; ein
abgebrochener Lauf wird einfach wiederholt. Ein bis zwei Stunden, fast alles
Downloads. `OHNE_DATEN=1` richtet nur Code und venv ein.

Was es unterwegs repariert, damit man es nicht noch einmal herausfinden muss:

- **torchaudio ≥ 2.9 lädt Audio nur noch über torchcodec**, das
  FFmpeg-Bibliotheken braucht. `openwakeword-kompat.patch` lässt openWakeWord
  WAV mit soundfile lesen.
- **scipy ≥ 1.15 bricht `acoustics`** (für die Augmentierung). Fest auf 1.14.
- **torch ≥ 2.9 braucht `onnxscript`** für den ONNX-Export.
- **`train.py` importiert eine alte API von piper-sample-generator.** Ein
  Einzeiler `generate_samples.py` stellt sie bereit.
- **Die Datenquellen sind umgezogen.** AudioSet liegt auf Hugging Face als
  Parquet, nicht mehr als tar; das FMA-Musik-Set lädt mit aktuellem
  `datasets` nicht mehr (AudioSet hat genug Musik). Die MIT-Impulsantworten
  kommen direkt vom MIT (`hintergrund_laden.py`).

### 1.2 Aussprache wählen (hinhören!)

`clips_vorlage.yaml` nach `<wort>_clips.yaml` kopieren und anpassen:

- **`modell`**: der Name des Worts, klein geschrieben. Er wird zum Bundle-Namen.
- **`positiv`**: eine oder mehrere Schreibweisen, je mit Anteil und Stimmen.
  Die TTS spricht, was dasteht, deshalb mehrere probieren: `Gaston`,
  `Gastong`, `Gastón`, oder IPA mit `phoneme: true`, das am Phonemizer
  vorbeigeht (hier die nasale Endung, die deutsche Stimmen aus der normalen
  Schreibung nicht machen). Stimmen sind Piper-Stimmen aus
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices); fehlende
  werden geladen. Jede Stimme bekommt Sprechtempi (`length_scale`, > 1
  langsamer).
- **`negativ`**: Wörter und Sätze, die ähnlich klingen und im eigenen Alltag
  vorkommen. Sie lehren die Grenze; die Masse an „nicht das Wort“ kommt aus
  den vorgerechneten Merkmalen.

Dann je Schreibweise × Stimme ein paar Proben erzeugen und **anhören**:

```bash
cd ~/wakeword-studio
venv/bin/python clips_erzeugen.py hoerproben <wort>_clips.yaml
# → hoerproben/<wort>/<schreibweise>__<stimme>/*.wav
```

Jede Stimme streichen, die das Wort so sagt, wie es bei euch niemand sagt,
oder sie auf die Tempi beschränken, bei denen es passt. Die Anteile der
Schreibweisen so setzen, wie das Wort wirklich gesagt wird. Das wiegt mehr
als jeder Trainingsparameter: eine schlechte Stimme im Pool bringt nicht nur
nichts, sie nimmt den guten einen Teil des Trainings weg. Bei `gaston`
erkannte das erste Modell die gedrosselte `kerstin` und `mls` kaum, und eine
nasale IPA-Form (60 %) neben `Gastong` (40 %) traf den Haushalt am besten.

### 1.3 Clips erzeugen

```bash
venv/bin/python clips_erzeugen.py alle <wort>_clips.yaml --prozesse 4
```

Rund 66.000 Clips (je 30k positiv und negativ fürs Training, je 3k zum
Testen), etwa eine Stunde auf vier Kernen. Sie landen in
`train_out/<wort>/{positive,negative}_{train,test}/` mit 16 kHz, so wie
`train.py` sie erwartet. Warnungen wie `Phoneme '̃' not found` heißen: der
Stimme fehlt ein Laut aus dem IPA, sie lässt ihn weg; das war in den Proben
zu hören.

### 1.4 Trainings-Config

```bash
cp training_vorlage.yaml <wort>.yaml
```

`model_name: <wort>` und `target_phrase` setzen, den Rest lassen, bis eine
Messung etwas anderes sagt. Was hier zählte:

- `layer_size: 64`, `max_negative_weight: 500`,
  `target_false_positives_per_hour: 0.5`, `steps: 50000`.
- Ein strengerer erster Versuch (`max_negative_weight: 1500`, Ziel 0,2 FP/h)
  ergab ein übervorsichtiges Modell, das 13 % der Test-Clips erkannte.

### 1.5 Trainieren

```bash
venv/bin/python train_runde.py --name r1 --rezept synth --laeufe 2 \
    --modell <wort> --basis-config <wort>.yaml --gpus 0
```

`--gpus 0,1` trainiert je GPU eins gleichzeitig. Belegt ein Dienst auf dem
Rechner Grafikspeicher in einem Podman-Container, stoppt `--weiche
<container>` ihn für die Dauer und startet ihn danach wieder, auch bei
Abbruch. Das Skript augmentiert die Clips einmal (Raumhall, Hintergrund; etwa
28 min auf einem CPU-Kern), trainiert dann `--laeufe` Läufe in eigenen
Ordnern (je etwa 11 min), misst FP/h auf dem generischen Validierungs-Set von
openWakeWord (rund 11 h Sprache und Geräusch ohne das Wort) und wandelt jedes
Modell in tflite. Ergebnis: `runden/r1/status.txt`, `ergebnis.json` und eine
Tabelle FP/h je Schwelle.

**Warum zwei Läufe:** `train.py` setzt keinen Seed. Auf denselben Daten lag
FP/h hier über acht Läufe zwischen 0,19 und 1,22. Ein Lauf sagt wenig.

### 1.6 Das erste Modell prüfen

Erkennung je Schreibweise und Stimme auf sauberen Test-Clips:

```bash
venv/bin/python clips_erzeugen.py pruefen <wort>_clips.yaml runden/r1/synth/l1/<wort>.onnx
```

Fällt eine Stimme weit hinter die anderen, liegt es meist an der Stimme, nicht
am Modell: zurück zu 1.2. Dann den Lauf mit dem niedrigeren FP/h bei der
geplanten Schwelle nehmen. Beim allerersten Modell ist das in Ordnung, es
gibt noch keine Daten aus dem eigenen Haus, gegen die man wählen könnte. Ab
Teil 2 ist genau das Auswählen des glücklichsten Laufs verboten.

### 1.7 Bundle und Gate

Auf dem Assistenz-Rechner:

```bash
mkdir -p models/wakewords/<wort>
scp gpu-rechner:wakeword-studio/runden/r1/synth/l1/tflite/<wort>_float32.tflite \
    models/wakewords/<wort>/<wort>.tflite
```

und eine `models/wakewords/<wort>/manifest.yaml`:

```yaml
name: <wort>
model: <wort>.tflite
threshold: 0.35     # aus der FP/h-Tabelle: niedrigste Schwelle mit vertretbarem FP/h bei 3 Frames
min_hits: 2         # Frames über der Schwelle bis zum Trigger; kurze Wörter brauchen 2
min_peak: 0.7       # bester Score in der Serie muss das erreichen
min_peak_short: 0.9 # strengerer Peak für Serien unter 3 Frames
```

Die `manifest.yaml` von `gaston` begründet jeden Wert. Zwei Lehren dahinter:
ein rein auf TTS trainiertes Modell liefert auf echter Sprache *spitze*
Scores, oft nur ein, zwei Frames hoch; die Schwelle zu senken hilft dann wenig,
`min_hits` und eine Peak-Bedingung helfen. Und FP/h immer *mit* der
Serien-Bedingung bewerten: `gaston` hatte roh 0,47 FP/h bei 0,35 und mit
3-Frame-Serie 0,00.

Ein kleines Test-Set echter Rufe aufnehmen und scoren:

```bash
ow-venv/bin/python -m wakeword_studio record --bundle <wort> --speaker <name>   # stoppt den Dienst, geführte Takes
ow-venv/bin/python -m wakeword_studio score --bundle <wort>
```

Die Takes liegen in `models/wakewords/<wort>/samples/<name>/`, das ist
git-ignoriert: **Stimmen der Familie gehören nicht in ein öffentliches Repo**,
sondern in ein privates. Dann das Bundle im Profil der `config.yaml`
eintragen (`wakewords: [{bundle: <wort>}]`) und den Assistenten neu starten.

### 1.8 Live, und sammeln

Ab jetzt wird jeder Trigger und Near-Miss mit Score-Verlauf, Pegel und Audio
archiviert. Das erste Modell wird Rufe mancher Leute verpassen (hier: die
Stimme des Hauptnutzers am schlechtesten) und ab und zu beim Fernsehen
auslösen. Das ist der Stoff für Teil 2. Zwei, drei Wochen normaler Betrieb
ergeben ein erstes Paket.

## Teil 2: der Nachtrainings-Kreislauf

### 2.1 Sammeln

Passiv, Tage bis Wochen. Jeder Trigger und jeder Near-Miss landet mit Score-
Verlauf, Pegel und Umgebung im `wake_events.log`, das Audio im Archiv.

### 2.2 Labeln

```bash
ow-venv/bin/python -m tools.wake_triage --seit 8 --auch-trigger
```

Vergibt Selbst-Labels aus dem Verhalten (ein Schaltbefehl wurde ausgeführt,
ein Barge-in brach mit Stopp-Wort ab, ein Ruf wurde nach einem Near-Miss
wiederholt) und für den Rest ein STT-Label. Beides ist schwach:

- **STT-Labels auf Trigger-Clips taugen nicht als Negativ.** Die STT verhört
  das Wort ständig („Gasthof“, „Ganz toll“). In einer Runde nannte sie 41
  von 53 per Ohr echten Rufen „Rauschen“.
- **Selbst-Labels können systematisch falsch sein.** Die Regel „Abbruch mit
  Stopp-Wort = Fehlauslöser“ lag per Ohr nur bei 16 von 29 richtig, und sie
  setzte das Label auf den Barge-in-Clip, der ein echtes „Stopp, Gaston“
  enthielt. Ein solches Negativ lehrt das Modell, das Wort zu überhören.
  Jede Selbst-Regel einmal per Ohr gegenprüfen, bevor sie Trainingsdaten
  erzeugt.

Das Ohr entscheidet:

```bash
# Mikro vorher stumm schalten, sonst lösen die abgespielten Clips Trigger aus
ow-venv/bin/python tools/review_audio.py export --klasse unklar   # Clips nach offen/ legen
# … anhören, nach positiv/ bzw. negativ/ sortieren …
ow-venv/bin/python tools/review_audio.py import                   # als Ohr-Labels zurücklesen
```

`--liste DATEI` nimmt genau die Clips, an denen eine Messung hängt; das sind
meist ein paar Dutzend statt Hunderte. Gehört wird der Clip so, wie das
Modell ihn bekommt (bei Positiven der Trainingsschnitt, siehe 2.4). Gefragt
ist, ob das Wort **zu hören** ist, nicht ob es gemeint war.

### 2.3 Sichern

```bash
ow-venv/bin/python -m tools.wake_corpus sichern
```

Kopiert gelabelte Clips in den Korpus und zieht geänderte Labels nach. **Vor
jedem Neustart des Assistenten**, denn der Start räumt alte Archiv-Dateien ab;
ohne Sicherung bleibt nur das Label, das Audio ist weg.

### 2.4 Paket bauen

```bash
ow-venv/bin/python -m tools.wake_paket bauen \
    --vorrunde ~/wake-pakete/<wort>_rN_paket --out ~/wake-pakete/<wort>_rN+1_paket \
    --neu-ab <erster Tag nach der Vorrunde, JJJJMMTT>
```

(Das allererste Paket: `wake_corpus paket`.) Die Regeln stehen im Docstring
des Werkzeugs, die wichtigsten:

- **Die Val-Seite der Vorrunde bleibt eingefroren** (`val_vorrunde/`). Nur so
  ist die Vorher-Zahl des laufenden Modells gültig: weder das alte noch das
  neue Modell hat diese Tage gesehen. Clips mit geändertem Label wandern in
  eigene Gruppen (`umgelabelt_*`).
- **Neue Tage werden ganz geteilt**, nicht Clip für Clip. Wiederholungen
  desselben Rufs („Gaston … Gaston!“) liegen Sekunden auseinander und landeten
  sonst auf beiden Seiten.
- **Positive werden auf die letzten 1,8 s geschnitten.** `augment_clips`
  schneidet längere Clips per Münzwurf vorn ODER hinten ab; bei 3-s-Clips
  mit dem Wort am Ende ging so die Hälfte der Positiven ohne Wort als
  „Wakewort“ ins Training (Recall im Studio 50 → 25 %).
- **Positive nur mit hörbarem Wort.** Ein Klopfer im Fenster lehrt sonst,
  dass Klopfen zum Wort gehört.

### 2.5 Vorher-Zahl und Schranke

```bash
ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/<paket>
```

misst das laufende Modell je Val-Gruppe mit dem Live-Gate. Daraus die
Schranke, **vor dem Training aufschreiben und committen**: je Gruppe, was ein
Kandidat mindestens (Rufe) oder höchstens (Fehlauslöser) erreichen muss, dazu
FP/h auf dem generischen Set. Wer nach den Ergebnissen formuliert, formuliert
das, was der beste Lauf zufällig geschafft hat.

### 2.6 Trainieren

Paket auf den GPU-Rechner kopieren und eine Runde mit einem oder mehreren
Rezepten starten:

```bash
scp -r ~/wake-pakete/<paket> gpu-rechner:wakeword-studio/
ssh gpu-rechner
cd wakeword-studio
venv/bin/python train_runde.py --name rN --paket <paket> \
    --rezept voll --rezept C=c_pos.txt,c_neg.txt --laeufe 2 \
    --modell <wort> --basis-config <wort>.yaml --gpus 0,1 --augment-parallel 2
```

Je Rezept ein eigener Ordner `runden/rN/<rezept>/` (synthetische Clips als
Symlinks, echte Clips ×10 verlinkt), ein augment, dann `--laeufe` Trainings in
eigenen Ordnern, je mit FP/h (`fp_pro_stunde.json`) und tflite
(`l<n>/tflite/<wort>_float32.tflite`). Erst `--trocken` laufen lassen: das
legt nur die Ordner an und zeigt die Mengen je Rezept.

Dauern (12 Kerne, zwei GPUs, 30k synthetische Clips je Seite): augment etwa
28 min auf einem CPU-Kern, ein Training etwa 11 min allein und 13–14 min, wenn
zwei gleichzeitig laufen. Augments verschiedener Rezepte laufen nebeneinander,
ohne sich zu bremsen. Zwei Rezepte mit je zwei Läufen: 56 min statt etwa 100
(Messtagebuch, Abschnitt „Paralleles Training“).

Fallen beim Trainieren:

- **`train.py` endet mit rc=1** (`No module named 'onnx_tf'`). Das Flag
  `--convert_to_tflite` hat als Default den String `"False"`, der wahr ist.
  Die onnx ist trotzdem fertig, die tflite macht `onnx2tf` danach. Das Skript
  prüft deshalb die onnx, nicht den Rückgabewert.
- **`onnx2tf` braucht `-kat x`**, sonst wird der Eingang transponiert.
  `train_runde.py` setzt es.
- **Grafikspeicher:** siehe `--weiche` in 1.5. Container starten danach im
  eigenen systemd-Scope; ohne ihn hängt die Port-Weiterleitung von rootless
  Podman an der SSH-Sitzung und stirbt mit ihr.
- **SSH-Sitzung offen halten**, wenn auf dem Rechner kein Linger für den
  Benutzer gesetzt ist, oder die Runde unter `systemd-run --user` starten.

### 2.7 Messen

Die tflite holen, je Kandidat ein Bundle-Verzeichnis mit dem **Live-Manifest**
anlegen (gleiches Gate wie im Betrieb) und gegen das Paket messen:

```bash
mkdir -p ~/wake-pakete/modelle/<kandidat>/<wort>
scp gpu-rechner:wakeword-studio/runden/rN/voll/l1/tflite/<wort>_float32.tflite \
    ~/wake-pakete/modelle/<kandidat>/<wort>/<wort>.tflite
cp models/wakewords/<wort>/manifest.yaml ~/wake-pakete/modelle/<kandidat>/<wort>/
ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/<paket> \
    --modell <wort> --modell ~/wake-pakete/modelle/<kandidat>/<wort>
```

Mit genau zwei Modellen zeigt `--paare` die Clips, die kippen. Verfehlt ein
Kandidat knapp, dort nachsehen, welche Labels die kippenden Clips tragen:
ein falsches Label ist ein Fund für 2.2, keine Ausrede für die Schranke.

FP/h (3 Frames in Folge, Live-Schwelle) aus `ergebnis.json`. Es misst nur die
Grundneigung zum Fehlauslösen auf fremdem Material, nicht die Fehlauslöser im
eigenen Raum.

### 2.8 Entscheiden

- **Die Streuung je Lauf ist groß.** Zwei Läufe auf denselben Merkmalen
  lagen bei FP/h 0,56 und 1,40; über acht Läufe eines Pakets zwischen 0,19
  und 1,22. Ein Einzellauf ist keine Messung einer Datenänderung. Deshalb
  mindestens zwei Läufe je Rezept.
- **Nicht so lange würfeln, bis ein Lauf besteht.** Das wählt am Val-Set aus,
  die Schranke wird zum Trainingskriterium, und der Glückslauf ist im Betrieb
  keiner mehr.
- Besteht ein Lauf: deployen. Besteht keiner, aber ein Kandidat ist in
  wichtigen Gruppen klar besser: ein **Live-Versuch** ist möglich, als
  bewusste Entscheidung mit Vergleichszahl und Abbruchregel, die vorher
  feststehen:

```bash
ow-venv/bin/python -m tools.wake_triage --seit 8 --auch-trigger
ow-venv/bin/python -m tools.wake_live_bilanz <von> <bis>   # Fehlauslöser/Tag, Rufe/Tag
```

### 2.9 Deployen und Rückweg

`<wort>.tflite` (und die onnx samt `.onnx.data`) ins Bundle, im Kopf des
Manifests vermerken, was es ist und gegen welche Schranke es gemessen wurde,
committen, Korpus sichern, Dienst neu starten. Die Gate-Parameter im Manifest
sind an den Scores des Vorgängers geeicht; ob sie zum neuen Modell passen,
zeigt erst der Betrieb. Rückweg: die Bundle-Dateien aus dem Commit davor.

Dann beginnt 2.1 von vorn.

## Die Kreislauf-Werkzeuge an ein anderes Wort anpassen

Die Werkzeuge des Kreislaufs (`wake_triage`, `wake_corpus`, `wake_paket`,
`wake_live_bilanz`) entscheiden am STT-Text, ob das Wort zu hören war. Was
dafür zählt, steht im Manifest des Bundles, nicht im Code
(`tools/wake_wort.py`):

```yaml
# models/wakewords/<wort>/manifest.yaml
text_erkennung:
  muster: "gast|kast|gasch|aston|caston"   # Regex irgendwo im Text (Triage)
  wortanfaenge: [gast, gas, gest, kast]    # ein Wort beginnt so (Paket, Schnitt)
  sprache: de                              # Sprache für die STT mit Wortzeiten
```

Ohne den Block gelten die ersten vier Buchstaben des Bundle-Namens als Muster
und Wortanfang und `de` als Sprache. Das ist ein Anfang, kein Ergebnis: die
erste Triage mit `--auch-trigger` laufen lassen, nachlesen, was die STT aus
echten Rufen gemacht hat („Gasthof“, „Kastor“, „Gastó“ bei `gaston`), und
diese Formen ergänzen. Ein zu enges Muster sortiert echte Rufe nach
„Rauschen“, das ist die gefährliche Richtung; ein zu weites verschiebt nur
Fälle nach „echter Ruf“, woran keine Regel etwas lockert.

Welches Bundle gemeint ist: `--bundle` am Werkzeug, sonst das erste eigene
Bundle aus dem `wakewords:`-Block des Profils.

## Was sich als wirksam gezeigt hat und was nicht

- **Echte Rufe aus dem Alltag** (×10 neben 30k synthetischen) bringen den
  Recall. Ein rein synthetisches Modell erkannte die Stimme des Hauptnutzers
  am schlechtesten. Mit Ohr-geprüften Selbst-Positiven stieg der Recall stark,
  FP/h aber auch.
- **Auto-Negative** (Near-Misses, in denen keine STT das Wort hört) machen das
  Modell strenger, ohne FP/h messbar zu senken.
- **Nur Ohr-Labels** ergeben FP/h 0, aber halbieren den Recall. Weniger
  Positive verschieben das Modell auf derselben Linie Recall gegen
  Fehlauslöser, sie heben die Linie nicht.
- **Die eigene TTS-Stimme** des Assistenten liegt in der Trainingsverteilung,
  wenn das Modell auf denselben Piper-Stimmen trainiert ist. Digital löste
  sie aus; erst die Echo-Unterdrückung des Mikrofons nahm das weg. Ohne
  Echo-Unterdrückung gehört die eigene Stimme als Negativ ins Training.
- **An den Score-Schwellen drehen** hilft ab einem Punkt nicht mehr: echte
  Rufe und Fehlauslöser überlappen im Peak. Dann ist es Modellarbeit oder eine
  zweite Stufe nach dem Trigger (`voice_assistant/wake_pruefung.py`).

Offen und notiert, nicht gemacht: mehrere Läufe als Ensemble mitteln, statt
einen auszuwählen (`WAKEWORD_PROCESS.md`, „Streuung senken statt Glückslauf
wählen“).
