#!/usr/bin/env python3
"""Musik-Erkenner (voice_assistant/services/musik_parser.py) gegen echte Transkripte.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.musik_parser_test              # Vorlagen + echte Sätze
    ow-venv/bin/python -m tools.musik_parser_test --vorlagen   # nur die geschriebenen Vorlagen
    ow-venv/bin/python -m tools.musik_parser_test --alle       # auch die richtigen zeigen

Quellen
-------
Vorlagen      data/musik/de_saetze.jsonl — ausgedacht, mit erwartetem Ergebnis.
              Zeigen nur, ob der Erkenner die Sätze kann, die er können soll.
Echte Sätze   testsets/musik_steuerung/echte_saetze.jsonl (privat): jeder
              Sprach-Turn aus dem OpenClaw-Verlauf (Label aus dem Werkzeug,
              das der Brain danach aufrief), dazu das Tor-Testset und die
              Aktuator-Logs als Gegenbeispiele. Von Jochen geprüft
              2026-10-18. DAS ist die Messung.

Bewertung je Fall
-----------------
    richtig   Gegenbeispiel → None, oder Befehl mit gleicher Aktion und Raum
    verpasst  Befehl erwartet, Erkenner → None: geht an den Brain, langsam,
              nicht falsch
    FALSCH    etwas anderes gelesen als gemeint, oder ein Gegenbeispiel als
              Befehl. FALSCH muss 0 bleiben.

Raum wird ohne Groß/klein verglichen; aufgelöst gegen die Bridge wird später.

Messreihe
---------
    2026-10-18  erster Erkenner
                Vorlagen       54 richtig / 0 verpasst / 0 FALSCH  (54 Fälle, 30 Befehle)
                echte Sätze   695 richtig / 1 verpasst / 0 FALSCH  (696 Fälle, 10 Befehle)
                Der verpasste ist „Das nächste Licht bitte.“ (Lied verhört),
                gewollt. Dünn: echte Befehle nur stop/next/resume, keiner
                für pause, previous, lauter, leiser.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.musik_parser_test", *sys.argv[1:]])

from voice_assistant.services.musik_parser import parse  # noqa: E402

_VORLAGEN = os.path.join(_REPO, "data", "musik", "de_saetze.jsonl")
_ECHT = os.path.join(_REPO, "testsets", "musik_steuerung", "echte_saetze.jsonl")


def _klein(raum: str | None) -> str | None:
    return raum.lower() if raum else None


def bewerte(text: str, erwartet: dict) -> tuple[str, object]:
    b = parse(text)
    if erwartet.get("aktion") is None:
        return ("richtig" if b is None else "FALSCH"), b
    if b is None:
        return "verpasst", b
    gleich = b.aktion == erwartet["aktion"] and _klein(b.raum) == _klein(erwartet.get("raum"))
    return ("richtig" if gleich else "FALSCH"), b


def _zeige(b) -> str:
    if b is None:
        return "None"
    return b.aktion + (f" raum={b.raum}" if b.raum else "")


def _erw(e: dict) -> str:
    if e.get("aktion") is None:
        return "None"
    return e["aktion"] + (f" raum={e['raum']}" if e.get("raum") else "")


def messe(titel: str, faelle: list[tuple[str, dict]], alle: bool) -> int:
    zaehler = {"richtig": 0, "verpasst": 0, "FALSCH": 0}
    zeilen = []
    for text, erwartet in faelle:
        urteil, b = bewerte(text, erwartet)
        zaehler[urteil] += 1
        if alle or urteil != "richtig":
            zeilen.append(f"  {urteil:<8} {text!r}\n"
                          f"           gelesen {_zeige(b):<22} erwartet {_erw(erwartet)}")
    befehle = sum(1 for _, e in faelle if e.get("aktion"))
    print(f"\n{titel}: {zaehler['richtig']} richtig / {zaehler['verpasst']} verpasst / "
          f"{zaehler['FALSCH']} FALSCH  ({len(faelle)} Fälle, {befehle} Befehle)")
    if zeilen:
        print("\n".join(zeilen))
    return zaehler["FALSCH"]


def _lesen(pfad: str) -> list[dict]:
    with open(pfad, encoding="utf-8") as f:
        return [json.loads(z) for z in f if z.strip() and not z.startswith("#")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vorlagen", action="store_true", help="nur die geschriebenen Vorlagen")
    ap.add_argument("--alle", action="store_true", help="auch die richtigen Fälle zeigen")
    args = ap.parse_args()

    falsch = messe("Vorlagen", [(v["satz"], v["erwartet"]) for v in _lesen(_VORLAGEN)],
                   args.alle)
    if not args.vorlagen:
        if not os.path.exists(_ECHT):
            print(f"\n❌ {_ECHT} fehlt (echte Sätze sind privat)")
            return 1
        falsch += messe("echte Sätze",
                        [(s["satz"], {"aktion": s["aktion"], "raum": s["raum"]})
                         for s in _lesen(_ECHT)], args.alle)
    return 1 if falsch else 0


if __name__ == "__main__":
    sys.exit(main())
