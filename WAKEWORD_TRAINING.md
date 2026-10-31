# Ein Wakewort aus dem Alltag nachtrainieren

Eine Anleitung für den wiederkehrenden Kreislauf: ein laufendes
openWakeWord-Modell verpasst Rufe und löst bei Dingen aus, die keine Rufe
sind. Aus beidem entsteht ein gelabelter Datensatz, daraus ein neues Modell,
das gegen eine vorher festgelegte Schranke gemessen wird.

Was hier NICHT steht:

- **Das erste Modell.** Das entsteht rein synthetisch (Piper-Stimmen sprechen
  das Wort, dazu ähnlich klingende Negative) mit der Trainingsumgebung von
  openWakeWord. Siehe `Wakeword_Studio_Spec.md`, „Kernidee“.
- **Die Messwerte dieser Installation.** Die stehen im Messtagebuch
  `WAKEWORD_PROCESS.md`, Runde für Runde. Diese Anleitung zitiert daraus nur,
  wo eine Zahl eine Regel begründet.

Das Beispiel-Wort ist durchgehend `gaston`. Für ein anderes Wort ändern sich
nur Bundle-Name und Trainings-Config.

## Begriffe

| Begriff | Bedeutung |
|---|---|
| Trigger | Das Modell hat ausgelöst, ein Turn begann. Audio: `*_wake.wav` (die 3 s bis zum Auslösen), dazu die Folgeaufnahme `*_rec.wav` |
| Near-Miss | Score hoch, aber ein Gate (Frames, Peak, Pegel, Umgebung) hat abgelehnt. Audio: `*_nearmiss.wav`. Hier stecken die verpassten Rufe |
| Label | `echter_ruf`, `rauschen` (= Fehlauslöser oder kein Ruf) oder `unklar` |
| Labelquelle | **Ohr** (`wake_review.jsonl`) vor **Selbst** (aus dem Verhalten, `wake_triage._selbst_labels`) vor **STT** (`wake_triage.jsonl`). Die stärkere Quelle gewinnt immer |
| Korpus | Gelabelte Clips, dauerhaft gesichert (`voice/corpus/`). Das Trigger-Archiv selbst löscht alte Dateien |
| Paket | Ein Trainingsstand: `train/{positive,negative}` und Val-Gruppen, nach ganzen Tagen geteilt (`tools/wake_paket.py`) |
| Rezept | Welche Clips eines Pakets ins Training gehen (`voll` = alle, oder eine Liste) |
| Lauf | Ein `--train_model` auf den Merkmalen eines Rezepts. train.py setzt keinen Seed, jeder Lauf ist anders |
| Schranke | Die Zahlen, die ein Kandidat erfüllen muss, **vor** dem Training aufgeschrieben und committet |

## Voraussetzungen

- Der Assistent läuft mit einem eigenen Bundle (`models/wakewords/<name>/`)
  und archiviert Trigger und Near-Misses (`TRIGGER_AUDIO_DIR`,
  `wake_events.log`).
- Ein GPU-Rechner mit der Trainingsumgebung von openWakeWord: ein Klon von
  `openwakeword` mit `train.py`, die vorgerechneten Merkmale
  (`openwakeword_features_ACAV100M_2000_hrs_16bit.npy`, ~17 GB, und
  `validation_set_features.npy`), Raumhall (`mit_rirs/`) und Hintergrund
  (`audioset_16k/`), die synthetischen Clips des ersten Modells im
  train.py-Layout (`train_out/<name>/{positive,negative}_{train,test}/`) und
  das CLI `onnx2tf`. Eine Config-Vorlage liegt in
  `wakeword_studio/training_vorlage.yaml`.
- Arbeitsspeicher: ein Training hält das Validierungs-Set als Fenster im
  Speicher, rund 3 GB. Zwei gleichzeitig passen in 32 GB.

## Der Kreislauf

### 1. Sammeln

Passiv, Tage bis Wochen. Jeder Trigger und jeder Near-Miss landet mit Score-
Verlauf, Pegel und Umgebung im `wake_events.log`, das Audio im Archiv.

### 2. Labeln

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
meist ein paar Dutzend statt Hunderte. Gehört wird der Clip so, wie das Modell ihn bekommt (bei Positiven der
Trainingsschnitt, siehe Schritt 4). Gefragt ist, ob das Wort **zu hören** ist,
nicht ob es gemeint war.

### 3. Sichern

```bash
ow-venv/bin/python -m tools.wake_corpus sichern
```

Kopiert gelabelte Clips in den Korpus und zieht geänderte Labels nach. **Vor
jedem Neustart des Assistenten**, denn der Start räumt alte Archiv-Dateien ab;
ohne Sicherung bleibt nur das Label, das Audio ist weg.

### 4. Paket bauen

```bash
ow-venv/bin/python -m tools.wake_paket bauen \
    --vorrunde ~/wake-pakete/<name>_rN_paket --out ~/wake-pakete/<name>_rN+1_paket \
    --neu-ab <erster Tag nach der Vorrunde, JJJJMMTT>
```

(Die allererste Runde: `wake_corpus paket`.) Die Regeln stehen im Docstring
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

