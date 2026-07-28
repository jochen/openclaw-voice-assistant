#!/usr/bin/env python3
"""Aktuator-Prompt gegen ein festes Test-Set messen.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.actuator_grammar_test
    ow-venv/bin/python -m tools.actuator_grammar_test --zeige-alle

Warum es dieses File gibt
-------------------------
Der Aktuator-Prompt ist empfindlich: eine geaenderte Zeile kippt Faelle, die
vorher sassen. Deshalb gilt im Projekt, Prompt-Aenderungen nur gegen ein
Test-Set zu entscheiden, nie nach Gefuehl.

Die Messung vom 2026-07-27 (Prompt-Variante B, "20/20 korrekt") lag in einem
Scratchpad und war einen Tag spaeter nicht mehr reproduzierbar — das Test-Set
war weg, die Zahl damit wertlos. Deshalb liegt es jetzt hier im Repo.

Gemessen wird gegen den ECHTEN Code-Pfad: Actuator.classify() mit dem Prompt,
der aus den aktuellen /capabilities gebaut wird. Nicht gegen eine Kopie des
Prompts — sonst misst man etwas, das so nie laeuft.

Der Aufbau des Satzes
---------------------
Schwerpunkt ist die ROLLO-OHNE-RAUM-Regel, weil dort der teuerste Fehler
sitzt: "Rollo zu" (Einzahl) auf alle_rollos zu schicken schliesst nachts das
ganze Haus. Die MEHRZAHL ist davon ausgenommen — "die Rollos zu" meint
umgangssprachlich alle und darf auf alle_rollos gehen (Entscheidung Jochen
2026-07-28). Die Regel darf also genau die Einzahl abfangen, nicht mehr.
Dazu die Gegenprobe, dass die Regel nicht zu viel frisst (Raum-Rollos und
"alle Rollos" muessen weiter durchgehen), die Grundfaelle Licht/Heizung, und
seit dem Pre-Roll (2026-07-28) Saetze mit vorangestelltem Wakewort — die
Aufnahme beginnt seither vor dem Wakewort, das Transkript traegt es also mit,
oft verhoert als Gastau/Gastraum/Gastronom.

wert=None in der Erwartung heisst: Wert wird nicht geprueft.
"""

from __future__ import annotations

import argparse
import os
import sys

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.actuator_grammar_test", *sys.argv[1:]])

from voice_assistant.config import load_profile  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402

# (Satz, ziel, aktion, wert) — ziel=None bedeutet: darf KEIN Kommando sein.
TESTS: list[tuple[str, str | None, str | None, int | None]] = [
    # --- ROLLO OHNE RAUM, EINZAHL: darf nicht auf alle_rollos einrasten ------
    # Nur die Einzahl ist unentscheidbar — "Rollo zu" nachts auf das ganze Haus
    # zu schicken ist der teure Fehler, den die Regel verhindern soll.
    ("Rollo auf 70%", None, None, None),
    ("Rollo zu", None, None, None),
    ("Mach das Rollo auf", None, None, None),
    ("Rollo hoch", None, None, None),

    # --- ROLLO OHNE RAUM, MEHRZAHL: alle_rollos ist richtig so ---------------
    # Entscheidung Jochen 2026-07-28: "die Rollos" meint umgangssprachlich sehr
    # wohl alle. Nur der Singular bleibt unentscheidbar. Frueher hat die Regel
    # beides verworfen — das war zu viel.
    ("Rollos runter", "alle_rollos", "zu", None),
    ("Mach die Rollos zu", "alle_rollos", "zu", None),

    # --- ... und "alle" muss weiter greifen ----------------------------------
    ("Mach alle Rollos zu", "alle_rollos", "zu", None),
    ("Alle Rollos auf", "alle_rollos", "auf", None),
    ("Alle Rollos auf 50 Prozent", "alle_rollos", "setzen", 50),

    # --- Raum/Geraet genannt: muss das Einzelziel treffen ---------------------
    ("Wohnzimmerrollo auf 70%", "wohnzimmerrollo", "setzen", 70),
    ("Mach das Buerorollo auf 30 Prozent", "buerorollo", "setzen", 30),
    ("Mach das Rollo im Wohnzimmer zu", "wohnzimmerrollo", "zu", None),
    ("Balkonrollo hoch", "balkonrollo", "auf", None),
    ("Felixrollo runter", "felixrollo", "zu", None),
    ("Mach das Kuechenrollo links zu", "kuechenrollo_links", "zu", None),

    # --- Einzahl gegen Raumgruppe --------------------------------------------
    ("Mach alle Rollos in der Kueche zu", "kuechenrollos", "zu", None),

    # --- Licht, Heizung -------------------------------------------------------
    ("Schalte das Flurlicht ein", "flurlicht", "ein", None),
    ("Mach das Kuechenlicht an", "kuechenlicht", "ein", None),
    # Hoeflichkeitsfloskel: hat frueher als Abbruch gegolten (siehe
    # _STOP_ANY in assistant.py) — hier gehoert sie ins Kommando.
    ("Schalt das Kuechenlicht bitte aus", "kuechenlicht", "aus", None),
    ("Tischlicht aus", "tischlicht", "aus", None),
    ("Stell die Felixheizung auf 22 Grad", "felixheizung", "setzen", 22),

    # --- kein Steuerkommando --------------------------------------------------
    ("Erzaehl mir einen Witz", None, None, None),
    ("Wie warm ist es draussen?", None, None, None),
    ("Wir haben heute Flammkuchen gegessen", None, None, None),

    # --- Wakewort vorangestellt (seit Pre-Roll 2026-07-28), inkl. Verhoerer ---
    ("Gaston, schalt das Tischlicht aus", "tischlicht", "aus", None),
    ("Gastau, Wohnzimmerlollo auf 70%", "wohnzimmerrollo", "setzen", 70),
    ("Gastronom, den Wohnzimmer Roller auf 70%", "wohnzimmerrollo", "setzen", 70),
    ("Gaston, alle Rollos auf", "alle_rollos", "auf", None),
    ("Gaston, erzaehl mir einen Witz", None, None, None),
]


