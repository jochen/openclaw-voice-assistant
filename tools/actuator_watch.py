#!/usr/bin/env python3
"""Überwacher Stufe 1 — Aktuator-Turns auf Diskrepanzen prüfen (nur LESEN + MELDEN).

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.actuator_watch
    ow-venv/bin/python -m tools.actuator_watch --seit 3     # nur letzte 3 Tage
    ow-venv/bin/python -m tools.actuator_watch --alles       # auch bereits gesehene

WARUM DAS HIER STEHT (und was es NICHT tut)
-------------------------------------------
Der Voice-Aktuator überspringt den Brain und schaltet direkt — schnell, aber
der Brain sieht diese Turns nicht. Dieser Überwacher ist die Kontrollinstanz,
die der Aktuator-Entwurf von Anfang an vorgesehen hat (Spiegel-Kanal
actuator_turns.log, siehe assistant.py:_log_actuator_turn).

STUFE 1: nur LESEN und MELDEN. Er greift NICHT ein, er schaltet nichts, er
korrigiert nichts. Er liest das Log und zeigt Diskrepanzen — der Mensch
entscheidet, ob etwas davon ein Problem ist. Bewusst konservativ, weil der
teuerste Fehler des Überwachers die EINGEBILDETE Korrektur wäre (physisch im
Haus, womöglich nachts). Erst wenn diese Erkennung sich über Wochen bewährt,
kann Stufe 2 (Gruppen-Vervollständigung) oder Stufe 3 (proaktive Rückfrage
bei objektivem Signal) folgen.

Was er erkennt — alles aus dem Log allein, ohne MQTT-State oder Digest:

  AKTIONS_MISMATCH  Transkript nennt eine andere Aktion als der Intent.
                    Klassischer Fall: „Schalt die Küchenbeleuchtung aus"
                    → Intent sagt aktion „ein". Das Wort „aus" steht im Satz,
                    der Aktuator hat „ein" klassifiziert. Das kann ein STT-
                    Verhör sein („Schaut" statt „Schalt") oder ein echtes
                    Missverständnis — der Überwacher urteilt nicht, er zeigt
                    es nur.
  STATUS_PROBLEM    Node-RED hat nicht „ausgefuehrt" geantwortet: keine_antwort,
                    abgelehnt, unbekanntes_ziel, zurueckgestellt. Jeder dieser
                    Status bedeutet, dass der Schaltversuch nicht glatt lief.
  EXEC_DIFFERS      Node-RED hat etwas anderes geschaltet als der Intent
                    verlangte (abweichendes ziel oder aktion in ausgefuehrt).
                    Das passiert z.B. bei Kosten-Rückfragen, Whitelist-Filter
                    oder wenn Node-RED einen Intent korrigiert hat.

Was er NICHT erkennt (bewusst — das wäre Stufe 2/3):
  - Ob ein Gerät physikalisch reagiert hat (braucht MQTT-State)
  - Ob eine Gruppe nur teilweise geschaltet wurde (braucht Digest/mitglieder)
  - Ob der User etwas anderes MEINTE als was er SAID (braucht Weltmodell)

ERGEBNIS geht als JSONL nach <workspace>/actuator_watch.jsonl, damit bereits
gesehene Turns beim nächsten Aufruf nicht erneut gemeldet werden (wie
wake_triage.jsonl). Mit --alles sieht man auch die alten wieder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.actuator_watch", *sys.argv[1:]])

from voice_assistant.config import (  # noqa: E402
    ACTUATOR_LOG_PATH,
    WORKSPACE,
)

WATCH_PATH = os.path.join(WORKSPACE, "actuator_watch.jsonl")

# Aktionswörter, die im Transkript gesucht werden. „an" ist heikel (erscheint
# in „anmachen", „daran", „wanne" …) — deshalb nur als isoliertes Wort am
# Satzanfang/-ende oder nach „Licht/Rollo/Gerät". „aus"同理, aber in einem
# Schaltkommando ist „aus" fast immer die Aktion. Konservative Heuristik:
# false alarm im Bericht ist harmlos (Mensch liest), missed detection wäre
# schlimmer. Wir suchen die Wörter als Ganzes (Word-Boundary), nicht als
# Teilstring.
#
# Mapping: gesprochenes Wort → Aktuator-aktion. „an" = „ein" (anmachen),
# „aus" = „aus" (ausmachen), „auf" = „auf" (Rollo auf), „zu" = „zu" (Rollo zu).
_AKTION_WORTE = {
    "ein": "ein", "an": "ein", "einschalten": "ein", "anschalten": "ein",
    "aus": "aus", "ausschalten": "aus", "ausmachen": "aus",
    "auf": "auf", "aufmachen": "auf", "oeffnen": "auf", "öffnen": "auf",
    "zu": "zu", "zumachen": "zu", "schliessen": "zu", "schließen": "zu",
}

# Nur diese Mismatches melden — ein Treffer im Transkript, der zur Intent-
# aktion PASST, ist kein Befund. Umgekehrt: wenn das Transkript „ein" UND
# „aus" enthält („Schalt das Licht ein, nicht aus"), ist das mehrdeutig —
# wir melden es NICHT (conservative, Stufe 1).


def _lade_log(seit_tage: float | None) -> list[dict]:
    """actuator_turns.log zeilenweise laden, optional altersgefiltert."""
    if not os.path.exists(ACTUATOR_LOG_PATH):
        return []
    cutoff = time.time() - seit_tage * 86400 if seit_tage else 0
    rows = []
    with open(ACTUATOR_LOG_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if cutoff and row.get("ts"):
                try:
                    ts = time.mktime(time.strptime(row["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            rows.append(row)
    return rows


def _lade_gesehen() -> set[str]:
    """bereits gemeldete request_ids aus actuator_watch.jsonl."""
    gesehen = set()
    if not os.path.exists(WATCH_PATH):
        return gesehen
    with open(WATCH_PATH) as f:
        for line in f:
            try:
                row = json.loads(line)
                if row.get("request_id"):
                    gesehen.add(row["request_id"])
            except ValueError:
                continue
    return gesehen


def _finde_aktionen(transcript: str) -> set[str]:
    """Sucht Aktionswörter im Transkript, gibt gefundene Aktuator-aktionen."""
    gefunden = set()
    # Wort-Tokenizer: Deutsch, lowercase. Trennt an Leer-/Satzzeichen.
    worte = re.findall(r"[a-zA-Zäöüß]+", transcript.lower())
    for w in worte:
        if w in _AKTION_WORTE:
            gefunden.add(_AKTION_WORTE[w])
    return gefunden


def _pruefe_aktions_mismatch(turn: dict) -> dict | None:
    """Transkript-Aktionen gegen Intent-Aktion. Mismatch nur wenn das
    Transkript eine eindeutige andre Aktion nennt als der Intent."""
    transcript = turn.get("transcript", "")
    intent = turn.get("intent") or {}
    if not transcript or not intent.get("ist_kommando"):
        return None
    intent_aktion = intent.get("aktion")
    if not intent_aktion:
        return None
    gefunden = _finde_aktionen(transcript)
    # Conservative: nur melden wenn das Transkript eine ANDERE Aktion nennt
    # UND die Intent-Aktion NICHT ebenfalls im Transkript vorkommt. „ein ...
    # nicht aus" mit intent „ein" ist Selbstkorrektur, kein Mismatch.
    if intent_aktion in gefunden:
        return None
    # Sonderfall: Intent ist „setzen" mit einem wert (z.B. „alle rollos auf
    # 10%"). Das Wort „auf" im Transkript ist hier die natürliche Sprechweise
    # („auf X%"), nicht die Aktion „auf" (ganz öffnen). Gemessen 2026-07-26:
    # „alle rollos auf 10%" löste fälschlich AKTIONS_MISMATCH aus, weil die
    # Heuristik „auf" als abweichende Aktion sah. Bei setzen+wert ist „auf"
    # niemals ein Mismatch — es ist die Präposition, nicht die Aktion.
    if intent_aktion == "setzen" and intent.get("wert") is not None:
        return None
    andere = gefunden - {intent_aktion}
    if not andere:
        return None
    # Mehrere andre Aktionen („aus" und „zu" gleichzeitig) sind stark
    # mehrdeutig — überspringen.
    if len(andere) > 1:
        return None
    return {
        "art": "AKTIONS_MISMATCH",
        "detail": (
            f"Transkript nennt '{next(iter(andere))}', Intent sagt "
            f"'{intent_aktion}'. Mögliche STT-Verhörmöglichkeit."
        ),
        "transcript": transcript,
        "intent_aktion": intent_aktion,
        "transkript_aktionen": sorted(gefunden),
    }


def _pruefe_status_problem(turn: dict) -> dict | None:
    """Status nicht „ausgefuehrt"."""
    status = turn.get("status", "")
    if status == "ausgefuehrt":
        return None
    return {
        "art": "STATUS_PROBLEM",
        "detail": f"Node-RED antwortete '{status}' statt 'ausgefuehrt'.",
        "status": status,
        "grund": turn.get("grund"),
    }


