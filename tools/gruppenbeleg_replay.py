#!/usr/bin/env python3
"""Regel A (Gruppen-Beleg) gegen echte Aktuator-Turns zurückspielen — nur LESEN.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.gruppenbeleg_replay
    ow-venv/bin/python -m tools.gruppenbeleg_replay --verbose   # alle Gruppen-Turns

Was hier gemessen wird
----------------------
Regel A (siehe actuator.verdict() Docstring, Auftrag 2026-08-02): wählt das
Modell ein Gruppen-Ziel (mit `mitglieder`), muss der gesprochene Satz das
Gruppenwort auch enthalten — sonst wäre es "ausführbar, aber breiteres Ziel
als gesagt" (Vorfall: 'Gastau Tyrol auf 40%' -> alle_rollos, neun Rollos).

Dieses Werkzeug nimmt die ECHTEN, historischen Turns aus
~/.openclaw/workspace/actuator_turns.log und fragt für jeden Gruppen-Turn:
HÄTTE Regel A ihn durchgelassen oder künftig nachgefragt?

Interessant ist die Zahl der **bereits ausgeführten Gruppen-Turns, die künftig
nachfragen würden** — das ist der Anteil des Vorfall-Typs, der bisher unsichtbar
ablief. Jeder einzelne Satz wird gelistet.

Zählen reicht nicht, die Fälle einzeln lesen
---------------------------------------------
Auf capabilities eaf5c0b3 sah die Rohzahl bedenklich aus: 8 von 17 Gruppen-Turns
würden künftig nachfragen. Die Einzelfälle sagten das Gegenteil — sechs davon
SOLLTEN nachfragen ("Manolo auf 70 Prozent", "macht alle alles zu", "und selber
rolle zu", "Rollo auf 70%", …): verhörte oder unsinnige Sätze, die bisher still
ALLE Rollos geschaltet haben. Plus der auslösende Vorfall ("Gastau Tyrol auf
40%"). Die Quote allein hätte entweder beruhigt oder erschreckt, je nach
Vorwissen; erst die Sätze zeigen, dass Regel A genau das trifft, was es treffen
soll. Deshalb listet dieses Werkzeug jeden Satz, nicht nur die Summe.

Dünne Daten, keine Schwäche der Regel
-------------------------------------
Der achte Fall war KEIN Fehlalarm der Regel, sondern dünne Daten: die Gruppe
kuechenrollos führte nur ihr Kompositum als Namen, also war ihr Beleg
{küchenrollos} — kein Satz, der die Gruppe zerlegt ausspricht ("Rollos in der
Küche"), konnte ihn erbringen. Abhilfe auf der Node-RED-Seite (nicht hier):
kuechenrollos und esstischrollos bekamen zerlegte Aliase ("die Rollos in der
Küche", "alle Rollos in der Küche"), capabilities eaf5c0b3 -> 9b429c57. Seither
ist der Beleg {küche, küchenrollos, rollos} und der Fall geht durch. Damit fiel
die Zahl 8 -> 7, der Grammatik-Test 30/32 -> 31/32.

Merkregel für die Datenpflege: eine Gruppe braucht alle Formen, in denen
Menschen sie aussprechen, nicht nur ihr Kompositum. Die Beleuchtungs-Gruppen
pflegen das längst ("die Beleuchtung in der Küche", "alle Lichter in der
Küche"); die Rollo-Gruppen standen hinten an.

Nur LESEN: das Log wird nie verändert, nichts wird gepostet, nichts geschaltet.

Beleg-Mengen kommen aus den AKTUELLEN /capabilities (refresh()). Die historischen
Turns liefen gegen damals aktuelle capabilities, aber die Gruppen bestehen
weiter, also ist der Beleg für die Frage "wäre der Satz heute belegt?"
maßgeblich.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.gruppenbeleg_replay", *sys.argv[1:]])

from voice_assistant.config import ACTUATOR_LOG_PATH, load_profile  # noqa: E402
from voice_assistant.services.actuator import (  # noqa: E402
    Actuator,
    _gruppe_im_satz,
    _gruppen_beleg,
)


def _load_turns(path: str) -> list[dict]:
    """JSONL-Zeilen -> Dicts. Kaputte Zeilen still überspringen."""
    turns: list[dict] = []
    if not os.path.exists(path):
        return turns
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return turns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verbose", action="store_true",
                    help="auch die durchgelassenen Gruppen-Turns auflisten")
    ap.add_argument("--log", default=ACTUATOR_LOG_PATH,
                    help=f"Pfad zum actuator_turns.log (default: {ACTUATOR_LOG_PATH})")
    args = ap.parse_args()

    profil = load_profile()
    akt = Actuator(profil.actuator)
    if not akt.refresh():
        print("capabilities-refresh fehlgeschlagen — läuft die Gegenstelle?")
        return 2

    with akt._lock:
        digest = dict(akt.digest or {})
        gruppen_beleg = {k: set(v) for k, v in (akt.gruppen_beleg or {}).items()}
    # Defensive: falls gruppen_beleg leer sein sollte (älterer Stand), hier
    # aus dem Digest nachbauen — selbe Bauweise wie refresh().
    if not gruppen_beleg:
        gruppen_beleg = {
            zid: beleg for zid, z in digest.items()
            if z.get("mitglieder") and (beleg := _gruppen_beleg(z))
        }

    turns = _load_turns(args.log)
    if not turns:
        print(f"Keine Turns in {args.log} gefunden.")
        return 0

    # Nur Turns mit Intent + Transkript sind entscheidbar.
    entscheidbar = [
        t for t in turns
        if (t.get("intent") or {}).get("ziel") and t.get("transcript") is not None
    ]

    gruppen_turns = []          # alle, die auf eine Gruppe gingen
    kuenftig_nachfragen = []    # davon: ausgeführt UND Beleg fehlt
    for t in entscheidbar:
        ziel = t["intent"]["ziel"]
        if ziel not in gruppen_beleg:
            continue  # Einzelziel — Regel A greift nicht
        beleg = gruppen_beleg[ziel]
        belegt = _gruppe_im_satz(t["transcript"], beleg)
        rec = {
            "ts": t.get("ts"),
            "ziel": ziel,
            "transcript": t.get("transcript"),
            "status": t.get("status"),
            "belegt": belegt,
            "beleg": sorted(beleg),
        }
        gruppen_turns.append(rec)
        if not belegt and t.get("status") == "ausgefuehrt":
            kuenftig_nachfragen.append(rec)

    print(f"Turns gesamt: {len(turns)} | entscheidbar (Intent+Transkript): "
          f"{len(entscheidbar)} | davon Gruppen-Turns: {len(gruppen_turns)}")
    print("Gruppen-Beleg-Mengen: "
          + ", ".join(f"{k}={{ {', '.join(sorted(v))} }}"
                      for k, v in sorted(gruppen_beleg.items())))
    print()
    print(f"Bisher ausgeführte Gruppen-Turns, die künftig nachfragen würden: "
          f"{len(kuenftig_nachfragen)}\n")
    for rec in kuenftig_nachfragen:
        print(f"  • [{rec['ts']}] {rec['ziel']}")
        print(f"      „{rec['transcript']}“")
        print(f"      Beleg benötigt eins von: {{ {', '.join(rec['beleg'])} }} "
              f"— im Satz nicht enthalten.")
    if args.verbose and gruppen_turns:
        durchgelassen = [r for r in gruppen_turns if r["belegt"]]
        print(f"\nDurchgelassene Gruppen-Turns (Beleg erbracht): {len(durchgelassen)}")
        for rec in durchgelassen:
            print(f"  ✓ [{rec['ts']}] {rec['ziel']}  «{rec['transcript']}»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
