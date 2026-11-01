"""Live-Bilanz eines Wake-Modells: Trigger und Near-Misses je Label in einem Zeitraum.

    ow-venv/bin/python -m tools.wake_live_bilanz 2026-10-24 2026-10-31T19 [--bundle NAME]

Labels wie überall: Ohr (wake_review.jsonl) vor wake_triage.jsonl (Selbst/STT).
Vorher `tools.wake_triage --seit N --auch-trigger` laufen lassen, sonst stehen
frische Trigger als "ohne". Gebaut für den Vergleich v5 gegen R7 voll l2
(WAKEWORD_PROCESS.md, "Live-Versuch R7 voll l2").
"""
import argparse, json, os, sys
from collections import Counter
# auch als Skript aufrufbar (tools/wake_live_bilanz.py), nicht nur mit -m
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import wake_wort  # noqa: E402
W = os.path.expanduser("~/.openclaw/workspace")
ap = argparse.ArgumentParser(description="Trigger und Near-Misses je Label in einem Zeitraum")
ap.add_argument("ab", help="Beginn, ISO (2026-10-24 oder 2026-10-31T18:09)")
ap.add_argument("bis", nargs="?", default="9999", help="Ende, ISO (ausschließlich)")
ap.add_argument("--bundle", default=None, help="Default: erstes eigenes Bundle des Profils")
a = ap.parse_args()
ab, bis = a.ab, a.bis
bundle = a.bundle or wake_wort.standard_bundle()
lab = {}
for z in open(f"{W}/wake_triage.jsonl"):
    try: d = json.loads(z)
    except: continue
    if d.get("audio"): lab[d["audio"]] = (d.get("klasse"), d.get("quelle"))
for z in open(f"{W}/wake_review.jsonl"):
    try: d = json.loads(z)
    except: continue
    if d.get("audio"): lab[d["audio"]] = (d.get("klasse"), "ohr")
c = Counter(); tage = set()
for z in open(f"{W}/wake_events.log"):
    try: d = json.loads(z)
    except: continue
    r, ts = d.get("result"), d.get("ts", "")
    if r not in ("trigger", "nearmiss") or not (ab <= ts < bis) or d.get("bundle", bundle) != bundle: continue
    tage.add(ts[:10])
    k, q = lab.get(d.get("audio"), (None, None))
    c[(r, k or "ohne", q == "ohr")] += 1
n = len(tage)
print(f"{ab} .. {bis}: {n} Tage mit Ereignissen")
for r in ("trigger", "nearmiss"):
    for k in ("echter_ruf", "rauschen", "unklar", "ohne"):
        s = c[(r, k, True)] + c[(r, k, False)]
        if s: print(f"  {r:9s} {k:11s} {s:4d}  (Ohr {c[(r,k,True)]:3d})  je Tag {s/n:.2f}")
