#!/usr/bin/env python3
"""Zwei STT-Modelle ueber die archivierten Aufnahmen: Transkript, Latenz, Aktuator-Ausgang.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.stt_vergleich
    ow-venv/bin/python -m tools.stt_vergleich --modelle guillaumekln/faster-whisper-medium \\
        deepdml/faster-whisper-large-v3-turbo-ct2 --json /tmp/stt.json

Modelle, die Speaches nicht kann (--export-wavs, --speaches-lauf, --transkripte)
----------------------------------------------------------------------------
Speaches bedient nur faster-whisper. Alles andere (NeMo, Qwen3-ASR, Voxtral)
läuft mit tools/stt_kandidaten.py auf einem GPU-Rechner; hier wird nur
ausgewertet:

    ow-venv/bin/python -m tools.stt_vergleich --export-wavs <ordner>   # -> GPU-Rechner
    ow-venv/bin/python -m tools.stt_vergleich --speaches-lauf medium.jsonl \\
        --modelle guillaumekln/faster-whisper-medium x                   # Basislinie
    ow-venv/bin/python -m tools.stt_vergleich --transkripte medium.jsonl <lauf>.jsonl ...

Die Basislinie MUSS über Speaches laufen, nicht als faster-whisper auf dem
GPU-Rechner: dort traf dasselbe Modell mit denselben Parametern nur 78/155
Live-Transkripte (Speaches: 151/155) — andere faster-whisper-/ctranslate2-
Fassung. Whisper-Varianten (Prompt, large-v3) deshalb auch über Speaches.

Zusätzliche Spalten: "Text auf Leer-Clip" (von den Clips, die live wegen
no_speech_prob verworfen wurden, liefert das Modell Text — ohne eigenes
Verwerfungs-Signal gingen die zum Brain), "Stopp erkannt" (live als
Stopp-Wort beendete Clips, Muster wie im Betrieb) und "FALSCH?" (Label vom
verstümmelten Live-Satz, das Modell hört etwas anderes — nachhören).

Kanal-Vergleich (--kanaele)
---------------------------
    ow-venv/bin/python -m tools.stt_vergleich --kanaele --seit 20261003_1200

Dasselbe Modell (A) auf beiden Kanaelen DERSELBEN Aufnahme: *_rec.wav ist
Kanal 1 (rechts, XVF (7,3): AEC-Residual eines Mikrofons — darauf laufen
Wakeword und STT seit April), *_rec_kanal2.wav ist der zweite Kanal, den
die Firmware seit 2026-10-03 mitschickt. Was darin liegt, bestimmt
respeaker.kanal2_quelle zur Aufnahmezeit — fuer diese Frage "asr" (XVF (8,0),
ASR-Strahl mit Beam, Rauschunterdrueckung und AGC; formatBCE nimmt ihn fuer
die STT). Deshalb --seit: nur Aufnahmen ab der Umstellung auf "asr" zaehlen.

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

    2026-10-05  Neuere Modelle (--transkripte). 169 Clips 2026-09-05..10-04,
                155 mit Live-Transkript, 96 gelabelt, 14 live verworfen, 11
                live als Stopp beendet; capabilities 70866bd6, Laya aktuator-v1.
                medium über Speaches (3060 Ti, HTTP), alle anderen in-process
                auf einer RTX 5060 Ti (Fablab-Server) — Latenzen also nur grob
                vergleichbar. Prompt = Gerätenamen aus /capabilities (106 Wörter).

                              = live verw. Text auf  Latenz med/p90/max  richtig verp. FALSCH FALSCH? Stopp
                                           Leer-Clip
                medium (live)   151/155  14   0/14    360 /  479 / 1117    88     7     1      0    9/11
                medium +prompt   76/155  27   3/14    402 /  541 / 2409    87     7     1      1    6/11
                parakeet-tdt-0.6b-v3 21   7   9/14     72 /  102 /  214    83    12     0      1    8/11
                canary-1b-v2         14   8  11/14    239 /  433 / 1853    79    15     1      1    7/11
                Qwen3-ASR-0.6B       10   0  14/14    431 /  947 / 4681    73    19     2      2    9/11
                Qwen3-ASR-1.7B       19   0  14/14    458 / 1141 / 4967    88     6     1      1    9/11
                Qwen3-ASR-1.7B +Kon. 26   0  14/14    443 / 1105 / 7492    92     3     0      1    9/11
                Voxtral-Mini-3B      22   0  14/14    439 /  954 / 4242    90     5     0      1    9/11
                large-v3 (fw 1.2.1)  33   4  11/14    309 /  531 / 3094    92     3     0      1    9/11

                FALSCH? ist überall derselbe Clip (20260926_194253): live
                "Atemgericht" (Label: Ziel offen), fast alle anderen hören
                "Abendlicht" — vermutlich richtig gehört, nicht nachgehört.

                + Am Aktuator sind Qwen3-ASR-1.7B mit Gerätenamen als Kontext
                  (92/3/0) und Voxtral-Mini (90/5/0) besser als medium
                  (88/7/1, das FALSCH ist "Lohnsimmerrolle" -> rosazimmer).
                  Aber auch Qwen gibt den Kontext zweimal komplett als
                  Transkript aus ("Gaston, Küchenlicht, Wohnzimmerrollo,
                  <weitere Gerätenamen> ...", 20260923_170033 und
                  20261003_140037, beides echte Kommandos -> Rückfrage).
                  Schlimmer bei Whisper: medium +prompt verwirft 16 Clips, die live Text hatten, verliert 3
                  Stopps und schreibt bei Fernsehton die Prompt-Wörter hin
                  (ein Gerätename aus dem Prompt, mehrfach wiederholt).
                - KEIN Kandidat hat ein Verwerfungs-Signal. Qwen und Voxtral
                  liefern auf allen 14 Leer-Clips Text (Hörspiel, Fernsehen,
                  Gespräche im Raum), der zum Brain ginge; Voxtral erfindet
                  bei Stille "Vielen Dank.", Qwen "Gaston." und "Ja.".
                  Die Token-Konfidenz von Parakeet trennt nicht (echte Turns
                  Median 0,15, Leer-Clips 0,01-0,32).
                - Parakeet/Canary: am schnellsten, aber am Aktuator
                  schlechter (12/15 verpasst) — Rollo/Licht-Verhörer
                  ("Rollus", "Rolls", "Wohnzimmerholder"). Alle zusätzlichen
                  Fehlgriffe von Parakeet enden als Rückfrage, keiner schaltet
                  falsch. VORBEHALT für alle Nicht-medium-Läufe: Layas
                  Trainingsdaten tragen die Verhörer von medium
                  (tor_trainset._VERHOERER, aus den Live-Logs), nicht die
                  eines anderen Modells ("Rollus", "Rolls"). Mit dessen
                  Verhörern im Training wären die Kandidaten eher besser
                  als hier.
                Nebenbefund zum Verwerfen selbst: von den 14 Leer-Clips sind
                2 echte Stopps ("Stopp, Stopp!", "Stopp!") und 1 echte
                Ansprache ("Gaston, ... auf die Essensliste") — medium
                verliert sie über no_speech_prob. Der Fernseh-Filter hängt
                also an einer Whisper-Eigenheit, die selbst Fehler macht.
                Nachtrag, gleicher Tag: Speaches 0.9.0-rc.3 bedient Parakeet
                schon (onnx-asr, istupakov/parakeet-tdt-0.6b-v3-onnx; nur
                response_format json, kein prompt). Gemessen in einem
                Speaches-Container auf der 5060 Ti, medium daneben:

                              = live verw. Text auf  Server-Latenz med/p90/max  richtig verp. FALSCH
                medium            124   15   0/14        157 / 249 / 731         88     7     1
                parakeet-onnx      20    7   9/14         66 /  94 / 223         83    11     1

                Die ONNX-Fassung entspricht NeMo (83/12/0); ihr FALSCH ist
                "Kirchenlicht" -> kleineszimmerlicht (bei NeMo dasselbe Wort,
                dort "aus?" -> Brain). Medium im GLEICHEN Image trifft auf
                der 5060 Ti nur 124/155 Live-Transkripte wortgleich — schon
                die GPU-Generation verschiebt den Wortlaut, am Aktuator
                ändert es nichts. Server-Latenz aus dem Speaches-Log; die
                Rundreise ins Fablab (~1,3 s) ist Leitung, nicht Modell.

                Ergebnis: medium bleibt, solange das Verwerfen an
                no_speech_prob hängt. Ein Wechsel zu Qwen3-ASR-1.7B (+Kontext)
                setzt ein eigenes Verwerfungs-Kriterium voraus (VAD-Anteil,
                Pegel, Sprecher — ungemessen), und 0 FALSCH gegen 1 bei
                96 Labels ist noch keine Signifikanz.

                Revidiert (Jochen, 2026-10-05): Verwerfen ist kein Kriterium.
                Ein Fehltrigger schaltet nichts, er geht zum Brain — und der
                bekommt jetzt einen Hinweis, wenn das Wakewort im Transkript
                fehlt (voice_assistant/anrede.py). Gemessen: VAD-Anteil und
                Pegel trennen Fernsehton NICHT von echten Turns (Fernsehen
                rms_median 140-374, echte 24-699), der Sprecher-Status nur
                halb (alle 11 Fernseh-Clips "unbekannt", aber auch 72 von 158
                echten Turns). Die fehlende Anrede trennt: 0/11 gegen 60/69.
                Damit fällt die Hürde für Parakeet und Qwen; offen bleibt
                ihr Abstand am Aktuator (Laya kennt ihre Verhörer nicht).
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


def export_wavs(ziel: str, ordner: str) -> int:
    """Die Clips so normalisiert ablegen, wie sie live an die STT gehen —
    Eingabe für tools/stt_kandidaten.py auf einem fremden GPU-Rechner."""
    os.makedirs(ziel, exist_ok=True)
    clips = sorted(glob.glob(os.path.join(ordner, "*_rec.wav")))
    for pfad in clips:
        with open(os.path.join(ziel, os.path.basename(pfad)), "wb") as f:
            f.write(lies_wav(pfad))
    print(f"{len(clips)} Clips -> {ziel}")
    return 0


def speaches_lauf(modell: str, ziel: str, ordner: str, prompt: str | None = None,
                  base: str | None = None) -> int:
    """Ein Modell über das Speaches des Profils, im Format von
    tools/stt_kandidaten.py — die Basislinie, die den Betrieb nachstellt.
    Ein faster-whisper auf einem anderen Rechner tut das NICHT (gemessen
    2026-10-05: gleiches Modell, gleiche Parameter, 78/155 statt 151/155
    wortgleich mit live — faster-whisper 1.2.1/ctranslate2 4.8.2 dort,
    1.1.1/4.5.0 in Speaches 0.9.0-rc.3). `prompt` geht als initial_prompt an
    Whisper."""
    stt = SpeachesStt(SpeachesState(), base or load_profile().speaches_base, modell)
    clips = sorted(glob.glob(os.path.join(ordner, "*_rec.wav")))
    transkribiere(stt, lies_wav(clips[0]))
    with open(ziel, "w", encoding="utf-8") as f:
        wo = " (Speaches extern)" if base else " (Speaches)"
        f.write(json.dumps({"name": _kurz(modell) + (" +prompt" if prompt else "") + wo,
                            "engine": "speaches", "modell": modell, "prompt": bool(prompt)}) + "\n")
        for i, pfad in enumerate(clips):
            t0 = time.monotonic()
            r = stt.transcribe_raw(lies_wav(pfad), prompt=prompt)
            seg = r.get("segments") or []
            nsp = sum(s.get("no_speech_prob", 0.0) for s in seg) / len(seg) if seg else 0.0
            f.write(json.dumps({"clip": os.path.basename(pfad), "text": (r.get("text") or "").strip(),
                                "nsp": round(nsp, 3), "ms": int((time.monotonic() - t0) * 1000)},
                               ensure_ascii=False) + "\n")
            print(f"\r{i + 1}/{len(clips)}", end="", file=sys.stderr, flush=True)
    print(f"\n-> {ziel}", file=sys.stderr)
    return 0


def _verworfen(e: dict) -> bool:
    """Wie im Betrieb: no_speech_prob im Mittel >= 0,5 (nur Whisper hat es),
    sonst nur leerer Text."""
    return not e.get("text") or (e.get("nsp") or 0.0) >= 0.5


def transkripte_auswerten(dateien: list[str], ohne_aktuator: bool, json_ziel: str | None) -> int:
    """Läufe aus tools/stt_kandidaten.py gegen Live-Transkript, Labels und
    die Laya-Kette — dieselbe Bewertung wie der Speaches-Vergleich."""
    laeufe = {}
    for d in dateien:
        zeilen = [json.loads(z) for z in open(d, encoding="utf-8") if z.strip()]
        kopf, rest = zeilen[0], zeilen[1:]
        laeufe[kopf["name"]] = {"kopf": kopf, "clips": {e["clip"]: e for e in rest}}
    namen = list(laeufe)
    clips = sorted(set.intersection(*(set(l["clips"]) for l in laeufe.values())))
    live = live_ausgaenge()
    lab = labels()
    akt = None
    if not ohne_aktuator:
        akt = Actuator(load_profile().actuator)
        if not akt.refresh():
            print("capabilities-refresh fehlgeschlagen — ohne Aktuator weiter")
            akt = None
    cache: dict[str, str] = {}          # Text -> Ausgang; Laya ist deterministisch

    zeilen = []
    for i, clip in enumerate(clips):
        lv = live.get(clip[:15]) or {}
        f = lab.get(_norm(lv.get("transcript") or "")) if lv.get("transcript") else None
        z = {"clip": clip, "live": lv.get("transcript"), "live_ausgang": lv.get("ausgang"),
             "label": f, "modelle": {}}
        for n in namen:
            e = dict(laeufe[n]["clips"][clip])
            text = None if _verworfen(e) else e["text"]
            e["text_roh"], e["text"] = e.get("text"), text
            if akt and text:
                if text not in cache:
                    k = aktuator_schatten.kette_laya(akt, text, timeout=10)
                    cache[text] = (k.intent, k.verdict)
                intent, verdict = cache[text]
                e["ausgang"] = ausgang(intent, verdict)
                if f:
                    e["klasse"] = bewerte(f, intent, verdict)
                    # Das Label gehört zum Live-Satz. War der verstümmelt (Label ohne
                    # Ziel, "-> Rückfrage") und hört dieses Modell einen anderen Satz,
                    # kann FALSCH auch heißen: richtig gehört. Gefunden 2026-10-05 an
                    # "Atemgericht" -> "Abendlicht". Entscheidet nur das Ohr.
                    if (e["klasse"] == "FALSCH" and not f.get("ziel")
                            and _norm(text) != _norm(lv.get("transcript"))):
                        e["klasse"] = "FALSCH?"
            elif f:
                e["ausgang"] = "verworfen"
                e["klasse"] = "verpasst" if f.get("schalten") and f.get("ziel") else "richtig"
            z["modelle"][n] = e
        zeilen.append(z)
        print(f"\r{i + 1}/{len(clips)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    from voice_assistant.assistant import _is_stop_command
    mit_live = sum(1 for z in zeilen if z["live"])
    leer = [z for z in zeilen if z["live_ausgang"] == "leer"]
    stopp = [z for z in zeilen if z["live_ausgang"] == "stopwort"]
    print(f"{len(clips)} Clips, mit Live-Transkript {mit_live}, "
          f"gelabelt {sum(1 for z in zeilen if z['label'])}, live verworfen {len(leer)}\n")
    print(f"{'Lauf':30s} {'= live':>7s} {'verw.':>5s} {'Text auf':>8s}   Latenz med / p90 / max"
          f"  {'richtig':>7s} {'verp.':>5s} {'FALSCH':>6s} {'FALSCH?':>7s}  {'Stopp':>6s}  laden")
    print(f"{'':30s} {'':7s} {'':5s} {'Leer-Clip':>8s}{'':63s}{'erkannt':>7s}")
    for n in namen:
        es = [z["modelle"][n] for z in zeilen]
        gleich = sum(1 for z in zeilen if z["live"] and _norm(z["modelle"][n]["text"]) == _norm(z["live"]))
        c = collections.Counter(e.get("klasse") for e in es if e.get("klasse"))
        ms = sorted(e["ms"] for e in es)
        p90 = ms[int(len(ms) * 0.9) - 1]
        auf_leer = sum(1 for z in leer if z["modelle"][n]["text"])
        # Stopp: auf den Clips, die live als Stopp-Wort endeten, nach demselben
        # Muster wie im Betrieb (Erst-Turn) — ein verlorenes Stopp ist ein
        # Auftrag an den Brain statt eines Abbruchs.
        st = sum(1 for z in stopp if _is_stop_command(z["modelle"][n]["text"] or "", 0))
        print(f"{n[:30]:30s} {gleich:3d}/{mit_live:<3d} {sum(e['text'] is None for e in es):5d} "
              f"{auf_leer:4d}/{len(leer):<3d}   {statistics.median(ms):5.0f} / {p90:4d} / {ms[-1]:5d} ms"
              f"  {c['richtig']:7d} {c['verpasst']:5d} {c['FALSCH']:6d} {c['FALSCH?']:7d}"
              f"  {st:2d}/{len(stopp):<3d}"
              f"  {laeufe[n]['kopf'].get('lade_s', '-')} s")

    print("\nFALSCH je Lauf (Label sagt etwas anderes; FALSCH? = Label vom verstümmelten Live-Satz):")
    for n in namen:
        for z in zeilen:
            e = z["modelle"][n]
            if e.get("klasse") in ("FALSCH", "FALSCH?"):
                print(f"  {e['klasse']:7s} {n[:22]:22s} {z['clip'][:15]}  {str(e['text'])[:55]:55s} -> {e.get('ausgang')}"
                      f"   (live: {str(z['live'])[:50]})")

    print("\nLive verworfene Clips (medium sagte: keine Sprache) — was hören die anderen?")
    for z in leer:
        print(f"  {z['clip'][:15]}")
        for n in namen:
            e = z["modelle"][n]
            print(f"    {n[:22]:22s} {str(e['text_roh'])[:70]:70s} {e.get('ausgang', '')}")

    if json_ziel:
        json.dump({"laeufe": {n: l["kopf"] for n, l in laeufe.items()}, "clips": zeilen},
                  open(json_ziel, "w"), ensure_ascii=False, indent=1)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export-wavs", metavar="ORDNER",
                    help="nur die normalisierten Clips ablegen (für tools/stt_kandidaten.py)")
    ap.add_argument("--transkripte", nargs="+", metavar="JSONL",
                    help="statt Speaches: Läufe aus tools/stt_kandidaten.py auswerten")
    ap.add_argument("--speaches-lauf", metavar="JSONL",
                    help="Modell A über Speaches laufen lassen und im Format von "
                         "tools/stt_kandidaten.py ablegen (Basislinie für --transkripte)")
    ap.add_argument("--speaches-url", help="mit --speaches-lauf: anderes Speaches als das des "
                                           "Profils (z. B. ein Test-Container auf dem GPU-Rechner)")
    ap.add_argument("--prompt-datei", help="mit --speaches-lauf: Anfangs-Prompt für Whisper "
                                           "(z. B. die Gerätenamen; Datei außerhalb des Repos)")
    ap.add_argument("--modelle", nargs=2, default=_DEFAULT, metavar=("A", "B"))
    ap.add_argument("--ordner", default=TRIGGER_AUDIO_DIR)
    ap.add_argument("--json", help="Ergebnis je Clip hierhin schreiben")
    ap.add_argument("--ohne-aktuator", action="store_true", help="nur Transkripte und Latenz")
    ap.add_argument("--kanaele", action="store_true",
                    help="statt zweier Modelle: Modell A auf Kanal 1 (*_rec.wav, live) "
                         "gegen Kanal 2 (*_rec_kanal2.wav)")
    ap.add_argument("--seit", help="nur Clips ab diesem Zeitpunkt (JJJJMMTT_HHMMSS oder ISO)")
    args = ap.parse_args()
    if args.export_wavs:
        return export_wavs(args.export_wavs, args.ordner)
    if args.speaches_lauf:
        prompt = open(args.prompt_datei, encoding="utf-8").read().strip() if args.prompt_datei else None
        return speaches_lauf(args.modelle[0], args.speaches_lauf, args.ordner, prompt,
                             args.speaches_url)
    if args.transkripte:
        return transkripte_auswerten(args.transkripte, args.ohne_aktuator, args.json)

    profil = load_profile()
    base = profil.speaches_base
    # Variante = (Modell, Datei-Endung). Zwei Modelle auf derselben Aufnahme,
    # oder (--kanaele) dasselbe Modell auf beiden Kanaelen derselben Aufnahme.
    if args.kanaele:
        m = args.modelle[0]
        varianten = {f"{_kurz(m)} Kanal 1": (m, "_rec.wav"),
                     f"{_kurz(m)} Kanal 2": (m, "_rec_kanal2.wav")}
    else:
        varianten = {m: (m, "_rec.wav") for m in args.modelle}
    stts = {m: SpeachesStt(SpeachesState(), base, m) for m, _ in set(varianten.values())}
    clips = sorted(glob.glob(os.path.join(args.ordner, "*_rec.wav")))
    if args.kanaele:
        clips = [c for c in clips if os.path.exists(c[:-len("_rec.wav")] + "_rec_kanal2.wav")]
    if args.seit:
        grenze = re.sub(r"[-:T]", "", args.seit).replace("_", "")[:14].ljust(14, "0")
        clips = [c for c in clips if os.path.basename(c)[:15].replace("_", "") >= grenze]
    if not clips:
        print("Keine passenden Clips" + (" mit *_rec_kanal2.wav" if args.kanaele else ""))
        return 1
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
    for stt in stts.values():
        transkribiere(stt, probe)

    zeilen = []
    for i, pfad in enumerate(clips):
        cid = os.path.basename(pfad)[:15]
        stamm = pfad[:-len("_rec.wav")]
        lv = live.get(cid) or {}
        z = {"clip": os.path.basename(pfad), "live": lv.get("transcript"),
             "live_ausgang": lv.get("ausgang"), "modelle": {}}
        f = lab.get(_norm(lv.get("transcript") or "")) if lv.get("transcript") else None
        z["label"] = f
        namen = list(varianten)
        reihe = namen if i % 2 == 0 else list(reversed(namen))
        for name in reihe:
            modell, endung = varianten[name]
            text, nsp, ms = transkribiere(stts[modell], lies_wav(stamm + endung))
            e = {"text": text, "nsp": nsp, "ms": ms}
            if akt and text:
                k = aktuator_schatten.kette_laya(akt, text, timeout=10)
                e["ausgang"] = ausgang(k.intent, k.verdict)
                if f:
                    e["klasse"] = bewerte(f, k.intent, k.verdict)
            elif f:
                e["ausgang"] = "verworfen"
                e["klasse"] = "verpasst" if f.get("schalten") and f.get("ziel") else "richtig"
            z["modelle"][name] = e
        zeilen.append(z)
        print(f"\r{i + 1}/{len(clips)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    a, b = list(varianten)
    print(f"{len(clips)} Clips aus {args.ordner}, Speaches {base}")
    print(f"  mit Live-Transkript {sum(1 for z in zeilen if z['live'])}, "
          f"davon gelabelt {sum(1 for z in zeilen if z['label'])}\n")
    print(f"{'Modell':22s} {'= live':>7s} {'verworfen':>9s}   Latenz median / max   "
          f"{'richtig':>7s} {'verpasst':>8s} {'FALSCH':>6s}")
    for m in varianten:
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
        json.dump({"varianten": varianten, "clips": zeilen},
                  open(args.json, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