def _ist_kommando(got: dict | None) -> bool:
    return bool(got) and bool(got.get("ist_kommando"))


def _kurz(got: dict | None) -> str:
    if not _ist_kommando(got):
        return "kein Kommando"
    wert = f"={got.get('wert')}" if got.get("wert") is not None else ""
    return f"{got.get('ziel')}/{got.get('aktion')}{wert}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--zeige-alle", action="store_true",
                    help="auch die bestandenen Faelle einzeln auflisten")
    args = ap.parse_args()

    profil = load_profile()
    if not profil.actuator.enabled or not profil.actuator.base_url:
        print("Aktuator ist in diesem Profil nicht konfiguriert.")
        return 2
    akt = Actuator(profil.actuator)
    if not akt.refresh():
        print("capabilities-refresh fehlgeschlagen — laeuft die Gegenstelle?")
        return 2

    # Erwartete Ziele gegen die echten capabilities pruefen: ein Tippfehler im
    # Test-Set wuerde sonst als Modellfehler durchgehen.
    fehlend = sorted({z for _, z, _, _ in TESTS if z and z not in (akt.digest or {})})
    if fehlend:
        print(f"⚠️  Test-Set nennt Ziele, die es nicht (mehr) gibt: {fehlend}")

    print(f"Prompt aus capabilities Version {akt.version}, "
          f"{len(akt.digest or {})} Ziele, {len(TESTS)} Testfaelle\n")

    fehler: list[str] = []
    for satz, ziel, aktion, wert in TESTS:
        got = akt.classify(satz)
        if ziel is None:
            gut = not _ist_kommando(got)
            soll = "kein Kommando"
        else:
            gut = (_ist_kommando(got)
                   and got.get("ziel") == ziel
                   and got.get("aktion") == aktion
                   and (wert is None or got.get("wert") == wert))
            soll = f"{ziel}/{aktion}" + (f"={wert}" if wert is not None else "")
        if not gut:
            fehler.append(satz)
        if not gut or args.zeige_alle:
            print(f"{'✓' if gut else '✗'} {satz:<44} soll {soll:<26} ist {_kurz(got)}")

    ok = len(TESTS) - len(fehler)
    print(f"\n{ok}/{len(TESTS)} korrekt")
    if fehler:
        print("Durchgefallen:")
        for s in fehler:
            print(f"  - {s}")
    return 0 if not fehler else 1


if __name__ == "__main__":
    sys.exit(main())
