#!/usr/bin/env python3
"""Clips zum Anhören exportieren und die Sortierung als harte Labels zurücklesen.

Aufruf:
    ow-venv/bin/python -m tools.review_audio export [--klasse rauschen] [--limit 40]
    ow-venv/bin/python -m tools.review_audio import

Warum es das gibt
-----------------
Die automatischen Labels sind ungleich stark. ``tools/wake_triage.py`` erzeugt
harte Selbst-Labels aus Handlungen (Wiederholung binnen 15 s, ausgeführtes
Schaltkommando, Stopp-Wort) — die tragen. Alles andere kommt aus der STT, und
die verhört „Gaston" notorisch als „Gestalt", „Gasthof", „Das ist toll".

Konkret gemessen (2026-08-02): von den 102 ``rauschen``-Labels, die als
FP-Meß-Set dienen sollen, sind nur **9 hart**. Der Rest heißt „rauschen", weil
die STT nichts erkannt hat. ``WAKEWORD_PROCESS.md`` hält ausdrücklich fest, daß
ein leeres Transkript **kein** verläßliches Fehltrigger-Label ist: der Ruf kann
echt gewesen sein und der Nutzer wurde unterbrochen.

Ein Menschenohr löst das in Minuten. Dieses Werkzeug macht daraus einen
Arbeitsgang statt einer Fleißaufgabe.

Der entscheidende Kniff: wake + rec werden verkettet
----------------------------------------------------
Ein Wake-Clip allein ist auch für einen Menschen schwer zu beurteilen — er ist
drei Sekunden lang und endet, bevor das nächste Wort beginnt. Der zuverlässige
Indikator ist laut Prozeß die **Folgeaufnahme**: was nach dem Trigger gesagt
wurde. Ein klares Kommando danach belegt den echten Ruf, „Stopp Stopp" belegt
den Fehltrigger.

Deshalb liegt im Review-Ordner **eine** Datei pro Fall: Wake-Clip, 0,4 s Stille,
Folgeaufnahme. Einmal anhören, einmal entscheiden. Ohne diese Verkettung müßte
man zwei Dateien nebeneinanderhalten und würde genau den Fehler machen, den der
Prozeß dokumentiert (Folgeaufnahme als Fehltrigger-Indikator mißdeuten).

Near-Misses haben keine Folgeaufnahme (es gab ja keinen Trigger) — die stehen
allein da und sind entsprechend schwerer. Sie sind auch seltener nötig: hat sich
der Nutzer wiederholt, liegt bereits ein hartes Selbst-Label vor.

Ablauf
------
1. ``export`` legt die Clips nach ``<workspace>/voice/review/offen/`` und
   erzeugt daneben die leeren Ordner ``positiv/``, ``negativ/``, ``unklar/``.
   Der Dateiname trägt Peak und Vermutung, damit die Liste sortiert eine
   sinnvolle Reihenfolge hat.
2. Anhören (``aplay``, Dateimanager, egal) und in den passenden Ordner
   **verschieben**. Was in ``offen/`` liegen bleibt, gilt als nicht bearbeitet.
3. ``import`` liest die drei Ordner und schreibt ``wake_review.jsonl``.

Vorrang der Labels
------------------
``wake_review.jsonl`` ist die **stärkste** Quelle — sie sticht Selbst-Labels und
STT. Ein Mensch, der den Clip gehört hat, weiß es besser als jede Regel. Die
auswertenden Werkzeuge müssen in dieser Reihenfolge lesen:

    wake_review.jsonl  >  Selbst-Labels  >  STT-Einstufung

``import`` meldet jeden Fall, in dem das Urteil vom bisherigen Label abweicht.
Diese Abweichungen sind kein Ärgernis, sondern das Ergebnis: sie zeigen, wie
verläßlich die automatische Einstufung wirklich war.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import wave
from collections import Counter

# --- venv-Re-Exec wie in den anderen tools/ -------------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.review_audio"] + sys.argv[1:])

sys.path.insert(0, _REPO)

from tools.wake_triage import (  # noqa: E402
    TRIGGER_AUDIO_DIR,
    _lade_triage,
    _lade_wake_events,
    _selbst_labels,
)

WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
REVIEW_DIR = os.path.join(WORKSPACE, "voice", "review")
OFFEN = os.path.join(REVIEW_DIR, "offen")
KLASSEN_ORDNER = {"positiv": "echter_ruf", "negativ": "rauschen", "unklar": "unklar"}
REVIEW_LABELS = os.path.join(WORKSPACE, "wake_review.jsonl")
INDEX = os.path.join(REVIEW_DIR, "index.json")

STILLE_SEK = 0.4

# Die Folgeaufnahme beginnt mit dem Pre-Roll aus demselben Ringpuffer, aus dem
# auch der Wake-Clip stammt — ihr Anfang ist mit dem Ende des Wake-Clips
# BITIDENTISCH. Verkettet man beide ungekürzt, hört man diese Sekunden zweimal.
# Gemessen 2026-08-02 über Stichproben quer durch das Archiv: Korrelation 1.000
# bei konstant 1,52 s (min = median = max). Beim Anhören fällt das vor allem bei
# Fehltriggern auf, weil danach oft nichts mehr kommt und die Doppelung frei
# steht; bei einem echten Ruf geht sie im Kommando unter.
PREROLL_SEK = 1.52


def _lies_wav(pfad: str) -> tuple[bytes, tuple]:
    with wave.open(pfad, "rb") as w:
        return w.readframes(w.getnframes()), (w.getnchannels(), w.getsampwidth(), w.getframerate())


def _verkette(ziel: str, teile: list[str], rec_max: float) -> bool:
    """wake + Stille + rec in eine Datei. False, wenn nichts lesbar war.

    Die Folgeaufnahme wird auf ``rec_max`` Sekunden gekappt. Ob ein Kommando
    kam oder ein „Stopp Stopp", steht in den ersten Sekunden — der Rest ist
    Hörzeit ohne Erkenntnisgewinn. Ungekappt liegen einzelne Aufnahmen bei
    über 30 s, was den ganzen Stapel unbezahlbar macht.
    """
    daten, params = [], None
    for i, p in enumerate(teile):
        if not os.path.exists(p):
            continue
        roh, pr = _lies_wav(p)
        if params is None:
            params = pr
        elif pr != params:
            continue  # abweichendes Format überspringen statt zu verzerren
        kanaele, breite, rate = params
        if i > 0:  # Teil 0 ist der Wake-Clip, der bleibt ganz
            roh = roh[int(rate * PREROLL_SEK) * breite * kanaele:]  # Doppelung weg
            if rec_max:
                roh = roh[: int(rate * rec_max) * breite * kanaele]
        if daten:
            daten.append(b"\x00" * int(rate * STILLE_SEK) * breite * kanaele)
        daten.append(roh)
    if params is None or not daten:
        return False
    kanaele, breite, rate = params
    with wave.open(ziel, "wb") as w:
        w.setnchannels(kanaele)
        w.setsampwidth(breite)
        w.setframerate(rate)
        w.writeframes(b"".join(daten))
    return True


def _kandidaten(args) -> list[dict]:
    """Zu reviewende Fälle, stärkste Labels zuerst ausgeschlossen."""
    triage = _lade_triage()
    selbst = _selbst_labels(_lade_wake_events())
    schon = _lade_review()

    nur = None
    if getattr(args, "liste", None):
        with open(args.liste, encoding="utf-8") as fh:
            nur = {z.strip() for z in fh if z.strip() and not z.startswith("#")}

    out = []
    for audio, r in triage.items():
        klasse = r.get("klasse")
        if nur is not None and audio not in nur:
            continue
        if nur is None and args.klasse != "alle" and klasse != args.klasse:
            continue
        if args.art != "alle" and r.get("art") != args.art:
            continue
        if audio in schon:
            continue  # bereits per Ohr entschieden
        hart = selbst.get(audio, {}).get("klasse")
        if nur is not None:
            out.append({
                "audio": audio, "art": r.get("art"), "klasse": klasse,
                "peak": r.get("peak"),
                "transkript": (r.get("transkript") or "").strip(),
                "danach": (r.get("danach") or "").strip(),
            })
            continue  # explizite Liste sticht jeden Filter, auch harte Labels
        if hart and not args.auch_harte:
            continue  # Selbst-Label trägt schon, Hörzeit woanders besser investiert
        out.append({
            "audio": audio,
            "art": r.get("art"),
            "klasse": klasse,
            "peak": r.get("peak"),
            "transkript": (r.get("transkript") or "").strip(),
            "danach": (r.get("danach") or "").strip(),
        })

    # Hoher Peak zuerst: diese Clips passieren das Gate und bestimmen die
    # FP-Zahl unmittelbar. Fälle ohne Peak (ältere Clips ohne Log-Zeile) ans Ende.
    out.sort(key=lambda d: (d["peak"] is None, -(d["peak"] or 0)))
    return out[: args.limit] if args.limit else out


def _lade_review() -> dict[str, dict]:
    if not os.path.exists(REVIEW_LABELS):
        return {}
    labels = {}
    with open(REVIEW_LABELS, encoding="utf-8") as fh:
        for zeile in fh:
            zeile = zeile.strip()
            if zeile:
                r = json.loads(zeile)
                labels[r["audio"]] = r
    return labels


def cmd_export(args) -> int:
    faelle = _kandidaten(args)
    if not faelle:
        print("Nichts zu tun — alle passenden Clips sind bereits entschieden.")
        return 0

    os.makedirs(OFFEN, exist_ok=True)
    for ordner in KLASSEN_ORDNER:
        os.makedirs(os.path.join(REVIEW_DIR, ordner), exist_ok=True)

    index, n = {}, 0
    for i, f in enumerate(faelle, 1):
        audio = f["audio"]
        teile = [os.path.join(TRIGGER_AUDIO_DIR, audio)]
        if f["art"] == "trigger":
            rec = audio.replace("_wake.wav", "_rec.wav")
            teile.append(os.path.join(TRIGGER_AUDIO_DIR, rec))

        peak = f"{f['peak']:.2f}" if f["peak"] is not None else "----"
        name = f"{i:03d}_peak{peak}_{f['art']}_{audio}"
        ziel = os.path.join(OFFEN, name)
        if not _verkette(ziel, teile, args.rec_max):
            continue
        index[name] = f
        n += 1

    with open(INDEX, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    arten = Counter(f["art"] for f in index.values())
    print(f"\n{n} Clips nach {OFFEN}/ gelegt  ({dict(arten)})")
    print(f"  Trigger tragen ihre Folgeaufnahme angehängt (durch {STILLE_SEK}s Stille getrennt).")
    print(f"  Near-Misses stehen allein — sie haben keine.\n")
    print("  Anhören und VERSCHIEBEN nach:")
    for ordner, klasse in KLASSEN_ORDNER.items():
        print(f"    {os.path.join(REVIEW_DIR, ordner):<50} → {klasse}")
    print(f"\n  Was in offen/ liegen bleibt, gilt als unbearbeitet.")
    print(f"  Danach:  ow-venv/bin/python -m tools.review_audio import")
    return 0


def cmd_import(args) -> int:
    if not os.path.exists(INDEX):
        print(f"Kein {INDEX} — erst 'export' laufen lassen.")
        return 1
    with open(INDEX, encoding="utf-8") as fh:
        index = json.load(fh)

    triage = _lade_triage()
    selbst = _selbst_labels(_lade_wake_events())
    bestand = _lade_review()

    neu, abweichungen, offen = {}, [], 0
    for ordner, klasse in KLASSEN_ORDNER.items():
        pfad = os.path.join(REVIEW_DIR, ordner)
        if not os.path.isdir(pfad):
            continue
        for name in os.listdir(pfad):
            if not name.endswith(".wav"):
                continue
            f = index.get(name)
            if f is None:
                print(f"  ⚠️  {name} steht nicht im Index — übersprungen.")
                continue
            audio = f["audio"]
            neu[audio] = {
                "audio": audio,
                "art": f["art"],
                "klasse": klasse,
                "quelle": "ohr",
                "vorher": f["klasse"],
                "vorher_hart": selbst.get(audio, {}).get("klasse"),
            }
            if klasse != "unklar" and f["klasse"] != klasse:
                abweichungen.append((audio, f["klasse"], klasse, f.get("peak")))

    offen = len([n for n in os.listdir(OFFEN) if n.endswith(".wav")]) if os.path.isdir(OFFEN) else 0

    zusammen = {**bestand, **neu}
    with open(REVIEW_LABELS, "w", encoding="utf-8") as fh:
        for r in zusammen.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    zahl = Counter(r["klasse"] for r in neu.values())
    print(f"\n{len(neu)} Urteile übernommen  {dict(zahl)}")
    print(f"  → {REVIEW_LABELS}  (gesamt {len(zusammen)})")
    if offen:
        print(f"  {offen} Clips liegen noch in offen/ und wurden nicht gewertet.")

    if abweichungen:
        print(f"\n  {len(abweichungen)} Abweichungen vom automatischen Label —")
        print(f"  das ist das eigentliche Ergebnis, es beziffert deren Verläßlichkeit:")
        for audio, vorher, jetzt, peak in sorted(
                abweichungen, key=lambda a: -(a[3] or 0)):
            p = f"peak {peak:.2f}" if peak is not None else "peak  ?  "
            print(f"    {p}  {vorher:>10} → {jetzt:<10}  {audio}")
        falsch_negativ = sum(1 for _, v, j, _ in abweichungen
                             if v == "rauschen" and j == "echter_ruf")
        if falsch_negativ:
            print(f"\n  ⚠️  {falsch_negativ} als 'rauschen' geführte Clips sind echte Rufe.")
            print(f"      Sie haben bisher die FP-Zahl aufgebläht und den Recall")
            print(f"      zu gut aussehen lassen. Messungen, die darauf beruhen,")
            print(f"      sind zu wiederholen.")
    else:
        print("\n  Keine Abweichung vom automatischen Label.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="Clips zum Anhören bereitlegen")
    ex.add_argument("--klasse", default="rauschen",
                    choices=["rauschen", "echter_ruf", "unklar", "alle"],
                    help="welche automatische Einstufung prüfen (Default: rauschen — "
                         "dort sind nur 9 von 102 Labels hart)")
    ex.add_argument("--art", default="alle", choices=["trigger", "nearmiss", "alle"])
    ex.add_argument("--limit", type=int, default=40,
                    help="höchstens so viele Clips (Default 40; 0 = alle)")
    ex.add_argument("--liste", default=None, metavar="DATEI",
                    help="Textdatei mit Clip-Namen (einer je Zeile, '#' = Kommentar). "
                         "Sticht alle anderen Filter — gedacht für die Clips, an "
                         "denen eine konkrete Messung hängt (z. B. die vom Verifier "
                         "eliminierten FPs). Viel kürzer als ein Voll-Review.")
    ex.add_argument("--rec-max", type=float, default=8.0, dest="rec_max",
                    help="Folgeaufnahme auf so viele Sekunden kappen "
                         "(Default 8; 0 = ungekappt)")
    ex.add_argument("--auch-harte", action="store_true", dest="auch_harte",
                    help="auch Clips mit hartem Selbst-Label exportieren "
                         "(normalerweise unnötig — die tragen schon)")
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser("import", help="Sortierung als harte Labels zurücklesen")
    im.set_defaults(func=cmd_import)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
