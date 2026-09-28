#!/usr/bin/env python3
"""Gemma und Laya als ganze Aktuator-Kette nebeneinander: Test-Set oder Schatten-Log.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.aktuator_vergleich                 # 334 gelabelte Saetze
    ow-venv/bin/python -m tools.aktuator_vergleich --schatten      # Live-Turns seit Start
    ow-venv/bin/python -m tools.aktuator_vergleich --schatten --seit 2026-09-29

Was verglichen wird
-------------------
Das AUSGEFUEHRTE Ergebnis, nicht die Rohantwort. Beide Ketten enden in
denselben Funktionen:

    Gemma   tor() -> classify() (darin _mehrzahl_gruppe) -> verdict()
    Laya    laya_intent.frage_laya() -> als_intent() -> _mehrzahl_gruppe()
            -> verdict()

Gemma laeuft genau wie live (tor_enabled des Profils). Laya fragt den
Dienst aus actuator.schatten_url.

Test-Set (testsets/actuator_tor.jsonl, siehe actuator_tor_test.py): ein
Fall ist
    richtig   schalten=true mit Ziel -> genau dieses Ziel/Aktion(/Wert)
              ausgefuehrt; sonst -> nichts ausgefuehrt (Brain ODER Rueckfrage)
    verpasst  sollte schalten, wurde nicht ausgefuehrt (langsam, nicht falsch)
    FALSCH    etwas ausgefuehrt, das nicht gemeint war — falsches Ziel,
              falsche Aktion/Wert, oder Gerede geschaltet. Der teure Fehler.
    Rueckfrage zaehlt bei Befehlen ohne bestimmbares Ziel als richtig, sonst
    als verpasst.

Schatten-Log (~/.openclaw/workspace/actuator_schatten.log): dort gibt es
keine Labels — gezaehlt wird Uebereinstimmung, die Abweichungen werden
aufgelistet. Die sind das Material fuers Urteil von Hand.

Messreihe (Test-Set, 330 gezaehlte Saetze; gilt fuer capabilities UND Checkpoint)
----------------------------------------------------------------------------

    2026-09-29  capabilities f07c67d0, Set 4391e3f
                Gemma  (Vega, Tor + kompaktes JSON):  315 richtig / 11 verpasst
                       / 4 FALSCH, median 380 ms, max 2252 ms
                Laya   (aktuator-v1, 3060 Ti, Schwelle 0,5): 313 / 16 / 1,
                       median 78 ms, max 96 ms
                Gemmas FALSCH: "Schaut bitte die Küchenbeleuchtung aus" -> ein
                (verkehrte Richtung), "Esstischrohlos auf" -> links ZU,
                "Zwiebel-Rolo 50 Prozent" -> kuechenrollo_links, "Alexa,
                stop." -> rollostop. Alle vier hat Laya richtig oder
                harmlos (Brain/Rueckfrage).
                Layas einziges FALSCH: "Mondzimmer Rollo auf 40%" ->
                wohnzimmerrollo. Label war "Ziel offen"; Wohnzimmer ist als
                Verhoerer plausibel (Gemma hat es live als majarollo
                ausgefuehrt).
                Layas Mehr an "verpasst" ist fast durchweg harmlos und hat
                ein erkennbares Muster: GETRENNTE Namen ("rosa Zimmer Rollo",
                "Wohnzimmerverlauf") -> ziel "keins". Die Trainingsvorlagen
                kennen nur die Namen aus den capabilities, also das
                Kompositum. Naechster Hebel im Generator, nicht im Modell.
                VORBEHALT wie beim Tor: die Vorlagen entstanden nach dem
                Lesen dieses Sets. Belastbar ist der Schattenbetrieb.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.aktuator_vergleich", *sys.argv[1:]])

from voice_assistant.config import ACTUATOR_SCHATTEN_LOG_PATH, load_profile  # noqa: E402
from voice_assistant.services.actuator import (  # noqa: E402
    VERDICT_AUSFUEHRBAR, Actuator,
)
from voice_assistant.services.aktuator_schatten import ausgang  # noqa: E402
from voice_assistant.services.laya_intent import als_intent, frage_laya  # noqa: E402

_TESTSET = os.path.join(_REPO, "testsets", "actuator_tor.jsonl")


def kette_gemma(akt: Actuator, satz: str) -> tuple[dict | None, str, float]:
    import time
    t0 = time.time()
    if akt.cfg.tor_enabled:
        u = akt.tor(satz)
        if not u.ja:
            return {"ist_kommando": False}, "kein_kommando", (time.time() - t0) * 1000
    intent = akt.classify(satz)
    v, _ = akt.verdict(intent, satz)
    return intent, v, (time.time() - t0) * 1000


def kette_laya(akt: Actuator, url: str, satz: str, schwelle: float):
    u = frage_laya(url, satz, akt.digest or {}, timeout=10)
    intent = als_intent(u, akt.digest or {}, schwelle)
    if intent is not None:
        intent = akt._mehrzahl_gruppe(satz, intent, still=True)
    v, _ = akt.verdict(intent, satz)
    return intent, v, u


def bewerte(f: dict, intent: dict | None, verdict: str) -> str:
    # Ein Ausfall ist kein Urteil. Am 2026-09-29 lieferte Laya 330-mal HTTP
    # 500, und die Tabelle zeigte trotzdem "226 richtig" — jeder Nicht-
    # Befehl war per Ausfall "richtig nicht geschaltet". Deshalb eigene Klasse.
    if intent is None:
        return "Ausfall"
    ausgefuehrt = verdict == VERDICT_AUSFUEHRBAR
    if f.get("schalten") and f.get("ziel"):
        if not ausgefuehrt:
            return "verpasst"
        ok = (intent.get("ziel") == f["ziel"] and intent.get("aktion") == f.get("aktion")
              and (f.get("wert") is None or intent.get("wert") == f["wert"]))
        return "richtig" if ok else "FALSCH"
    return "FALSCH" if ausgefuehrt else "richtig"


def testset(akt: Actuator, url: str, schwelle: float, datei: str, json_aus: str | None) -> int:
    faelle = [json.loads(z) for z in open(datei, encoding="utf-8")
              if z.strip() and not z.startswith("#")]
    faelle = [f for f in faelle if f.get("schalten") is not None]
    akt.tor("Mach das Licht an", timeout=120)          # Prompt-Cache warm
    akt.classify("Mach das Licht an", timeout=120)
    frage_laya(url, "Mach das Licht an", akt.digest or {}, timeout=60)

    zaehl = {"gemma": collections.Counter(), "laya": collections.Counter()}
    ms = {"gemma": [], "laya": []}
    zeilen = []
    for f in faelle:
        gi, gv, gms = kette_gemma(akt, f["satz"])
        li, lv, lu = kette_laya(akt, url, f["satz"], schwelle)
        gk, lk = bewerte(f, gi, gv), bewerte(f, li, lv)
        zaehl["gemma"][gk] += 1
        zaehl["laya"][lk] += 1
        ms["gemma"].append(gms)
        ms["laya"].append(lu.ms)
        z = {"satz": f["satz"], "soll": ausgang(f, VERDICT_AUSFUEHRBAR) if f.get("ziel") and f.get("schalten")
             else ("schalten, Ziel offen" if f.get("schalten") else "nichts schalten"),
             "gemma": ausgang(gi, gv), "gemma_klasse": gk,
             "laya": ausgang(li, lv), "laya_klasse": lk, "laya_roh": lu.als_dict()}
        zeilen.append(z)

    print(f"{len(faelle)} Saetze, capabilities {akt.version}, Laya {url} (Schwelle {schwelle})\n")
    print(f"{'':8s} {'richtig':>8s} {'verpasst':>9s} {'FALSCH':>7s} {'Ausfall':>8s}   Latenz median / max")
    for k in ("gemma", "laya"):
        c = zaehl[k]
        print(f"{k:8s} {c['richtig']:8d} {c['verpasst']:9d} {c['FALSCH']:7d} {c['Ausfall']:8d}   "
              f"{statistics.median(ms[k]):.0f} / {max(ms[k]):.0f} ms")
        if c["Ausfall"]:
            print(f"   ⚠️  {k}: {c['Ausfall']} Ausfaelle — die Zeile ist KEIN Vergleich")
    print("\nAbweichungen (mindestens eine Kette nicht richtig, oder beide verschieden):")
    for z in zeilen:
        if z["gemma_klasse"] == z["laya_klasse"] == "richtig" and z["gemma"] == z["laya"]:
            continue
        print(f"  {z['satz'][:80]}\n      soll  {z['soll']}\n"
              f"      gemma {z['gemma']:34s} {z['gemma_klasse']}\n"
              f"      laya  {z['laya']:34s} {z['laya_klasse']}  "
              f"(P ja {z['laya_roh']['p_ja']}, ziel {z['laya_roh']['ziel']} {z['laya_roh']['p_ziel']})")
    if json_aus:
        json.dump({"capabilities": akt.version, "laya": url, "schwelle": schwelle, "faelle": zeilen},
                  open(json_aus, "w"), ensure_ascii=False, indent=1)
    return 0


def schatten(seit: str | None) -> int:
    if not os.path.exists(ACTUATOR_SCHATTEN_LOG_PATH):
        print(f"noch kein Schatten-Log: {ACTUATOR_SCHATTEN_LOG_PATH}")
        return 2
    zeilen = [json.loads(z) for z in open(ACTUATOR_SCHATTEN_LOG_PATH, encoding="utf-8") if z.strip()]
    if seit:
        zeilen = [z for z in zeilen if z["ts"] >= seit]
    c = collections.Counter()
    abw = []
    lms = [z["laya"]["ms"] for z in zeilen if not z["laya"].get("fehler")]
    for z in zeilen:
        g, la = z["gemma_ausgang"], z["laya_ausgang"]
        if z["laya"].get("fehler"):
            c["Laya-Ausfall"] += 1
        elif g == la:
            c["gleich"] += 1
        else:
            art = ("nur Gemma schaltet" if "/" in g and "/" not in la else
                   "nur Laya schaltet" if "/" in la and "/" not in g else
                   "beide schalten, verschieden" if "/" in g and "/" in la else
                   "beide schalten nicht, verschieden")
            c[art] += 1
            abw.append((art, z))
    print(f"{len(zeilen)} Turns im Schatten-Log" + (f" seit {seit}" if seit else ""))
    for k, n in c.most_common():
        print(f"  {k:32s} {n}")
    if lms:
        print(f"  Laya-Latenz median {statistics.median(lms):.0f} ms, max {max(lms)} ms")
    if abw:
        print("\nAbweichungen:")
        for art, z in abw:
            print(f"  [{z['ts']}] {z['transcript'][:80]}\n      {art}: gemma {z['gemma_ausgang']}, "
                  f"laya {z['laya_ausgang']} (P ja {z['laya']['p_ja']}, ziel {z['laya']['ziel']} "
                  f"{z['laya']['p_ziel']})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--schatten", action="store_true", help="Schatten-Log statt Test-Set auswerten")
    ap.add_argument("--seit", help="nur Schatten-Turns ab diesem Zeitstempel (ISO)")
    ap.add_argument("--laya-url", help="Default: actuator.schatten_url des Profils")
    ap.add_argument("--schwelle", type=float, help="P(ja) ab der Laya schaltet (Default: Profil)")
    ap.add_argument("--datei", default=_TESTSET)
    ap.add_argument("--json", help="Ergebnis je Satz hierhin schreiben")
    args = ap.parse_args()
    if args.schatten:
        return schatten(args.seit)

    profil = load_profile()
    cfg = profil.actuator
    url = args.laya_url or cfg.schatten_url
    if not cfg.enabled or not url:
        print("Aktuator aus oder keine Laya-URL (actuator.schatten_url / --laya-url).")
        return 2
    akt = Actuator(cfg)
    if not akt.refresh():
        print("capabilities-refresh fehlgeschlagen — laeuft die Gegenstelle?")
        return 2
    return testset(akt, url, args.schwelle if args.schwelle is not None else cfg.schatten_schwelle,
                   args.datei, args.json)


if __name__ == "__main__":
    sys.exit(main())
