#!/usr/bin/env python3
"""Gelabelte Wakeword-Clips dauerhaft sichern und das Modell dagegen messen.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.wake_corpus bilanz     # was ist da, was fehlt
    ow-venv/bin/python -m tools.wake_corpus sichern    # Clips in den Dauer-Korpus
    ow-venv/bin/python -m tools.wake_corpus messen     # Modell gegen den Korpus

Warum es dieses Werkzeug gibt
-----------------------------
Das Trigger-Archiv (``TRIGGER_AUDIO_DIR``) löscht sich nach 30 Tagen selbst —
sonst läuft die Platte voll. Die LABELS dazu leben unbegrenzt weiter:
``wake_review.jsonl`` (per Ohr gefällt) und die aus dem Verhalten abgeleiteten
Selbst-Labels. Beides zusammen ist die Messbasis von
``tools/wake_rms_replay.py`` und der Trainingsstoff fürs Nachtraining.

Am 2026-08-22 ist genau das eingetreten, was diese Kombination erzwingt: ein
Service-Neustart räumte 56 Dateien nach Alter ab, darunter das Audio zu 6
Ohr-Urteilen. Die Labels standen weiter im JSONL, das Audio fehlte — die
Negativseite des Pegel-Sweeps fiel im selben Lauf von 26 auf 20 belegte
Fehltrigger. Der Verlust war unsichtbar: das Replay meldet keine fehlenden
Dateien, es rechnet einfach mit weniger. Ohr-Urteile sind das teuerste Label
im ganzen Verfahren (ein Mensch muss jeden Clip anhören) und waren am
schlechtesten geschützt.

Zwei Konsequenzen, beide umgesetzt:
 1. ``_cleanup_trigger_audio`` verschont ungesicherte Ohr-Urteile
    (``voice_assistant/assistant.py``).
 2. Dieses Werkzeug hebt gelabelte Clips aus dem selbstlöschenden Verzeichnis
    in einen Dauer-Korpus. Erst dann darf das Original verschwinden.

Was gesichert wird
------------------
Nur der **Wake-Clip** (``*_wake.wav`` / ``*_nearmiss.wav``, ~3 s Ringpuffer vor
dem Trigger) — auf ihn beziehen sich alle Labels und alle Messungen. Die
Folgeaufnahme (``*_rec.wav``) bleibt im Archiv und läuft dort nach 30 Tagen ab;
sie ist Beleg beim Anhören, aber weder Trainings- noch Messmaterial.

Gesichert wird nur, was ein **haltbares** Label hat: Ohr > Selbst. STT-Labels
sind ausdrücklich ausgenommen (``--auch-stt`` erzwingt sie) — die STT verhört
"Gaston" regelmäßig, siehe Docstring von ``tools/wake_triage.py``. Ein falsch
als Rauschen gesicherter echter Ruf wäre im Nachtraining ein hartes
Negativbeispiel für genau das Wort, das erkannt werden soll.

``messen`` — die Zahl, die das Nachtraining schlagen muss
--------------------------------------------------------
Spielt das aktuelle Bundle mit Live-Trigger-Semantik über den Korpus
(``wakeword_studio.scoring.BundleScorer``, mehrere Frame-Offsets) und zählt
getrennt:

    POSITIV  wie viele belegte echte Rufe das Modell auslöst   → darf NICHT fallen
    NEGATIV  wie viele belegte Fehltrigger es auslöst          → soll fallen

Das ist die Negativ-Hälfte des Validierungs-Gates aus
``Wakeword_Studio_Spec.md`` (Phase D), gerechnet auf echtem Haus-Material statt
auf Fremd-Audio.

``paket`` — Trainingspaket mit Tages-Split für das Nachtraining
---------------------------------------------------------------
Schnürt aus Dauer-Korpus + Studio-Takes ein tar.gz für die Trainings-
Pipeline auf dem ai-stack (``~/ai-stack/wakeword-studio/``, siehe README
dort). Die echten Clips werden VOR dem Packen in Train und Val geteilt,
und zwar über GANZE TAGE (Verfahren aus ``tools/verifier_probe.py``,
gleicher Grund: Wiederholungs-Cluster — derselbe Ruf mehrfach binnen
Sekunden — dürfen nicht über beide Seiten verteilt werden, sonst misst
die Validierung das Training).

Drei Entscheidungen, die das Paket festschreibt:

1. STUDIO-TAKES GEHEN KOMPLETT IN DIE VALIDIERUNG. Sie sind das härteste
   Recall-Set (absichtlich leise/fern/abgewandt) und das etablierte
   Test-Set von ``wakeword_studio score``. Gingen sie ins Training, wäre
   die empfindlichste Messlatte verbrannt.
2. AUCH DIE NEGATIVES WERDEN GESPLITTET, mit derselben Tagespartition.
   Gingen alle harten Fehltrigger ins Training, wäre die Negativseite von
   ``wake_corpus messen`` hinterher Selbstmessung.
3. DIE VAL-SEITE DES PAKETS IST DIE NACHHER-MESSUNG. Nach dem Training
   zählt nur sie (plus FP/h gegen ``validation_set_features.npy`` auf dem
   ai-stack) — nicht die Train-Clips, auf denen das Modell gut sein MUSS.

Einspeisung drüben: WAVs aus ``train/`` zu den synthetischen Clips nach
``train_out/gaston/{positive,negative}_train/`` legen, dann
``--augment_clips --overwrite`` und ``--train_model`` (vorher
``podman stop llm``!). Details im README, das im Paket liegt.

Grenzen, ehrlich
----------------
1. KEINE FP-RATE PRO STUNDE. Der Korpus enthält nur Clips, die das Modell
   bereits erregt haben — er misst, ob ein neues Modell die BEKANNTEN
   Fehltrigger abstellt, nicht wie es sich über Stunden Alltag schlägt. Für
   FP/Stunde braucht es durchgehendes Negativ-Audio (Spec, Phase D).
2. ÜBERANPASSUNG IST MÖGLICH. Wer auf genau diese Clips trainiert und auf
   genau diesen Clips misst, misst sich selbst. Das Ergebnis ist eine untere
   Schranke, kein Beleg für Generalisierung — der kommt erst aus frischen
   Fehltriggern der Wochen nach dem Deploy. Der Tages-Split in ``paket``
   entschärft das für die Val-Seite, hebt es aber nicht auf.
3. DER KORPUS IST SCHIEF. Fehltrigger sammeln sich abends (TV), echte Rufe
   verteilen sich über den Tag. Klassenanteile hier sind kein Abbild des
   Alltags.
4. DIE VAL-NEGATIVSEITE IST KLEIN. Bei ~25 harten Fehltriggern landen nach
   dem Split nur eine Handvoll in Val — die Nachher-Zahl dort hat breite
   Streuung und trägt erst zusammen mit FP/h und frischen Betriebswochen.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.wake_corpus", *sys.argv[1:]])

from voice_assistant.config import (  # noqa: E402
    TRIGGER_AUDIO_DIR,
    WAKE_CORPUS_DIR,
    WAKE_LOG_PATH,
)
from tools.wake_rms_replay import labels_fuer_clips  # noqa: E402

MANIFEST = os.path.join(WAKE_CORPUS_DIR, "manifest.jsonl")
ORDNER = {"echter_ruf": "positiv", "rauschen": "negativ"}


def _wake_events() -> dict[str, dict]:
    """audio -> Messwerte des Streaks (peak/hits/rms/beam) aus dem Wake-Log."""
    out: dict[str, dict] = {}
    try:
        with open(WAKE_LOG_PATH, encoding="utf-8") as fh:
            for zeile in fh:
                if not zeile.strip():
                    continue
                r = json.loads(zeile)
                if r.get("result") not in ("trigger", "nearmiss"):
                    continue
                out[r.get("audio", "")] = {
                    k: r.get(k) for k in ("ts", "peak", "hits", "rms", "beam",
                                          "result", "failed_on", "bundle")
                }
    except FileNotFoundError:
        pass
    return out


def _manifest_lesen() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            for zeile in fh:
                if zeile.strip():
                    r = json.loads(zeile)
                    out[r["audio"]] = r
    except FileNotFoundError:
        pass
    return out


def _korpus_dateien() -> dict[str, str]:
    """audio -> Pfad im Korpus."""
    out = {}
    for klasse, unter in ORDNER.items():
        pfad = os.path.join(WAKE_CORPUS_DIR, unter)
        if os.path.isdir(pfad):
            for name in os.listdir(pfad):
                if name.endswith(".wav"):
                    out[name] = os.path.join(pfad, name)
    return out


def run_sichern(args) -> int:
    labels = labels_fuer_clips()
    events = _wake_events()
    manifest = _manifest_lesen()
    im_korpus = _korpus_dateien()

    neu = Counter()
    schon = 0
    verloren = []
    uebersprungen = Counter()

    for audio, lab in sorted(labels.items()):
        klasse, quelle = lab["klasse"], lab["quelle"]
        if klasse not in ORDNER:
            uebersprungen["unklar"] += 1
            continue
        if quelle == "stt" and not args.auch_stt:
            uebersprungen["stt-Label (schwach, siehe Docstring)"] += 1
            continue
        if audio in im_korpus:
            schon += 1
            continue
        quelle_pfad = os.path.join(TRIGGER_AUDIO_DIR, audio)
        if not os.path.exists(quelle_pfad):
            verloren.append((audio, klasse, quelle))
            continue
        ziel_dir = os.path.join(WAKE_CORPUS_DIR, ORDNER[klasse])
        os.makedirs(ziel_dir, exist_ok=True)
        if not args.trocken:
            shutil.copy2(quelle_pfad, os.path.join(ziel_dir, audio))
            manifest[audio] = {
                "audio": audio,
                "klasse": klasse,
                "quelle": quelle,
                **{k: v for k, v in (events.get(audio) or {}).items() if v is not None},
            }
        neu[klasse] += 1

    if not args.trocken and manifest:
        os.makedirs(WAKE_CORPUS_DIR, exist_ok=True)
        with open(MANIFEST, "w", encoding="utf-8") as fh:
            for audio in sorted(manifest):
                fh.write(json.dumps(manifest[audio], ensure_ascii=False) + "\n")

    kopf = "WÜRDE SICHERN (Trockenlauf)" if args.trocken else "GESICHERT"
    print(f"{kopf}: {neu['echter_ruf']} echte Rufe, {neu['rauschen']} Fehltrigger")
    print(f"  schon im Korpus : {schon}")
    for grund, n in uebersprungen.most_common():
        print(f"  übersprungen    : {n}  ({grund})")
    if verloren:
        print(f"\n  ⚠️  {len(verloren)} gelabelte Clips haben KEIN Audio mehr "
              f"(Archiv-Cleanup nach {30} Tagen):")
        for audio, klasse, quelle in verloren:
            print(f"      {audio}  {klasse} ({quelle})")
        print("      Diese Labels sind für jede Messung verloren — nur der "
              "Eintrag im JSONL bleibt.")
    print(f"\nKorpus: {WAKE_CORPUS_DIR}")
    return 0


def run_bilanz(args) -> int:
    labels = labels_fuer_clips()
    im_korpus = _korpus_dateien()
    im_archiv = set(os.listdir(TRIGGER_AUDIO_DIR)) if os.path.isdir(TRIGGER_AUDIO_DIR) else set()

    tab: Counter = Counter()
    erosion = []
    for audio, lab in labels.items():
        klasse, quelle = lab["klasse"], lab["quelle"]
        if klasse not in ORDNER:
            continue
        tab[(klasse, quelle)] += 1
        if audio not in im_korpus and audio not in im_archiv:
            erosion.append((audio, klasse, quelle))

    print("=" * 62)
    print("KORPUS-BILANZ")
    print("=" * 62)
    print(f"Korpus   : {sum(1 for a in im_korpus if a in labels)} Clips gesichert "
          f"({WAKE_CORPUS_DIR})")
    print(f"Archiv   : {len([a for a in im_archiv if a.endswith('.wav')])} Dateien "
          "(löscht sich nach 30 Tagen selbst)")
    print("\nLabels nach Klasse und Quelle (Ohr sticht Selbst sticht STT):")
    for (klasse, quelle), n in sorted(tab.items()):
        gesichert = sum(1 for a, l in labels.items()
                        if l["klasse"] == klasse and l["quelle"] == quelle and a in im_korpus)
        print(f"  {klasse:11s} {quelle:7s} {n:4d}   davon gesichert: {gesichert}")

    if erosion:
        print(f"\n⚠️  EROSION: {len(erosion)} Label(s) ohne Audio — weder im Korpus "
              "noch im Archiv.")
        print("    Diese Clips fehlen in jeder Messung, ohne dass ein Werkzeug es meldet.")
        for audio, klasse, quelle in sorted(erosion):
            print(f"      {audio}  {klasse} ({quelle})")
    else:
        print("\n✅ Keine Erosion: zu jedem Label existiert noch Audio.")
    return 0


def run_messen(args) -> int:
    from wakeword_studio.scoring import BundleScorer

    im_korpus = _korpus_dateien()
    manifest = _manifest_lesen()
    if not im_korpus:
        print("Korpus ist leer — erst 'sichern' laufen lassen.")
        return 1

    scorer = BundleScorer(args.bundle, args.threshold)
    print(f"Bundle '{scorer.bundle}' — threshold={scorer.threshold}, "
          f"min_hits={scorer.min_hits}, min_peak={scorer.min_peak}")
    print(f"Korpus: {len(im_korpus)} Clips\n")

    ergebnis: dict[str, list] = {"positiv": [], "negativ": []}
    for audio, pfad in sorted(im_korpus.items()):
        klasse = manifest.get(audio, {}).get("klasse")
        unter = ORDNER.get(klasse) or os.path.basename(os.path.dirname(pfad))
        if unter not in ergebnis:
            continue
        r = scorer.score_wav(pfad)
        ergebnis[unter].append((audio, r))
        if args.verbose:
            print(f"  {audio}  score={r['max_score']:.2f} streak={r['best_streak']} "
                  f"trigger={'JA' if r['triggered'] else 'nein'} robust={r['robust']}")

    print("=" * 62)
    print("AUSGANGSMESSUNG — was ein nachtrainiertes Modell schlagen muss")
    print("=" * 62)
    for unter, richtung in (("positiv", "soll hoch bleiben"), ("negativ", "soll fallen")):
        clips = ergebnis[unter]
        if not clips:
            print(f"{unter:8s} — keine Clips im Korpus")
            continue
        feuert = sum(1 for _, r in clips if r["triggered"])
        print(f"{unter:8s} {feuert:3d}/{len(clips):3d} lösen aus  "
              f"({feuert / len(clips):.0%})   ← {richtung}")
    print("\nGrenzen dieser Zahl: siehe Docstring (keine FP/Stunde, "
          "Überanpassungs-Gefahr, schiefer Korpus).")
    return 0


def run_paket(args) -> int:
    """Trainingspaket bauen: Tages-Split, Verzeichnisbaum, Manifest, tar.gz."""
    import tarfile

    from tools.verifier_probe import _day, _split_days

    im_korpus = _korpus_dateien()
    manifest = _manifest_lesen()
    pos = sorted(a for a in im_korpus if manifest.get(a, {}).get("klasse") == "echter_ruf")
    neg = sorted(a for a in im_korpus if manifest.get(a, {}).get("klasse") == "rauschen")
    if not pos or not neg:
        print("Korpus unvollständig — erst 'sichern' laufen lassen.")
        return 1

    studio = sorted(
        os.path.join(d, f)
        for d, _, files in os.walk(args.samples_dir)
        for f in files if f.endswith(".wav")
    )

    val_tage, train_tage = _split_days(pos, neg, args.seed, args.val_anteil)

    ziel = args.out
    if os.path.exists(ziel):
        print(f"Zielverzeichnis existiert schon: {ziel} — erst wegräumen.")
        return 1

    plan = []  # (quelle, relpfad)
    zaehl = Counter()
    for audio in pos + neg:
        klasse = "positive" if audio in set(pos) else "negative"
        seite = "val" if _day(audio) in val_tage else "train"
        plan.append((im_korpus[audio], os.path.join(seite, klasse, audio)))
        zaehl[f"{seite}/{klasse}"] += 1
    for pfad in studio:
        # Sprecher bleibt im Namen — Kollisionen zwischen Sprechern ausschließen
        name = os.path.basename(os.path.dirname(pfad)) + "_" + os.path.basename(pfad)
        plan.append((pfad, os.path.join("val", "positive_studio", name)))
        zaehl["val/positive_studio"] += 1

    for quelle, rel in plan:
        dst = os.path.join(ziel, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(quelle, dst)

    paket_manifest = {
        "erstellt": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "val_anteil_ziel": args.val_anteil,
        "split_verfahren": "ganze Tage, tools/verifier_probe._split_days",
        "train_tage": sorted(train_tage),
        "val_tage": sorted(val_tage),
        "zaehlung": dict(zaehl),
        "labels": {a: manifest[a] for a in pos + neg},
        "vorher_messung": "wake_corpus messen 2026-08-22: positiv 51/68, negativ 19/20 (gaston @0.35)",
    }
    with open(os.path.join(ziel, "paket_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(paket_manifest, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(ziel, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(_PAKET_README.format(seed=args.seed))

    tar_pfad = ziel.rstrip("/") + ".tar.gz"
    with tarfile.open(tar_pfad, "w:gz") as tar:
        tar.add(ziel, arcname=os.path.basename(ziel.rstrip("/")))

    print(f"Paket: {tar_pfad}")
    for k in sorted(zaehl):
        print(f"  {k:22s} {zaehl[k]:3d} Clips")
    print(f"  Split: {len(train_tage)} Train-Tage / {len(val_tage)} Val-Tage (Seed {args.seed})")
    return 0


_PAKET_README = """# Nachtrainings-Paket gaston — echte Clips mit Tages-Split (Seed {seed})

