# Training your own wake word

*Deutsche Fassung: [WAKEWORD_TRAINING.de.md](WAKEWORD_TRAINING.de.md)*

How the wake word of this assistant came about, written so that you can do
the same for your own word, in your own language, on your own hardware:

- **Part 1, the first model.** Built purely from synthetic speech: Piper
  voices say the word thousands of times, plus similar-sounding words as
  negatives. Takes an afternoon, most of it downloads and waiting.
- **Part 2, the retraining loop.** The running model misses calls and fires
  on things that are not calls. Both end up as labelled clips, those become a
  new model, and the new model has to beat a bar that was written down
  before training. This is where a model becomes good in *your* room, and it
  takes weeks of normal use, not an afternoon.

If you work with a coding agent (Claude Code or another agent that reads
Agent Skills), the skill in `.claude/skills/wakeword-training/` walks you
through this guide step by step and stops at the decisions that are yours.

The example word throughout is `gaston`, a German household. Everything
specific to it lives in two YAML files and a handful of places listed under
[Adapting the loop tools to your word](#adapting-the-loop-tools-to-your-word).
The measurement log of this installation, round by round with every number,
is `WAKEWORD_PROCESS.md` (German); this guide only quotes it where a number
justifies a rule.

## Terms

| Term | Meaning |
|---|---|
| Trigger | The model fired and a turn began. Audio: `*_wake.wav` (the 3 s up to the trigger), plus the following recording `*_rec.wav` |
| Near-miss | High score, but a gate (frames, peak, level, ambient noise) rejected it. Audio: `*_nearmiss.wav`. This is where the missed calls are |
| Label | `echter_ruf` (real call), `rauschen` (false trigger, no call) or `unklar` (unclear) |
| Label source | **Ear** (`wake_review.jsonl`) beats **self** (derived from behaviour, `wake_triage._selbst_labels`) beats **STT** (`wake_triage.jsonl`). The stronger source always wins |
| Corpus | Labelled clips, kept permanently (`voice/corpus/`). The trigger archive itself deletes old files |
| Package | One training data set: `train/{positive,negative}` and validation groups, split by whole days (`tools/wake_paket.py`) |
| Recipe | Which clips go into training: `synth` (synthetic only), `voll` (all of a package) or a list |
| Run | One `--train_model` on the features of a recipe. `train.py` sets no seed, every run is different |
| Bar | The numbers a candidate must reach, **written down and committed before training** |

## What you need

- **The assistant from this repo**, running with an archive of triggers and
  near-misses (on by default: `~/.openclaw/workspace/voice/triggers/` and
  `wake_events.log`).
- **A machine with an NVIDIA GPU** for training. It can be the same machine.
  Ours: 12 cores, 32 GB RAM, two 16 GB GPUs; the model itself is tiny, a
  smaller GPU will do. Training on CPU only has not been tried here.
- **About 25 GB of disk** for the training environment, most of it the
  precomputed negative features (17 GB).
- **32 GB of RAM** is comfortable: one training holds the validation set as
  windows in memory, about 3 GB, two at once fit easily.
- **Your own ears.** Twice in this process a person has to listen: to the
  synthetic voices before the first model, and to clips before every
  retraining. Neither step can be handed to a machine; Part 2 says why.

## Part 1: the first model

### 1.1 Set up the training environment

On the GPU machine, with this repo checked out (or just the
`wakeword_studio/` folder copied over):

```bash
TORCH_INDEX=https://download.pytorch.org/whl/cu130 \
  wakeword_studio/gpu_einrichten.sh ~/wakeword-studio
```

Pick `TORCH_INDEX` for your driver's CUDA version (`nvidia-smi` shows it;
`cu126`, `cu128`, `cu130`, …). The script creates a venv with the pinned
versions from `wakeword_studio/training_requirements.txt`, clones
openWakeWord and piper-sample-generator at the commits used here, downloads
the precomputed features, room impulse responses and background audio, and
copies the helper scripts into the working folder. It skips whatever already
exists, so an interrupted run can simply be repeated. One to two hours, almost
all of it downloads. `OHNE_DATEN=1` installs only code and venv.

