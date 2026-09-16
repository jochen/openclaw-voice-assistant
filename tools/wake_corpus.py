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

Grenzen, ehrlich
----------------
1. KEINE FP-RATE PRO STUNDE. Der Korpus enthält nur Clips, die das Modell
   bereits erregt haben — er misst, ob ein neues Modell die BEKANNTEN
   Fehltrigger abstellt, nicht wie es sich über Stunden Alltag schlägt. Für
   FP/Stunde braucht es durchgehendes Negativ-Audio (Spec, Phase D).
2. ÜBERANPASSUNG IST MÖGLICH. Wer auf genau diese Clips trainiert und auf
   genau diesen Clips misst, misst sich selbst. Das Ergebnis ist eine untere
   Schranke, kein Beleg für Generalisierung — der kommt erst aus frischen
   Fehltriggern der Wochen nach dem Deploy.
3. DER KORPUS IST SCHIEF. Fehltrigger sammeln sich abends (TV), echte Rufe
   verteilen sich über den Tag. Klassenanteile hier sind kein Abbild des
   Alltags.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter

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

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