### 5. Vorher-Zahl und Schranke

```bash
ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/<paket>
```

misst das laufende Modell je Val-Gruppe mit dem Live-Gate. Daraus die
Schranke, **vor dem Training aufschreiben und committen**: je Gruppe, was ein
Kandidat mindestens (Rufe) oder höchstens (Fehlauslöser) erreichen muss, dazu
FP/h auf dem generischen Set. Wer nach den Ergebnissen formuliert, formuliert
das, was der beste Lauf zufällig geschafft hat.

### 6. Trainieren

Paket auf den GPU-Rechner kopieren, dazu `wakeword_studio/train_runde.py`:

```bash
scp -r ~/wake-pakete/<paket> wakeword_studio/train_runde.py gpu-rechner:wakeword-studio/
ssh gpu-rechner
cd wakeword-studio
venv/bin/python train_runde.py --name rN --paket <paket> \
    --rezept voll --rezept C=c_pos.txt,c_neg.txt --laeufe 2 \
    --gpus 0,1 --augment-parallel 2 --weiche <container>
```

Je Rezept ein eigener Ordner `runden/rN/<rezept>/` (synthetische Clips als
Symlinks, echte Clips ×10 verlinkt), ein augment, dann `--laeufe` Trainings in
eigenen Ordnern, je mit FP/h (`fp_pro_stunde.json`) und tflite
(`l<n>/tflite/<name>_float32.tflite`). Am Ende `ergebnis.json` und eine Tabelle.
Erst `--trocken` laufen lassen: das legt nur die Ordner an und zeigt die
Mengen je Rezept.

Dauern (Rechner mit 12 Kernen und zwei GPUs, 30k synthetische Clips je Seite):
augment etwa 28 min auf einem CPU-Kern, ein Training etwa 11 min allein und
13–14 min, wenn zwei gleichzeitig laufen. Augments verschiedener Rezepte
laufen nebeneinander, ohne sich zu bremsen, Trainings je eines pro GPU. Zwei
Rezepte mit je zwei Läufen: 56 min statt etwa 100 (Messtagebuch, Abschnitt
„Paralleles Training“).

Fallen beim Trainieren:

- **`train.py` endet mit rc=1** (`No module named 'onnx_tf'`). Das Flag
  `--convert_to_tflite` hat als Default den String `"False"`, der wahr ist.
  Die onnx ist trotzdem fertig, die tflite macht `onnx2tf` danach. Das Skript
  prüft deshalb die onnx, nicht den Rückgabewert.
- **Grafikspeicher teilen:** `--weiche` stoppt einen Podman-Container für die
  Dauer und startet ihn am Ende im eigenen Scope (`systemd-run --user
  --scope`), auch bei Abbruch. Ohne eigenen Scope hängt die
  Port-Weiterleitung von rootless Podman an der SSH-Sitzung und stirbt mit ihr.
- **SSH-Sitzung offen halten**, wenn auf dem Rechner kein Linger für den
  Benutzer gesetzt ist.

### 7. Messen

Die tflite holen, je Kandidat ein Bundle-Verzeichnis mit dem **Live-Manifest**
anlegen (gleiches Gate wie im Betrieb) und gegen das Paket messen:

```bash
mkdir -p ~/wake-pakete/modelle/<kandidat>/gaston
scp gpu-rechner:wakeword-studio/runden/rN/voll/l1/tflite/gaston_float32.tflite \
    ~/wake-pakete/modelle/<kandidat>/gaston/gaston.tflite
cp models/wakewords/gaston/manifest.yaml ~/wake-pakete/modelle/<kandidat>/gaston/
ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/<paket> \
    --modell gaston --modell ~/wake-pakete/modelle/<kandidat>/gaston
```

Mit genau zwei Modellen zeigt `--paare` die Clips, die kippen. Verfehlt ein
Kandidat knapp, dort nachsehen, welche Labels die kippenden Clips tragen:
ein falsches Label ist ein Fund für Schritt 2, keine Ausrede für die Schranke.

FP/h (3 Frames in Folge, Live-Schwelle) aus `ergebnis.json`. Es misst nur die
Grundneigung zum Fehlauslösen auf fremdem Material, nicht die Fehlauslöser im
eigenen Raum.

### 8. Entscheiden

- **Die Streuung je Lauf ist groß.** Zwei Läufe auf denselben Merkmalen
  lagen bei FP/h 0,56 und 1,40, in einer anderen Runde bei 0,28 und 0,75.
  Ein Einzellauf ist keine Messung einer Datenänderung. Deshalb mindestens
  zwei Läufe je Rezept.
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

### 9. Deployen und Rückweg

`<name>.tflite` (und die onnx samt `.onnx.data`) ins Bundle, im Kopf des
Manifests vermerken, was es ist und gegen welche Schranke es gemessen wurde,
committen, Korpus sichern, Dienst neu starten. Die Gate-Parameter im Manifest
sind an den Scores des Vorgängers geeicht; ob sie zum neuen Modell passen,
zeigt erst der Betrieb. Rückweg: die Bundle-Dateien aus dem Commit davor.

Dann beginnt Schritt 1 von vorn.

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
