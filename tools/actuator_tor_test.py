#!/usr/bin/env python3
"""Torfrage des Aktuators gegen ein gelabeltes Test-Set messen.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.actuator_tor_test
    ow-venv/bin/python -m tools.actuator_tor_test --zeige-alle --json out.json
    ow-venv/bin/python -m tools.actuator_tor_test --quelle brain

Was gemessen wird
-----------------
Nur die Torfrage (`Actuator.tor()`, tor_enabled): will der Sprecher ein
Haus-Geraet schalten? Nicht classify(), nicht verdict() — dafuer gibt es
tools/actuator_grammar_test.py. Gemessen wird der ECHTE Code-Pfad mit dem
Tor-Prompt, den refresh() aus den aktuellen /capabilities baut.

Neben richtig/uebersehen/FALSCH zaehlt hier die zweite Frage mindestens so
viel: ist P(ja) KALIBRIERT? Bei Gemma nicht (2026-09-23: 16 von 19
uebersehenen Kommandos mit P(ja) < 0,01) — Gemma irrt selbstsicher, eine
Schwelle oder ein Rueckfrage-Band faengt dann nichts. Ein Kandidat fuer den
Tor-Platz (Laya o. ae.) lohnt sich erst, wenn seine Irrtuemer im mittleren
P-Bereich liegen. Deshalb die Kalibrier-Tabelle und der Brier-Score.

Das Test-Set
------------
Liegt NICHT im Repo (testsets/ ist gitignored, eigenes privates Git): die
Saetze stammen aus echten Turns dieses Haushalts — Namen, Termine, Essen,
Arbeitszeiten. Dieses Repo ist oeffentlich. Eine JSON-Zeile pro Satz,
Zeilen mit '#' sind Kommentare:

    {"satz": "...", "schalten": true|false|null,
     "ziel": "...", "aktion": "...", "wert": 50,
     "quelle": "brain:recording", "ts": "...",
     "label_von": "claude-2026-09-28" | "jochen" | "testset", "notiz": "..."}

`schalten` ist die ABSICHT des Sprechers, nicht was ausgefuehrt wurde:
  true   er will ein Haus-Geraet schalten — auch wenn das Geraet kein Ziel
         ist ("Monitor an") oder das Ziel nicht erkennbar ist ("Rollo zu",
         verhoerte Namen). Das zu klaeren ist Sache von classify/verdict,
         nicht des Tors.
  false  alles andere, auch Auftraege an den Brain, die nach Schalten
         klingen ("zeig den Ofen auf dem Monitor", Kalender, Timer).
  null   Absicht nicht zu erkennen — wird nicht gezaehlt, nur aufgelistet.
ziel/aktion/wert sind optional und fuer spaetere Messungen (Fine-Tuning,
classify auf echten Turns) mitgefuehrt; null heisst "nicht erkennbar".

Wie es entstand (2026-09-28): 32 Saetze aus actuator_grammar_test.py, alle
Turns aus actuator_turns.log und actuator_tor.log, alle Brain-Turns aus
endpoint.log (recording + followup), dedupliziert — 334 Saetze. Vorlaeufig
gelabelt nach Ausgang, dann jede Zeile von Hand gelesen: 18 Saetze, die
beim Brain landeten, waren in Wahrheit Kommandos (13 aus der Zeit vor dem
Aktuator, 5 seit dem Tor-Umbau, vom Tor abgewiesen — darunter "Gaston,
schalte bitte das Abendlicht ein."). Zwei ausgefuehrte waren keine
Schaltabsicht ("Alexa, stop." -> rollostop, und der Kalender-Vorfall vom
2026-09-23), 4 sind unklar. `label_von` sagt, wer eine Zeile zuletzt
beurteilt hat.

Messreihe (jede Zahl gilt nur fuer ihre capabilities- UND Tor-Prompt-Version):

    2026-09-28  Gemma (gemma-4-E2B Q4_K_M, Vega), Tor-Prompt e19deab3,
                capabilities f07c67d0, Set-Stand 4391e3f:
                330 gezaehlt (121 Kommandos / 209 Gerede), 4 unklar
                richtig 305, uebersehen 24 (8 mit Ziel), FALSCH 1, Ausfall 0
                FALSCH: "Alexa, stop." mit P(ja)=1,000
                Brier 0,075; 21 von 24 uebersehenen mit P(ja) < 0,01 —
                bestaetigt den Befund vom 2026-09-23 auf dem groesseren Set:
                Gemmas P(ja) taugt nicht als Schwelle. Median 331 ms.
                Die Nulllinie fuer jeden Tor-Kandidaten.
                Schwellenfrei: AUROC 0,973; Kommandos durch bei 1 / 3
                zugelassenen FALSCH: 102 / 115 von 121.

    2026-09-28  Laya 0.3.21 laya-multilingual ZERO-SHOT, CPU (6 Threads),
                laya-serve, gleiches Set. Median ~70 ms.
                Frage "schlicht" (noul, Ja/Nein-Frage):
                  bei 0,5: richtig 280, uebersehen 39, FALSCH 11
                  AUROC 0,883; durch bei 0/1/3 FALSCH: 5 / 20 / 48
                Frage "neutral" (noul mit criteria + Labels A/B, #156):
                  bei 0,5: richtig 224, uebersehen 2, FALSCH 104
                  AUROC 0,930; durch bei 0/1/3 FALSCH: 0 / 11 / 54
                Zero-shot klar schlechter als Gemma. Das oberste Gerede sind
                AUFTRAEGE an den Brain ("trag ... ein", "setz mir auf die
                To-do-Liste", P 0,99+) — Laya trennt "will etwas erledigt
                haben" nicht von "will ein Geraet schalten". Dafuer liegen
                seine Irrtuemer im mittleren P-Bereich (schlicht: 1 von 39
                uebersehenen < 0,01), das ist die Eigenschaft, die Gemma
                fehlt. Laut eigener Doku kommt die Faehigkeit erst aus dem
                Fine-Tuning ("a fast base to specialise, not a zero-shot
                decision engine") — die Zahl hier ist die Ausgangslage dafuer,
                kein Urteil ueber den Tor-Platz.

    2026-09-28  Laya FEINABGESTIMMT (tools/laya_tor_train.py, heute laya_aktuator_train.py; ckpt tor-v1):
                laya-multilingual, 2 Epochen auf testsets/tor_train.jsonl
                (6.553 Saetze, tools/tor_trainset.py, Stand 26f0371),
                Einbettungen eingefroren, 100 s auf der 3060 Ti (3,05 GB),
                Temperatur noul 1,825. CPU, Frage "schlicht":
                  bei 0,5: richtig 317, uebersehen 9 (8 mit Ziel), FALSCH 4
                  AUROC 0,995; durch bei 0/1/3 FALSCH: 102 / 108 / 110
                  Brier 0,036; kein uebersehenes Kommando mit P < 0,01;
                  median 70 ms
                Gegen Gemma (0,973; 102/115 bei 1/3 FALSCH): besser getrennt,
                halber Brier, 5x schneller. Die FALSCH sind Kauderwelsch
                ("Das sieht schlichten aus.", "Ich soll das einfach
                hintellen.", P 0,94-0,97) und "Monitor bitte wieder zurück
                auf die normale Ansicht"; uebersehen u.a. "Stopp alle
                Rollos!" (P 0,19).
                VORSICHT, die Zahl ist optimistisch: die Vorlagen des
                Generators entstanden, NACHDEM dieses Set von Hand gelesen
                war (Anreden, Essensliste, Arbeitszeiten stammen aus
                denselben Logs). Keine woertliche Ueberschneidung, aber der
                Stil ist abgeschaut. Belastbar ist erst eine Messung auf
                Saetzen, die NACH dem 2026-09-28 gesprochen wurden.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.actuator_tor_test", *sys.argv[1:]])

from voice_assistant.config import load_profile  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402
from voice_assistant.services.laya_intent import TOR_FRAGE  # noqa: E402

_DEFAULT_SET = os.path.join(_REPO, "testsets", "actuator_tor.jsonl")

# Kalibrier-Faecher fuer P(ja). Das unterste ist bewusst eng: dort lagen bei
# Gemma die selbstsicheren Irrtuemer.
_FAECHER = [0.0, 0.01, 0.1, 0.5, 0.9, 0.99, 1.0000001]


def lade_set(pfad: str) -> list[dict]:
    faelle = []
    with open(pfad, encoding="utf-8") as f:
        for nr, zeile in enumerate(f, 1):
            zeile = zeile.strip()
            if not zeile or zeile.startswith("#"):
                continue
            try:
                faelle.append(json.loads(zeile))
            except json.JSONDecodeError as e:
                sys.exit(f"{pfad}:{nr}: kein JSON ({e})")
    return faelle


def modell_gemma(akt: Actuator):
    """Die Torfrage, wie sie live laeuft. Timeout grosszuegig: gemessen wird
    das Urteil, nicht ob der Live-Timeout reicht (die Latenz steht daneben)."""
    def frage(satz: str) -> tuple[bool | None, float | None, float]:
        u = akt.tor(satz, timeout=60)
        return u.ja, u.p_ja, u.ms
    return frage


# Laya-Fragen (noul = kalibrierte P(true), ein Forward-Pass, kein Text).
# Bewusst OHNE Zielliste: der Options-Teil hat bei laya-multilingual nur 256
# Token (head_max_len), die ~2700 Token des Gemma-Tor-Prompts passen nicht.
# "neutral" setzt die Modell-Labels auf A/B — laut Laya-README (#156) kann
# noul sonst am Wortpaar false:/true: haengen statt am Satz.
_LAYA_FRAGEN = {
    "schlicht": TOR_FRAGE,   # die Frage, auf die tools/laya_aktuator_train.py trainiert
    "neutral": {
        "type": "noul",
        "instructions": "Was will der Sprecher mit diesem Satz?",
        "criteria": {
            "true": "ein Gerät im Haus schalten oder einstellen, zum Beispiel "
                    "Licht, Rollo oder Heizung",
            "false": "etwas anderes: eine Frage, eine Notiz, einen Termin, "
                     "oder er spricht gar nicht mit dem Assistenten",
        },
        "labels": {"true": "A", "false": "B"},
    },
}


def modell_laya(url: str, frage_name: str):
    """Laya ueber laya-serve (/v1/systemone, Jev-Protokoll) — so, wie es auch
    live angebunden waere. Urteil ja bei P(true) >= 0,5."""
    import time
    import urllib.request
    body_frage = {"tor": _LAYA_FRAGEN[frage_name]}

    def frage(satz: str) -> tuple[bool | None, float | None, float]:
        t0 = time.time()
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/systemone",
            data=json.dumps({"state": {"satz": satz}, "questions": body_frage,
                             "model": "multilingual"}).encode(),
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                antwort = json.load(r)
            p = float(antwort["answers"]["tor"]["noul"])
        except Exception as e:
            print(f"⚠️  laya: {e}")
            return None, None, (time.time() - t0) * 1000
        return p >= 0.5, p, (time.time() - t0) * 1000
    return frage


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--datei", default=_DEFAULT_SET, help="Test-Set (JSONL)")
    ap.add_argument("--quelle", help="nur Faelle, deren 'quelle' so beginnt")
    ap.add_argument("--json", help="Ergebnis je Satz hierhin schreiben")
    ap.add_argument("--zeige-alle", action="store_true",
                    help="auch die richtigen Faelle einzeln auflisten")
    ap.add_argument("--modell", choices=("gemma", "laya"), default="gemma",
                    help="gemma = Actuator.tor() wie live; laya = laya-serve")
    ap.add_argument("--laya-url", default="http://127.0.0.1:8095")
    ap.add_argument("--laya-frage", choices=sorted(_LAYA_FRAGEN), default="neutral")
    args = ap.parse_args()

    if not os.path.exists(args.datei):
        print(f"Test-Set fehlt: {args.datei} (liegt bewusst nicht im Repo)")
        return 2
    faelle = lade_set(args.datei)
    if args.quelle:
        faelle = [f for f in faelle if (f.get("quelle") or "").startswith(args.quelle)]

    profil = load_profile()
    if not profil.actuator.enabled or not profil.actuator.base_url:
        print("Aktuator ist in diesem Profil nicht konfiguriert.")
        return 2
    if args.modell == "gemma" and not profil.actuator.tor_enabled:
        print("tor_enabled ist in diesem Profil aus — es gibt keine Torfrage zu messen.")
        return 2
    akt = Actuator(profil.actuator)
    if not akt.refresh():
        print("capabilities-refresh fehlgeschlagen — laeuft die Gegenstelle?")
        return 2
    if args.modell == "gemma":
        frage = modell_gemma(akt)
        prompt = akt.tor_prompt or ""
        wer = f"Gemma {profil.actuator.llm_url}"
    else:
        frage = modell_laya(args.laya_url, args.laya_frage)
        prompt = json.dumps(_LAYA_FRAGEN[args.laya_frage], sort_keys=True)
        wer = f"Laya {args.laya_url} Frage '{args.laya_frage}'"
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]

    fehlend = sorted({f["ziel"] for f in faelle
                      if f.get("ziel") and f["ziel"] not in (akt.digest or {})})
    if fehlend:
        print(f"⚠️  Test-Set nennt Ziele, die es nicht (mehr) gibt: {fehlend}")

    frage("Mach das Licht an")  # aufwaermen (Gemma: Prompt-Cache, ~2700 Token)

    print(f"{wer}, Prompt {prompt_hash}, capabilities {akt.version}, "
          f"{len(faelle)} Saetze\n")

    ergebnis = []
    for f in faelle:
        ja, p_ja, ms = frage(f["satz"])
        soll = f.get("schalten")
        if soll is None:
            klasse = "unklar"
        elif ja is None:
            klasse = "ausfall"          # live: geht an den Brain
        elif ja == soll:
            klasse = "richtig"
        elif soll:
            klasse = "uebersehen"       # langsam ueber den Brain, nicht gefaehrlich
        else:
            klasse = "FALSCH"           # Tor offen fuer Gerede
        ergebnis.append({**f, "tor": ja, "p_ja": p_ja, "ms": round(ms), "klasse": klasse})
        if klasse != "richtig" or args.zeige_alle:
            p = f"{p_ja:.3f}" if p_ja is not None else "  —  "
            print(f"{klasse:10s} P(ja)={p}  soll={str(soll):5s} {f['satz'][:90]}")

    gezaehlt = [e for e in ergebnis if e["klasse"] != "unklar"]
    k = {c: sum(1 for e in gezaehlt if e["klasse"] == c)
         for c in ("richtig", "uebersehen", "FALSCH", "ausfall")}
    n_ja = sum(1 for e in gezaehlt if e["schalten"])
    print(f"\n{len(gezaehlt)} gezaehlt ({n_ja} Kommandos, {len(gezaehlt) - n_ja} Gerede), "
          f"{len(ergebnis) - len(gezaehlt)} unklar ausgelassen")
    print(f"  richtig {k['richtig']}   uebersehen {k['uebersehen']}   "
          f"FALSCH {k['FALSCH']}   Ausfall {k['ausfall']}")
    # Ein uebersehenes Kommando ohne bestimmbares Ziel haette auch classify
    # nicht ausfuehren duerfen — der Brain (Rueckfrage) ist dort der richtige
    # Weg. Wirklich verloren sind nur die mit Ziel.
    mit_ziel = sum(1 for e in gezaehlt if e["klasse"] == "uebersehen" and e.get("ziel"))
    print(f"  davon uebersehen mit bestimmbarem Ziel: {mit_ziel}, "
          f"ohne (Einzahl-Rollo, kein Ziel, verhoert): {k['uebersehen'] - mit_ziel}")

    mit_p = [e for e in gezaehlt if e["p_ja"] is not None]
    if mit_p:
        brier = statistics.fmean((e["p_ja"] - (1.0 if e["schalten"] else 0.0)) ** 2 for e in mit_p)
        print(f"\nKalibrierung P(ja), Brier {brier:.3f} (0 = perfekt, 0,25 = Muenzwurf)")
        print(f"  {'Fach':>13s}  {'n':>4s}  {'davon Kommando':>14s}")
        for lo, hi in zip(_FAECHER, _FAECHER[1:]):
            fach = [e for e in mit_p if lo <= e["p_ja"] < hi]
            if not fach:
                continue
            anteil = sum(1 for e in fach if e["schalten"]) / len(fach)
            print(f"  [{lo:.2f}, {min(hi, 1.0):.2f})  {len(fach):4d}  {anteil:13.0%}")
        ueb = [e["p_ja"] for e in mit_p if e["klasse"] == "uebersehen"]
        if ueb:
            print(f"  uebersehene Kommandos mit P(ja) < 0,01: {sum(p < 0.01 for p in ueb)} von {len(ueb)}")

        # Schwellenfrei: ein unkalibriertes Modell ist bei 0,5 nicht fair
        # beurteilt (Laya zero-shot lag je nach Frageform ganz oben oder ganz
        # unten). AUROC = wie gut trennt P(ja) ueberhaupt; dazu, wie viele
        # Kommandos bei der Schwelle durchkaemen, die k Stueck Gerede zulaesst.
        pos = [e["p_ja"] for e in mit_p if e["schalten"]]
        neg = sorted((e["p_ja"] for e in mit_p if not e["schalten"]), reverse=True)
        if pos and neg:
            auroc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / len(pos) / len(neg)
            durch = "  ".join(f"{k} FALSCH: {sum(p > neg[k] for p in pos)}"
                              for k in (0, 1, 3) if k < len(neg))
            print(f"  AUROC {auroc:.3f}; Kommandos durch (von {len(pos)}) bei Schwelle fuer  {durch}")

    lat = [e["ms"] for e in ergebnis if e["tor"] is not None]
    if lat:
        print(f"\nLatenz median {statistics.median(lat):.0f} ms, max {max(lat)} ms")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as o:
            json.dump({"modell": wer, "prompt": prompt_hash, "capabilities": akt.version,
                       "faelle": ergebnis},
                      o, ensure_ascii=False, indent=1)
    return 0 if not (k["FALSCH"] or k["uebersehen"]) else 1


if __name__ == "__main__":
    sys.exit(main())
