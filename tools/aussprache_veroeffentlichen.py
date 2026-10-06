#!/usr/bin/env python3
"""Geprueft ergaenzte Aussprache-Eintraege ins Repo uebernehmen (oeffentlich).

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.aussprache_veroeffentlichen           # zeigen
    ow-venv/bin/python -m tools.aussprache_veroeffentlichen --schreiben

Liest ``<workspace>/voice/aussprache/<sprache>_ergaenzt.tsv`` (privat, vom
Ergaenzungslauf) und schreibt die oeffentlichen Eintraege nach
``data/aussprache/<sprache>_ergaenzt.tsv``. Committen bleibt ein bewusster
Schritt — das Repo ist oeffentlich.

Was NICHT hinausgeht, auch wenn das LLM es "oeffentlich" nannte: im ersten
Lauf (2026-10-06) markierte es "Majarollo" — ein Rollo, benannt nach einem
Familienmitglied — als oeffentlich. Deshalb zusaetzlich harte Filter:

- Woerter, die einen angelernten Sprecher enthalten (voice/speakers/*.wav),
- Woerter, die einen Bestandteil der Geraetenamen aus /capabilities
  enthalten, der kein allgemeines deutsches Wort ist ("Maja" aus
  "Majas Rollo" ja, "Rollo" nein).
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import sys

_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.aussprache_veroeffentlichen", *sys.argv[1:]])

from voice_assistant.config import VOICE_DIR, load_profile  # noqa: E402
from voice_assistant.services import aussprache  # noqa: E402


def _zeilen(pfad: str) -> list[list[str]]:
    try:
        return [z.rstrip("\n").split("\t") for z in open(pfad, encoding="utf-8")
                if z.strip() and not z.startswith("#")]
    except FileNotFoundError:
        return []


def private_bestandteile(profil, bekannt: frozenset[str]) -> set[str]:
    teile = set()
    sp = os.path.join(VOICE_DIR, "speakers")
    if os.path.isdir(sp):
        teile |= {os.path.splitext(n)[0].lower() for n in os.listdir(sp) if n.endswith(".wav")}
    if profil.actuator.enabled:
        try:
            from voice_assistant.services.actuator import Actuator
            akt = Actuator(profil.actuator)
            akt.refresh()
            for z in akt.ziele or []:
                for name in z.get("namen") or []:
                    for w in re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", name):
                        stamm = w.lower().rstrip("s")
                        if w.lower() not in bekannt and w.capitalize() not in bekannt:
                            teile.add(stamm)
        except Exception as e:
            print(f"⚠️  capabilities nicht lesbar ({e}) — ohne diesen Filter wird nichts geschrieben")
            raise SystemExit(1)
    return {t for t in teile if len(t) >= 3}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--schreiben", action="store_true")
    a = ap.parse_args()

    profil = load_profile()
    p = aussprache.pfade(profil.aussprache.sprache)
    ziel = p["grundstock"][1]
    try:
        with gzip.open(p["bekannt"], "rt", encoding="utf-8") as f:
            bekannt = frozenset(w.rstrip("\n") for w in f)
    except OSError:
        bekannt = frozenset()
    privat = private_bestandteile(profil, bekannt)

    vorhanden = {z[0]: z for z in _zeilen(ziel)}
    neu, gesperrt = [], []
    for z in _zeilen(p["ergaenzt"]):
        wort, phoneme, oeff = z[0], z[1], (z[2] if len(z) > 2 else "0")
        if oeff != "1" or wort in vorhanden:
            continue
        treffer = [t for t in privat if t in wort.lower()]
        if treffer:
            gesperrt.append((wort, treffer))
            continue
        neu.append([wort, phoneme, z[3] if len(z) > 3 else "", z[4] if len(z) > 4 else ""])

    for w, t in gesperrt:
        print(f"  privat: {w} (enthaelt {', '.join(t)})")
    for z in neu:
        print(f"  neu:    {z[0]}\t{z[1]}\t{z[2]}")
    print(f"{len(neu)} neu, {len(gesperrt)} privat gesperrt, {len(vorhanden)} schon im Repo")
    if not a.schreiben or not neu:
        return 0
    alle = sorted([*vorhanden.values(), *neu], key=lambda z: z[0])
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    with open(ziel, "w", encoding="utf-8") as f:
        f.write("# Aussprache-Ergaenzungen: vom LLM vorgeschlagen, per STT-Rueckprobe geprueft\n")
        f.write("# (tools/aussprache_ergaenzen.py), oeffentlich (tools/aussprache_veroeffentlichen.py).\n")
        f.write("# CC BY-SA 4.0, siehe LIZENZ.md\n")
        f.write("# wort\tphoneme\tumschreibung\tdatum\n")
        for z in alle:
            f.write("\t".join(z[:4]) + "\n")
    print(f"-> {ziel} (committen nicht vergessen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
