#!/usr/bin/env python3
"""Timer-Parser (voice_assistant/services/timer_parser.py) gegen echte Transkripte.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.timer_parser_test              # Qwen + medium der Aufnahmen
    ow-venv/bin/python -m tools.timer_parser_test --vorlagen   # die geschriebenen Vorlagen
    ow-venv/bin/python -m tools.timer_parser_test --datei testsets/timer/auswertung_ohne_kontext.jsonl

Quellen
-------
Vorlagen     data/timer/de_saetze.jsonl — ausgedacht, mit erwartetem Ergebnis
Aufnahmen    testsets/timer/aufnahmen.jsonl (privat) — dieselben Sätze,
             eingesprochen und von Qwen/medium transkribiert
             (tools/timer_aufnahme.py). DAS ist die Messung; die Vorlagen
             zeigen nur, ob der Parser die Sätze kann, die er können soll.

Bewertung je Fall
-----------------
    richtig   Gegenbeispiel → None, oder Befehl mit gleicher Aktion, Name
              (Vergleichsform `schluessel`), Dauer und Klingelanzahl
    verpasst  Befehl erwartet, Parser → None. Der Satz geht an den Brain:
              langsam, nicht falsch. Meist hat die STT "Timer" verhört.
    FALSCH    etwas anderes gelesen als gemeint, oder ein Gegenbeispiel als
              Timer. FALSCH muss 0 bleiben.

Ein Befehl, den die STT inhaltlich verdreht hat ("Timer für die Stunde"
statt "Viertelstunde"), zählt gegen die erwartete Bedeutung — auch wenn der
Parser das Transkript korrekt liest. Ehrlich, weil es das ist, was in der
Küche ankäme.

Messreihe (Aufnahmen 2026-10-10: Jochen, 54 Sätze, flüssig gesprochen)
-----------------------------------------------------------------------
    2026-10-10  erster Parser, capabilities a8bf2cb7 (Qwen-Kontext)
                Vorlagen                 54 richtig /  0 verpasst / 0 FALSCH
                Qwen, Live-Kontext       45 / 9 / 0   (31 von 40 Befehlen)
                Qwen, Wiederholung       44 / 10 / 0  (Satz 7 diesmal
                                         "Timer für die Stunde" -> None, gut so)
                Qwen, ohne Kontext       39 / 15 / 0
                Qwen, Kontext + "Timer"  44 / 10 / 0
                medium                   35 / 18 / 1
                Alle verpassten Qwen-Fälle haben "Timer" verhört (Timeout,
                Thalma, Teilmann, Kaima, Nudelkleinbau, Pizzakart, Muddel-
                heimer, Pizzafreimer, Nudelteig) - der Parser übersieht
                nichts, was im Text steht. Das eine FALSCH bei medium ist
                "Timer Reiß" (Name falsch geschrieben, Dauer richtig).
                Beim Bau korrigiert, nur an den Vorlagen sichtbar: "'nen
                Timer" wurde zum Namen "Nen".
    2026-10-10  Zahl vor "Klingeln" ist Anzahl (live: "mit einer Minute
                und einem Klingen" -> 61 s). Alle Zahlen oben unverändert.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.timer_parser_test", *sys.argv[1:]])

from voice_assistant.services.timer_parser import parse, schluessel  # noqa: E402

_VORLAGEN = os.path.join(_REPO, "data", "timer", "de_saetze.jsonl")
_AUFNAHMEN = os.path.join(_REPO, "testsets", "timer", "aufnahmen.jsonl")


def bewerte(text: str | None, erwartet: dict) -> tuple[str, object]:
    b = parse(text) if text else None
    if erwartet.get("aktion") is None:
        return ("richtig" if b is None else "FALSCH"), b
    if b is None:
        return "verpasst", b
    gleich = (b.aktion == erwartet["aktion"]
              and schluessel(b.name) == schluessel(erwartet.get("name"))
              and b.dauer_s == erwartet.get("dauer_s")
              and b.klingeln == erwartet.get("klingeln"))
    return ("richtig" if gleich else "FALSCH"), b


def _zeige(b) -> str:
    if b is None:
        return "None"
    teile = [b.aktion]
    if b.name:
        teile.append(f"name={b.name}")
    if b.dauer_s is not None:
        teile.append(f"{b.dauer_s}s")
    if b.klingeln:
        teile.append(f"{b.klingeln}x")
    return " ".join(teile)


def _erw(e: dict) -> str:
    if e.get("aktion") is None:
        return "None"
    teile = [e["aktion"]]
    if e.get("name"):
        teile.append(f"name={e['name']}")
    if e.get("dauer_s") is not None:
        teile.append(f"{e['dauer_s']}s")
    if e.get("klingeln"):
        teile.append(f"{e['klingeln']}x")
    return " ".join(teile)


def messe(titel: str, faelle: list[tuple[str, str | None, dict]], alle: bool) -> int:
    zaehler = {"richtig": 0, "verpasst": 0, "FALSCH": 0}
    zeilen = []
    for nr, text, erwartet in faelle:
        urteil, b = bewerte(text, erwartet)
        zaehler[urteil] += 1
        if alle or urteil != "richtig":
            zeilen.append(f"  {urteil:<8} {nr:>3}  {text!r}\n"
                          f"               gelesen {_zeige(b):<28} erwartet {_erw(erwartet)}")
    befehle = sum(1 for _, _, e in faelle if e.get("aktion"))
    print(f"\n{titel}: {zaehler['richtig']} richtig / {zaehler['verpasst']} verpasst / "
          f"{zaehler['FALSCH']} FALSCH  ({len(faelle)} Fälle, {befehle} Befehle)")
    print("\n".join(zeilen))
    return zaehler["FALSCH"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vorlagen", action="store_true", help="nur die geschriebenen Vorlagen")
    ap.add_argument("--datei", default=None, help="andere Auswertung (JSONL mit nr/qwen/erwartet)")
    ap.add_argument("--alle", action="store_true", help="auch die richtigen Fälle zeigen")
    args = ap.parse_args()

    falsch = 0
    if args.vorlagen:
        with open(_VORLAGEN, encoding="utf-8") as f:
            v = [json.loads(z) for z in f if z.strip()]
        return 1 if messe("Vorlagen", [(i + 1, z["satz"], z["erwartet"]) for i, z in enumerate(v)],
                          args.alle) else 0

    datei = args.datei or _AUFNAHMEN
    if not os.path.exists(datei):
        print(f"❌ {datei} fehlt (Aufnahmen sind privat, siehe tools/timer_aufnahme.py)")
        return 1
    with open(datei, encoding="utf-8") as f:
        z = [json.loads(x) for x in f if x.strip()]
    for feld in ("qwen", "medium"):
        if any(feld in x for x in z):
            falsch += messe(f"{os.path.basename(datei)} — {feld}",
                            [(x["nr"], x.get(feld), x["erwartet"]) for x in z], args.alle)
    return 1 if falsch else 0


if __name__ == "__main__":
    sys.exit(main())
