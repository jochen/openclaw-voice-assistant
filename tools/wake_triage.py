#!/usr/bin/env python3
"""Wakeword-Near-Misses in drei Klassen sortieren — selbst gelabelt, sonst per STT.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.wake_triage
    ow-venv/bin/python -m tools.wake_triage --seit 3        # nur letzte 3 Tage
    ow-venv/bin/python -m tools.wake_triage --auch-trigger  # Kontrollgruppe mit

Zwei Label-Quellen, in dieser Rangfolge
---------------------------------------
1. SELBST-LABELS (_selbst_labels) — aus Handlungen, die nur bei einem echten
   Ruf bzw. nur bei einem Fehltrigger vorkommen: der Nutzer wiederholt sich
   nach einem Near-Miss, ein Schaltkommando wird ausgeführt, der Nutzer bricht
   mit einem Stopp-Wort ab. Kein Mensch, keine STT, kein Schwellwert. Diese
   Labels stechen die STT-Einstufung immer, auch nachträglich. Details und
   Grenzen stehen an der Funktion.
2. STT-EINSTUFUNG (_klassifiziere) — für alles, was Regel 1 nicht erreicht.
   Der Rest dieses Textes gilt für diese zweite Quelle; sie ist deutlich
   schwächer, deshalb kam sie in Wirkung erst an zweiter Stelle.

Warum die STT-Einstufung allein nicht als Label taugt
-----------------------------------------------------
Die naheliegende Idee ist: Near-Miss-Audio durch die STT schicken, und wenn
"Gaston" drin steht, war es ein echter Ruf. Gemessen am 2026-07-26: die STT
erkennt nur einen Teil der echten Rufe. "Gaston" kam u.a. als "Gestalt.",
"Gasthof.", "Gastholm.", "Kastor!" oder "Gestern?" zurück — Whisper auf einem
isoliert stehenden Einzelwort ist schwach, und das Wort liegt nah an deutschen
Alltagswörtern.

FALLE BEI DER KONTROLLGRUPPE (mir selbst passiert): das Trigger-Archiv ist
KEINE Positivkontrolle. Ein gefeuertes Wakeword belegt nicht, dass jemand
"Gaston" gesagt hat — das ist die Definition eines False Positive. Von 55
Trigger-Aufnahmen enthielten 37 kein Gaston-artiges Wort, darunter "Prost!",
"Bis dann." und "Ich habe jetzt 10 Kilowattstunden." — das sind schlicht
Fehltrigger, kein Versagen der Methode.
Eine saubere automatische Positivkontrolle gibt es im Archiv NICHT. Auch der
naheliegende Proxy "existiert eine Folgeaufnahme" trägt nicht: *_rec.wav wird
nach jedem Trigger geschrieben, unabhängig davon, ob jemand gesprochen hat.
Die Trefferquote der Methode lässt sich also nur an einem von Hand gelabelten
Satz bestimmen — dafür ist die UNKLAR-Liste unten gedacht.
Was das Urteil für den Menschen erheblich erleichtert: bei Triggern wird das
Transkript der FOLGEAUFNAHME mitgezeigt. Steht im Wake-Clip "Gestalt." und in
der Folgeaufnahme "Mach das Küchenlicht an", war es offensichtlich ein echter
Ruf. Diese Paarung ist Beleg, nicht Metrik.

Als Ja/Nein-Label wäre das also gefährlich: es hätte die Hälfte der echten Rufe
zu False Positives erklärt, und ein danach gelockertes Gate wäre in die falsche
Richtung getunt.

Was verlässlich trägt, ist ein anderes Signal aus demselben Durchlauf:
`no_speech_prob`. In keiner Rausch-Aufnahme hat die STT ein "Gaston"
halluziniert. Daraus die drei Klassen:

    ECHTER RUF   Gaston-artiges Wort erkannt        -> hohe Präzision
    RAUSCHEN     kein Sprachanteil                  -> hohe Präzision
    UNKLAR       Sprache, aber kein Gaston-Wort     -> muss ein Mensch anhören

Der Nutzen liegt im Aussortieren der beiden sicheren Klassen. Die UNKLAR-Fälle
sind die eigentliche Arbeit — das Skript listet sie mit Pfad zum Anhören auf.

Ergebnis geht als JSONL nach <workspace>/wake_triage.jsonl, damit Labels über
mehrere Läufe erhalten bleiben und sich mit dem Gate-Sweep zusammenführen
lassen. Bereits gelabelte Dateien werden nicht erneut durch die STT geschickt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.wake_triage", *sys.argv[1:]])

from voice_assistant.config import (  # noqa: E402
    TRIGGER_AUDIO_DIR,
    WAKE_LOG_PATH,
    WORKSPACE,
    load_profile,
)
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.services.stt import SpeachesStt  # noqa: E402

TRIAGE_PATH = os.path.join(WORKSPACE, "wake_triage.jsonl")

# Verhörer von "Gaston" aus echten Logs: Gasthof, Gastholm, Gastronom, Kastor,
# Kasto, Gaston, Gastó. Absichtlich weit gefasst — ein zu weites Muster
# verschiebt Fälle nach ECHTER RUF (harmlos, wird eh nicht zum Lockern
# benutzt), ein zu enges verschiebt sie nach RAUSCHEN (gefährlich).
GASTON_PAT = re.compile(r"gast|kast|gasch|aston|caston", re.IGNORECASE)
# Ab diesem no_speech_prob gilt die Aufnahme als sprachfrei. 0.7 liegt in der
# gemessenen Lücke: Rausch-Clips lagen bei 0.79-0.86, Sprach-Clips bei 0.04-0.35.
NO_SPEECH_SCHWELLE = 0.7

ECHT, RAUSCH, UNKLAR = "echter_ruf", "rauschen", "unklar"


def _transcribe(stt: SpeachesStt, path: str) -> tuple[str, float | None]:
    """Transkript + no_speech_prob. SpeachesStt verwirft Halluzinationen selbst
    (gibt dann None), deshalb wird die Rohantwort separat gelesen."""
    with open(path, "rb") as f:
        wav = f.read()
    text = stt.transcribe(wav)
    # transcribe() liefert None, wenn es die Aufnahme als Halluzination
    # verworfen hat — genau der Fall, der uns als RAUSCHEN interessiert.
    return (text or ""), (None if text else 1.0)


def _klassifiziere(text: str, verworfen: bool) -> str:
    if GASTON_PAT.search(text):
        return ECHT
    if verworfen or not text.strip():
        return RAUSCH
    return UNKLAR


def _lade_wake_log() -> dict[str, dict]:
    """audio-Dateiname -> zusammengeführte Log-Zeilen.

    Zu einem Trigger können mehrere Zeilen mit derselben audio-Datei gehören:
    das Trigger-Ereignis selbst (peak, hits, scores) und die spätere
    ack-Entscheidung (ein_satz). Die werden gemischt statt überschrieben —
    sonst verlöre die zweite Zeile die Gate-Werte der ersten.
    """
    out: dict[str, dict] = {}
    if not os.path.exists(WAKE_LOG_PATH):
        return out
    with open(WAKE_LOG_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            audio = row.get("audio")
            if not audio:
                continue
            ziel = out.setdefault(audio, {})
            # result nicht überschreiben: "trigger"/"nearmiss" ist die Art des
            # Ereignisses, "ack" nur ein Nachtrag dazu.
            ziel.update({k: v for k, v in row.items()
                         if k != "result" or "result" not in ziel})
    return out


def _lade_wake_events() -> list[dict]:
    """Alle Wake-Log-Zeilen in zeitlicher Reihenfolge (für Nachbarschaftsregeln)."""
    rows: list[dict] = []
    if not os.path.exists(WAKE_LOG_PATH):
        return rows
    with open(WAKE_LOG_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("ts"):
                rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


# Innerhalb dieser Spanne gilt ein Trigger als Wiederholung des vorangegangenen
# Near-Miss. Gemessen am Archiv 2026-07-25..28: die 19 gefundenen Paare liegen
# bei 2-15 s, der Schwerpunkt bei 3-8 s. Weiter aufmachen würde anfangen,
# unabhängige Rufe einzusammeln.
WIEDERHOLUNG_SEK = 15.0


def _selbst_labels(events: list[dict]) -> dict[str, dict]:
    """Labels, die das System sich selbst gibt — audio-Datei -> {klasse, grund}.

    Kein Mensch, keine STT, kein Schwellwert. Alle drei Regeln stützen sich auf
    eine Handlung des Nutzers bzw. der Haussteuerung, die nur bei einem echten
    Ruf (oder nur bei einem Fehltrigger) vorkommt:

    1. Near-Miss, dem binnen WIEDERHOLUNG_SEK ein Trigger folgt → der Nutzer hat
       sich wiederholt, weil der erste Ruf nicht ankam. Das ist ein VERLORENER
       echter Ruf. Diese Regel ist die wertvollste: sie labelt genau das, was
       das Gate verpasst hat, statt zu bestätigen was es ohnehin durchlässt.
       Am Archiv gegengeprüft: von 19 so gefundenen Fällen hatte die
       STT-Heuristik 10 als 'unklar' liegen lassen und 2 als 'rauschen'
       FALSCH einsortiert (darunter ein per Transkript belegtes 'Gaston macht…').
    2. Trigger, aus dem ein Schaltkommando wurde → echter Ruf. Ein Fehltrigger
       erzeugt praktisch nie ein gültiges Intent auf ein existierendes Ziel.
    3. Trigger, den der Nutzer mit einem Stopp-Wort abgebrochen hat →
       Fehltrigger. Der Abbruch ist ein ausdrückliches Urteil des Nutzers.

    Bewusst NICHT als Label: 'keine_sprache' und 'leer' nach einem Trigger.
    Das sieht nach Fehltrigger aus, deckt aber auch den Fall ab, dass der Ruf
    echt war und der Nutzer dann unterbrochen wurde — zu unsicher für ein
    hartes Label, es bleibt bei der STT-Einstufung.

    GRENZE: ein verlorener Ruf, den der Nutzer NICHT wiederholt hat, taucht
    hier nie auf. Die Bilanz schätzt den Recall deshalb systematisch zu gut.
    """
    labels: dict[str, dict] = {}
    for i, ev in enumerate(events):
        audio = ev.get("audio")
        if not audio:
            continue

        if ev.get("result") == "nearmiss":
            t0 = datetime.fromisoformat(ev["ts"])
            for spaeter in events[i + 1:]:
                dt = (datetime.fromisoformat(spaeter["ts"]) - t0).total_seconds()
                if dt > WIEDERHOLUNG_SEK:
                    break
                if spaeter.get("result") == "trigger":
                    labels[audio] = {"klasse": ECHT,
                                     "grund": f"wiederholt nach {dt:.0f}s"}
                    break

        elif ev.get("result") == "outcome":
            ausgang = ev.get("ausgang")
            if ausgang == "aktuator" and ev.get("status") in ("ausgefuehrt", "zurueckgestellt"):
                labels[audio] = {"klasse": ECHT,
                                 "grund": f"Aktuator: {ev.get('ziel')}/{ev.get('aktion')}"}
            elif ausgang == "stopwort":
                labels[audio] = {"klasse": RAUSCH, "grund": "vom Nutzer abgebrochen"}
    return labels


def _norm_text(text: str) -> str:
    """Wortlaut auf Vergleichsform bringen (klein, ohne Satzzeichen)."""
    return re.sub(r"[^\wäöüß ]", "", (text or "").lower()).strip()


def _sprechfluss(meta: dict) -> str:
    """'ein_satz' | 'pause' | '' — aus der protokollierten ack-Entscheidung.

    Leer heißt: unbekannt (Near-Miss, oder Trigger von vor dem 2026-07-28,
    als die Entscheidung noch nicht mitgeschrieben wurde). Bewusst KEIN
    Rateverfahren aus dem Audio als Ersatz — gegen die echte Entscheidung
    gemessen traf die naheliegende Tail-RMS-Heuristik nur 6 von 9 Fällen,
    und ein falsches Label ist hier schlimmer als gar keins.
    """
    if "ein_satz" not in meta:
        return ""
    return "ein_satz" if meta["ein_satz"] else "pause"


def _lade_triage() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not os.path.exists(TRIAGE_PATH):
        return out
    with open(TRIAGE_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            out[row["audio"]] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seit", type=float, default=None,
                    help="nur Dateien der letzten N Tage")
    ap.add_argument("--auch-trigger", action="store_true",
                    help="Kontrollgruppe (echte Trigger) mitprüfen — dort MUSS "
                         "ein Gaston drin sein, das zeigt die Trefferquote der Methode")
    ap.add_argument("--neu", action="store_true",
                    help="bereits gelabelte Dateien erneut durch die STT schicken")
    args = ap.parse_args()

    if not os.path.isdir(TRIGGER_AUDIO_DIR):
        print(f"Kein Trigger-Archiv unter {TRIGGER_AUDIO_DIR}")
        return 1

    profil = load_profile()
    stt = SpeachesStt(SpeachesState(), profil.speaches_base, profil.speaches_stt_model)
    wake_log = _lade_wake_log()
    selbst = _selbst_labels(_lade_wake_events())
    bekannt = {} if args.neu else _lade_triage()

    endungen = ["_nearmiss.wav"] + (["_wake.wav"] if args.auch_trigger else [])
    cutoff = time.time() - args.seit * 86400 if args.seit else 0
    dateien = sorted(
        n for n in os.listdir(TRIGGER_AUDIO_DIR)
        if any(n.endswith(e) for e in endungen)
        and os.path.getmtime(os.path.join(TRIGGER_AUDIO_DIR, n)) >= cutoff
    )
    if not dateien:
        print("Keine passenden Aufnahmen gefunden.")
        return 0

    neue: list[dict] = []
    zeilen: list[dict] = []
    for name in dateien:
        if name in bekannt:
            zeilen.append(bekannt[name])
            continue
        pfad = os.path.join(TRIGGER_AUDIO_DIR, name)
        text, _ = _transcribe(stt, pfad)
        verworfen = not text
        meta = wake_log.get(name, {})
        # Folgeaufnahme = nach dem Trigger wurde Sprache aufgenommen. Das ist
        # der beste verfügbare Beleg, dass der Ruf echt war (siehe Modul-Doku).
        rec = name.replace("_wake.wav", "_rec.wav")
        row = {
            "audio": name,
            "art": "trigger" if name.endswith("_wake.wav") else "nearmiss",
            # Nur bei Triggern: nach einem Near-Miss folgt keine Aufnahme,
            # und rec == name würde sonst denselben Clip doppelt transkribieren.
            "danach": (_transcribe(stt, os.path.join(TRIGGER_AUDIO_DIR, rec))[0]
                       if name.endswith("_wake.wav")
                       and os.path.exists(os.path.join(TRIGGER_AUDIO_DIR, rec)) else ""),
            "klasse": _klassifiziere(text, verworfen),
            "quelle": "stt",
            "transkript": text,
            "peak": meta.get("peak"),
            "hits": meta.get("hits"),
            "failed_on": meta.get("failed_on"),
            "scores": meta.get("scores"),
            "sprechfluss": _sprechfluss(meta),
        }
        neue.append(row)
        zeilen.append(row)

    # Der Sprechfluss kam später dazu als die ersten Labels — bei schon
    # gelabelten Zeilen aus dem Wake-Log nachtragen, ohne die STT erneut zu
    # bemühen (das Label selbst bleibt unangetastet).
    aktualisiert: list[dict] = []
    for row in zeilen:
        if not row.get("sprechfluss"):
            row["sprechfluss"] = _sprechfluss(wake_log.get(row["audio"], {}))
        # Ein Selbst-Label sticht die STT-Einstufung immer — es beruht auf einer
        # Handlung, nicht auf einem Transkript. Auch bei laengst gelabelten
        # Zeilen, denn die Handlung kann erst nach dem letzten Lauf passiert sein.
        sl = selbst.get(row["audio"])
        if sl and (row.get("klasse") != sl["klasse"] or row.get("quelle") != "selbst"):
            row["vorher_stt"] = row.get("klasse")
            row.update(klasse=sl["klasse"], quelle="selbst", grund=sl["grund"])
            if row not in neue:
                aktualisiert.append(row)

    if neue or aktualisiert:
        with open(TRIAGE_PATH, "a") as f:
            for row in neue + aktualisiert:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # --- Ausgabe -----------------------------------------------------------
    for art in ("trigger", "nearmiss"):
        teil = [r for r in zeilen if r["art"] == art]
        if not teil:
            continue
        c = Counter(r["klasse"] for r in teil)
        titel = ("TRIGGER (echte Rufe UND Fehltrigger gemischt — keine Positivkontrolle!)"
                 if art == "trigger" else "NEAR-MISSES")
        print(f"\n=== {titel}: {len(teil)} ===")
        if art == "trigger":
            print(f"    {c[ECHT]} mit Gaston-Wort, {c[UNKLAR]} unklar, {c[RAUSCH]} ohne Sprache.")
            print("    KEINE Metrik ableiten: hier stecken echte Rufe UND Fehltrigger drin,")
            print("    und die STT verhört 'Gaston' regelmäßig. Die Folgeaufnahme daneben")
            print("    ('→ danach:') zeigt, was nach dem Trigger gesagt wurde — das trennt")
            print("    beim Anhören echten Ruf von Fehltrigger.")
        for klasse, label in ((ECHT, "ECHTER RUF"), (RAUSCH, "RAUSCHEN"), (UNKLAR, "UNKLAR")):
            gruppe = [r for r in teil if r["klasse"] == klasse]
            if not gruppe:
                continue
            print(f"\n  {label} ({len(gruppe)})")
            for r in sorted(gruppe, key=lambda x: -(x.get("peak") or 0)):
                pk = f"peak {r['peak']:.2f}" if r.get("peak") is not None else "peak   ? "
                fo = f" {r['failed_on']:9s}" if r.get("failed_on") else " " * 10
                sf = {"ein_satz": " [Ein-Satz]", "pause": " [Pause]  "}.get(
                    r.get("sprechfluss") or "", " " * 11)
                print(f"    {pk}{fo}{sf} {r['transkript'][:48]!r}")
                if r.get("quelle") == "selbst":
                    vorher = (f", STT sagte {r['vorher_stt']}"
                              if r.get("vorher_stt") and r["vorher_stt"] != r["klasse"] else "")
                    print(f"       selbst gelabelt: {r.get('grund')}{vorher}")
                if r.get("danach"):
                    print(f"       → danach: {r['danach'][:70]!r}")
                if klasse == UNKLAR:
                    print(f"       anhören: aplay {os.path.join(TRIGGER_AUDIO_DIR, r['audio'])}")

    # --- Ein-Satz gegen Pause: triggert der Durchsprech-Fall schlechter? ---
    # Die Frage entscheidet, ob Nachtrainieren mit Ein-Satz-Aufnahmen nötig ist.
    # Gemessen wird an den Triggern, weil nur dort der Sprechfluss feststeht.
    # Aussagekräftig ist der Anteil KURZER Streaks: die müssen am Gate einen
    # höheren Peak erreichen (min_peak_short/min_peak_single) und fallen
    # deshalb als erste durch.
    trig = [r for r in zeilen if r["art"] == "trigger" and r.get("sprechfluss")]
    if trig:
        print("\n=== EIN-SATZ GEGEN PAUSE (nur Trigger, Sprechfluss protokolliert) ===")
        for fluss, label in (("ein_satz", "durchgesprochen"), ("pause", "Pause danach")):
            g = [r for r in trig if r["sprechfluss"] == fluss]
            if not g:
                continue
            hits = [r["hits"] for r in g if r.get("hits")]
            peaks = [r["peak"] for r in g if r.get("peak")]
            kurz = sum(1 for h in hits if h < 3)
            print(f"    {label:<16} n={len(g):>3}   "
                  f"kurze Streaks (<3 Frames): {kurz}/{len(hits)}"
                  f"{f'   Peak-Median {sorted(peaks)[len(peaks) // 2]:.2f}' if peaks else ''}")
        print("    Deutlich mehr kurze Streaks beim Durchsprechen heißt: das Modell")
        print("    kennt das Wort nur isoliert gesprochen — Ein-Satz-Aufnahmen ins")
        print("    Nachtraining aufnehmen (siehe WAKEWORD_PROCESS.md).")

    nm = [r for r in zeilen if r["art"] == "nearmiss"]
    # --- Wiederkehrer: gleiches Transkript mehrfach = wiederkehrende Quelle ---
    # Ein Mensch ruft nicht viermal denselben Satz mit identischem Wortlaut.
    # Solche Gruppen sind Fernseher-Abspänne, Jingles, Ansagen — sie gehören
    # gebündelt in den Negativ-Korpus statt einzeln angehört zu werden.
    if nm:
        haeufig = Counter(_norm_text(r["transkript"]) for r in nm
                          if (r["transkript"] or "").strip())
        wieder = [(t, n) for t, n in haeufig.most_common() if n > 1]
        if wieder:
            print("\n=== WIEDERKEHRER (gleicher Wortlaut mehrfach) ===")
            print("    Ein mehrfach identischer Wortlaut hat ZWEI mögliche Ursachen, und")
            print("    die Selbst-Labels entscheiden welche:")
            print("      ohne Selbst-Label → wiederkehrende Fremdquelle (TV, Jingle,")
            print("        Ansage). Als Block in den NEGATIV-Korpus.")
            print("      mit Selbst-Label 'echter Ruf' → kein Fremdgeräusch, sondern ein")
            print("        wiederkehrender VERHÖRER des Wakeworts. Wer zweimal ruft, wird")
            print("        auch zweimal gleich verhört. Gehört in den POSITIV-Korpus.")
            for t, n in wieder:
                gruppe = [r for r in nm if _norm_text(r["transkript"]) == t]
                echt = sum(1 for r in gruppe
                           if r.get("quelle") == "selbst" and r["klasse"] == ECHT)
                marke = (f"  ⚠️ {echt}× selbst als echter Ruf gelabelt → Verhörer, nicht TV"
                         if echt else "")
                print(f"    {n:>2}×  {t[:52]!r}{marke}")
    if nm:
        c = Counter(r["klasse"] for r in nm)
        echt_peaks = [r["peak"] for r in nm if r["klasse"] == ECHT and r.get("peak")]
        rausch_peaks = [r["peak"] for r in nm if r["klasse"] == RAUSCH and r.get("peak")]
        print(f"\n=== BILANZ NEAR-MISSES ===")
        print(f"    echte Rufe verloren : {c[ECHT]}   (das kostet das Gate an Recall)")
        print(f"    echtes Rauschen     : {c[RAUSCH]}   (das verhindert das Gate zu Recht)")
        print(f"    unklar, anzuhören   : {c[UNKLAR]}")
        if echt_peaks:
            print(f"    Peaks der echten Rufe : {sorted(round(p,2) for p in echt_peaks)}")
        if rausch_peaks:
            print(f"    Peaks des Rauschens   : {sorted(round(p,2) for p in rausch_peaks)}")
        if echt_peaks and rausch_peaks and min(echt_peaks) <= max(rausch_peaks):
            print("    ⚠️  Die Peak-Bereiche ÜBERLAPPEN — eine reine Peak-Schwelle kann")
            print("        echte Rufe und Rauschen hier nicht trennen.")
    print(f"\nLabels: {TRIAGE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