Erzeugt von `tools/wake_corpus.py paket` (Repo openclaw_voice_assist, Branch
feature/wakeword-nachtraining). Labels: Ohr > Selbst, keine STT-Labels.

## Einspeisung (ai-stack, ~/wakeword-studio/)

1. `train/positive/*.wav`  -> zu den synthetischen Clips nach `train_out/gaston/positive_train/`
2. `train/negative/*.wav`  -> nach `train_out/gaston/negative_train/` (adversarial negatives)
3. `val/**`                -> NICHT einspeisen. Das ist die Nachher-Messung.
4. `podman stop llm`, dann `train.py --training_config gaston.yaml --augment_clips --overwrite`
   und `--train_model`, danach `podman start llm`. Stolpersteine: README im ai-stack-Repo.

## Validierungs-Gate vor jedem Deploy

- Recall: `val/positive/` + `val/positive_studio/` durchs neue Modell (Ziel >= 0.9);
  auf dem Pi: `wakeword_studio score` + `wake_corpus messen`.
- FP-Seite: `val/negative/` (klein! nur Richtungsindikator) UND
  `eval_debounce.py` gegen `validation_set_features.npy` (Ziel < 1 FP/h,
  immer MIT Debounce rechnen).
- Vorher-Zahl, die zu schlagen ist: positiv 51/68, negativ 19/20 (@0.35).

