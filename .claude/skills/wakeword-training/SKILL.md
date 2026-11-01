---
name: wakeword-training
description: Guide a user through training their own openWakeWord wake word for this voice assistant, from the first synthetic model (setup, voice selection by ear, clip generation, training, bundle) to the retraining loop with real clips from their home (labelling by ear, package, bar before training, parallel training, measuring, deciding, deploying). Use when someone wants a new or custom wake word, wants to retrain or improve the existing one, asks about false triggers or missed calls as training material, or mentions train_runde.py, clips_erzeugen.py, gpu_einrichten.sh, wake_paket or WAKEWORD_TRAINING.md.
---

# Wake word training

You are guiding a person through `WAKEWORD_TRAINING.md` in this repository.
That guide is the source of truth for commands, durations and pitfalls; read
the section for the current step before acting and do not duplicate it from
memory. This skill adds what a guide cannot: where the person stands, the
order, and the points where you must stop and hand the decision back.

Talk to the person in their language. The tools and file names are German
(`clips_erzeugen`, `rezept`, `laeufe`); explain them once when they come up.

## 1. Find out where they stand

Ask, or check yourself where you can:

- **No own model yet** (no `models/wakewords/<word>/`, or only built-in
  models like `hey_jarvis`) → Part 1.
- **Own model running, little data** (fewer than ~2 weeks of triggers in
  `~/.openclaw/workspace/wake_events.log`, or no labels yet) → collecting
  phase. Explain 2.1–2.3, set up the labelling routine, and say plainly that
  retraining now would train on too little.
- **Own model running for weeks, labelled clips in the corpus** → Part 2.

Also establish once and write down for the session:

- the word, its bundle name, the language (also `text_erkennung.sprache` in
  the manifest), and whether this is the assistant
  machine or a separate GPU machine (host name, user, working folder);
- GPU(s) and their CUDA version (`nvidia-smi`), free disk (≥ 25 GB), RAM;
- whether a container on the GPU machine holds GPU memory (→ `--weiche`);
- where the person keeps private data. Clips and recordings contain the
  voices of their household and **never** go into a public repository,
  including forks of this one. Check `.gitignore` covers `samples/` and the
  corpus before the first recording.

## 2. Part 1: the first model

Follow guide sections 1.1–1.8 in order. Your job at each step:

1. **Setup (1.1):** choose `TORCH_INDEX` from the CUDA version, run
   `gpu_einrichten.sh`, check its final "Prüfung" block (torch sees the GPU,
   feature files present, `mit_rirs` > 200 files, `audioset_16k` > 1000). Long
   downloads: run in the background and tell the person how long.
2. **Spellings and voices (1.2):** help write `<word>_clips.yaml` from
   `clips_vorlage.yaml`: candidate spellings (plain, variants, IPA for sounds
   the language's voices get wrong), voices of that language from
   rhasspy/piper-voices, similar-sounding negatives from their daily speech.
   Run `hoerproben`. **STOP: the person listens and decides** which voices
   and spellings stay and with which shares. Do not judge audio yourself, do
   not use an STT as a stand-in: the question is whether it sounds like it
   is said in *their* home.
3. **Clips (1.3), config (1.4), training (1.5):** run `clips_erzeugen.py alle`,
   copy `training_vorlage.yaml`, set `model_name` and `target_phrase`, leave
   the rest. Train with `--rezept synth --laeufe 2`. Report both runs.
4. **Check (1.6):** run `pruefen`. If a voice group is far below the others,
   propose removing it and regenerating, and let the person decide.
5. **Bundle (1.7):** build the bundle and manifest, explain each gate value.
   Have them record test takes with `wakeword_studio record` (it stops the
   assistant service; the microphone is exclusive) and score them.
   **STOP before switching the live profile:** the person enables it.
6. **Live (1.8):** explain what gets archived and when to come back.

## 3. Part 2: the retraining loop

Follow guide sections 2.1–2.9. For a word other than `gaston`, the bundle's
manifest needs a `text_erkennung` block ("Adapting the loop tools to your
word"). Run the first triage with `--auch-trigger`, collect what the STT made
of their real calls, propose `muster` and `wortanfaenge` from that, show the
person and let them confirm. Err towards too wide, never too narrow: a narrow
pattern turns real calls into negatives.

Rules you hold to, because each one was learned by getting it wrong:

- **Labels: the ear wins.** Use `wake_triage` for pre-sorting only. For every
  clip a decision depends on, the person listens (`review_audio.py export`
  with `--liste`, the microphone muted first). Never turn STT "noise" labels
  on trigger clips into negatives, and check a new self-label rule by ear
  before it produces training data.
- **Back up before restart:** `wake_corpus sichern` before every restart of
  the assistant, or audio of labelled clips is deleted.
- **The bar comes first.** After `wake_paket messen` gives the before number,
  draft the bar per validation group plus FP/h. **STOP: the person sets it.**
  Write it into their log and commit it *before* starting training. If they
  want to train without a bar, say what is lost (any result can then be
  called a success) and let them decide.
- **Recipes:** propose, then **STOP: the person chooses** which recipes and
  how many runs (at least two each). Run `train_runde.py --trocken` first and
  show the counts.
- **No lucky picking.** Report every run against the bar, not just the best.
  Never start extra runs because none passed. Spread between runs on the same
  data was up to a factor of six in FP/h. The ensemble of all runs of a recipe
  (`model:` as a list in the manifest) is a fair candidate because it picks
  nothing; measure it against the same bar, and say so if you compared more
  than one ensemble.
- **Narrow misses:** use `messen --paare` to show which clips flip and which
  label source they carry. A wrong label goes back to labelling. It does not
  change the bar.
- **Deploy or live trial: STOP, the person decides.** For a live trial, fix
  the comparison number (`wake_live_bilanz` over the previous week, after a
  fresh triage) and an abort rule before switching. Keep the way back: note
  the commit with the previous bundle files in the manifest header.

## 4. Running long jobs

- Clip generation (~1 h), augmentation (~30 min) and training (~11 min per
  run) are long. Run them in the background and keep the SSH session alive
  (or use `systemd-run --user` on the GPU machine when lingering is off).
- `train.py` exit code 1 at the end is expected (`onnx_tf` missing); judge by
  the ONNX file and `status.txt`, as `train_runde.py` does.
- If you stopped something to free GPU memory, confirm it runs again
  afterwards.

## 5. After each round

Write down, in the person's measurement log: date, package, recipes, bar,
every run's numbers, the decision and who made it. A round without a written
bar and a recorded decision cannot be compared with the next one.