What it fixes along the way, so you do not have to find out again:

- **torchaudio ≥ 2.9 loads audio only through torchcodec**, which needs FFmpeg
  libraries. `openwakeword-kompat.patch` makes openWakeWord read WAV with
  soundfile instead.
- **scipy ≥ 1.15 breaks `acoustics`** (used by augmentation). Pinned to 1.14.
- **torch ≥ 2.9 needs `onnxscript`** for the ONNX export.
- **`train.py` imports an old piper-sample-generator API.** A one-line shim
  `generate_samples.py` provides it.
- **The data sources moved.** AudioSet is Parquet on Hugging Face now, not
  tar; the FMA music set no longer loads with current `datasets` (AudioSet has
  enough music). The MIT impulse responses come straight from MIT
  (`hintergrund_laden.py`).

### 1.2 Choose how the word is spoken (listen!)

Copy `clips_vorlage.yaml` to `<word>_clips.yaml` and edit it:

- **`modell`**: the name of your word, lower case. It becomes the bundle name.
- **`positiv`**: one or more spellings of the word, each with a share and a
  set of voices. The TTS speaks what is written, so try several: `Gaston`,
  `Gastong`, `Gastón`, or IPA with `phoneme: true`, which bypasses the
  phonemizer (here the nasal ending that German voices do not produce from the
  plain spelling). Voices are Piper voice ids from
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices); missing
  ones are downloaded. Each voice gets a list of speaking rates
  (`length_scale`, > 1 is slower).
- **`negativ`**: words and phrases that sound similar and occur in your home's
  daily speech. They teach the border; the bulk of "not the word" comes from
  the precomputed features.

Then generate a few samples of every spelling × voice and **listen**:

```bash
cd ~/wakeword-studio
venv/bin/python clips_erzeugen.py hoerproben <word>_clips.yaml
# → hoerproben/<word>/<spelling>__<voice>/*.wav
```

Remove every voice that says the word in a way nobody in your home would, or
restrict it to the rates where it sounds right. Give the spellings the shares
that match how the word is actually said. This matters more than any training
parameter: a bad voice in the pool does not just add nothing, it takes a
share of the training that the good voices would have had. For `gaston`, the
throttled `kerstin` voice and the `mls` voice were recognised poorly by the
first model, and a nasal IPA spelling (60 %) next to `Gastong` (40 %) matched
the household best.

### 1.3 Generate the clips

```bash
venv/bin/python clips_erzeugen.py alle <word>_clips.yaml --prozesse 4
```

About 66,000 clips (30k positive and 30k negative for training, 3k each for
testing), roughly an hour on four cores. They land in
`train_out/<word>/{positive,negative}_{train,test}/` at 16 kHz, the layout
`train.py` expects. Warnings like `Phoneme '̃' not found` mean a voice lacks a
sound from your IPA and drops it; that is what you heard in the samples.

### 1.4 Training config

```bash
cp training_vorlage.yaml <word>.yaml
```

Set `model_name: <word>` and `target_phrase`. Leave the rest as it is until a
measurement says otherwise. The settings that mattered here:

- `layer_size: 64`, `max_negative_weight: 500`,
  `target_false_positives_per_hour: 0.5`, `steps: 50000`.
- A stricter first attempt (`max_negative_weight: 1500`, target 0.2 FP/h)
  gave an over-cautious model that recognised 13 % of the test clips.

### 1.5 Train

```bash
venv/bin/python train_runde.py --name r1 --rezept synth --laeufe 2 \
    --modell <word> --basis-config <word>.yaml --gpus 0
```

`--gpus 0,1` runs one training per GPU at the same time. If a service on that
machine holds GPU memory in a Podman container, `--weiche <container>` stops it
for the duration and starts it again afterwards, also on abort. The script
augments the clips once (room reverb, background noise; about 28 min on one
CPU core), then trains `--laeufe` runs in separate folders (about 11 min each),
measures FP/h on openWakeWord's generic validation set (about 11 h of speech
and noise without the word) and converts each model to tflite. Results:
`runden/r1/status.txt`, `ergebnis.json` and a table of FP/h per threshold.

