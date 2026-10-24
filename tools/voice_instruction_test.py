#!/usr/bin/env python3
"""Befolgt Gaston die Voice-Anweisung? Alter Weg gegen neuen.

    ow-venv/bin/python -m tools.voice_instruction_test [--runden 3] [--nur A --nur D]
                       [--alt-rev 49c7f10] [--out ergebnis.jsonl]

A = Anweisung im Stand von --alt-rev, angehängt ans Transkript (der Weg bis
    2026-10-24). B = die aktuelle Anweisung als Feld "instructions" (landet im
    Systemprompt des Turns, nicht im Verlauf). C = die aktuelle Anweisung, aber
    angehängt wie A: trennt die Wirkung des Orts von der des Wortlauts.
    D = B plus die kurze Erinnerung je Turn im input (der Weg im Betrieb). Jeder Satz läuft je Variante
    --runden mal, jeweils in einer frischen Incognito-Session
    (agent:main:dashboard:incognito-…): nichts davon landet im Verlauf, im
    Gedächtnis-Index oder als Codex-Rollout — sonst verschmutzte die Messung
    genau das, was der neue Weg sauber halten soll. Geprüft 2026-10-24: eine
    solche Session lässt sich über /v1/responses anlegen und hinterlässt
    nichts in openclaw-agent.sqlite.

Folge: die Werkzeug-Aufrufe sind nicht im Rollout nachlesbar. Was zählt, geht
über unseren eigenen Sprech-Server und steht im Journal des Assistenten:
voice_speak_text ("[speak-server] queued:") und voice_enroll_speaker
("Speaker enrolled:"). Die Sätze laufen deshalb nacheinander, das Zeitfenster
eines Aufrufs ordnet die Journal-Zeilen zu. Die Werkzeuge sind echt: eine
Ansage ist im Raum zu hören, ein falsches Anlernen legt eine Referenz an
(danach mit voice_remove_speaker / speakers/<name>.wav entfernen).

Geprüft wird am Antworttext, ohne Modell:
- ziffern: Ziffern in der Antwort (Regel: Zahlen ausgeschrieben)
- markdown: *, #, `, Listenzeilen (Regel: Fließtext)
- no_followup: NO_FOLLOWUP am Ende — nur bei Sätzen mit klarer Erwartung
  bewertet (Dank → ja, offene Bitte um Vorschläge → nein), sonst nur gezählt
- ansage: mindestens eine voice_speak_text-Ansage während des Aufrufs
- anlernen: voice_enroll_speaker sofort aufgerufen (falsch: erst Trainingssatz)
- trainingssatz: Antwort nennt den Trainingssatz aus der Werkzeugbeschreibung
  (Zeichen, dass Gaston das Werkzeug gesucht und gelesen hat)

Ziffern sind fürs Ohr meist harmlos: clean_for_tts schreibt Zahlen, Daten
und Abkürzungen selbst aus. Was wirklich schlecht klingt, sind Listen
("1. Nudeln 2. Ofenkäse" -> "ersten Nudeln zweiten Ofenkäse"); darauf achten.

Messreihe (glm über OpenClaw 2026.10.5, Incognito-Sessions, je Satz 3 Läufe):
2026-10-24, alle 8 Sätze, A gegen B:
  fehlerfrei A 20/24, B 17/24. Listen A 0, B 3 (kochen 2x, lang 1x).
  Trainingssatz beim Anlernen A 3/3, B 0/3. Ziffern bei "lang" beide 3/3.
  In beiden Varianten sagte Gaston oft auch kurze Antworten selbst per
  voice_speak_text an (A 9, B 11 Läufe): in einer Dashboard-Session glaubt er
  nicht, dass seine Antwort vorgelesen wird. Testartefakt, verzerrt aber
  vermutlich das Format.
2026-10-24, C (neuer Text im input), ostern/kochen/lang/anlernen:
  Listen 0/6, Trainingssatz 0/3 -> Listen kommen vom Ort (Systemprompt),
  das fehlende Anlernen vom gestrichenen Absatz.
2026-10-24 nach Gateway-Neustart, A gegen D (B + Verweis auf
  voice_enroll_speaker in der Regel + Erinnerung je Turn), dieselben 4 Sätze:
  fehlerfrei A 7/12, D 8/12. Listen beide 0. Trainingssatz beide 3/3.
  NO_FOLLOWUP bei "kochen" beide je 1 falsch (Rauschen, in allen Läufen).
  Gaston lehnte das Anlernen nicht mehr ab (AGENTS.md: neuer Name auch bei
  'unbekannt' erlaubt, seit demselben Tag). "lang" dauerte in D 81–142 s,
  in A 56–64 s; bei drei Läufen nicht deutbar, im Betrieb beobachten.
  -> D ist der Betriebsweg.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice_assistant.config import (  # noqa: E402
    _DEFAULT_VOICE_ERINNERUNG,
    _DEFAULT_VOICE_INSTRUCTION,
    load_profile,
)
from voice_assistant.services import openclaw  # noqa: E402

URL = "http://127.0.0.1:18789/v1/responses"

# (id, Sprecher, Satz, Erwartungen). Harmlos gewählt: nichts schaltet, nichts
# wird gespeichert. "no_followup": True/False = bewertet, None = nur gezählt.
SAETZE = [
    ("ostern", "jochen", "Wann ist Ostern im Jahr 2027?",
     {"no_followup": None}),
    ("rechnen", "jochen", "Was ist 17 mal 23?",
     {"no_followup": None}),
    ("uhr", "jochen", "Wie spät ist es gerade und welches Datum haben wir?",
     {"no_followup": None}),
    ("danke", "jochen", "Danke, das war's schon.",
     {"no_followup": True}),
    ("kochen", "jochen", "Was soll ich heute kochen? Mach mir zwei Vorschläge.",
     {"no_followup": False}),
    ("erklaeren", "jochen", "Erklär mir kurz, wie eine Wärmepumpe funktioniert.",
     {"no_followup": None}),
    ("lang", "jochen",
     "Recherchiere bitte im Netz, welche drei Elektroautos 2025 in Deutschland "
     "am häufigsten neu zugelassen wurden, und vergleiche ihre Reichweite.",
     {"no_followup": None, "ansage": True}),
    ("anlernen", "unbekannt", "Lerne meine Stimme, ich bin Testperson.",
     {"no_followup": False, "anlernen": False}),
]

_MARKDOWN = re.compile(r"[*#`]|^\s*[-•]\s", re.M)
_ZIFFER = re.compile(r"\d")
_NO_FOLLOWUP = re.compile(r"\b(NO_FOLLOWUP|KEIN_FOLLOWUP)\W*$")


def alte_anweisung(rev: str) -> str:
    src = subprocess.run(["git", "show", f"{rev}:voice_assistant/config.py"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "_DEFAULT_VOICE_INSTRUCTION"):
            return ast.literal_eval(node.value)
    raise SystemExit(f"_DEFAULT_VOICE_INSTRUCTION nicht in {rev}")


def journal(seit: datetime, bis: datetime) -> list[str]:
    out = subprocess.run(
        ["journalctl", "--user", "-u", "openclaw-voice-assist.service", "--no-pager",
         "-o", "cat", "--since", seit.strftime("%Y-%m-%d %H:%M:%S"),
         "--until", bis.strftime("%Y-%m-%d %H:%M:%S")],
        capture_output=True, text=True).stdout
    return out.splitlines()


def senden(token: str, text: str, sprecher: str, anweisung: str,
           instructions_feld: bool, erinnerung: str = "") -> tuple[str | None, dict, float]:
    session = f"agent:main:dashboard:incognito-vitest-{uuid.uuid4().hex[:12]}"
    body = openclaw._payload(f"🎤 [Sprecher: {sprecher}] {text}", session,
                             anweisung, instructions_feld, erinnerung)
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "x-openclaw-session-key": session})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return None, {"fehler": f"HTTP {e.code}"}, time.time() - t0
    except Exception as e:  # Timeout, Verbindung
        return None, {"fehler": str(e)}, time.time() - t0
    teile = [c.get("text", "") for o in d.get("output", []) if o.get("type") == "message"
             for c in o.get("content", [])]
    return "\n".join(t for t in teile if t).strip() or None, d.get("usage") or {}, time.time() - t0


def bewerten(antwort: str | None, erwartung: dict, jlines: list[str]) -> dict:
    a = antwort or ""
    nf = bool(_NO_FOLLOWUP.search(a))
    rein = _NO_FOLLOWUP.sub("", a)
    r = {
        "leer": not a,
        "ziffern": bool(_ZIFFER.search(rein)),
        "markdown": bool(_MARKDOWN.search(rein)),
        "no_followup": nf,
        "ansage": sum("[speak-server] queued:" in z for z in jlines),
        "anlernen": any("Speaker enrolled:" in z for z in jlines),
        "trainingssatz": "grünen Felder" in a or "gruenen Felder" in a,
    }
    fehler = []
    if r["leer"]:
        fehler.append("leer")
    if r["ziffern"]:
        fehler.append("ziffern")
    if r["markdown"]:
        fehler.append("markdown")
    if erwartung.get("no_followup") is not None and nf != erwartung["no_followup"]:
        fehler.append("no_followup" if not nf else "no_followup_falsch")
    if erwartung.get("ansage") and not r["ansage"]:
        fehler.append("keine_ansage")
    if "anlernen" in erwartung and r["anlernen"] != erwartung["anlernen"]:
        fehler.append("sofort_angelernt")
    r["fehler"] = fehler
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runden", type=int, default=3)
    ap.add_argument("--nur", choices=["A", "B", "C", "D"], action="append")
    ap.add_argument("--satz", action="append", help="nur diese ids")
    ap.add_argument("--alt-rev", default="49c7f10")
    ap.add_argument("--out")
    args = ap.parse_args()

    token = load_profile().openclaw_token
    varianten = {
        "A": (alte_anweisung(args.alt_rev), False, ""),
        "B": (_DEFAULT_VOICE_INSTRUCTION, True, ""),
        "C": (_DEFAULT_VOICE_INSTRUCTION, False, ""),
        "D": (_DEFAULT_VOICE_INSTRUCTION, True, _DEFAULT_VOICE_ERINNERUNG),
    }
    varianten = {k: v for k, v in varianten.items() if k in (args.nur or ["A", "D"])}
    saetze = [s for s in SAETZE if not args.satz or s[0] in args.satz]
    out = open(args.out, "a") if args.out else None

    zeilen = []
    # Runde außen, Variante innen: A und B laufen zeitlich verschränkt, damit
    # eine langsame Phase des Modells nicht nur eine Variante trifft.
    for runde in range(args.runden):
        for sid, sprecher, text, erw in saetze:
            for var, (anweisung, feld, erinnerung) in varianten.items():
                seit = datetime.now()
                antwort, usage, dauer = senden(token, text, sprecher, anweisung, feld, erinnerung)
                time.sleep(2)  # Journal-Zeilen der Ansage nachlaufen lassen
                r = bewerten(antwort, erw, journal(seit, datetime.now()))
                zeile = {"runde": runde, "satz": sid, "variante": var,
                         "dauer_s": round(dauer, 1), "antwort": antwort,
                         "usage": usage, **r}
                zeilen.append(zeile)
                if out:
                    out.write(json.dumps(zeile, ensure_ascii=False) + "\n")
                    out.flush()
                print(f"{runde} {sid:10} {var} {dauer:6.1f}s "
                      f"{'OK' if not r['fehler'] else ','.join(r['fehler']):24} "
                      f"{(antwort or '')[:90]!r}", flush=True)

    print("\nVariante  Läufe  fehlerfrei  ziffern  markdown  NO_FOLLOWUP  Ansage  angelernt  leer  Ø s")
    for var in varianten:
        z = [x for x in zeilen if x["variante"] == var]
        if not z:
            continue
        print(f"{var:9} {len(z):5}  {sum(not x['fehler'] for x in z):10}  "
              f"{sum(x['ziffern'] for x in z):7}  {sum(x['markdown'] for x in z):8}  "
              f"{sum(x['no_followup'] for x in z):11}  {sum(x['ansage'] > 0 for x in z):6}  "
              f"{sum(x['anlernen'] for x in z):9}  {sum(x['leer'] for x in z):4}  "
              f"{sum(x['dauer_s'] for x in z) / len(z):4.1f}")
    print(f"\nFehler je Satz ({' | '.join(varianten)}):")
    for sid, *_ in saetze:
        teile = []
        for var in varianten:
            f = [",".join(x["fehler"]) or "ok" for x in zeilen
                 if x["satz"] == sid and x["variante"] == var]
            teile.append(" ".join(f))
        print(f"  {sid:10} " + " | ".join(teile))


if __name__ == "__main__":
    main()
