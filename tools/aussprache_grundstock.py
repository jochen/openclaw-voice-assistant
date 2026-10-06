#!/usr/bin/env python3
"""Aussprache-Grundstock aus dem deutschen Wiktionary: die Woerter, bei denen
Piper (espeak-ng, Regeln "de") falsch liegt.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.aussprache_grundstock ~/aussprache-daten/dewiktionary-raw.jsonl.gz
    ow-venv/bin/python -m tools.aussprache_grundstock DUMP --schwelle 0.25 --probe 20000

Quelle: kaikki.org/dewiktionary (Wiktextract-Abzug des deutschen Wiktionary,
raw-wiktextract-data.jsonl.gz). Die Ausgabe steht unter CC BY-SA 4.0 wie
Wiktionary selbst — siehe data/aussprache/LIZENZ.md.

Warum Wiktionary
----------------
Es fuehrt Fremdwoerter mit der im Deutschen UEBLICHEN Aussprache, nicht mit
der der Herkunftssprache. Genau die hat Jochen im Blindtest am 2026-10-06 in
vier von sechs Faellen gewaehlt ("Sauce" [ˈzoːsə], "Currywurst", "Headset",
"Bruschetta" — Wiktionary trifft alle vier), und sie liegt in dem Lautvorrat,
auf dem die deutschen Piper-Stimmen trainiert sind.

Was in die Liste kommt
----------------------
Jedes Wort mit Wiktionary-IPA wird zusaetzlich durch espeak-de geschickt.
Beide Lautketten werden angeglichen (``_vergleichsform``: Schreibweisen, die
denselben Laut meinen, z. B. ʁ/ɾ/r, n̩/ən, Vokallaenge, Betonung) und per
Levenshtein verglichen. Nur ueber der Schwelle wird ein Eintrag geschrieben —
darunter spricht espeak schon richtig, und ein Eintrag waere nur Ballast.

Gemessen auf 20 000 zufaelligen Eintraegen (2026-10-06): ohne Angleichung lagen
42 % der Woerter ohne Lehnwort-Herkunft ueber 0,15 — reine Schreibunterschiede
("Schluesselbund" ʃlʏsl̩ gegen ʃlʏsəl). Mit Angleichung sind es bei 0,25 noch
2,1 %, und die Stichprobe dort sind ueberwiegend echte espeak-Fehler
(Cheeseburger, Servern, downgecycelte, "Sucht" als zuːxt).

Weggelassen, denn ein falscher Eintrag ist schlimmer als keiner — dort
bleibt espeak:

- Homographen: verschiedene Eintraege gleicher Schreibung mit verschiedener
  Aussprache ("ratendes" auch als Form des englischen "raten" = bewerten),
  AUCH ueber Gross-/Kleinschreibung: "Seine" (Fluss, zˈɛːn) gegen "seine",
  "Die" (englisch, daɪ) gegen "die". Am Satzanfang stuende sonst jedes
  "Seine Frau …" als Fluss da.
- Funktionswoerter (Artikel, Pronomen, Praepositionen, …): Wiktionary fuehrt
  die betonte Zitierform ("der" deːɐ̯), im Satz klingen sie anders, und espeak
  spricht sie richtig.
- Woerter unter 4 Buchstaben ("er", "on", "Us"): zu oft gleich geschrieben.

Gefunden 2026-10-06 an den Woertern, die Gaston in 90 Tagen tatsaechlich
gesprochen hat: ohne diese Sperren waren 5 der 21 Treffer falsch ("der",
"Die", "Seine", "er", "Us") — darunter die zwei haeufigsten Woerter ueberhaupt.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sys
import unicodedata
from collections import defaultdict

_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.aussprache_grundstock", *sys.argv[1:]])

from piper.phonemize_espeak import EspeakPhonemizer  # noqa: E402

from voice_assistant.services.aussprache import piper_phoneme  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIEL = os.path.join(REPO, "data", "aussprache", "de_wiktionary.tsv")
BEKANNT = os.path.expanduser("~/.openclaw/workspace/voice/aussprache/bekannt_de.txt.gz")  # = aussprache.pfade("de")["bekannt"]

_WEG = re.compile(r"[\[\]ˈˌ͡‿ʔ.\s\-ˑ̥̝̞̊̆̑̚ː]")
_GLEICH = (("n̩", "ən"), ("l̩", "əl"), ("m̩", "əm"), ("ŋ̍", "əŋ"), ("ɐ̯", "r"), ("ɐ", "r"),
           ("ɜ", "r"), ("ʁ", "r"), ("ɾ", "r"), ("ʀ", "r"), ("ər", "r"), ("ɡ", "g"),
           ("ɑ", "a"), ("ɒ", "ɔ"), ("ʏ", "y"), ("̯", ""))


def _vergleichsform(ipa: str) -> str:
    s = _WEG.sub("", unicodedata.normalize("NFD", ipa))
    for a, b in _GLEICH:
        s = s.replace(a, b)
    return s


def _abstand(a: str, b: str) -> float:
    if a == b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / max(len(a), len(b), 1)


def _bevorzugt(sounds: list[dict]) -> list[str]:
    ipas = [s for s in sounds if "ipa" in s]
    ohne = [s for s in ipas if not s.get("tags") and not s.get("raw_tags")]
    de = [s for s in ipas if "Germany" in (s.get("tags") or [])]
    return [s["ipa"] for s in (ohne or de or ipas)]


_FUNKTION = {"pron", "prep", "conj", "particle", "det", "postp", "article", "num",
             "character", "prefix", "suffix", "affix", "interfix", "circumfix", "infix"}


def lies_dump(pfad: str) -> dict[str, set[str]]:
    """Wort -> je Eintrag die erste bevorzugte IPA-Fassung.

    Varianten INNERHALB eines Eintrags ("Sauce" [ˈzoːsə], [ˈzoːs]) sind
    gleichwertig, es gilt die erste. Verschiedene EINTRAEGE mit gleicher
    Schreibung sind dagegen moegliche Homographen — die vergleicht main().
    """
    woerter: dict[str, set[str]] = defaultdict(set)
    with gzip.open(pfad, "rt", encoding="utf-8") as f:
        for zeile in f:
            d = json.loads(zeile)
            if d.get("lang_code") != "de":
                continue
            wort = (d.get("word") or "").strip()
            if not wort or len(wort) > 40:
                continue
            ipas = [i for i in _bevorzugt(d.get("sounds") or []) if "…" not in i]
            if ipas:
                woerter[wort].add(ipas[0])
                if d.get("pos") in _FUNKTION:
                    woerter[wort].add("FUNKTION")
    return woerter


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dump", help="raw-wiktextract-data.jsonl.gz der deutschen Ausgabe")
    ap.add_argument("--schwelle", type=float, default=0.25)
    ap.add_argument("--probe", type=int, default=0, help="nur N zufaellige Woerter (zum Messen)")
    ap.add_argument("--aus", default=ZIEL)
    ap.add_argument("--bekannt", default=BEKANNT,
                    help="Wortschatz-Datei fuer das Sammeln unbekannter Woerter (nicht im Repo)")
    a = ap.parse_args()

    woerter = lies_dump(a.dump)
    namen = sorted(woerter)
    if a.probe:
        random.seed(1)
        namen = random.sample(namen, a.probe)
    e = EspeakPhonemizer()
    eintraege, mehrdeutig = [], 0
    je_klein: dict[str, list[str]] = defaultdict(list)
    for w in woerter:
        je_klein[w.lower()].append(w)
    for wort in namen:
        if len(wort) < 4 or "FUNKTION" in woerter[wort]:
            continue
        fassungen = set(woerter[wort])
        for anders in je_klein[wort.lower()]:         # Seine / seine
            fassungen |= woerter[anders]
        if "FUNKTION" in fassungen:
            mehrdeutig += 1
            continue
        formen = {_vergleichsform(i) for i in fassungen}
        # Homograph oder echte Doppel-Aussprache: nur wenn sich die Fassungen
        # nicht nur in Kleinigkeiten unterscheiden.
        if max(_abstand(x, y) for x in formen for y in formen) > 0.15:
            mehrdeutig += 1
            continue
        ipa = min(woerter[wort], key=len)
        es = "".join(sum(e.phonemize("de", wort), []))
        d = _abstand(_vergleichsform(es), _vergleichsform(ipa))
        if d > a.schwelle:
            eintraege.append((wort, piper_phoneme(ipa), round(d, 2)))

    os.makedirs(os.path.dirname(a.aus), exist_ok=True)
    with open(a.aus, "w", encoding="utf-8") as f:
        f.write("# Aussprache-Grundstock aus dem deutschen Wiktionary (CC BY-SA 4.0, siehe LIZENZ.md)\n")
        f.write(f"# erzeugt von tools/aussprache_grundstock.py, Schwelle {a.schwelle}\n")
        f.write("# wort\tphoneme (Piper/espeak-Schreibweise)\tabstand zu espeak-de\n")
        for wort, ph, d in sorted(eintraege):
            f.write(f"{wort}\t{ph}\t{d}\n")
    if not a.probe:
        # Alle deutschen Woerter mit Aussprache = "bekannt": was hier steht,
        # ist kein Fall fuer den Ergaenzungslauf. Gross (~1 Mio. Formen),
        # deshalb nicht im Repo.
        os.makedirs(os.path.dirname(a.bekannt), exist_ok=True)
        with gzip.open(a.bekannt, "wt", encoding="utf-8") as f:
            for wort in sorted(woerter):
                f.write(wort + "\n")
    print(f"{len(namen)} Woerter mit IPA, {mehrdeutig} mehrdeutig weggelassen, "
          f"{len(eintraege)} ueber Schwelle {a.schwelle} -> {a.aus}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