def _pruefe_exec_differs(turn: dict) -> dict | None:
    """ausgefuehrt weicht von Intent ab (ziel oder aktion)."""
    intent = turn.get("intent") or {}
    ausgefuehrt = turn.get("ausgefuehrt") or {}
    if not intent.get("ist_kommando") or not ausgefuehrt:
        return None
    diffs = []
    if intent.get("ziel") and ausgefuehrt.get("ziel") and \
            intent["ziel"] != ausgefuehrt["ziel"]:
        diffs.append(f"ziel: Intent '{intent['ziel']}' → ausgeführt '{ausgefuehrt['ziel']}'")
    if intent.get("aktion") and ausgefuehrt.get("aktion") and \
            intent["aktion"] != ausgefuehrt["aktion"]:
        diffs.append(
            f"aktion: Intent '{intent['aktion']}' → ausgeführt '{ausgefuehrt['aktion']}'"
        )
    if not diffs:
        return None
    return {
        "art": "EXEC_DIFFERS",
        "detail": "Node-RED hat etwas anderes ausgeführt als der Intent verlangte: " + "; ".join(diffs),
        "diffs": diffs,
    }


def main() -> int:
    desc = (__doc__ or "").split("\n")[0]
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--seit", type=float, default=None,
                    help="nur Turns der letzten N Tage")
    ap.add_argument("--alles", action="store_true",
                    help="auch bereits gesehene Turns anzeigen")
    args = ap.parse_args()

    turns = _lade_log(args.seit)
    if not turns:
        print(f"Keine Turns im Log ({ACTUATOR_LOG_PATH}).")
        return 0

    gesehen = set() if args.alles else _lade_gesehen()
    befunde = []
    neue_jsonl = []

    for turn in turns:
        rid = turn.get("request_id", "")
        if rid in gesehen:
            continue
        for pruefer in (_pruefe_aktions_mismatch, _pruefe_status_problem, _pruefe_exec_differs):
            b = pruefer(turn)
            if b is None:
                continue
            eintrag = {
                "ts": turn.get("ts"),
                "request_id": rid,
                "transcript": turn.get("transcript"),
                "speaker": turn.get("speaker"),
                "wakeword": turn.get("wakeword"),
                "intent": turn.get("intent"),
                "status": turn.get("status"),
                "ausgefuehrt": turn.get("ausgefuehrt"),
                "gesprochen": turn.get("gesprochen"),
                **b,
            }
            befunde.append(eintrag)
            neue_jsonl.append(eintrag)

    # Persistiere neue Befunde
    if neue_jsonl:
        with open(WATCH_PATH, "a") as f:
            for b in neue_jsonl:
                f.write(json.dumps(b, ensure_ascii=False) + "\n")

    # --- Ausgabe ---
    total = len(turns)
    neu = sum(1 for t in turns if t.get("request_id") not in gesehen)
    print(f"\n{'='*72}")
    print(f"  Überwacher Stufe 1 — Aktuator-Turns geprüft")
    print(f"{'='*72}")
    print(f"  Log: {ACTUATOR_LOG_PATH}")
    print(f"  Turns insgesamt: {total}   davon neu: {neu}")
    if befunde:
        print(f"  ⚠  Befunde: {len(befunde)}")
    else:
        print(f"  ✅ Keine Diskrepanzen in den neuen Turns.")
    print()

    if not befunde:
        print(f"Befunde werden gespeichert in: {WATCH_PATH}")
        return 0

    # Nach Art gruppieren
    by_art = {}
    for b in befunde:
        by_art.setdefault(b["art"], []).append(b)

    for art in ("AKTIONS_MISMATCH", "STATUS_PROBLEM", "EXEC_DIFFERS"):
        gruppe = by_art.get(art, [])
        if not gruppe:
            continue
        print(f"\n--- {art} ({len(gruppe)}) ---")
        for b in gruppe:
            ts = b.get("ts") or "?"
            spk = b.get("speaker") or "?"
            print(f"\n  [{ts}] Sprecher: {spk}")
            print(f"  Transkript: {b.get('transcript', '?')}")
            print(f"  Befund:     {b['detail']}")
            intent = b.get("intent") or {}
            if intent:
                print(f"  Intent:     ziel='{intent.get('ziel')}' aktion='{intent.get('aktion')}'")
            ausg = b.get("ausgefuehrt")
            if ausg:
                print(f"  Ausgeführt: ziel='{ausg.get('ziel')}' aktion='{ausg.get('aktion')}'")
            if b.get("gesprochen"):
                print(f"  Gesprochen: {b['gesprochen']}")
            if b.get("grund"):
                print(f"  Grund:      {b['grund']}")

    c = Counter(b["art"] for b in befunde)
    print(f"\n{'='*72}")
    print(f"  Bilanz: ", end="")
    print("  ".join(f"{k}: {v}" for k, v in c.most_common()))
    print(f"{'='*72}")
    print(f"\nBefunde gespeichert in: {WATCH_PATH}")
    print("Dies ist Stufe 1 (nur LESEN + MELDEN). Kein Eingriff erfolgt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