**Why two runs:** `train.py` sets no random seed. On identical data, FP/h
ranged from 0.19 to 1.22 across eight runs here. One run tells you little.

### 1.6 Check the first model

Recognition per spelling and voice, on clean test clips:

```bash
venv/bin/python clips_erzeugen.py pruefen <word>_clips.yaml runden/r1/synth/l1/<word>.onnx
```

If one voice falls far behind the others, it is usually the voice, not the
model: go back to 1.2. Then pick the run with the lower FP/h at the threshold
you intend to use. For the very first model that is fine, there is no data
from your home yet to choose against. From Part 2 on, choosing the luckiest
run is exactly what you must not do.

### 1.7 Bundle and gate

On the assistant machine:

```bash
mkdir -p models/wakewords/<word>
scp gpu-host:wakeword-studio/runden/r1/synth/l1/tflite/<word>_float32.tflite \
    models/wakewords/<word>/<word>.tflite
```

and a `models/wakewords/<word>/manifest.yaml`:

```yaml
name: <word>
model: <word>.tflite
threshold: 0.35     # from the FP/h table: lowest threshold with acceptable FP/h at 3 frames
min_hits: 2         # frames above threshold until trigger; short words need 2
min_peak: 0.7       # the best score in the streak must reach this
min_peak_short: 0.9 # stricter peak for streaks shorter than 3 frames
```

The `manifest.yaml` of `gaston` documents why each value is what it is. Two
lessons behind them: a model trained on TTS alone gives *spiky* scores on real
speech, often only one or two frames high, so lowering the threshold does not
help much, `min_hits` and a peak condition do. And always judge FP/h *with*
the streak condition: `gaston` had 0.47 FP/h on raw scores at 0.35 and 0.00
with a 3-frame streak.

Record a small test set of real calls and score it:

```bash
ow-venv/bin/python -m wakeword_studio record --bundle <word> --speaker <name>   # stops the service, guided takes
ow-venv/bin/python -m wakeword_studio score --bundle <word>
```

Takes land in `models/wakewords/<word>/samples/<name>/`, which is git-ignored:
**voices of your family do not belong in a public repository**, keep them in a
private one. Then add the bundle to your profile in `config.yaml`
(`wakewords: [{bundle: <word>}]`) and restart the assistant.

### 1.8 Live, and start collecting

