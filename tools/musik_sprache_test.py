#!/usr/bin/env python3
"""Englisch-Erkennung (voice_assistant/services/englisch.py) gegen die eigene Musiksammlung.

Aufruf:
    ow-venv/bin/python -m tools.musik_sprache_test ziehen --lms http://<lms>:9000
    ow-venv/bin/python -m tools.musik_sprache_test stichprobe [--n 200]
    ow-venv/bin/python -m tools.musik_sprache_test labeln [--port 8777]   # Seite im Browser
    ow-venv/bin/python -m tools.musik_sprache_test messen

Dateien (testsets/musik_sprache/, privat):
    sammlung.jsonl     alle Titel und Interpreten, je einmal
    stichprobe.jsonl   geschichtete Stichprobe (Schicht = Urteil der Regel)
    labels.jsonl       blind gehoert/gelesen: wie spricht man es aus?

Label je Eintrag: ``de`` · ``en`` · ``gemischt`` · ``anders`` (andere Sprache,
weiss nicht). Die Label-Seite zeigt das Urteil der Regel nicht.

Bewertung
---------
    richtig      de ohne englische Stelle, en ganz englisch
    teilweise    en, aber nur ein Teil der Woerter englisch
    uebersehen   en ohne englische Stelle — klingt wie heute
    FALSCH       de mit englischer Stelle — klingt schlechter als heute.
                 Das ist die Zahl, die klein bleiben muss.
``gemischt`` und ``anders`` werden gezaehlt, aber nicht bewertet. Die
Hochrechnung auf die Sammlung gewichtet jede Schicht mit ihrem Anteil.

Messreihe
---------
    2026-10-15  39 Labels (Jochen; „das Muster ist immer gleich, wir brauchen
                keine 100 %“), 46 344 Titel + 4 462 Interpreten
                E-Folgen + b-Nachbarn, Funktionswoerter am Rand deutsch
                    richtig 15, teilweise 13, uebersehen 4, FALSCH 3
                + unbekannte Woerter erben Englisch vom Nachbarn
                    richtig 26, teilweise 2, uebersehen 4, FALSCH 3
                    hochgerechnet: richtig 77 %, uebersehen 10 %, FALSCH 2 %
                Die 3 FALSCH schaden nicht: „Skit“, „Intro“, „Christopher
                Amott“ in deutsch gelabelten Eintraegen sind englische Woerter
                und klingen englisch richtig. Uebersehen: einzelne
                Fantasiewoerter ohne englischen Nachbarn („Demonfist“).
"""

from __future__ import annotations

import argparse
import collections
import http.server
import json
import os
import random
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services.englisch import WORT, englische_stellen  # noqa: E402

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "testsets", "musik_sprache")
SAMMLUNG = os.path.join(_DIR, "sammlung.jsonl")
STICHPROBE = os.path.join(_DIR, "stichprobe.jsonl")
LABELS = os.path.join(_DIR, "labels.jsonl")
LABEL_WERTE = ("de", "en", "gemischt", "anders")


def urteil(text: str) -> str:
    """keins / teil / ganz: wie viele Woerter die Regel englisch spricht."""
    n = len(WORT.findall(text))
    ne = sum(len(WORT.findall(text[a:e])) for a, e in englische_stellen(text))
    return "keins" if ne == 0 else "ganz" if ne == n else "teil"


def _lies(pfad: str) -> list[dict]:
    if not os.path.exists(pfad):
        return []
    with open(pfad, encoding="utf-8") as f:
        return [json.loads(z) for z in f if z.strip()]


def _schreib(pfad: str, zeilen: list[dict]) -> None:
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    with open(pfad, "w", encoding="utf-8") as f:
        for z in zeilen:
            f.write(json.dumps(z, ensure_ascii=False) + "\n")


def ziehen(lms: str) -> None:
    body = json.dumps({"id": 1, "method": "slim.request",
                       "params": ["", ["titles", "0", "200000", "tags:a"]]}).encode()
    with urllib.request.urlopen(urllib.request.Request(lms.rstrip("/") + "/jsonrpc.js", body),
                                timeout=180) as r:
        titel = json.load(r)["result"].get("titles_loop", [])
    gesehen: dict[tuple[str, str], None] = {}
    for t in titel:
        for art, feld in (("titel", "title"), ("interpret", "artist")):
            s = (t.get(feld) or "").strip()
            if s and WORT.search(s):
                gesehen.setdefault((art, s), None)
    _schreib(SAMMLUNG, [{"art": a, "text": s} for a, s in gesehen])
    zaehl = collections.Counter(a for a, _ in gesehen)
    print(f"{len(titel)} Lieder → {zaehl['titel']} Titel, {zaehl['interpret']} Interpreten → {SAMMLUNG}")


def stichprobe(n: int, seed: int) -> None:
    alle = _lies(SAMMLUNG)
    schichten: dict[str, list[dict]] = collections.defaultdict(list)
    for z in alle:
        schichten[urteil(z["text"])].append(z)
    rnd = random.Random(seed)
    je = n // len(schichten)
    probe = []
    for name, zeilen in sorted(schichten.items()):
        for z in rnd.sample(zeilen, min(je, len(zeilen))):
            probe.append({**z, "schicht": name, "schicht_anteil": len(zeilen) / len(alle)})
    rnd.shuffle(probe)
    _schreib(STICHPROBE, probe)
    print(f"{len(probe)} Eintraege, je Schicht {je}: "
          + ", ".join(f"{k} {len(v)} ({len(v) / len(alle):.0%})" for k, v in sorted(schichten.items())))


