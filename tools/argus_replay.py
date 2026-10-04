#!/usr/bin/env python3
"""Argus-Replay: alte und neue Fassung des Überwachers über dieselben Turns.

Argus (voice_assistant/services/watcher.py) urteilt nach jedem Aktuator-Turn,
ob der ausgeführte Befehl zum gesprochenen Satz passt. Bis 2026-10-04 (V1)
kannte er das Haus nicht und erfand Räume und Bedeutungen; V2 bekommt das
Weltmodell der Gegenstelle, Kontext zum Turn und begründet vor dem Urteil.
Ob das besser ist, entscheidet dieses Werkzeug — nicht der Eindruck.

Gemessen wird gegen zwei Mengen:
  - die gelabelten Befunde (testsets/argus_befunde_labels.jsonl, privat):
    echt / fehlalarm / unklar / nicht_geschaltet. Zählt: werden echte Fehler
    weiter gemeldet, verschwinden die Fehlalarme?
  - alle übrigen Kommando-Turns aus actuator_turns.log. Für die gibt es kein
    Label; jede NEUE Meldung von V2 wird aufgelistet und muss ein Mensch
    beurteilen (neuer Fund oder neuer Fehlalarm).

Ausführen:
    PYTHONPATH=. ow-venv/bin/python tools/argus_replay.py --weltmodell-datei W.json
    PYTHONPATH=. ow-venv/bin/python tools/argus_replay.py --nur-befunde
LLM-Zugang aus dem watcher-Block des aktiven Profils. Das Weltmodell aus
--weltmodell-datei oder über haus_mcp_url/haus_mcp_token des Profils. Die
Messung gilt nur für die Weltmodell-Version, die oben ausgegeben wird.

Kosten: V2 schickt das ganze Weltmodell (~6–8k Token) je Turn.

Messreihe (glm-5-2 über ai.noris.de; gilt nur für ihre Weltmodell-Version):

  2026-10-04, Weltmodell 65087090, 15 gelabelte Befunde + 122 Turns:
    gelabelt   echt 9: V1 7, V2 9 · fehlalarm 3: V1 1, V2 0
               unklar 2: V1 2, V2 0–1 (kippt zwischen Läufen)
               nicht_geschaltet 1: V1 1, V2 0
    ACHTUNG: der V2-Prompt wurde an genau diesen 15 abgestimmt (eine
    Runde: die Hörfehler-Regel). Aussagekräftig ist deshalb vor allem:
    ungelabelt V1 meldet 5 (1 berechtigt, 3 Fehlalarme, 1 fraglich),
               V2 meldet 7 (5 berechtigt, 1 Fehlalarm "Das Tor schaltet das
               Tischlicht ein", 1 fraglich "Zwiebel-Rolo") — von Hand
               beurteilt, gastonllm-Claude mit Jochen.
    Temperatur 0 ist nicht deterministisch: dieselben Sätze kippen
    zwischen Läufen. Einzelne Zahlen nicht überbewerten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import WORKSPACE, load_profile  # noqa: E402
from voice_assistant.services import watcher  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABELS = os.path.join(REPO, "testsets", "argus_befunde_labels.jsonl")
TURNS = os.path.join(WORKSPACE, "actuator_turns.log")
BEFUNDE = os.path.join(WORKSPACE, "actuator_watch.jsonl")


def _jsonl(pfad: str) -> list[dict]:
    out = []
    if not os.path.exists(pfad):
        return out
    for zeile in open(pfad, encoding="utf-8"):
        try:
            out.append(json.loads(zeile))
        except json.JSONDecodeError:
            pass
    return out


def turns_sammeln(nur_befunde: bool) -> list[dict]:
    """Kommando-Turns: Befunde (mit Label) zuerst, dann der Rest des Logs."""
    labels = {d["request_id_prefix"]: d for d in _jsonl(LABELS)}
    gesehen, liste = set(), []

    def label_zu(rid: str):
        for pre, d in labels.items():
            if rid.startswith(pre):
                return d
        return None

    for b in _jsonl(BEFUNDE):
        rid = b.get("request_id") or ""
        lab = label_zu(rid)
        if lab is None or rid in gesehen:
            continue
        gesehen.add(rid)
        liste.append({**b, "_label": lab["label"], "_notiz": lab["notiz"]})
    if nur_befunde:
        return liste
    for t in _jsonl(TURNS):
        rid = t.get("request_id") or ""
        if (t.get("phase") != "intent" or rid in gesehen
                or not (t.get("intent") or {}).get("ist_kommando")):
            continue
        gesehen.add(rid)
        liste.append({**t, "_label": None})
    return liste


def urteil(system: str, user: str, wt) -> dict:
    for versuch in (1, 2):
        try:
            return watcher.llm_urteil(system, user, wt.llm_url, wt.llm_model,
                                      wt.llm_api_key, max(wt.llm_timeout, 60.0))
        except Exception as e:  # noqa: BLE001 — Messwerkzeug: Fehler zählen, weiter
            if versuch == 2:
                return {"fehler": str(e)[:200]}
            time.sleep(3)
    return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--weltmodell-datei", help="Weltmodell als JSON-Datei statt MCP")
    ap.add_argument("--nur-befunde", action="store_true", help="nur die gelabelten Befunde")
    ap.add_argument("--out", default="argus_replay.jsonl", help="Ergebnis je Turn (JSONL)")
    args = ap.parse_args()

    wt = load_profile().watcher
    if not (wt.llm_url and wt.llm_model):
        sys.exit("watcher.llm_url/llm_model fehlen im Profil")
    if args.weltmodell_datei:
        welt = open(args.weltmodell_datei, encoding="utf-8").read()
    else:
        from voice_assistant.services.haus_mcp import HausMcp, Weltmodell
        if not wt.haus_mcp_url:
            sys.exit("weder --weltmodell-datei noch watcher.haus_mcp_url")
        _, welt, fehler = Weltmodell(HausMcp(wt.haus_mcp_url, wt.haus_mcp_token,
                                             client_name="argus-replay")).aktuell()
        if fehler:
            sys.exit(f"Weltmodell nicht abrufbar: {fehler}")
    welt_version = json.loads(welt).get("version")
    system_v2 = watcher.system_prompt(welt)

    turns = turns_sammeln(args.nur_befunde)
    print(f"Modell {wt.llm_model}, Weltmodell {welt_version}, {len(turns)} Turns "
          f"({sum(1 for t in turns if t['_label'])} gelabelt)\n")

    ergebnisse = []
    with open(args.out, "w", encoding="utf-8") as out:
        for i, t in enumerate(turns, 1):
            u1 = urteil(watcher._PROMPT_V1, watcher.user_nachricht(t, mit_kontext=False), wt)
            u2 = urteil(system_v2, watcher.user_nachricht(t), wt)
            e = {"request_id": t.get("request_id"), "ts": t.get("ts"),
                 "transcript": t.get("transcript"), "intent": t.get("intent"),
                 "status": t.get("status"), "label": t["_label"],
                 "v1": u1, "v2": u2, "weltmodell": welt_version}
            out.write(json.dumps(e, ensure_ascii=False) + "\n")
            out.flush()
            ergebnisse.append(e)
            m1 = "FEHLER" if "fehler" in u1 else ("ok" if u1.get("ok") else "MELDET")
            m2 = "FEHLER" if "fehler" in u2 else ("ok" if u2.get("ok") else "MELDET")
            print(f"[{i:3}/{len(turns)}] {t['_label'] or '-':16} V1 {m1:6} V2 {m2:6} "
                  f"{(t.get('transcript') or '')[:60]}")

    auswerten(ergebnisse)


def _meldet(u: dict) -> bool | None:
    if "fehler" in u:
        return None
    return not u.get("ok")


def auswerten(ergebnisse: list[dict]) -> None:
    print("\n=== Gelabelte Befunde ===")
    for lab in ("echt", "fehlalarm", "unklar", "nicht_geschaltet"):
        grp = [e for e in ergebnisse if e["label"] == lab]
        if not grp:
            continue
        v1 = sum(1 for e in grp if _meldet(e["v1"]))
        v2 = sum(1 for e in grp if _meldet(e["v2"]))
        print(f"  {lab:17} n={len(grp):2}  V1 meldet {v1:2}  V2 meldet {v2:2}")
    print("  (echt: hoch ist gut; fehlalarm: niedrig ist gut)")

    rest = [e for e in ergebnisse if e["label"] is None]
    if rest:
        n1 = sum(1 for e in rest if _meldet(e["v1"]))
        n2 = sum(1 for e in rest if _meldet(e["v2"]))
        print(f"\n=== Ungelabelte Turns: {len(rest)} — V1 meldet {n1}, V2 meldet {n2} ===")
        print("Neue Meldungen von V2 (von Hand beurteilen):")
        for e in rest:
            if _meldet(e["v2"]) and not _meldet(e["v1"]):
                print(f"  {e['ts']}  \"{e['transcript']}\" -> "
                      f"{(e['intent'] or {}).get('ziel')}/{(e['intent'] or {}).get('aktion')}"
                      f"\n      {e['v2'].get('gedanke', '')[:220]}")
    fehler = sum(1 for e in ergebnisse for k in ("v1", "v2") if "fehler" in e[k])
    if fehler:
        print(f"\n{fehler} Aufrufe fehlgeschlagen (nicht als Urteil gezählt)")


if __name__ == "__main__":
    main()