From now on every trigger and near-miss is archived with score curve, level
and audio. Expect the first model to miss calls from some people (here: the
main user's voice was recognised worst) and to fire on television now and
then. That is the material for Part 2. Two or three weeks of normal use give a
first package.

## Part 2: the retraining loop

### 2.1 Collect

Passive, days to weeks. Each trigger and near-miss lands in `wake_events.log`
with score curve, level and ambient noise, the audio in the archive.

### 2.2 Label

```bash
ow-venv/bin/python -m tools.wake_triage --seit 8 --auch-trigger
```

Assigns *self labels* from behaviour (a switching command was executed, a
barge-in was cancelled with the stop word, a call was repeated after a
near-miss) and an STT label for the rest. Both are weak:

- **STT labels on trigger clips are useless as negatives.** The STT mishears
  the word all the time ("Gasthof", "Ganz toll"). In one round it called 41 of
  53 real calls (by ear) "noise".
- **Self labels can be systematically wrong.** The rule "cancelled with stop
  word = false trigger" was right in only 16 of 29 cases by ear, and it put
  the label on the barge-in clip, which contained a real "Stop, Gaston". Such
  a negative teaches the model to ignore the word. Check every self-label rule
  by ear once before it produces training data.

The ear decides:

```bash
# Mute the microphone first, or the clips you play back trigger the assistant
ow-venv/bin/python tools/review_audio.py export --klasse unklar   # clips into offen/
# … listen, sort into positiv/ or negativ/ …
ow-venv/bin/python tools/review_audio.py import                   # read back as ear labels
```

`--liste FILE` takes exactly the clips a measurement depends on; that is
usually a few dozen instead of hundreds. Listen to the clip the way the model
gets it (for positives the training cut, see 2.4). The question is whether the
word **can be heard**, not whether it was meant.

### 2.3 Keep

```bash
ow-venv/bin/python -m tools.wake_corpus sichern
```

Copies labelled clips into the corpus and follows label changes. **Before
every restart of the assistant**, because the start cleans old archive files;
without this only the label is left, the audio is gone.

### 2.4 Build a package

```bash
ow-venv/bin/python -m tools.wake_paket bauen \
    --vorrunde ~/wake-pakete/<word>_rN_paket --out ~/wake-pakete/<word>_rN+1_paket \
    --neu-ab <first day after the previous round, YYYYMMDD>
```

(The very first package: `wake_corpus paket`.) The rules are in the tool's
docstring, the important ones:

- **The previous round's validation side stays frozen** (`val_vorrunde/`).
  Only then is the "before" number of the running model valid: neither the
  old nor the new model has seen those days. Clips whose label changed move
  into separate groups (`umgelabelt_*`).
- **New days are split whole**, not clip by clip. Repetitions of the same call
  ("Gaston … Gaston!") are seconds apart and would otherwise land on both sides.
- **Positives are cut to the last 1.8 s.** `augment_clips` cuts longer clips
  at the front OR the back by coin toss; with 3 s clips and the word at the
  end, half of the positives went into training without the word (recall on
  the studio takes 50 → 25 %).
- **Positives only with an audible word.** A knock in the window otherwise
  teaches that knocking belongs to the word.

### 2.5 Before number and bar

```bash
ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/<package> --modell <word>
```

measures the running model per validation group, with the live gate. From
that, write the bar **and commit it before training**: per group, what a
candidate must at least (calls) or at most (false triggers) reach, plus FP/h
on the generic set. Whoever writes the bar after seeing the results writes
down what the best run happened to achieve.

### 2.6 Train

Copy the package to the GPU machine and run a round with one or more recipes:

```bash
scp -r ~/wake-pakete/<package> gpu-host:wakeword-studio/
ssh gpu-host
cd wakeword-studio
venv/bin/python train_runde.py --name rN --paket <package> \
    --rezept voll --rezept C=c_pos.txt,c_neg.txt --laeufe 2 \
    --modell <word> --basis-config <word>.yaml --gpus 0,1 --augment-parallel 2
```

Each recipe gets its own folder `runden/rN/<recipe>/` (synthetic clips as
symlinks, real clips linked ×10 next to them), one augmentation, then
`--laeufe` trainings in their own folders, each with FP/h
(`fp_pro_stunde.json`) and tflite (`l<n>/tflite/<word>_float32.tflite`).
Run with `--trocken` first: it only prepares the folders and shows the counts
per recipe.

Durations (12 cores, two GPUs, 30k synthetic clips per side): augmentation
about 28 min on one CPU core, one training about 11 min alone and 13–14 min
when two run at once. Augmentations of different recipes run side by side
without slowing each other down. Two recipes × two runs: 56 min instead of
about 100.

Pitfalls:

- **`train.py` ends with exit code 1** (`No module named 'onnx_tf'`). The flag
  `--convert_to_tflite` defaults to the *string* `"False"`, which is truthy.
  The ONNX model is complete anyway, the tflite comes from `onnx2tf`
  afterwards. The script therefore checks for the ONNX file, not the exit code.
- **`onnx2tf` needs `-kat x`**, otherwise the input gets transposed.
  `train_runde.py` passes it.
- **GPU memory:** see `--weiche` in 1.5. Containers are started again in their
  own systemd scope; without it, rootless Podman's port forwarding hangs on
  your SSH session and dies with it.
- **Keep the SSH session open** if lingering is not enabled for the user on
  that machine, or start the round under `systemd-run --user`.

### 2.7 Measure

Fetch the tflite, give each candidate a bundle folder with the **live
manifest** (same gate as in operation) and measure against the package:

```bash
mkdir -p ~/wake-pakete/modelle/<candidate>/<word>
scp gpu-host:wakeword-studio/runden/rN/voll/l1/tflite/<word>_float32.tflite \
    ~/wake-pakete/modelle/<candidate>/<word>/<word>.tflite
cp models/wakewords/<word>/manifest.yaml ~/wake-pakete/modelle/<candidate>/<word>/
ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/<package> \
    --modell <word> --modell ~/wake-pakete/modelle/<candidate>/<word>
```

With exactly two models, `--paare` lists the clips that flip. If a candidate
misses narrowly, look at the labels of those clips: a wrong label is a finding
for 2.2, not an excuse for the bar.

FP/h (3 frames in a row at the live threshold) comes from `ergebnis.json`. It
measures only the general tendency to fire on foreign material, not the false
triggers in your room.

### 2.8 Decide

- **The spread between runs is large.** Two runs on the same features had
  FP/h 0.56 and 1.40; across eight runs of one package it ranged from 0.19 to
  1.22. A single run does not measure a change in the data. Hence at least two
  runs per recipe.
- **Do not keep rolling until one run passes.** That selects on the
  validation set, the bar becomes a training criterion, and the lucky run is
  not lucky in operation.
- If a run passes: deploy. If none passes but a candidate is clearly better in
  important groups, a **live trial** is possible, as a deliberate decision,
  with a comparison number and an abort rule fixed beforehand:

```bash
ow-venv/bin/python -m tools.wake_triage --seit 8 --auch-trigger
ow-venv/bin/python -m tools.wake_live_bilanz <from> <to>   # false triggers/day, calls/day
```

### 2.9 Deploy and the way back

Put `<word>.tflite` (and the ONNX with its `.onnx.data`) into the bundle,
note at the top of the manifest what it is and which bar it was measured
against, commit, back up the corpus, restart the service. The gate values in
the manifest were calibrated on the predecessor's scores; whether they fit the
new model only operation will show. The way back: the bundle files from the
commit before.

Then 2.1 starts again.

## Adapting the loop tools to your word

Part 1 is fully parameterised. The loop tools of Part 2 grew around `gaston`
and still recognise the word in STT text through word-specific patterns.
For another word, change these before the first triage:

| Where | What |
|---|---|
| `tools/wake_triage.py`, `GASTON_PAT` | regex for the word and its typical mishearings in STT text |
| `tools/wake_corpus.py`, `_WAKE_FORM` | word beginnings that count as "the word was heard" (also used by `wake_paket`) |
| `tools/wake_corpus.py`, `--bundle` default | your bundle name (or pass `--bundle`) |
| `tools/wake_paket.py`, `messen` without `--modell` | defaults to the bundle `gaston` (pass `--modell <word>`) |
| `tools/wake_live_bilanz.py` | counts only events of bundle `gaston` |

Collect the mishearings from your own triage output: how the STT
misspells your word depends on the word, the language and the STT model.

## What worked and what did not

- **Real calls from daily life** (×10 next to 30k synthetic clips) bring the
  recall. A purely synthetic model recognised the main user's voice worst.
  With ear-checked self-positives, recall rose sharply, and FP/h with it.
- **Automatic negatives** (near-misses in which no STT hears the word) make
  the model stricter without measurably lowering FP/h.
- **Ear labels only** gave FP/h 0 but halved the recall. Fewer positives move
  the model along the same recall-versus-false-trigger line; they do not lift
  the line.
- **The assistant's own TTS voice** is inside the training distribution if the
  model was trained on the same Piper voices. Played digitally it triggered
  the model; only the microphone's echo cancellation removed that. Without
  echo cancellation, put your own voice into training as a negative.
- **Turning the score thresholds** stops helping at some point: real calls and
  false triggers overlap in their peak. Then it is model work, or a second
  stage after the trigger (`voice_assistant/wake_pruefung.py`).

Open and noted, not done: averaging several runs as an ensemble instead of
picking one (`WAKEWORD_PROCESS.md`, "Streuung senken statt Glückslauf wählen").
