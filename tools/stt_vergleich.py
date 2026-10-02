#!/usr/bin/env python3
"""Zwei STT-Modelle ueber die archivierten Aufnahmen: Transkript, Latenz, Aktuator-Ausgang.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.stt_vergleich
    ow-venv/bin/python -m tools.stt_vergleich --modelle guillaumekln/faster-whisper-medium \\
        deepdml/faster-whisper-large-v3-turbo-ct2 --json /tmp/stt.json

Was gespielt wird
-----------------
Die `*_rec.wav` aus TRIGGER_AUDIO_DIR — genau die Chunks, die live an die
STT gingen (mit Pre-Roll, roh). Davor dieselbe Peak-Normalisierung wie im
Betrieb (`stt.normalize_peak`), dieselbe Anfrage (`SpeachesStt.transcribe_raw`)
und derselbe Halluzinations-Filter (no_speech_prob im Mittel >= 0,5 ->
verworfen). Die Modelle wechseln sich je Clip ab, damit keins systematisch
die waermere GPU bekommt.

Woran gemessen wird
-------------------
Es gibt keine Referenz-Transkripte. Drei Dinge statt dessen:

1. Das LIVE-Transkript je Clip (wake_events.log, result "outcome", ueber den
   Clip-Namen). Laeuft das Live-Modell mit, zeigt "= live", ob das Replay
   den Betrieb ueberhaupt nachstellt.
2. Das LABEL aus testsets/actuator_tor.jsonl, wo das Live-Transkript dort von
   Hand gelabelt steht. Beide Transkripte gehen durch die Aktuator-Kette des
   Betriebs (Laya, aktuator_schatten.kette_laya) und werden bewertet wie in
   aktuator_vergleich.py: richtig / verpasst / FALSCH. Das ist die Zahl, um
   die es geht — ein Verhoerer kostet erst, wenn er den Aktuator verfehlt.
   Vorbehalt: das Label gehoert zum Satz, den das Live-Modell gehoert hat.
   Hoert das andere Modell einen ANDEREN Satz, ist das Label nur so gut, wie
   der Sinn gleich blieb — die Abweichungsliste zeigt beides nebeneinander.
3. Die ABWEICHUNGEN im Wortlaut, zum Urteil von Hand (Audio liegt daneben).

Messreihe
---------

    2026-10-02  medium (guillaumekln, live) gegen large-v3-turbo (deepdml),
                beide Speaches int8 auf der 3060 Ti; 130 Clips 2026-09-02..
                10-02, 117 mit Live-Transkript, 96 gelabelt; capabilities
                e93fcc67, Laya aktuator-v1.

                          = live  verworfen  Latenz med/max  richtig verp. FALSCH
                medium   113/117     13       360 / 1109 ms     88     7     1
                turbo     26/117      0       366 /  825 ms     89     5     2

                Das Replay stellt den Betrieb nach (113/117 wortgleich).
                Turbo schreibt fast jeden Satz anders, am Aktuator ist das
                Ergebnis aber ein Tausch, kein Gewinn:
                + Rollo-Familie: "alle Wolos" -> "alle Rollos", "Lohnsimmer-
                  rolle" -> "Wohnzimmerrolle" (medium: FALSCH rosazimmer),
                  "Wohnzimmer mal dazu" -> "Wohnzimmer-Roller zu".
                - "Licht" kippt zu "nicht": "Kichel nicht an" -> FALSCH
                  kugellicht (medium: Kuechenlicht richtig), "Wohnzimmer
                  nicht aus" (zweimal), "Tisch liegt an". Dazu "Atemlicht" ->
                  FALSCH basteltischlicht, "Wohnzimmerverlauf".
                - SCHWERWIEGEND: turbo liefert no_speech_prob durchweg 0,0.
                  Der Halluzinations-Filter (Mittel >= 0,5) ist damit tot —
                  die 13 Clips, die medium verwirft (Fernsehen, Hoerspiel),
                  kaemen als Auftraege beim Brain an ("Gut, ruf sie gleich
                  mal an."). Zwei davon waren echte "Stopp! Stopp!", die
                  medium verloren hat.
                Ergebnis: medium bleibt. Turbo waere erst ein Kandidat mit
                einem anderen Verwerfungs-Kriterium (avg_logprob,
                compression_ratio — ungemessen).

    2026-10-02  medium gegen large-v3 (Systran), gleiche 130 Clips. large-v3
                belegt in Speaches 1,8 GB (gemessen: 2506 -> 4306 MiB), auf
                der 3060 Ti bleiben 848 MiB frei.

                          = live  verworfen  Latenz med/p90/max   richtig verp. FALSCH
                medium   113/117     13       360 / - / 1119 ms     88     7     1
                large-v3  26/117      5       489 / 801 / 2110 ms   91     3     2

                + Bester Treffer bei Verhoerern: alle Wolos, Lohnsimmerrolle,
                  Wohnzimmer mal hinzu, abendlich, abend Licht — alle richtig.
                - Neues FALSCH: "Kuechenlicht aus" -> "Kirchenlicht" ->
                  kleineszimmerlicht (medium richtig); "Atemlicht" ->
                  basteltischlicht wie bei turbo.
                - no_speech_prob funktioniert, liegt aber tiefer: Fernsehen/
                  Hoerspiel bei 0,15-0,48, echte Turns bis 0,32. Keine
                  Schwelle trennt: 0,1 verwirft 19/106 echte Turns, 0,3 laesst
                  5/10 Leer-Clips durch (darunter 2 echte "Stopp Stopp"). Bei
                  0,5 kaemen 8 Fernseh-Clips als Auftraege an den Brain.
                - +130 ms im Median, Ausreisser bis 2,1 s.
                Ergebnis: am Aktuator leicht besser (+3 richtig, +1 FALSCH),
                beim Verwerfen deutlich schlechter. Medium bleibt.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import glob
import io
import json
import os
import re
import statistics
import sys
import time
import wave

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.stt_vergleich", *sys.argv[1:]])

import numpy as np  # noqa: E402

from voice_assistant.config import TRIGGER_AUDIO_DIR, WORKSPACE, load_profile  # noqa: E402
from voice_assistant.services import aktuator_schatten  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402
from voice_assistant.services.aktuator_schatten import ausgang  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.services.stt import SpeachesStt, chunks_to_wav_bytes  # noqa: E402
from tools.aktuator_vergleich import bewerte  # noqa: E402

_TESTSET = os.path.join(_REPO, "testsets", "actuator_tor.jsonl")
_WAKE_EVENTS = os.path.join(WORKSPACE, "wake_events.log")
_DEFAULT = ["guillaumekln/faster-whisper-medium", "deepdml/faster-whisper-large-v3-turbo-ct2"]


def _norm(s: str) -> str:
    return re.sub(r"[^\wäöüß]+", " ", (s or "").lower()).strip()


def _kurz(modell: str) -> str:
    m = modell.split("/")[-1]
    for weg in ("faster-whisper-", "whisper-", "-ct2"):
        m = m.replace(weg, "")
    return m


def live_ausgaenge() -> dict[str, dict]:
    """Clip-Praefix (YYYYMMDD_HHMMSS) -> outcome-Zeile aus wake_events.log."""
    out = {}
    for z in open(_WAKE_EVENTS, encoding="utf-8"):
        try:
            d = json.loads(z)
        except ValueError:
            continue
        if d.get("result") == "outcome" and d.get("audio"):
            out[d["audio"][:15]] = d
    return out


def labels() -> dict[str, dict]:
    out = {}
    for z in open(_TESTSET, encoding="utf-8"):
        if z.strip() and not z.startswith("#"):
            f = json.loads(z)
            if f.get("schalten") is not None:
                out[_norm(f["satz"])] = f
    return out


def lies_wav(pfad: str) -> bytes:
    with wave.open(pfad) as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2, pfad
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    with contextlib.redirect_stdout(io.StringIO()):          # Gain-Zeile schlucken
        return chunks_to_wav_bytes([audio], normalize=True)


def transkribiere(stt: SpeachesStt, wav: bytes) -> tuple[str | None, float, int]:
    """(Text oder None wenn verworfen, no_speech_prob-Mittel, ms) — Filter wie
    SpeachesStt.transcribe, aber ohne Wirkung auf den Cooldown-Zustand."""
    t0 = time.monotonic()
    r = stt.transcribe_raw(wav)
    ms = int((time.monotonic() - t0) * 1000)
    seg = r.get("segments") or []
    nsp = sum(s.get("no_speech_prob", 0.0) for s in seg) / len(seg) if seg else 0.0
    text = (r.get("text") or "").strip()
    return (None if nsp >= 0.5 or not text else text), round(nsp, 2), ms


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--modelle", nargs=2, default=_DEFAULT, metavar=("A", "B"))
    ap.add_argument("--ordner", default=TRIGGER_AUDIO_DIR)
    ap.add_argument("--json", help="Ergebnis je Clip hierhin schreiben")
    ap.add_argument("--ohne-aktuator", action="store_true", help="nur Transkripte und Latenz")
    args = ap.parse_args()

    profil = load_profile()
    base = profil.speaches_base
    stts = {m: SpeachesStt(SpeachesState(), base, m) for m in args.modelle}
    clips = sorted(glob.glob(os.path.join(args.ordner, "*_rec.wav")))
    live = live_ausgaenge()
    lab = labels()
    akt = None
    if not args.ohne_aktuator:
        akt = Actuator(profil.actuator)
        if not akt.refresh():
            print("capabilities-refresh fehlgeschlagen — ohne Aktuator weiter")
            akt = None

    # Aufwaermen: das erste Laden eines Modells kostet Sekunden und gehoert
    # nicht in die Latenz.
    probe = lies_wav(clips[0])
    for m, stt in stts.items():
        transkribiere(stt, probe)

    zeilen = []
    for i, pfad in enumerate(clips):
        cid = os.path.basename(pfad)[:15]
        wav = lies_wav(pfad)
        lv = live.get(cid) or {}
        z = {"clip": os.path.basename(pfad), "live": lv.get("transcript"),
             "live_ausgang": lv.get("ausgang"), "modelle": {}}
        f = lab.get(_norm(lv.get("transcript") or "")) if lv.get("transcript") else None
        z["label"] = f
        reihe = args.modelle if i % 2 == 0 else list(reversed(args.modelle))
        for m in reihe:
            text, nsp, ms = transkribiere(stts[m], wav)
            e = {"text": text, "nsp": nsp, "ms": ms}
            if akt and text:
                k = aktuator_schatten.kette_laya(akt, text, timeout=10)
                e["ausgang"] = ausgang(k.intent, k.verdict)
                if f:
                    e["klasse"] = bewerte(f, k.intent, k.verdict)
            elif f:
                e["ausgang"] = "verworfen"
                e["klasse"] = "verpasst" if f.get("schalten") and f.get("ziel") else "richtig"
            z["modelle"][m] = e
        zeilen.append(z)
        print(f"\r{i + 1}/{len(clips)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    a, b = args.modelle
    print(f"{len(clips)} Clips aus {args.ordner}, Speaches {base}")
    print(f"  mit Live-Transkript {sum(1 for z in zeilen if z['live'])}, "
          f"davon gelabelt {sum(1 for z in zeilen if z['label'])}\n")
    print(f"{'Modell':22s} {'= live':>7s} {'verworfen':>9s}   Latenz median / max   "
          f"{'richtig':>7s} {'verpasst':>8s} {'FALSCH':>6s}")
    for m in args.modelle:
        es = [z["modelle"][m] for z in zeilen]
        gleich = sum(1 for z in zeilen if z["live"] and _norm(z["modelle"][m]["text"]) == _norm(z["live"]))
        mit_live = sum(1 for z in zeilen if z["live"])
        c = collections.Counter(e.get("klasse") for e in es if e.get("klasse"))
        ms = [e["ms"] for e in es]
        print(f"{_kurz(m):22s} {gleich:3d}/{mit_live:<3d} {sum(e['text'] is None for e in es):9d}   "
              f"{statistics.median(ms):6.0f} / {max(ms):5d} ms        "
              f"{c['richtig']:7d} {c['verpasst']:8d} {c['FALSCH']:6d}")

    print(f"\nAbweichungen im Wortlaut ({_kurz(a)} ≠ {_kurz(b)}):")
    n = 0
    for z in zeilen:
        ea, eb = z["modelle"][a], z["modelle"][b]
        if _norm(ea["text"]) == _norm(eb["text"]):
            continue
        n += 1
        soll = ""
        if z["label"]:
            f = z["label"]
            soll = (ausgang(f, "ausfuehrbar") if f.get("schalten") and f.get("ziel")
                    else ("schalten, Ziel offen" if f.get("schalten") else "nichts schalten"))
        print(f"  {z['clip'][:15]}" + (f"   soll: {soll}" if soll else "")
              + (f"   live: {z['live_ausgang']}" if z["live_ausgang"] else ""))
        for m, e in ((a, ea), (b, eb)):
            print(f"    {_kurz(m):14s} {str(e['text'])[:70]:70s} "
                  f"{e.get('ausgang', ''):28s} {e.get('klasse', '')}")
    print(f"  ({n} von {len(zeilen)})")

    if args.json:
        json.dump({"modelle": args.modelle, "clips": zeilen},
                  open(args.json, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