_SEITE = """<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Musik-Sprache labeln</title>
<style>
 body{font:18px system-ui;margin:0;padding:16px;background:#111;color:#eee;max-width:640px;margin:auto}
 #t{font-size:30px;margin:32px 0 8px;min-height:2.5em}
 #a{color:#999;margin-bottom:24px} button{font-size:20px;padding:18px 0;width:48%;margin:1%;
 border:0;border-radius:10px;background:#333;color:#eee} button:active{background:#555}
 #s{color:#999;margin-top:16px}
</style>
<p>Wie würdest du es aussprechen? Tasten: <b>d</b> deutsch · <b>e</b> englisch ·
<b>g</b> gemischt · <b>a</b> anders/weiß nicht · <b>z</b> zurück</p>
<div id="t"></div><div id="a"></div>
<button data-l="de">deutsch (d)</button><button data-l="en">englisch (e)</button>
<button data-l="gemischt">gemischt (g)</button><button data-l="anders">anders (a)</button>
<button data-l="zurueck" style="width:98%">zurück (z)</button>
<div id="s"></div>
<script>
let liste=[],i=0;
const zeig=()=>{const z=liste[i];
 document.getElementById('t').textContent=z?z.text:'Fertig, danke!';
 document.getElementById('a').textContent=z?z.art:'';
 document.getElementById('s').textContent=i+' von '+liste.length;};
const setz=async l=>{if(l==='zurueck'){if(i>0)i--;zeig();return}
 const z=liste[i];if(!z)return;
 await fetch('label',{method:'POST',body:JSON.stringify({art:z.art,text:z.text,label:l})});i++;zeig();};
document.querySelectorAll('button').forEach(b=>b.onclick=()=>setz(b.dataset.l));
const K={d:'de',e:'en',g:'gemischt',a:'anders',z:'zurueck'};
document.onkeydown=e=>{if(K[e.key])setz(K[e.key])};
fetch('offen').then(r=>r.json()).then(d=>{liste=d.liste;i=d.weiter;zeig()});
</script></html>"""


def _labels() -> dict[tuple[str, str], str]:
    """Letztes Label je Eintrag (zurueck + neu setzen ueberschreibt)."""
    return {(z["art"], z["text"]): z["label"] for z in _lies(LABELS)}


class _Seite(http.server.BaseHTTPRequestHandler):
    def _antwort(self, code: int, body: bytes, typ: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/offen"):
            probe = _lies(STICHPROBE)
            fertig = _labels()
            weiter = next((i for i, z in enumerate(probe)
                           if (z["art"], z["text"]) not in fertig), len(probe))
            liste = [{"art": z["art"], "text": z["text"]} for z in probe]
            self._antwort(200, json.dumps({"liste": liste, "weiter": weiter}).encode(),
                          "application/json")
        else:
            self._antwort(200, _SEITE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        z = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if z.get("label") not in LABEL_WERTE:
            self._antwort(400, b"label?", "text/plain")
            return
        with open(LABELS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"art": z["art"], "text": z["text"], "label": z["label"]},
                               ensure_ascii=False) + "\n")
        self._antwort(200, b"ok", "text/plain")

    def log_message(self, *a):
        pass


def labeln(port: int) -> None:
    print(f"Label-Seite auf Port {port} (Strg-C beendet)")
    http.server.ThreadingHTTPServer(("0.0.0.0", port), _Seite).serve_forever()


def messen() -> None:
    labels = _labels()
    probe = [z for z in _lies(STICHPROBE) if (z["art"], z["text"]) in labels]
    if not probe:
        print("noch keine Labels")
        return
    erg = collections.Counter()
    je_schicht: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    anteil: dict[str, float] = {}
    falsch, ueber = [], []
    for z in probe:
        label, u = labels[(z["art"], z["text"])], urteil(z["text"])
        if label == "de":
            b = "richtig" if u == "keins" else "FALSCH"
        elif label == "en":
            b = {"ganz": "richtig", "teil": "teilweise", "keins": "uebersehen"}[u]
        else:
            b = label
        erg[b] += 1
        je_schicht[z["schicht"]][b] += 1
        anteil[z["schicht"]] = z["schicht_anteil"]
        stellen = [z["text"][a:e] for a, e in englische_stellen(z["text"])]
        if b == "FALSCH":
            falsch.append(f"  {z['art']:9} {z['text']}  → englisch: {stellen}")
        elif b == "uebersehen":
            ueber.append(f"  {z['art']:9} {z['text']}")
    print(f"{len(probe)} gelabelt: " + ", ".join(f"{k} {v}" for k, v in erg.most_common()))
    print("\nje Schicht (Regel-Urteil):")
    hoch = collections.Counter()
    for s, c in sorted(je_schicht.items()):
        print(f"  {s:6} ({anteil[s]:.0%} der Sammlung): "
              + ", ".join(f"{k} {v}" for k, v in c.most_common()))
        n = sum(c.values())
        for k, v in c.items():
            hoch[k] += anteil[s] * v / n
    print("\nhochgerechnet auf die Sammlung: "
          + ", ".join(f"{k} {v:.1%}" for k, v in hoch.most_common()))
    if falsch:
        print("\nFALSCH (deutsch, aber englisch gesprochen):", *falsch, sep="\n")
    if ueber:
        print("\nuebersehen:", *ueber, sep="\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ziehen")
    p.add_argument("--lms", required=True, help="Basis-URL des Musikservers (JSON-RPC)")
    p = sub.add_parser("stichprobe")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--seed", type=int, default=20261015)
    p = sub.add_parser("labeln")
    p.add_argument("--port", type=int, default=8777)
    sub.add_parser("messen")
    a = ap.parse_args()
    if a.cmd == "ziehen":
        ziehen(a.lms)
    elif a.cmd == "stichprobe":
        stichprobe(a.n, a.seed)
    elif a.cmd == "labeln":
        labeln(a.port)
    else:
        messen()


if __name__ == "__main__":
    main()
