"""Bricht der Antwort-Dienst Sätze vorzeitig ab? Messung je Stimme, ohne Hören.

Anlass 2026-11-09: „Brauchst du daraus eine Einkaufsliste für die Zutaten?“
kam als „… daraus eine Einkaufs“ (3,0 s statt ~4,6 s). Ein Sprachmodell-TTS
beendet die Ausgabe manchmal zu früh, Strom und Player hatten alles gespielt.

Je Stimme × Variante × Wiederholung wird jeder Satz aus
``testsets/tts_abbruch/saetze.jsonl`` gerechnet und von Qwen-ASR
mitgeschrieben (STT-Rückprobe wie bei der Aussprache). Abbruch heißt: keines
der letzten zwei Wörter des Satzes steht unter den letzten vier Wörtern der
Mitschrift (unscharf verglichen, Qwen verhört Namen) UND die Länge liegt unter
80 % dessen, was das Tempo dieser Stimme erwarten lässt. Nur eines von beiden
zählt als „fraglich“ und wird mitgeschrieben, nicht gezählt.

Varianten (``--varianten``): ``orig`` wie gesprochen; ``punkt`` Satzende als
Punkt („?“/„!“ → „.“, fehlendes Satzzeichen → „.“; Jochens Idee).

    ow-venv/bin/python -m tools.tts_abbruch_test --lager DIR --stimme gaston2 ...
        [--wdh 2] [--varianten orig,punkt]

Ergebnis je Lauf: ``testsets/tts_abbruch/laeufe/<zeit>.jsonl`` (eine Zeile je
Rechnung), Zusammenfassung auf stdout. Die Messreihe steht unten.

Für eine neue Stimme genügt der heikle Satz (``--set heikel.jsonl --wdh 15
--varianten orig``, 60 Rechnungen, ~3 min): **0 Abbrüche ist die Latte.**

Messreihe
---------
2026-11-09, CosyVoice3 (TRT max 2000, vLLM 0.07), Fablab GPU1:

- Breit (saetze.jsonl, 40 echte Antwortsätze, je 2×, orig/punkt):
  gaston2 1/80 orig + 0/80 punkt Abbruch (beide Male „… sieben Komma fünf
  Grad.“ → „… sieben Komma.“/„… sieben.“, der Satz endet in beiden Varianten
  mit Punkt — also Zufall, kein Satzzeichen-Effekt); thorsten 0/160,
  misterhandy 0/160. Fragen: 0 Abbrüche in 72 Rechnungen. Ein Fall „ohne
  Ende“ war ein Qwen-Hörfehler (Länge passte), deshalb die Doppelbedingung.
- Mindestlänge im Modell (min_token_text_ratio) taugt nicht: die
  abgeschnittenen Fassungen hatten 72 % und 88 % der erwarteten Länge, 5 %
  der vollständigen liegen unter 72 %.
- Heikel (heikel.jsonl, 4 Sätze × 15): gaston2 (Probe 12 s, zwei Sätze,
  1,2 s Pause) **9/60** (Staubsauger-Satz 7/15); dieselbe Probe mit Pause auf
  0,4 s gekürzt **6/60**; gaston2kurz (nur der erste Satz, 5,3 s) **0/60**,
  dazu Rechenzeit 0,46 statt 0,75 der Sprechzeit. Es liegt an der Probe:
  einsätzige Proben (gaston2kurz, thorsten, misterhandy) brachen nie ab.
- Heikel, nach Gehör gewählte Stücke derselben Probe
  (``tools/stimmprobe_zerlegen.py``): erster Satz 4,8 s **0/60**, zweiter
  Satz 4,4 s **0/60**; Längen-Streuung beim zweiten größer (bis 8,8 s statt
  6,7 s). Der erste ist seit dem Abend die Stimme ``gaston``.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

from voice_assistant.config import load_profile
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stimmen import AntwortTts, StimmenLager
from voice_assistant.services.stimmen_eingang import aehnlich, ende_da, wav16k, woerter  # noqa: F401
from voice_assistant.services.stt import LlamaCppAsrStt

SET = os.path.join(os.path.dirname(__file__), "..", "testsets", "tts_abbruch")


def variante(satz: str, art: str) -> str:
    if art == "punkt":
        s = satz.rstrip()
        if s and s[-1] in "?!":
            return s[:-1] + "."
        if s and s[-1] not in ".":
            return s + "."
        return s
    return satz


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lager", required=True, help="Stimmen-Lager (voice/stimmen-Aufbau)")
    ap.add_argument("--stimme", action="append", required=True)
    ap.add_argument("--wdh", type=int, default=2)
    ap.add_argument("--varianten", default="orig,punkt")
    ap.add_argument("--namensraum", default="messung")
    ap.add_argument("--asr", help="Qwen-ASR-URL (Default: stt_llamacpp_url des Profils)")
    ap.add_argument("--max", type=int, default=0, help="nur die ersten N Sätze")
    ap.add_argument("--set", default="saetze.jsonl", help="Datei in testsets/tts_abbruch/")
    a = ap.parse_args()

    p = load_profile()
    lager = StimmenLager(a.lager, a.namensraum, p.antwort_tts_url, p.antwort_tts_model,
                         a.stimme[0], timeout=60)
    tts = AntwortTts(lager, SpeachesState(), timeout=60)
    asr = LlamaCppAsrStt(a.asr or p.stt_llamacpp_url, timeout=60, sprache="German")
    saetze = [json.loads(z)["satz"] for z in open(os.path.join(SET, a.set), encoding="utf-8")]
    if a.max:
        saetze = saetze[:a.max]
    os.makedirs(os.path.join(SET, "laeufe"), exist_ok=True)
    aus = os.path.join(SET, "laeufe", time.strftime("%Y%m%d_%H%M%S") + ".jsonl")
    varianten = a.varianten.split(",")
    zeilen = []
    with open(aus, "w", encoding="utf-8") as f:
        for stimme in a.stimme:
            for art in varianten:
                for w in range(a.wdh):
                    for i, satz in enumerate(saetze):
                        text = variante(satz, art)
                        t0 = time.time()
                        pcm, rate = tts._roh(text, stimme)
                        rechnen = time.time() - t0
                        mitschrift = asr._anfrage(wav16k(pcm, rate), None) or ""
                        z = {"stimme": stimme, "variante": art, "wdh": w, "nr": i, "text": text,
                             "audio_s": round(len(pcm) / rate, 2), "rechnen_s": round(rechnen, 2),
                             "mitschrift": mitschrift, "ende_da": ende_da(satz, mitschrift)}
                        zeilen.append(z)
                        f.write(json.dumps(z, ensure_ascii=False) + "\n")
                        f.flush()
                    print(f"  {stimme} {art} Wdh {w + 1}: "
                          f"{sum(not z['ende_da'] for z in zeilen if (z['stimme'], z['variante'], z['wdh']) == (stimme, art, w))}"
                          f" ohne Ende", flush=True)

    # Tempo je Stimme aus den Rechnungen MIT Ende (s je Zeichen, Median)
    tempo = {s: statistics.median([z["audio_s"] / max(1, len(z["text"])) for z in zeilen
                                   if z["stimme"] == s and z["ende_da"]] or [0.08])
             for s in a.stimme}
    print(f"\nLauf: {aus}\n")
    print(f"{'Stimme':12} {'Variante':8} {'n':>4} {'Abbruch':>8} {'fraglich':>9}  s/Zeichen")
    for stimme in a.stimme:
        for art in varianten:
            zz = [z for z in zeilen if z["stimme"] == stimme and z["variante"] == art]
            abbruch = fraglich = 0
            for z in zz:
                kurz = z["audio_s"] < 0.8 * tempo[stimme] * len(z["text"])
                if not z["ende_da"] and kurz:
                    abbruch += 1
                elif not z["ende_da"] or kurz:
                    fraglich += 1
            print(f"{stimme:12} {art:8} {len(zz):4} {abbruch:8} {fraglich:9}  {tempo[stimme]:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
