#!/usr/bin/env python3
"""Misst, ob ein sprecherspezifischer ``custom_verifier`` (openwakeword 0.6.0)
das Gaston-Wakeword verbessert — mehr Recall und/oder weniger Fehltrigger.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.verifier_probe
    ow-venv/bin/python -m tools.verifier_probe --schnitt 2.5 --seed 20260802

Zweck
-----
Eine Zahl liefern: bringt ein ``custom_verifier``-Modell, das auf den echten
Rufen dieses Hauses trainiert ist, mehr Recall UND/ODER weniger Fehltrigger
als das Basismodell allein? Das Ergebnis entscheidet Jochen — das Werkzeug
empfiehlt nichts. Motto der Session: **„Nur bei belegtem Gewinn ins Bundle."**

Was es NICHT tut (Nicht-Zweck)
------------------------------
- kein Deploy, keine ``verifier.pkl``/``.joblib`` ins Bundle
- schreibt nichts ins Repo, kein ``manifest.yaml``, kein ``voice_assistant/``
- liest nur Dateien (Archive, Log, Modell) — stoppt keinen Service
- das trainierte Modell landet in ``/tmp``, nicht im Repo

Datenlage (Bestand 2026-08-02, siehe WAKEWORD_PROCESS.md)
--------------------------------------------------------
    Labels   ~/.openclaw/workspace/wake_triage.jsonl
    Events   ~/.openclaw/workspace/wake_events.log
    Audio    ~/.openclaw/workspace/voice/triggers/   (*_nearmiss/*_wake/*_rec)
    Modell   models/wakewords/gaston/gaston.tflite

Harte Positiv-Labels liefert ``tools.wake_triage._selbst_labels`` (importiert,
nicht nachgebaut): 55 echter_ruf aus Handlungen (Wiederholung binnen 15 s /
ausgeführtes Schaltkommando). Die übrigen echter_ruf aus ``wake_triage.jsonl``
(STT-geraten, ``quelle != "selbst"``) sind WEICH — separate Testgruppe, nur
Hinweis, kein Beweis. Trainiert wird ausschließlich mit den harten.

Negativ-Quelle: die Folgeaufnahmen ``*_rec.wav`` (137) — echte deutsche Sätze
derselben Bewohner, gleiches Mikro, gleicher Raum. Davon wird der Anfang
abgeschnitten (Pre-Roll 1,5 s + Wakewort), damit „Gaston" verschwindet.

Der zentrale Fallstrick: ``klasse=='rauschen'`` ist als Negativ-Set untauglich
(nur 2/104 dieser Clips enthalten überhaupt Sprache). ``train_custom_verifier``
will „miscellaneous speech NOT containing the target wake word" — mit stummen
Negativen lernt der Verifier bloß Sprache-vs-Stille und schlägt im Betrieb bei
jeder Äußerung an. Deshalb die rec-Clips.

Aufteilung: nach KALENDERTAG, nicht zufällig
--------------------------------------------
Ein Near-Miss und der Trigger 4 s später sind derselbe Ruf; ein zufälliger
Clip-Split verteilt sie über Train und Test und mißt sich selbst. Tage
(``YYYYMMDD`` aus dem Dateinamen) bleiben ganz zusammen. Seed-gemischte
Tagesliste, dann gierig aufgefüllt bis der TEST-Anteil ≈ 30 % der Clip-Menge
 erreicht — deterministisch, reproduzierbar. Positiv- und Negativ-Clips
werdenselbe Tagespartition genutzt (ein Clip gehört dem Tag entsprechend).

Messung
-------
Live-Gate-Semantik wird NICHT neu erfunden — ``wakeword_studio.scoring.
BundleScorer.score_pcm`` bildet sie ab (Streck ≥ ``min_hits``, Streck-Peak ≥
``min_peak``, 1-Frame-Gap-Toleranz). Für Basis UND Verifier jeweils auf dem
Holdout:

- Recall     = Anteil der Positiv-Clips, die das Gate passieren
- Fehltrigger = Anteil der Negativ-Clips, die das Gate passieren
- beides mit absoluten Zahlen (``13/16``), nicht nur Prozent

Verifier-Pfad: ``openwakeword.train_custom_verifier`` trainiert eine logistische
Regression auf openWakeWord-Embeddings; im Betrieb ersetzt sie ab Modell-Score
≥ ``custom_verifier_threshold`` (0,1) den Modell-Score durch ihre Klasse-1-
Wahrscheinlichkeit (``openwakeword/model.py``). Diese Ersetzung wird hier
originalgetreu nachgebildet, indem ein zweites ``Model`` MIT
``custom_verifier_models`` geladen wird — ``BundleScorer.score_pcm`` läuft dann
über die substituierten Scores, das Gate bleibt dasselbe. Apples-to-apples.

CAVEAT zum Gate: ``BundleScorer`` verwendet einheitlich ``min_peak`` (0,7) und
``min_hits`` (2). Die LIVE-Engine (``assistant.py``) hat zusätzlich gestaffelte
Peak-Bedingungen für Kurz-Streaks (``min_peak_short`` 0,9 ab 2 Frames,
``min_peak_single`` 0,75 für einzelne Spitzen). ``BundleScorer`` bildet das
NICHT ab — absolute Zahlen sind also keine Produktionsfiguren. Da Basis und
Verifier aber amSELBEN (vereinfachten) Gate gemessen werden, bleibt das Δ
verlässlich. Siehe Ausgabe-Caveat am Ende.

Selbstkontrolle (eingebaut)
---------------------------
Nach dem Schneiden werden ALLE Negativ-Clips mit ``BundleScorer`` gegen das
Basismodell gescored und ausgegeben, wie viele noch ``max_score >= 0,35``
erreichen — Clips mit „Gaston"-Rest, die das Negativ-Set verseuchen würden.
Übersteigt das ein paar Prozent, Schnittlänge erhöhen. Die gewählte Länge
samt dieser Zahl steht im Docstring unten (Messreihe) und in jedem Lauf.

MESSREIHE (Stand 2026-08-02, Bestand siehe oben, Seed 20260802, Schnitt 2,5 s)
-----------------------------------------------------------------------------
Selbstkontrolle Negativ-Schnitt (Base-Modell, alle 137 rec-Clips):
  - Schnitt 2,0 s: 135 verwertbar, 2 zu kurz, max_score>=0,35: 2 (1,5 %),
    davon 1 gate-getriggert (peak 0,914) — „Gaston"-Rest bei langsamer Aussprache
  - Schnitt 2,5 s: 132 verwertbar, 5 zu kurz, max_score>=0,35: 1 (0,8 %),
    0 gate-getriggert  ← gewählt
  - Der verbliebene Clip (20260721_125551, max 0,457) wird aus dem TRAININGS-
    Negativ-Set entfernt (Verdacht auf Wakewort-Rest), im Holdout behalten
    (konservatives FP-Maß; beeinflußt Basis und Verifier gleich).

Hard-Positiv-Beitrag zum Training (Base max_score >= 0,5 nötig, damit
``train_custom_verifier`` Features erfasst):
  - 53/55 harter Rufe tragen Features bei; 2 (peak 0,383 / 0,450) fallen unter
    0,5 und werden von der Bibliothek STILLSCHAWEIGEND ignoriert — sie sind
    Lost-Rufe, die das Basismodell kaum erkennt. Der Verifier lernt also nur
    von den EINFACHEN Rufen. Methodische Grenze (siehe Ausgabe).

Tages-Split (Seed 20260802, Ziel 30 % TEST nach Clip-Menge):
  - TEST:  16 harte Pos + 19 weiche Pos + 48 rec (davon 46 nach Schnitt verwertbar)
  - TRAIN: 39 harte Pos + 89 rec → 38 pos + 86 neg im Training
    (1 pos unter 0,5 ignoriert; 1 kontaminierter + 2 zu kurze rec entfernt)

Ergebnis Holdout (Basis vs. Verifier, BundleScorer-Gate, 2026-08-02):
  - Recall hart   Basis 12/16 (75 %)  Verifier 15/16 (94 %)  Δ +19 %
  - Fehltrigger   Basis  0/46 ( 0 %)  Verifier  0/46 ( 0 %)  Δ  ±0   ⚠ FP-Achse offen
  - Recall weich  Basis 18/19 (95 %)  Verifier 17/19 (89 %)  Δ −5 %  (nur Hinweis)

Belastbarkeit (McNemar exakt, gepaart — nachgetragen 2026-08-02):
  - hart        3 gewonnen / 0 verloren   p = 0,250   NICHT signifikant
  - hart+weich  4 gewonnen / 2 verloren   p = 0,688   NICHT signifikant
  - Für p < 0,05 bräuchte es bei diesem Holdout 6 Wechsel ohne Gegenverlust.

FP-MEßSET (rauschen-Clips, nachgetragen 2026-08-02; 104 Clips, nicht im
Training, alle als Test — 36 trigger + 68 nearmiss, davon 9 hart):
  - trigger  (Feuer senken)  Basis 30/36 → Verifier  7/36   net −23  p=0,000 sig.
  - nearmiss (neues Feuer?)  Basis 14/68 → Verifier  2/68   net −12  p=0,002 sig.
  - nur TEST-Tage (39)       trigger 10/12→2/12 (−8,p=.008)  nearmiss 6/27→0/27 (−6,p=.031)
  - nur harte Labels (9)     trigger  8/9 →1/9  (−7, p=0,016 sig.)
  - FP-Saldo über alle 104:  Basis 44 → Verifier 9   (−35, McNemar p=0,000 sig.)
  - Neu erzeugte FPs: 1 (20260728_193833_gaston_nearmiss). Der befürchtete
    Schaden (Verifier HEBT Near-Misses über das Gate) bleibt aus — er senkt
    stattdessen massiv. 1 neuer FP steht 35 eliminierten gegenüber.

Befund (Stand 2026-08-02, beide Achsen): Der Verifier senkt die Fehltrigger
auf den rauschen-Clips HOCH-signifikant (44→9, p<0,001) und erzeugt nur 1
neuen FP — das Risiko, das den Bau motivierte, ist damit widerlegt. ABER
der Recall-Gewinn (12→15 von 16) bleibt NICHT signifikant (p=0,25): 3
Wechsel gegen 0 reichen bei diesem Holdout nicht. Nach der strengen Regel
des Auftrags (eine Achse nicht signifikant ⇒ nicht deploy-reif) ist das
damit **nicht deploy-reif** — der Blocker ist jetzt allein die kleine
Recall-Stichprobe, nicht mehr die FP-Achse.

Die erste Fassung dieses Docstrings schrieb „Recall-Plus belegt". Das war
ein Quotenvergleich (75 % → 94 %) über gepaarte Stichproben — der Test dazu
fehlte. In diesem Projekt gilt: eine Richtung ist noch kein Befund.

Was dieses FP-Meß-Set NICHT mißt (zusätzlich zu den Caveats in der Ausgabe):
  - Es sind 3-s-Schnappschüsse rund um ein Gate-Ereignis, keine Dauer-
    Mitschrift → FP/Stunde läßt sich nicht ableiten.
  - Es sind Clips, die das BASISMODELL schon fast gereizt hatte. Sprache, die
    das Basis-Modell nie erregte, fehlt im Archiv — der Verifier könnte darauf
    weitere FP erzeugen, die hier unsichtbar bleiben.
  - 95/104 Labels sind STT-geraten (meist „kein Text"); nur 9 hart. Steckt ein
    echter, unterbrochener Ruf im Set, hält der Verifier ihn hoch und zählt
    als FP → FP-Zahl ist konservative OBERGRENZE, verzerrt GEGEN den Verifier.
    Kehrseite: war ein „eliminierter FP" in Wahrheit ein echter Ruf, hat der
    Verifier ihn UNTERDRÜCKT — dann ist −35 teils versteckter Recall-Verlust,
    nicht nur FP-Gewinn. Die 9 harten Fälle (−7, p=0,016, 0 neu) sind frei von
    diesem Einwand und tragen allein.

Noch offen (vor Deploy):
  1. Holdout vergrößern: k-fache Kreuzvalidierung über die Tagespartitionen,
     damit alle 55 harten Rufe als Testfall dienen, statt 39 nur zum Trainieren.
  2. FP-Label-Qualität: die 95 weichen rauschen-Clips per Ohr prüfen, soweit
     möglich — ein echter Ruf darin würde die FP-Zahl als Obergrenze entlarven.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict

import numpy as np

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.verifier_probe", *sys.argv[1:]])

from voice_assistant.config import TRIGGER_AUDIO_DIR  # noqa: E402
from tools.wake_triage import (  # noqa: E402
    TRIAGE_PATH,
    _lade_wake_events,
    _selbst_labels,
)
from wakeword_studio.scoring import BundleScorer, load_wav_16k  # noqa: E402

RATE = 16000
# Determinismus: train_custom_verifier nutzt np.random.randint intern
# (get_reference_clip_features, N=5). Globaler Seed macht Train + Split
# reproduzierbar.
DEFAULT_SEED = 20260802
# Schnitt vor den rec-Clips: Pre-Roll 1,5 s + Wakewort. 2,5 s gewählt, weil
# 2,0 s noch 1,5 % „Gaston"-Rest ließen (siehe Messreihe oben).
DEFAULT_CUT_SEC = 2.5
# Post-cut Base-max_score ab hier gilt als Wakewort-Verdacht -> raus aus dem
# TRAININGS-Negativ-Set (Betriebs-threshold des Gaston-Modells, manifest.yaml).
CONTAM_THRESHOLD = 0.35
# Betriebsschwelle des Verifiers (openwakeword/model.py: custom_verifier_threshold).
CUSTOM_VERIFIER_THRESHOLD = 0.1
# Mindestrestlänge eines geschnittenen Negativ-Clips, sonst verworfen.
_NEG_MIN_SEC = 0.25
_DAY_RE = re.compile(r"^(\d{8})_\d{6}_")


def _day(name: str) -> str | None:
    m = _DAY_RE.match(name)
    return m.group(1) if m else None


def _split_days(pos_names: list[str], neg_names: list[str],
                seed: int, frac: float) -> tuple[set[str], set[str]]:
    """Tages-Partition (TEST/TRAIN) über die Union beider Populationen.

    Seed-gemischte Tagesliste, dann gierig Tage zum TEST-Anteil, bis dessen
    Clip-Menge ≈ ``frac`` der Gesamt-Clip-Menge. Ganze Tage bleiben zusammen,
    Positiv- und Negativ-Clips nutzen dieselbe Partition.
    """
    names = pos_names + neg_names
    by_day: dict[str, int] = defaultdict(int)
    for n in names:
        d = _day(n)
        if d:
            by_day[d] += 1
    days = sorted(by_day)
    rng = np.random.RandomState(seed)
    order = list(days)
    rng.shuffle(order)
    target = frac * len(names)
    test_days: set[str] = set()
    tc = 0
    for d in order:
        if tc < target:
            test_days.add(d)
            tc += by_day[d]
    train_days = set(days) - test_days
    return test_days, train_days


def _cut(samples: np.ndarray, sec: float) -> np.ndarray:
    off = int(RATE * sec)
    return samples[off:] if len(samples) > off else samples[:0]


def _verifier_scorer(model_path: str, verifier_path: str, base: BundleScorer) -> BundleScorer:
    """BundleScorer mit ausgetauschtem Modell: das zweite Model lädt denselben
    .tflite, aber MIT ``custom_verifier_models``. Dessen ``predict`` ersetzt ab
    Score >= CUSTOM_VERIFIER_THRESHOLD den Modell-Score durch die Verifier-
    Wahrscheinlichkeit — ``BundleScorer.score_pcm`` läuft dann über die
    substituierten Scores, das Gate bleibt dasselbe. Apples-to-apples."""
    from openwakeword import Model  # type: ignore[import-not-found]

    key = base._key
    vm = Model(
        wakeword_models=[model_path],
        custom_verifier_models={key: verifier_path},
        custom_verifier_threshold=CUSTOM_VERIFIER_THRESHOLD,
    )
    clone = BundleScorer("gaston")
    clone._model = vm
    return clone


def _soft_positive_audio() -> list[str]:
    """Weiche Positiv-Labels: echter_ruf aus wake_triage.jsonl, die NICHT
    schon hart (selbst) gelabelt sind. Reihenfolge stabil."""
    if not os.path.exists(TRIAGE_PATH):
        return []
    ev = _lade_wake_events()
    hart = {a for a, v in _selbst_labels(ev).items() if v["klasse"] == "echter_ruf"}
    soft: list[str] = []
    with open(TRIAGE_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("klasse") == "echter_ruf" and row["audio"] not in hart:
                soft.append(row["audio"])
    return soft


def _gate_rate(clips: list[tuple[str, dict]], scorer: BundleScorer) -> tuple[int, int]:
    """Wie viele Clips passieren das Gate? Liefert (getriggert, gesamt)."""
    treffer = 0
    for _, res in clips:
        if res["triggered"]:
            treffer += 1
    return treffer, len(clips)


def _lade_rauschen() -> list[dict]:
    """rauschen-Clips aus wake_triage.jsonl als FP-Meßmenge (kein Training).

    Liefert ``[{audio, art, hard}]``, art ∈ {'trigger','nearmiss'}:

    - trigger: das Gate feuerte damals (echter Fehltrigger, ``_wake.wav``).
    - nearmiss: das Gate feuerte FAST (``_nearmiss.wav``) — ein Verifier kann
      solche Clips durch Score-Substitution ab 0,1 NEU über das Gate heben.

    ``hard=True`` nur bei Selbst-Label 'rauschen' (Nutzer brach mit Stopp-Wort
    ab); der Rest ist STT-geraten (meist „kein Text erkannt"), was laut
    WAKEWORD_PROCESS.md KEIN verläßliches Fehltrigger-Label ist — ein echter
    Ruf, bei dem der Nutzer unterbrochen wurde, sieht hier wie Rauschen aus.
    Das verzerrt die FP-Zahl GEGEN den Verifier (s. Caveat in der Ausgabe).
    """
    if not os.path.exists(TRIAGE_PATH):
        return []
    ev = _lade_wake_events()
    hart = {a for a, v in _selbst_labels(ev).items() if v["klasse"] == "rauschen"}
    out: list[dict] = []
    with open(TRIAGE_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("klasse") != "rauschen":
                continue
            out.append({"audio": row["audio"],
                        "art": row.get("art") or "",
                        "hard": row["audio"] in hart})
    return out


def _mcnemar(basis: list[tuple[str, dict]],
             verifier: list[tuple[str, dict]]) -> tuple[int, int, float]:
    """Gepaarter exakter McNemar-Test über dieselben Clips.

    Liefert (gewonnen, verloren, p). Gewonnen = Basis verfehlt, Verifier
    trifft; verloren = umgekehrt. Clips, die beide gleich bewerten, tragen
    nichts bei — genau deshalb ist der Test hier richtig und ein Vergleich
    zweier Quoten falsch: Basis und Verifier laufen über DIESELBEN Clips,
    die Stichproben sind nicht unabhängig.

    Warum das eingebaut ist: die erste Fassung schrieb bei +3 Rufen
    „Recall-Plus ist belegt". Gemessen sind das 3 Wechsel gegen 0 in der
    Gegenrichtung — p = 0,25. Bei einem Holdout dieser Größe braucht es
    6 Wechsel ohne Gegenverlust für p < 0,05. „Belegt" war es nicht.
    """
    vm = {a: r for a, r in verifier}
    gewonnen = verloren = 0
    for a, rb in basis:
        rv = vm.get(a)
        if rv is None:
            continue
        if not rb["triggered"] and rv["triggered"]:
            gewonnen += 1
        elif rb["triggered"] and not rv["triggered"]:
            verloren += 1
    n = gewonnen + verloren
    if n == 0:
        return gewonnen, verloren, 1.0
    from math import comb
    p = min(1.0, 2 * sum(comb(n, k)
                         for k in range(0, min(gewonnen, verloren) + 1)) / 2 ** n)
    return gewonnen, verloren, p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--schnitt", type=float, default=DEFAULT_CUT_SEC,
                    help=f"Schnittlänge (s) am Anfang der rec-Clips "
                         f"(Default {DEFAULT_CUT_SEC})")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help=f"Seed für Tages-Shuffle und Verifier-Training "
                         f"(Default {DEFAULT_SEED})")
    ap.add_argument("--frac", type=float, default=0.30,
                    help="Angestrebter TEST-Anteil nach Clip-Menge (Default 0.30)")
    ap.add_argument("--keep-verifier", action="store_true",
                    help="Verifier-.joblib nach /tmp schreiben und Pfad ausgeben "
                         "(sonst im Temp-Verzeichnis, das nach Lauf weg ist)")
    args = ap.parse_args()

    if not os.path.isdir(TRIGGER_AUDIO_DIR):
        print(f"Kein Trigger-Archiv unter {TRIGGER_AUDIO_DIR}")
        return 1

    np.random.seed(args.seed)  # Reproduzierbarkeit des Trainings

    da = set(os.listdir(TRIGGER_AUDIO_DIR))

    # --- 1. Labels: hart (selbst) vs. weich (nur STT) --------------------
    ev = _lade_wake_events()
    selbst = _selbst_labels(ev)
    hart_pos = sorted(a for a, v in selbst.items() if v["klasse"] == "echter_ruf")
    hart_pos = [a for a in hart_pos if a in da]
    soft_pos = [a for a in _soft_positive_audio() if a in da]
    rec_roh = sorted(n for n in da if n.endswith("_rec.wav"))

    print("=" * 72)
    print("  Verifier-Probe — mißt Basis gegen custom_verifier (Holdout)")
    print("=" * 72)
    print(f"  Archiv : {TRIGGER_AUDIO_DIR}")
    print(f"  Modell  : models/wakewords/gaston/gaston.tflite")
    print(f"  harte Pos (selbst): {len(hart_pos)}   "
          f"weiche Pos (nur STT): {len(soft_pos)}   rec-Clips: {len(rec_roh)}")
    print(f"  Schnitt {args.schnitt:.1f}s | Seed {args.seed} | TEST-Anteil {args.frac:.0%}")
    print()

    # --- 2. Audio laden (einmal) -----------------------------------------
    def _lad(name: str) -> np.ndarray | None:
        try:
            return load_wav_16k(os.path.join(TRIGGER_AUDIO_DIR, name))
        except (OSError, ValueError):
            return None

    pos_audio = {a: _lad(a) for a in hart_pos}
    pos_audio = {a: s for a, s in pos_audio.items() if s is not None and len(s) > 0}
    soft_audio = {a: _lad(a) for a in soft_pos}
    soft_audio = {a: s for a, s in soft_audio.items() if s is not None and len(s) > 0}
    rec_audio = {n: _lad(n) for n in rec_roh}
    rec_audio = {n: s for n, s in rec_audio.items() if s is not None and len(s) > 0}

    base = BundleScorer("gaston")
    model_path = os.path.join("models", "wakewords", "gaston", "gaston.tflite")
    if not os.path.exists(model_path):
        # absolut fallback, falls BundleScorer einen anderen Pfad auflöst
        mp_alt = None
        for cand in (getattr(base, "_model_arg", None),):
            if cand and os.path.exists(str(cand)):
                mp_alt = str(cand)
        model_path = mp_alt or model_path

    # --- 3. Selbstkontrolle: Base-Scores der geschnittenen rec-Clips -----
    print("  [1/4] Selbstkontrolle: rec-Clips geschnitten gegen Basismodell …")
    cut_results: dict[str, dict] = {}
    zu_kurz = 0
    for n, s in rec_audio.items():
        stueck = _cut(s, args.schnitt)
        if len(stueck) < int(RATE * _NEG_MIN_SEC):
            zu_kurz += 1
            continue
        cut_results[n] = base.score_pcm(stueck)
    verwertbar = len(cut_results)
    kontam = [n for n, r in cut_results.items() if r["max_score"] >= CONTAM_THRESHOLD]
    kontam_gate = [n for n, r in cut_results.items() if r["triggered"]]
    print(f"        verwertbar {verwertbar}, zu kurz {zu_kurz}, "
          f"max_score>={CONTAM_THRESHOLD}: {len(kontam)} "
          f"({len(kontam)/max(verwertbar,1)*100:.1f}%), "
          f"davon gate-getriggert: {len(kontam_gate)}")
    if len(kontam) > 0.05 * verwertbar:
        print("        ⚠  Über 5 % Wakewort-Rest — Schnittlänge erhöhen (--schnitt).")
    if kontam:
        for n in kontam:
            print(f"           • {n}  max_score {cut_results[n]['max_score']:.3f}")

    # --- 4. Tages-Split --------------------------------------------------
    test_days, train_days = _split_days(hart_pos, list(rec_audio.keys()),
                                        args.seed, args.frac)

    def bucket(name: str) -> str:
        return "TEST" if _day(name) in test_days else "TRAIN"

    pos_train = [a for a in hart_pos if bucket(a) == "TRAIN"]
    pos_test = [a for a in hart_pos if bucket(a) == "TEST"]
    soft_test = [a for a in soft_audio if bucket(a) == "TEST"]
    # Negativ: TRAIN ohne Wakewort-Verdacht (Selbstkontrolle), TEST komplett.
    neg_train = [n for n in rec_audio
                 if bucket(n) == "TRAIN" and n not in kontam]
    neg_test = [n for n in rec_audio if bucket(n) == "TEST"]

    print()
    print("  [2/4] Tages-Split (ganze Tage, deterministisch):")
    print(f"        TEST  {len(test_days)} Tage: {len(pos_test)} harte Pos, "
          f"{len(soft_test)} weiche Pos, {len(neg_test)} rec")
    print(f"        TRAIN {len(train_days)} Tage: {len(pos_train)} harte Pos, "
          f"{len(neg_train)} rec (nach Selbstkontrolle bereinigt)")

    # --- 5. Base-Scores der Positiv-Clips (für Recall + Train-Beitrag) ---
    print()
    print("  [3/4] Base-Scores der harten Positiven …")
    pos_base: dict[str, dict] = {}
    for a in hart_pos:
        if a in pos_audio:
            pos_base[a] = base.score_pcm(pos_audio[a])
    beitraegt = sum(1 for r in pos_base.values() if r["max_score"] >= 0.5)
    print(f"        {beitraegt}/{len(pos_base)} harte Pos mit max_score>=0,5 "
          f"(nur diese liefern Verifier-Trainings-Features; der Rest wird "
          f"von train_custom_verifier stillschweigend ignoriert)")
    ignoriert = [(a, round(pos_base[a]["max_score"], 3)) for a in pos_base
                 if pos_base[a]["max_score"] < 0.5]
    if ignoriert:
        for a, m in ignoriert:
            print(f"           • ignoriert: {a}  max_score {m}")

    # --- 6. Verifier trainieren (nur TRAIN, nur hart) --------------------
    print()
    print("  [4/4] Verifier trainieren (TRAIN: harte Pos + bereinigte rec) …")
    tmpdir = tempfile.mkdtemp(prefix="verifier_probe_")
    ver_path = os.path.join(tmpdir, "gaston_verifier.joblib")
    if args.keep_verifier:
        ver_path = os.path.join(tempfile.gettempdir(),
                                f"gaston_verifier_seed{args.seed}.joblib")
    pos_arrays = [pos_audio[a] for a in pos_train if a in pos_audio
                  and pos_base.get(a, {}).get("max_score", 0) >= 0.5]
    neg_arrays = [_cut(rec_audio[n], args.schnitt) for n in neg_train
                  if len(_cut(rec_audio[n], args.schnitt)) >= int(RATE * _NEG_MIN_SEC)]

    from openwakeword import train_custom_verifier  # type: ignore[import-not-found]
    try:
        train_custom_verifier(
            positive_reference_clips=pos_arrays,
            negative_reference_clips=neg_arrays,
            output_path=ver_path,
            model_name=model_path,
        )
    except ValueError as exc:
        print()
        print("  ✗ Training fehlgeschlagen:", exc)
        print("    (typisch: keine Positiv-Features, weil zu wenige Rufe den "
              "Base-Score 0,5 erreichen — siehe ignorierte Clips oben)")
        return 1
    print(f"        Verifier geschrieben: {ver_path}")

    # --- 7. Messung auf dem Holdout (Basis vs. Verifier) -----------------
    ver_scorer = _verifier_scorer(model_path, ver_path, base)

    def _score_pos(scorer, names, audio_map):
        out = []
        for a in names:
            if a in audio_map:
                out.append((a, scorer.score_pcm(audio_map[a])))
        return out

    # Base-Scores fürs Holdout werden WIEDERVERWENDET, nicht neu berechnet:
    # pos_base (Schritt 3) für die harten, cut_results (Schritt 1) für die
    # rec-Clips. Nur die weichen Positiven waren noch nicht gescored.
    hb = [(a, pos_base[a]) for a in pos_test if a in pos_base]
    hv = _score_pos(ver_scorer, pos_test, pos_audio)
    # Soft-Pos (nur Hinweis)
    sb = _score_pos(base, soft_test, soft_audio)
    sv = _score_pos(ver_scorer, soft_test, soft_audio)
    # Negativ (Cut) — Base aus cut_results, Verifier frisch
    nb = [(n, cut_results[n]) for n in neg_test if n in cut_results]
    nv = [(n, ver_scorer.score_pcm(_cut(rec_audio[n], args.schnitt)))
          for n in neg_test
          if len(_cut(rec_audio[n], args.schnitt)) >= int(RATE * _NEG_MIN_SEC)]

    hb_t, hb_n = _gate_rate(hb, base)
    hv_t, hv_n = _gate_rate(hv, ver_scorer)
    sb_t, sb_n = _gate_rate(sb, base)
    sv_t, sv_n = _gate_rate(sv, ver_scorer)
    nb_t, nb_n = _gate_rate(nb, base)
    nv_t, nv_n = _gate_rate(nv, ver_scorer)

    def _fmt(t, n):
        return f"{t}/{n} ({t/max(n,1)*100:.0f}%)" if n else "—"

    def _delta(t_neu, n_neu, t_alt, n_alt):
        if not n_neu or not n_alt:
            return ""
        d = t_neu / n_neu - t_alt / n_alt
        return f"Δ {d:+.0%}"

    print()
    print("=" * 72)
    print("  ERGEBNIS Holdout (Basis vs. Verifier, BundleScorer-Gate)")
    print("=" * 72)
    print(f"  {'':22} {'Basis':>14} {'Verifier':>14} {'':>8}")
    print(f"  {'Recall hart (n='+str(hb_n)+')':22} {_fmt(hb_t,hb_n):>14} "
          f"{_fmt(hv_t,hv_n):>14} {_delta(hv_t,hv_n,hb_t,hb_n):>8}")
    print(f"  {'Fehltrigger rec (n='+str(nb_n)+')':22} {_fmt(nb_t,nb_n):>14} "
          f"{_fmt(nv_t,nv_n):>14} {_delta(nv_t,nv_n,nb_t,nb_n):>8}")
    print(f"  {'Recall weich (n='+str(sb_n)+')':22} {_fmt(sb_t,sb_n):>14} "
          f"{_fmt(sv_t,sv_n):>14} {_delta(sv_t,sv_n,sb_t,sb_n):>8}")
    print(f"  (weich = nur STT-geraten, separater Hinweis — kein Beweis)")

    # --- Trägt der Unterschied? Gepaart testen, nicht Quoten vergleichen ---
    print()
    print("  Belastbarkeit (McNemar exakt, gepaart über dieselben Clips):")
    g_h, v_h, p_h = _mcnemar(hb, hv)
    g_a, v_a, p_a = _mcnemar(hb + sb, hv + sv)
    for label, g, v, p in (("hart", g_h, v_h, p_h),
                           ("hart+weich", g_a, v_a, p_a)):
        urteil = "signifikant" if p < 0.05 else "NICHT signifikant"
        print(f"    {label:<12} {g} gewonnen / {v} verloren   p = {p:.3f}   {urteil}")
    if p_h >= 0.05:
        noetig = next(b for b in range(1, 30) if 2 / 2 ** b < 0.05)
        print(f"    → Bei diesem Holdout braucht es {noetig} Wechsel ohne Gegenverlust")
        print(f"      für p < 0,05. Ein Recall-Plus ist hier SICHTBAR, nicht BELEGT —")
        print(f"      für die Deploy-Entscheidung zu wenig.")

    # --- Pro-Clip-Beleg der harten Holdout-Positiven ---------------------
    # Wo die ±Clips herkommen. Zeigt auch, ob die 2 Low-Peak-Rufe (Base
    # max_score < 0,5, vom Training ignoriert) wenigstens zur Inferenz vom
    # Verifier zurückgewonnen werden — der Verifier feuert ab Base-Score 0,1,
    # nicht erst 0,5.
    hv_map = {a: r for a, r in hv}
    print()
    print("  Pro-Clip Beleg (harte Holdout-Positive, B=basisgetriggert, V=verifiergetriggert):")
    for a, rb in hb:
        rv = hv_map.get(a)
        bm = rb["max_score"]
        vm = rv["max_score"] if rv else float("nan")
        flag = "  ★ gewonnen" if (rv and not rb["triggered"] and rv["triggered"]) else ""
        if rv and rb["triggered"] and not rv["triggered"]:
            flag = "  ✗ verloren"
        print(f"    {a}  basis {bm:.2f}{'B' if rb['triggered'] else '-'} "
              f"ver {vm:.2f}{'V' if rv and rv['triggered'] else '-'}{flag}")

    # --- 8. FP-Meß-Set: rauschen-Clips (nicht im Training, reiner Test) --
    # Die rec-Clips triggern praktisch nie (0/46) — die FP-Achse blieb dort
    # offen. Hier die echte FP-Meßmenge: rauschen-Clips aus wake_triage.jsonl,
    # getrennt nach art. Ein Verifier kann FPs nicht nur SENKEN, sondern auch
    # ERZEUGEN: er ersetzt den Score ab 0,1 und hebt damit Near-Misses über
    # das Gate. Beide Richtungen zählen — ein Verifier, der 5 echte FPs beseitigt
    # und 5 neue erzeugt, hat nichts gewonnen.
    print()
    print("-" * 72)
    print("  FP-MEßSET (rauschen-Clips — nicht im Training, alle als Test)")
    print("-" * 72)
    raus = _lade_rauschen()
    raus_audio: dict[str, np.ndarray] = {}
    for r in raus:
        s = _lad(r["audio"])
        if s is not None and len(s) > 0:
            raus_audio[r["audio"]] = s
    rmeta = {r["audio"]: r for r in raus if r["audio"] in raus_audio}
    n_trig = sum(1 for a in rmeta if rmeta[a]["art"] == "trigger")
    n_nm = sum(1 for a in rmeta if rmeta[a]["art"] == "nearmiss")
    n_hard = sum(1 for a in rmeta if rmeta[a]["hard"])
    print(f"  geladen: {len(raus_audio)} Clips   "
          f"trigger {n_trig} (Gate feuerte), nearmiss {n_nm} (fast), "
          f"davon hart {n_hard} (selbst 'abgebrochen')")
    print(f"  ⚠ Label-Qualität: {len(raus_audio)-n_hard}/{len(raus_audio)} sind "
          f"STT-geraten (meist 'kein Text'). Leeres Transkript ist laut "
          f"WAKEWORD_PROCESS.md KEIN verläßliches FP-Label — steckt ein echter")
    print(f"    Ruf darunter, hält der Verifier ihn hoch und er wirkt wie FP. "
          f"Die FP-Zahl ist somit eine konservative OBERGRENZE (verzerrt GEGEN "
          f"den Verifier). Die {n_hard} harten Fälle unten zusätzlich separat.")

    rb = [(a, base.score_pcm(s)) for a, s in raus_audio.items()]
    rv = [(a, ver_scorer.score_pcm(s)) for a, s in raus_audio.items()]

    def _fp_line(lbl: str, b, v) -> tuple[list[str], list[str], float]:
        bm = {a: r for a, r in b}
        vm = {a: r for a, r in v}
        bt = sum(1 for r in bm.values() if r["triggered"])
        vt = sum(1 for r in vm.values() if r["triggered"])
        elim = [a for a in bm if bm[a]["triggered"] and not vm[a]["triggered"]]
        neu = [a for a in bm if not bm[a]["triggered"] and vm[a]["triggered"]]
        _, _, p = _mcnemar(b, v)
        delta = vt - bt
        dz = f"{delta:+d}".replace("+0", "±0")
        sig = "sig." if p < 0.05 else "n.s."
        print(f"  {lbl:<26} Basis {bt:>3}/{len(b):<3}  Verifier {vt:>3}/{len(b):<3}  "
              f"net {dz:>3}  (−{len(elim)} elim, +{len(neu)} neu)  p={p:.3f} {sig}")
        return elim, neu, p

    def _grp(lst, art):
        return [(a, r) for a, r in lst if rmeta[a]["art"] == art]

    print()
    print("  Alle Clips (volle Meßmenge, keine Tageshaltung):")
    etrig, ntrig, ptrig = _fp_line("trigger (Feuer senken)", _grp(rb, "trigger"), _grp(rv, "trigger"))
    enm, nnm, pnm = _fp_line("nearmiss (neues Feuer?)", _grp(rb, "nearmiss"), _grp(rv, "nearmiss"))
    # Neue FPs = Schaden. Clip-Namen ausgeben, wie der Auftrag verlangt.
    if nnm:
        print(f"    ↳ neue FPs (nearmiss, {len(nnm)}):")
        for a in nnm:
            print(f"        • {a}")
    if ntrig:
        print(f"    ↳ neue FPs (trigger, {len(ntrig)} — base ließ sie durch, "
              f"verifier auch): {ntrig}")
    # Eliminierte echte FPs = Benefit. Nur nennen, wenn welche da sind.
    if etrig:
        print(f"    ↳ eliminierte echte FPs (trigger, {len(etrig)}):")
        for a in etrig:
            print(f"        • {a}")

    # --- Nur TEST-Tage: faire Meßmenge ohne Trainings-Tagesbindung -------
    # Die rauschen-Clips sind nicht im Training, aber die rec-Trainingsnegativen
    # stammen von TRAIN-Tagen. Stand die nämliche Störquelle an einem TRAIN-Tag
    # auch in den rec-Clips, könnte der Verifier sie daraus gelernt haben — dann
    # wäre die Gesamt-FP-Zahl zu optimistisch. Restriktiv auf TEST-Tage geprüft
    # (deren rec-Clips hielt der Split ebenfalls fern): weicht sie stark ab,
    # liegt Tagesbindung vor.
    def _test(lst):
        return [(a, r) for a, r in lst if _day(a) in test_days]

    rb_t = _test(rb)
    rv_t = _test(rv)
    if rb_t:
        print()
        print(f"  Nur TEST-Tage ({len(rb_t)} Clips — fair, kein Trainings-Leak):")
        _fp_line("trigger (TEST)", _grp(rb_t, "trigger"), _grp(rv_t, "trigger"))
        _fp_line("nearmiss (TEST)", _grp(rb_t, "nearmiss"), _grp(rv_t, "nearmiss"))

    # --- Nur harte Labels (9, selbst 'abgebrochen') -----------------------
    def _hard(lst):
        return [(a, r) for a, r in lst if rmeta[a]["hard"]]

    rb_h = _hard(rb)
    rv_h = _hard(rv)
    if rb_h:
        print()
        print(f"  Nur harte Labels ({len(rb_h)} Clips — verläßlich, Nutzer brach ab):")
        eh, nh, ph = _fp_line("trigger (hart)", _grp(rb_h, "trigger"), _grp(rv_h, "trigger"))
        if nh:
            print(f"    ↳ neue FPs unter harten Triggern: {nh}")
            for a in nh:
                print(f"        • {a}")

    # --- Gesamt-FP-Saldo über beide Gruppen ---
    fb_t = sum(1 for _, r in rb if r["triggered"])
    fv_t = sum(1 for _, r in rv if r["triggered"])
    saldo = fv_t - fb_t
    sz = f"{saldo:+d}".replace("+0", "±0")
    print()
    print(f"  FP-Saldo über alle {len(raus_audio)} rauschen-Clips: "
          f"Basis {fb_t} → Verifier {fv_t}  ({sz}). "
          f"{'Senkung.' if saldo < 0 else 'Erhöhung/Schaden.' if saldo > 0 else 'Unverändert.'}")

    # --- Bewertung: Gesamturteil über BEIDE Achsen -----------------------
    # Deploy nur, wenn Recall-Gewinn UND FP-Verhalten zusammen tragen. Ist eine
    # Achse nicht signifikant, heißt das „nicht deploy-reif“ — auch wenn die
    # Richtung stimmt (Auftrag 2026-08-02).
    print()
    print("=" * 72)
    print("  GESAMTURTEIL (beide Achsen müssen tragen)")
    print("=" * 72)
    recall_sig = p_h < 0.05
    recall_richtung = "+" if hv_t > hb_t else ("-" if hv_t < hb_t else "0")
    # FP-Achse: Signifikanz über alle rauschen-Clips gepaart (trigger+nearmiss).
    _, _, p_fp = _mcnemar(rb, rv)
    fp_sig = p_fp < 0.05
    fp_richtung = "-" if saldo < 0 else ("+" if saldo > 0 else "0")
    print(f"  Recall : {hb_t}/{hb_n} → {hv_t}/{hv_n}  ({recall_richtung})   "
          f"McNemar p={p_h:.3f} {'sig.' if recall_sig else 'n.s.'}")
    print(f"  FP     : Basis {fb_t} → Verifier {fv_t}  ({fp_richtung})   "
          f"McNemar p={p_fp:.3f} {'sig.' if fp_sig else 'n.s.'}")
    print()
    # Strenger Auftrag: eine Achse nicht signifikant ⇒ nicht deploy-reif, auch
    # wenn die Richtung stimmt. Die einzige Ausnahme, die „deploy-reif" zuläßt:
    # Recall signifikant PLUS FP nicht schlechter (sinkt oder unverändert).
    if recall_sig and saldo <= 0:
        if saldo < 0:
            print("  ➜ DEPLOY-REIF: Recall signifikant +, FP signifikant −. Beide "
                  "Achsen tragen — Jochen entscheidet den Deploy.")
        else:
            print("  ➜ DEPLOY-REIF: Recall signifikant +, FP unverändert. Recall-"
                  "Gewinn ohne FP-Preis — Jochen entscheidet.")
    elif recall_sig and saldo > 0:
        print("  ➜ Nicht deploy-reif: Recall signifikant, ABER FP steigt "
              f"({fb_t}→{fv_t}). Preis zu hoch.")
    else:
        # Recall nicht signifikant — das ist der Blocker, egal wie gut die
        # FP-Achse aussieht. OB die FP-Achse trägt, wird nevertheless genannt,
        # weil es das Risiko profiliert, das den Bau motiviert hat.
        if fp_sig and saldo < 0:
            print(f"  ➜ Nicht deploy-reif: Recall nicht signifikant (p={p_h:.3f}). "
                  f"FP-Achse trägt wohl (signifikant −, {fb_t}→{fv_t}), aber eine "
                  f"signifikante Achse reicht nicht.")
        elif fp_sig and saldo > 0:
            print(f"  ➜ Nicht deploy-reif: Recall nicht signifikant UND FP steigt "
                  f"signifikant ({fb_t}→{fv_t}). Beide Achsen unzureichend.")
        else:
            print(f"  ➜ Nicht deploy-reif: Recall nicht signifikant (p={p_h:.3f}). "
                  f"FP-Achse nicht signifikant oder unverändert — keine Achse "
                  f"trägt ausreichend.")
        print("    Eine Richtung ist hier kein Befund — siehe McNemar-Zeilen oben.")

    # --- Caveats ----------------------------------------------------------
    print()
    print("  Caveats (was diese Messung NICHT leistet):")
    print("    · FP-Label-Qualität: nur "+str(n_hard)+"/"+str(len(raus_audio))+
          " rauschen-Clips sind hart (Nutzer 'abgebrochen'), der Rest STT-")
    print("      geraten. Leeres Transkript ist laut WAKEWORD_PROCESS.md KEIN")
    print("      verläßliches FP-Label — ein echter, unterbrochener Ruf sieht hier")
    print("      wie Rauschen aus. Die FP-Zahl ist eine konservative OBERGRENZE,")
    print("      verzerrt GEGEN den Verifier (hält er einen echten Ruf hoch, zählt")
    print("      er als FP). Die Kehrseite trifft die Senkung: war ein 'eliminierter")
    print("      FP' in Wahrheit ein echter Ruf (falsches rauschen-Label), hat der")
    print("      Verifier ihn UNTERDRÜCKT — dann ist −35 teils versteckter Recall-")
    print("      Verlust, nicht nur FP-Gewinn. Die 9 harten Fälle (−7, p=0,016, 0")
    print("      neu) sind frei von diesem Einwand und tragen allein.")
    print("    · FP-Meßmenge ist ereignisbezogen, nicht stundenbasiert: die")
    print("      rauschen-Clips sind 3-s-Schnappschüsse rund um ein Gate-Ereignis,")
    print("      keine Dauer-Mitschrift. Eine FP/Stunde läßt sich daraus NICHT")
    print("      ableiten — nur 'feuert der Verifier auf historisches Nah-Fehl-")
    print("      Audio?'. Zudem sind es Clips, die das BASISMODELL schon fast")
    print("      gereizt hatten; völlig andere Sprache, die das Basis-Modell nie")
    print("      erregte, fehlt im Archiv. Der Verifier könnte darauf weitere FP")
    print("      erzeugen, die hier unsichtbar bleiben.")
    print("    · Gate: BundleScorer nutzt einheitlich min_peak 0,7 / min_hits 2.")
    print("      Die LIVE-Engine hat zusätzlich min_peak_short 0,9 (2-Frame) und")
    print("      min_peak_single 0,75 (1-Spitze). Absolute Zahlen sind daher keine")
    print("      Produktionsfiguren — das Δ (Basis↔Verifier) aber schon, weil beide")
    print("      am selben vereinfachten Gate gemessen werden.")
    print("    · Train-Beitrag: train_custom_verifier erfaßt nur Positiv-Frames")
    print("      mit Base-Score >= 0,5. Lost-Rufe darunter (hier "
          f"{len(ignoriert)}/55) liefern keine Features — der Verifier lernt nur")
    print("      von Rufen, die das Basismodell ohnehin gut erkennt. Zur Inferenz")
    print("      feuert er aber schon ab 0,1 (siehe Pro-Clip-Beleg).")
    print("    · Sprecherbindung: der Verifier ist sprecherspezifisch. Der Recall-")
    print("      Plus gilt für DIESEN Sprecher auf Diesem Mikro — nicht allgemein.")
    print("    · Holdout klein: 16 harte Pos → Recall-Schätzung grobkörnig; ")
    if not args.keep_verifier:
        print(f"    · Verifier lag in {tmpdir} (temporär) — mit --keep-verifier ")
        print("      nach /tmp sichern.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