Alle WAVs: 16 kHz mono int16, ~3 s (Wake-Ring vor dem Trigger).
Familienstimmen — bleiben auf diesem Host, kein Upload irgendwohin.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="befehl", required=True)

    p = sub.add_parser("sichern", help="gelabelte Clips in den Dauer-Korpus kopieren")
    p.add_argument("--auch-stt", action="store_true",
                   help="auch STT-geratene Labels sichern (schwach — siehe Docstring)")
    p.add_argument("--trocken", action="store_true", help="nur zeigen, nichts kopieren")
    p.set_defaults(func=run_sichern)

    p = sub.add_parser("bilanz", help="Bestand und Erosion zeigen")
    p.set_defaults(func=run_bilanz)

    p = sub.add_parser("messen", help="aktuelles Modell gegen den Korpus")
    p.add_argument("--bundle", default="gaston")
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=run_messen)

    p = sub.add_parser("paket", help="Trainingspaket mit Tages-Split schnüren")
    p.add_argument("--seed", type=int, default=20260916,
                   help="Seed der Tagespartition — im Manifest festgehalten")
    p.add_argument("--val-anteil", type=float, default=0.3, dest="val_anteil")
    p.add_argument("--out", default="/tmp/gaston_nachtraining_paket")
    p.add_argument("--samples-dir", dest="samples_dir",
                   default=os.path.join(os.path.dirname(os.path.dirname(
                       os.path.abspath(__file__))),
                       "models", "wakewords", "gaston", "samples"),
                   help="Studio-Takes (gehen komplett in die Validierung)")
    p.set_defaults(func=run_paket)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
