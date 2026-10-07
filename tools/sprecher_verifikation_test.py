#!/usr/bin/env python3
"""Sprecher-Verifikation gegen gehörte Labels: wie viele richtig / unbekannt / FALSCH?

    ow-venv/bin/python -m tools.sprecher_verifikation_test [--schwelle 0.40 --abstand 0.15]

Wahrheit:
  - testsets/sprecher_labels.jsonl — von Hand gehört, wer spricht (privat, gitignored).
    Audio aus TRIGGER_AUDIO_DIR oder voice/corpus_sprecher/ (dort gesichert,
    weil das Archiv nach 30 Tagen löscht).
  - --mit-leer: zusätzlich alle Clips, die live als "leer" endeten (Fernsehen,
    Hörspiel) — schwaches Label "niemand", ohne die drei, die echte Stopps
    oder eine echte Ansprache waren (Liste unten).

FALSCH heißt: einer angelernten Stimme zugeordnet, die es nicht war. Das ist
der teure Fehler (Sprecher-Schranke, SPEAKER_STATE.md); "unbekannt" für einen
bekannten Sprecher ist harmlos. Eine Schwelle wird so gewählt, dass FALSCH 0
bleibt, mit Abstand.

Messreihe
---------
    2026-10-05  Referenzen jochen + petra, WeSpeaker ResNet34-LM (CPU, 53 ms).
                33 gehörte Clips (27 jochen, 4 petra, 2 mehrere) + 11 leer.
                Über alle 171 Archiv-Clips, dazu die 86 von der Diarization als
                jochen erkannten (alle >= 0,53, nicht gehört):

                  Schwelle  richtig  unbekannt  FALSCH
                    0,35      128        0        1   (Mehrpersonen-Clip 0,38)
                    0,38      129        0        0   (dieser Clip auf der Kante)
                    0,40      128        1        0   <- gewählt
                    0,42      127        2        0

                Abstand 0,10 oder 0,15: kein Unterschied. Fernsehen max 0,31.
                Die Diarization erkannte von den 27 Jochen-Clips nur 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.sprecher_verifikation_test", *sys.argv[1:]])

from voice_assistant.config import TRIGGER_AUDIO_DIR, VOICE_DIR, WORKSPACE  # noqa: E402
from voice_assistant.services.sprecher_verifikation import SprecherVerifikation, _wav_audio  # noqa: E402

_LABELS = os.path.join(_REPO, "testsets", "sprecher_labels.jsonl")
_KORPUS = os.path.join(VOICE_DIR, "corpus_sprecher")
# live "leer", aber gehört echt: zwei Stopps und eine Ansprache (2026-10-05)
_LEER_ABER_ECHT = {"20260921_191514", "20260925_104107", "20261003_201808"}


def _audio(clip: str) -> bytes | None:
    for d in (TRIGGER_AUDIO_DIR, _KORPUS):
        p = os.path.join(d, f"{clip}_gaston_rec.wav")
        if os.path.exists(p):
            return open(p, "rb").read()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--schwelle", type=float, default=0.40)
    ap.add_argument("--abstand", type=float, default=0.15)
    ap.add_argument("--mit-leer", action="store_true")
    ap.add_argument("--url", default="",
                    help="Fingerabdruck dort rechnen (voice-analysis /fingerabdruck) statt lokal")
    args = ap.parse_args()

    wahr = {}
    for z in open(_LABELS, encoding="utf-8"):
        if z.startswith("{"):
            d = json.loads(z)
            wahr[d["clip"]] = d["sprecher"]
    if args.mit_leer:
        for z in open(os.path.join(WORKSPACE, "wake_events.log"), encoding="utf-8"):
            d = json.loads(z)
            c = (d.get("audio") or "")[:15]
            if d.get("result") == "outcome" and d.get("ausgang") == "leer" and c not in _LEER_ABER_ECHT:
                wahr.setdefault(c, "niemand")

    v = SprecherVerifikation(schwelle=args.schwelle, abstand=args.abstand, url=args.url)
    zaehl = {"richtig": 0, "unbekannt": 0, "FALSCH": 0, "ohne Audio": 0}
    for clip, soll in sorted(wahr.items()):
        b = _audio(clip)
        if b is None:
            zaehl["ohne Audio"] += 1
            continue
        u = v.diarize(b)
        ist = u.name if u.name else None
        ziel = soll if soll not in ("mehrere", "niemand") else None
        if ist == ziel:
            zaehl["richtig"] += 1
        elif ist is None:
            zaehl["unbekannt"] += 1
        else:
            zaehl["FALSCH"] += 1
            print(f"  FALSCH {clip}: ist {ist}, gehört {soll}")
    print(f"Schwelle {args.schwelle:.2f}, Abstand {args.abstand:.2f}: " +
          ", ".join(f"{k} {n}" for k, n in zaehl.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
