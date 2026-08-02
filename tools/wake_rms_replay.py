#!/usr/bin/env python3
"""Pegel-Gate fürs Wakeword gegen das Archiv messen statt raten.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.wake_rms_replay
    ow-venv/bin/python -m tools.wake_rms_replay --schwelle 300
    ow-venv/bin/python -m tools.wake_rms_replay --von 100 --bis 700 --schritt 50
    ow-venv/bin/python -m tools.wake_rms_replay --nur-studio   # ohne Archiv

Was hier gemessen wird
---------------------
Für jeden archivierten Wake-/Nearmiss-Clip das lauteste 300-ms-Fenster als
RMS bestimmt und gegen eine Schwelle gehalten. Getrennt ausgewiesen nach
Labelklasse:

  - ECHTE RUFFE VERLOREN — der Preis: ein Ruf, den das Gate blockt.
  - FEHLTRIGGER GEBLOCKT — der Gewinn: Rauschen, das ohne Gate durchkäme.

Die Rechnung ist IDENTISCH zum Live-Gate (``voice_assistant/assistant.py``);
beide importieren ``loudest_window_rms`` aus ``voice_assistant/wake_rms.py``.
Sonst misst das Replay etwas anderes als das Gate tut.

Warum die Schwelle 300 und nicht 400
------------------------------------
Gemessen 2026-08-02 (Bestand siehe unten): 400 ist genau der leiseste
beobachtete echte Ruf (402) — eine auf die Stichprobe angepasste Schwelle, bei
der der nächste leise Ruf durchfällt. 300 lässt 25 % Sicherheitsabstand und
blockt immer noch 42 % der Fehltrigger. Gewählt wird 300, nicht 400.

Labelquellen in dieser Rangfolge (stärkste zuerst)
--------------------------------------------------
    wake_review.jsonl   ← Jochens Ohr (stärkste Quelle, sticht alles)
    _selbst_labels()    ← hart, aus Handlungen (wiederholt/aktuator/stopp)
    wake_triage.jsonl   ← STT-geraten (schwach, verhört „Gaston")

Diese Rangfolge ist neu und wird öfter gebraucht — sie steckt hier als
``labels_fuer_clips()`` und ist bewusst eine eigene Hilfsfunktion, nicht
verstreut im Hauptprogramm. Ein Ohr-Urteil sticht jede Regel: es hat zwei als
„rauschen" geführte Clips als echte Rufe entlarvt (siehe review_audio.py).

Studio-Takes aus ``models/wakewords/gaston/samples/*/*.wav`` sind eine
zusätzlich mitgemessene Positivgruppe — die absichtlich leisen/fernen Rufe
(abgewandt, fern, leise) und damit der härteste Test gegen einen
Recall-Verlust. Keiner fällt unter 400.

Bekannte Schwächen (ehrlich)
----------------------------
1. ABSOLUTE RMS-WERTE SIND GAIN-ABHÄNGIG. Der ReSpeaker verstärkt ×4; ändert
   sich Hardware oder Gain, verschiebt sich die ganze Skala und die Schwelle
   stimmt nicht mehr. Robuster wäre ein Abstand zum gemessenen Grundpegel
   (``wakeword_studio/recorder.py:331`` macht das bereits so). Für diesen
   Schritt bleibt es beim absoluten Wert. Woran man merkt, dass er nicht mehr
   passt: steigt der Anteil geblockter echter Rufe im Near-Miss-Log
   (``failed_on: min_rms``), ist die Schwelle zu hoch für die aktuelle
   Verstärkung.
2. DIE NEGATIVSEITE IST MIT n=24 KLEIN, die Positivseite (77) trägt. Der
   Befund steht und fällt mit dem ausbleibenden Recall-Verlust, nicht mit der
   genauen Blockquote.

Modus ``--nur-studio``: Schwelle ohne Alltagsarchiv
---------------------------------------------------
Der normale Lauf braucht ``wake_triage.jsonl`` und ``wake_review.jsonl`` —
Ergebnisse aus Wochen Betrieb plus Handarbeit am Ohr. Am Tag eins sind beide
leer; ein Fremder kann die Schwelle so nicht herleiten. ``--nur-studio``
verzichtet auf beides und arbeitet allein aus geführten Takes
(``wakeword_studio record``, ~10 Minuten).

Vorschlag: **round(leisester Take × 0,7)**. Dieser Faktor ist nicht erfunden,
sondern nachgerechnet: hier liegt der leiseste Studio-Take bei 427, ×0,7 = 299
— praktisch exakt die 300, die aus dem *vollen* Datensatz (57 Alltagsrufe +
24 belegte Fehltrigger, leisester echter Ruf 402) abgeleitet wurde. 0,7 bildet
den Sicherheitsabstand ab, den wir dort von Hand gewählt haben (25 % unter dem
leisesten beobachteten Ruf).

Zwei Grenzen, die der Modus offen ausgibt (siehe ``_run_nur_studio``):

1. STIL-ABDECKUNG. Die Untergrenze entsteht durch die *schwierigen* Varianten
   — ``leise``, ``fern``, ``abgewandt``, ``beilaeufig`` (Slug im Dateinamen
   ``<ts>_<stil>.wav``, Liste in ``wakeword_studio/recorder.py:VARIATIONS``).
   Wer nur ``normal``/``laut`` aufgenommen hat, bekommt eine zu hohe Schwelle
   und verliert später leise Rufe. Fehlen diese Stile, fordert der Modus zum
   Nachaufnehmen auf statt eine Zahl aus unvollständigen Daten zu raten.
2. NEGATIVSEITE FEHLT PRINZIPIELL. Ohne Archiv lässt sich nur sagen „diese
   Schwelle kostet keinen deiner Takes" — NICHT, wie viele Fehltrigger sie
   blockt. Der Modus liefert eine sichere Untergrenze, keine
   Wirksamkeitsaussage. Sobald ein Archiv existiert, ist der volle Lauf
   (ohne ``--nur-studio``) die bessere Quelle.

Messreihe (2026-08-02)
----------------------
Bestand: 77 echte Rufe (57 aus dem Alltagsarchiv + 20 geführte Studio-Takes
aus models/wakewords/gaston/samples/) gegen 24 per Ohr entschiedene
Fehltrigger aus wake_review.jsonl. RMS des lautesten 300-ms-Fensters — bei
einem echten Ruf das Wakewort, beim Fehltrigger das auslösende Geräusch.

    AUC (Pegel allein)                        0,876
    Schwelle 300   echte Rufe verloren  0/77   Fehltrigger geblockt 10/24   p = 1,0e-07
    Schwelle 400   echte Rufe verloren  0/77   Fehltrigger geblockt 16/24   p = 4,6e-13
    leisester echter Ruf 402      lautester Fehltrigger 2314

Unter den Studio-Takes sind absichtlich schwierige — „leise" (427),
„abgewandt" (675), „fern" (1129). Keiner fällt unter 400.

``--nur-studio`` am selben Bestand: leisester Take 427 × 0,7 = 299 ≈ 300.
Vorschlag ohne jedes Alltagsarchiv deckt sich also mit der aus dem vollen
Datensatz abgeleiteten Schwelle. Vier kritische Stile alle vorhanden.

Stand der Capabilities/Archive zum Messzeitpunkt: siehe ``--verbose``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from collections import Counter

import numpy as np

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.wake_rms_replay", *sys.argv[1:]])

from voice_assistant.config import (  # noqa: E402
    RATE_OW,
    TRIGGER_AUDIO_DIR,
    WAKEWORDS_DIR,
    WORKSPACE,
)
from voice_assistant.wake_rms import loudest_window_rms  # noqa: E402
from tools.wake_triage import (  # noqa: E402
    ECHT,
    RAUSCH,
    UNKLAR,
    _lade_triage,
    _lade_wake_events,
    _selbst_labels,
    _fisher_zweiseitig,
)

WORKSPACE_WS = os.path.expanduser("~/.openclaw/workspace")
REVIEW_LABELS = os.path.join(WORKSPACE_WS, "wake_review.jsonl")


def _lade_review() -> dict[str, dict]:
    """wake_review.jsonl → audio -> zeile. Stärkste Quelle (Jochens Ohr)."""
    out: dict[str, dict] = {}
    if not os.path.exists(REVIEW_LABELS):
        return out
    with open(REVIEW_LABELS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "audio" in r:
                out[r["audio"]] = r
    return out


def labels_fuer_clips() -> dict[str, dict]:
    """audio -> {klasse, quelle} in der Rangfolge: Ohr > Selbst > STT.

    Neue Hilfsfunktion, bewusst zentral — diese Rangfolge wird in weiteren
    Auswertungen gebraucht. Ein Ohr-Urteil (quelle=="ohr") sticht jede Regel,
    auch jedes Selbst-Label, auch nachträglich. ``quelle`` trägt die Herkunft,
    damit das Replay nachvollziehbar ausgibt, WORAUF ein Label beruht.
    """
    review = _lade_review()
    selbst = _selbst_labels(_lade_wake_events())
    triage = _lade_triage()

    audios = set(review) | set(selbst) | set(triage)
    out: dict[str, dict] = {}
    for a in audios:
        if a in review:
            out[a] = {"klasse": review[a]["klasse"], "quelle": "ohr"}
        elif a in selbst:
            out[a] = {"klasse": selbst[a]["klasse"], "quelle": "selbst"}
        elif a in triage:
            out[a] = {"klasse": triage[a]["klasse"], "quelle": "stt"}
    return out


def _studio_samples() -> list[str]:
    """Alle *.wav unter models/wakewords/<bundle>/samples/*/*.wav.

    Geführte Studio-Takes — die absichtlich leisen/fernen/abgewandten Rufe.
    Eigene Positivgruppe: sie sind KEINE Fehltrigger, sondern der härteste
    Test gegen einen Recall-Verlust durch das Pegel-Gate.
    """
    out: list[str] = []
    if not os.path.isdir(WAKEWORDS_DIR):
        return out
    for bundle in os.listdir(WAKEWORDS_DIR):
        samp = os.path.join(WAKEWORDS_DIR, bundle, "samples")
        if not os.path.isdir(samp):
            continue
        for sprecher in os.listdir(samp):
            sd = os.path.join(samp, sprecher)
            if not os.path.isdir(sd):
                continue
            for name in sorted(os.listdir(sd)):
                if name.endswith(".wav"):
                    out.append(os.path.join(sd, name))
    return sorted(out)


def _lies_wav_int16(pfad: str) -> np.ndarray:
    with wave.open(pfad) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def _clip_rms(pfad: str) -> float | None:
    try:
        audio = _lies_wav_int16(pfad)
    except (wave.Error, OSError):
        return None
    if len(audio) == 0:
        return None
    return loudest_window_rms(audio, rate=RATE_OW, window_ms=300)


# Stile, die die Untergrenze des Pegels setzen — die schwierigen, nicht die
# lauten. Wer nur 'normal'/'laut' aufgenommen hat, bekommt eine zu hohe
# Schwelle und verliert später leise Rufe. Slug steckt im Dateinamen
# '<ts>_<stil>.wav'; die Liste der geführten Varianten steht in
# wakeword_studio/recorder.py:VARIATIONS.
KRITISCHE_STILE = ("leise", "fern", "abgewandt", "beilaeufig")


def _stil_aus_datei(name: str) -> str:
    """Stil-Slug aus '<ts>_<stil>.wav' → z.B. '20260706-173008_leise' → 'leise'.

    Timestamp nutzt Bindestrich, Stil und Timestamp sind durch genau einen
    Unterstrich getrennt — rsplit zerlegt robuster gegen etwaige weitere '_'
    im (hypothetischen) Slugged-Stil. 'fern-laut' bleibt dank rsplit am
    Bindestrich unangetastet als eigener Stil erhalten.
    """
    return os.path.splitext(name)[0].rsplit("_", 1)[-1]


def _sweep_print(schwelle: float, echt_pos: list[float], rausch_pos: list[float]):
    verloren = sum(1 for r in echt_pos if r < schwelle)
    geblockt = sum(1 for r in rausch_pos if r < schwelle)
    # Fisher: 2×2 mit (verloren, durchgelassen) × (echt, rauschen).
    # "Durchgelassen" = bestanden (>= schwelle). Beachte: ein geblockter
    # echter Ruf ist der Schaden, ein geblockter Fehltrigger der Nutzen.
    p = _fisher_zweiseitig(
        verloren, len(echt_pos),
        geblockt, len(rausch_pos),
    )
    sterne = "***" if p < 1e-4 else "**" if p < 1e-2 else "*" if p < 0.05 else ""
    print(f"  {schwelle:5.0f}   {verloren:>2}/{len(echt_pos):<3}     "
          f"{geblockt:>2}/{len(rausch_pos):<3}     p={p:.2e} {sterne}".rstrip())


def _run_nur_studio(args) -> int:
    """Schwellenvorschlag allein aus geführten Studio-Takes, ohne Alltagsarchiv.

    Warum es diesen Modus gibt: der normale Lauf braucht ``wake_triage.jsonl``
    und ``wake_review.jsonl`` — Ergebnisse aus Wochen Betrieb plus Handarbeit
    am Ohr. Am Tag eins sind beide leer; ein Fremder kann die Schwelle so nicht
    herleiten. Etwa 10 Minuten ``wakeword_studio record`` (mit allen Stilen!)
    liefern genug Positiv-Beispiele für eine sichere UNTENGRENZE.

    Vorschlag: round(leisester Take × 0,7). Der Faktor ist nicht erfunden:
    hier liegt der leiseste Studio-Take bei 427, ×0,7 = 299 — praktisch exakt
    die 300, die aus dem VOLLEN Datensatz (57 Alltagsrufe + 24 belegte
    Fehltrigger, leisester echter Ruf 402) abgeleitet wurde. 0,7 bildet den
    Sicherheitsabstand ab, den wir dort von Hand gewählt haben (25 % unter dem
    leisesten beobachteten Ruf).

    ZWEI WARNUNGEN, die dieser Modus ausgeben muss (und die nicht überspielt
    werden dürfen):

    1. STIL-ABDECKUNG. Die Untergrenze entsteht durch die schwierigen Varianten
       (leise, fern, abgewandt, beilaeufig). Wer nur 'normal'/'laut'
       aufgenommen hat, bekommt eine zu hohe Schwelle und verliert später leise
       Rufe. Fehlen diese Stile, wird das benennt und zum Nachaufnehmen
       aufgefordert — statt eine Zahl aus unvollständigen Daten zu raten.
    2. DIE NEGATIVSEITE FEHLT PRINZIPIELL. Ohne Archiv lässt sich nur sagen
       „diese Schwelle kostet keinen deiner Takes" — NICHT, wie viele
       Fehltrigger sie blockt. Der Modus liefert eine sichere Untergrenze,
       keine Wirksamkeitsaussage. Sobald ein Archiv existiert, ist der volle
       Lauf (ohne --nur-studio) die bessere Quelle.
    """
    studioclips = _studio_samples()
    if not studioclips:
        print("Keine Studio-Takes gefunden unter "
              f"{os.path.join(WAKEWORDS_DIR, '<bundle>', 'samples', '*')}.")
        print("Etwa 10 Minuten aufnehmen:  "
              "ow-venv/bin/python -m wakeword_studio record --speaker <name>")
        print("Wichtig: ALLE Stile, besonders leise/fern/abgewandt/beilaeufig —")
        print("         nur aus ihnen entsteht die Untergrenze.")
        return 1

    # (basename, pfad, stil, rms) — Stil aus dem Dateinamen.
    daten: list[tuple[str, str, str, float | None]] = []
    for pfad in studioclips:
        name = os.path.basename(pfad)
        daten.append((name, pfad, _stil_aus_datei(name), _clip_rms(pfad)))

    vals = [(n, s, r) for n, _, s, r in daten if r is not None]
    if not vals:
        print("Keine lesbaren Studio-Takes — Format prüfen (16-kHz mono int16).")
        return 1

    print("=" * 72)
    print("PEGEL-GATE — NUR-STUDIO (kein Alltagsarchiv, keine Labels nötig)")
    print("=" * 72)
    print(f"Takes: {len(vals)}  |  Fenster: 300 ms (identisch zum Live-Gate)")
    print()

    # Je Stil min/median — die Streuung zeigt, ob die schwierigen Stile da sind.
    print(f"{'Stil':<12} {'n':>3}  {'min':>6}  {'median':>7}")
    print("-" * 36)
    stile = sorted(set(s for _, s, _ in vals))
    for stil in stile:
        rs = sorted(r for _, s, r in vals if s == stil)
        if not rs:
            continue
        print(f"{stil:<12} {len(rs):>3}  {rs[0]:6.0f}  {rs[len(rs)//2]:7.0f}")

    # Leisester Take insgesamt + Dateiname.
    leisester = min(vals, key=lambda x: x[2])
    print(f"\nLeisester Take: {leisester[2]:.0f} RMS  ({leisester[0]})")

    # Warnung 1: fehlende kritische Stile.
    vorhandene = set(stile)
    fehlen = [s for s in KRITISCHE_STILE if s not in vorhandene]
    if fehlen:
        print(f"\n⚠️  FEHLENDE STILE: {', '.join(fehlen)}.")
        print("    Die Untergrenze entsteht aus den schwierigen Varianten —")
        print("    ohne sie ist jeder Vorschlag zu HOCH und kostet später")
        print("    leise Rufe. Bitte nachaufnehmen:")
        print("      ow-venv/bin/python -m wakeword_studio record --speaker <name>")
        print("    und dabei gezielt die fehlenden Stile ansagen.")
        # Trotzdem Vorschlag anzeigen, aber als unzuverlässig markieren.
        vorschlag = round(leisester[2] * 0.7)
        print(f"\nVorschlag (UNZUVERLÄSSIG, Stile fehlen): {vorschlag}")
        print(f"  wake_rms_min: {vorschlag}")
        return 0

    vorschlag = round(leisester[2] * 0.7)
    print(f"\nVorschlag: leisester Take × 0,7 = {leisester[2]:.0f} × 0,7 = {vorschlag}")
    print("Herleitung des Faktors: siehe Docstring (0,7 ≙ 25 % Sicherheitsabstand,")
    print("nachgerechnet am vollen Datensatz: 427 × 0,7 = 299 ≈ die 300 aus")
    print("57 Alltagsrufen + 24 belegten Fehltriggern).")
    print(f"\nKonfigzeile (ins Profil, nicht ins Bundle):")
    print(f"  wake_rms_min: {vorschlag}")

    # Warnung 2: Negativseite fehlt.
    print("\n⚠️  DIESE ZAHL IST NUR EINE UNTENGRENZE.")
    print("    Sie garantiert: die Schwelle kostet keinen deiner Takes.")
    print("    Sie sagt NICHT, wie viele Fehltrigger sie blockt — dafür fehlt")
    print("    das Alltagsarchiv. Sobald du eines hast, ist der volle Lauf")
    print("    die bessere Quelle:")
    print("      ow-venv/bin/python -m tools.wake_rms_replay")
    print()
    print("Absolute RMS-Werte sind gain-abhängig (ReSpeaker ×4). Ändert sich")
    print("Hardware/Gain, verschiebt sich die Skala — siehe Docstring.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--nur-studio", action="store_true",
                    help="Schwellenvorschlag allein aus Studio-Takes, OHNE "
                         "Alltagsarchiv/Labels (für den ersten Tag oder fremde "
                         "Installation — siehe Docstring)")
    ap.add_argument("--schwelle", type=float, default=300.0,
                    help="Pegel-Schwelle (Default 300, siehe Docstring)")
    ap.add_argument("--von", type=float, default=100.0, dest="von",
                    help="Sweep-Anfang (Default 100)")
    ap.add_argument("--bis", type=float, default=700.0, dest="bis",
                    help="Sweep-Ende (Default 700)")
    ap.add_argument("--schritt", type=float, default=50.0,
                    help="Sweep-Schrittweite (Default 50)")
    ap.add_argument("--verbose", action="store_true",
                    help="je Clip: Klasse, Quelle, RMS (zum Nachvollziehen)")
    args = ap.parse_args()

    if args.nur_studio:
        return _run_nur_studio(args)

    if not os.path.isdir(TRIGGER_AUDIO_DIR):
        print(f"Kein Trigger-Archiv unter {TRIGGER_AUDIO_DIR}")
        print("(Am ersten Tag fehlt es. Für einen Schwellenvorschlag ohne "
              "Archiv: --nur-studio)")
        return 1

    labels = labels_fuer_clips()

    # --- Positiv- und Negativ-Gruppen sammeln ---
    # MAßSTAB SIND HALTBARE LABELS (ohr + selbst). STT-Labels sind zu schwach
    # für eine Quote: review_audio.py warnt ausdrücklich, daß ein leeres
    # Transkript KEIN verläßliches Fehltrigger-Label ist (der Ruf kann echt
    # gewesen sein, der Nutzer wurde unterbrochen). Nur 9 von 102 STT-
    # „rauschen"-Labels sind hart. Nimmt man STT-rauschen ins Negativ-Set,
    # bläht das die Blockquote künstlich auf — und blockt man versehentlich
    # einen echten Ruf, zählt er als Gewinn, obwohl er der Preis wäre.
    #
    # Die Rangfolge (Ohr > Selbst > STT) entscheidet also nicht nur, WELCHE
    # Klasse ein Clip hat, sondern OB er überhaupt in die Quote eingeht:
    #   - Ohr/Selbst → maßgeblich, geht in Positiv- oder Negativ-Set ein.
    #   - STT        → nur Hinweis, fällt für die Quote heraus (wie UNKLAR).
    # UNKLAR fällt ebenfalls raus — weder Preis noch Gewinn.
    echt_clips: list[tuple[str, str]] = []   # (audiobasename, pfad)
    rausch_clips: list[tuple[str, str]] = []
    for name in sorted(os.listdir(TRIGGER_AUDIO_DIR)):
        if not (name.endswith("_wake.wav") or name.endswith("_nearmiss.wav")):
            continue
        lab = labels.get(name)
        if lab is None or lab["quelle"] == "stt":
            continue
        pfad = os.path.join(TRIGGER_AUDIO_DIR, name)
        if lab["klasse"] == ECHT:
            echt_clips.append((name, pfad))
        elif lab["klasse"] == RAUSCH:
            rausch_clips.append((name, pfad))

    # Studio-Takes: eigene Positivgruppe (absichtlich leise/fern/abgewandt).
    studioclips: list[tuple[str, str]] = []
    for pfad in _studio_samples():
        studioclips.append((os.path.basename(pfad), pfad))

    echt_rms = [(n, _clip_rms(p)) for n, p in echt_clips]
    rausch_rms = [(n, _clip_rms(p)) for n, p in rausch_clips]
    studio_rms = [(n, _clip_rms(p)) for n, p in studioclips]

    echt_vals = [r for _, r in echt_rms if r is not None]
    rausch_vals = [r for _, r in rausch_rms if r is not None]
    studio_vals = [r for _, r in studio_rms if r is not None]

    print("=" * 72)
    print("PEGEL-GATE SWEEP — lautestes 300-ms-Fenster, RMS(int16)")
    print("=" * 72)
    print(f"Alltag:  {len(echt_vals)} echte Rufe, {len(rausch_vals)} Fehltrigger (maßgeblich: Ohr + Selbst)")
    quellen = Counter(labels[n]['quelle'] for n, _ in echt_clips + rausch_clips)
    print(f"         Quellen: {dict(quellen)}")
    print(f"Studio:  {len(studio_vals)} geführte Takes (absichtlich leise/fern/abgewandt)")
    print(f"Fenster: 300 ms (Sample-genau, identisch zum Live-Gate via voice_assistant.wake_rms)")

    if echt_vals:
        print(f"\nEchte Rufe:   min={min(echt_vals):.0f}  median={sorted(echt_vals)[len(echt_vals)//2]:.0f}  max={max(echt_vals):.0f}")
    if studio_vals:
        print(f"Studio-Takes: min={min(studio_vals):.0f}  median={sorted(studio_vals)[len(studio_vals)//2]:.0f}  max={max(studio_vals):.0f}")
    if rausch_vals:
        print(f"Fehltrigger:  min={min(rausch_vals):.0f}  median={sorted(rausch_vals)[len(rausch_vals)//2]:.0f}  max={max(rausch_vals):.0f}")

    print(f"\n{'Schwelle':>8}  {'Rufe verloren':>14}  {'FP geblockt':>12}  Fisher")
    print("-" * 60)
    # Sweep kombiniert Alltag + Studio als Positivgruppe — so wie der Befund
    # im Docstring zustande gekommen ist. Die Spalte "Rufe verloren" zählt
    # BEIDE zusammen; ein Verlust in den Studio-Takes ist der deutlichere
    # Alarm, weil die absichtlich leise sind.
    pos_all = echt_vals + studio_vals
    s = args.von
    while s <= args.bis + 1e-6:
        _sweep_print(s, pos_all, rausch_vals)
        s += args.schritt

    # Einzel-Schwelle mit Aufschlüsselung Alltag vs. Studio
    print(f"\n--- Einzel-Schwelle {args.schwelle:.0f} ---")
    sv = sum(1 for r in echt_vals if r < args.schwelle)
    ss = sum(1 for r in studio_vals if r < args.schwelle)
    gb = sum(1 for r in rausch_vals if r < args.schwelle)
    print(f"  Echte Rufe verloren (Alltag): {sv}/{len(echt_vals)}")
    print(f"  Echte Rufe verloren (Studio): {ss}/{len(studio_vals)}")
    print(f"  Fehltrigger geblockt:        {gb}/{len(rausch_vals)}")
    if echt_vals and studio_vals:
        print(f"  Leisester echter Ruf (Alltag): {min(echt_vals):.0f}")
        print(f"  Leisester echter Ruf (Studio): {min(studio_vals):.0f}")
    if rausch_vals:
        print(f"  Lautester Fehltrigger:         {max(rausch_vals):.0f}")

    if args.verbose:
        print("\n--- Je Clip ---")
        print(f"{'Klasse':<12} {'Quelle':<8} {'RMS':>6}  Clip")
        for n, r in sorted(echt_rms, key=lambda x: x[1] or 0):
            if r is None:
                continue
            q = labels.get(n, {}).get("quelle", "?")
            print(f"  ECHT      {q:<8} {r:6.0f}  {n}")
        for n, r in sorted(studio_rms, key=lambda x: x[1] or 0):
            if r is None:
                continue
            print(f"  STUDIO    studio   {r:6.0f}  {n}")
        for n, r in sorted(rausch_rms, key=lambda x: x[1] or 0):
            if r is None:
                continue
            q = labels.get(n, {}).get("quelle", "?")
            print(f"  RAUSCH    {q:<8} {r:6.0f}  {n}")

    print("\nHinweis: absolute RMS-Werte sind gain-abhängig (ReSpeaker ×4).")
    print("Ändert sich Hardware/Gain, verschiebt sich die Skala — siehe Docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
