"""Eine Stimmprobe zerlegen und nach Gehör das beste Stück wählen.

Anlass 2026-11-09 (tools/tts_abbruch_test.py): die zweisätzige 12-s-Probe
gaston2 brach 9 von 60 heiklen Sätzen vorzeitig ab, ihr erster Satz allein
(5,3 s) keinen. Welches Stück einer Probe trägt, hängt an Länge, Inhalt und
Sprechweise — Jochen: „kurz von vorne? kurz hinten? Das hat alles Einfluss“.

Ablauf:

1. Die Probe wird an ihren Pausen geschnitten (20-ms-Fenster, höchstens 6 dB
   über dem leisesten Zehntel, mindestens ``--pause`` s). Jedes
   zusammenhängende Stück von ``--min``–``--max`` s (Vorgabe Pause 0,08 s: an 0,15 s fand gastone2 nur drei Stücke, an 0,06 s 25 fast gleiche) wird ein Kandidat.
2. Je Kandidat: auf −18 LUFS (ffmpeg loudnorm), Text per Qwen-ASR,
   als Stimme ``<name>_<nr>`` in ein eigenes Lager.
3. Hörseite: Kandidat selbst und dieselben Testsätze mit ihm gesprochen,
   „gut“ merkt ihn vor (``<ausgabe>/auswahl.json``).
4. Danach die Vorgemerkten durch den Abbruch-Test:
   ``tools.tts_abbruch_test --lager <ausgabe>/lager --set heikel.jsonl --wdh 15
   --varianten orig --stimme <name>_<nr> ...`` — 0 Abbrüche ist die Latte.

    ow-venv/bin/python -m tools.stimmprobe_zerlegen PROBE.mp3 --name gaston
        [--ausgabe DIR] [--port 8766] [--neu]

Ein zweiter Aufruf nimmt den vorhandenen Schnitt samt von Hand korrigierter
``probe.txt`` und rechnet nur fehlende Testsätze (``--neu`` zerlegt neu).
Die Seite am besten als eigene Unit laufen lassen (``systemd-run --user
--unit=stimmprobe-seite …``), dann überlebt sie das Ende der Sitzung.
Den Text der Probe gegenlesen: Qwen schrieb „der Kiel“ statt „der Kerl“.

Erste Runde 2026-11-09 (gastone2.mp3, 12 s, 11 Stücke): jedes klingt anders,
gewählt hat das Ohr 02 (erster Satz, 4,8 s) und 10 (zweiter Satz bis
„ausgeschlossen ist“, 4,4 s), beide 0/60 im Abbruch-Test (die ganze Probe:
9/60). Was sich vorab messen ließ: Stücke, die mitten im Wort enden („…ab“),
setzen sich in der Ausgabe fort („Abbrauchst du …“), und die Länge der
Testsätze streut je Stück stark (Kellerraum-Satz 6,1–10,0 s) — Stille,
Atmen, Zischen. SER (arousal/valence, Label) und Tonhöhen-Schwankung der
Probe sagten das Ohr-Urteil nicht voraus (die beiden Gewählten lagen an
entgegengesetzten Enden); die Ausgaben sind fast alle „neutral“.

Die Probe und alles daraus ist privat (Stimmen): Ausgabe nicht ins Repo,
Seite nur im eigenen Netz, Mikro vor dem Anhören stumm schalten.
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import wave

import numpy as np

from voice_assistant.config import STIMMEN_CACHE_DIR, load_profile
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stimmen import AntwortTts, StimmenLager
from voice_assistant.services.stt import LlamaCppAsrStt
from tools.tts_abbruch_test import wav16k

RATE = 24000
TESTSAETZE = [
    "Im Kellerraum sind es gerade siebzehn Komma neun Grad, draußen sieben Komma fünf.",
    "Brauchst du daraus eine Einkaufsliste für die Zutaten?",
    "Der Akku vom Staubsauger steht bei dreiundsechzig Prozent.",
]


def laden(pfad: str) -> np.ndarray:
    roh = subprocess.run(["ffmpeg", "-v", "error", "-i", pfad, "-ac", "1", "-ar", str(RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(roh, dtype="<i2")


def normalisieren(pcm: np.ndarray, ziel: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        with wave.open(f.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f.name, "-af",
                        "loudnorm=I=-18:TP=-1.5:LRA=11", "-ar", str(RATE), "-ac", "1", ziel], check=True)


def stuecke(pcm: np.ndarray, pause: float, mn: float, mx: float) -> list[tuple[int, int]]:
    """Kandidaten als (anfang, ende) in Samples: zusammenhängende Folgen von
    Sprachabschnitten zwischen Pausen, Gesamtlänge mn..mx Sekunden."""
    f = RATE // 50
    n = len(pcm) // f
    x = pcm[:n * f].astype(np.float64).reshape(n, f)
    db = 20 * np.log10(np.sqrt(np.mean(x ** 2, axis=1)) / 32768 + 1e-9)
    # Schwelle über dem Grundpegel, nicht absolut: Hörspiel-Proben haben
    # Hintergrund (gastone2: −30 bis −35 dB unter der Spitze, nie „still“).
    laut = db > np.percentile(db, 10) + 6
    abschnitte, i = [], 0
    while i < n:
        if not laut[i]:
            i += 1
            continue
        j = i
        stille = 0
        while j < n and stille * 0.02 < pause:
            stille = 0 if laut[j] else stille + 1
            j += 1
        abschnitte.append((i * f, (j - stille) * f))
        i = j
    rand = int(0.08 * RATE)
    kandidaten = []
    for a in range(len(abschnitte)):
        for b in range(a, len(abschnitte)):
            s, e = max(0, abschnitte[a][0] - rand), min(len(pcm), abschnitte[b][1] + rand)
            if mn <= (e - s) / RATE <= mx:
                kandidaten.append((s, e))
    return kandidaten


def zerlegen(a, p, lager_dir: str, kand_pfad: str) -> list[dict]:
    asr = LlamaCppAsrStt(p.stt_llamacpp_url, timeout=60, sprache="German")
    pcm = laden(a.probe)
    kand = stuecke(pcm, a.pause, a.min, a.max)
    print(f"{len(pcm) / RATE:.1f} s Probe → {len(kand)} Kandidaten", flush=True)
    eintraege = []
    for nr, (s, e) in enumerate(kand, 1):
        name = f"{a.name}_{nr:02d}"
        d = os.path.join(lager_dir, name)
        os.makedirs(d, exist_ok=True)
        normalisieren(pcm[s:e], os.path.join(d, "probe.wav"))
        text = (asr._anfrage(wav16k(pcm[s:e], RATE), None) or "").strip()
        with open(os.path.join(d, "probe.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        eintraege.append({"name": name, "von_s": round(s / RATE, 2), "bis_s": round(e / RATE, 2),
                          "dauer_s": round((e - s) / RATE, 2), "text": text})
        print(f"  {name} {s / RATE:5.1f}–{e / RATE:5.1f} s  {text}", flush=True)
    with open(kand_pfad, "w", encoding="utf-8") as f:
        json.dump(eintraege, f, ensure_ascii=False, indent=1)

    return eintraege


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("probe")
    ap.add_argument("--name", required=True)
    ap.add_argument("--ausgabe")
    ap.add_argument("--pause", type=float, default=0.08)
    ap.add_argument("--min", type=float, default=3.0)
    ap.add_argument("--max", type=float, default=9.0)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--neu", action="store_true",
                    help="neu zerlegen, auch wenn <ausgabe>/kandidaten.json schon da ist")
    a = ap.parse_args()
    aus = a.ausgabe or os.path.join(STIMMEN_CACHE_DIR, "_zerlegt", a.name)
    lager_dir = os.path.join(aus, "lager")
    os.makedirs(lager_dir, exist_ok=True)

    p = load_profile()
    kand_pfad = os.path.join(aus, "kandidaten.json")
    if os.path.exists(kand_pfad) and not a.neu:
        # Wiederaufnahme: Schnitt und (von Hand korrigierte) probe.txt bleiben,
        # nur fehlende Testsätze werden gerechnet.
        with open(kand_pfad, encoding="utf-8") as f:
            eintraege = json.load(f)
        for e in eintraege:
            with open(os.path.join(lager_dir, e["name"], "probe.txt"), encoding="utf-8") as f:
                e["text"] = f.read().strip()
        print(f"{len(eintraege)} Kandidaten aus {kand_pfad} (--neu zerlegt neu)", flush=True)
    else:
        eintraege = zerlegen(a, p, lager_dir, kand_pfad)
    if not eintraege:
        print("keine Kandidaten — --pause/--min/--max anpassen")
        return 1
    lager = StimmenLager(lager_dir, f"probe-{a.name}", p.antwort_tts_url, p.antwort_tts_model,
                         eintraege[0]["name"], timeout=60)
    tts = AntwortTts(lager, SpeachesState(), timeout=60)
    stand = {"fertig": 0}

    def rechnen() -> None:
        for e in eintraege:
            for i, t in enumerate(TESTSAETZE):
                ziel = os.path.join(aus, f"{e['name']}_t{i}.wav")
                if os.path.exists(ziel):
                    continue
                try:
                    roh, rate = tts._roh(t, e["name"])
                    with open(ziel, "wb") as f:
                        f.write(tts._wav_bytes(roh, rate))
                except Exception as ex:  # noqa: BLE001
                    print(f"⚠️  {e['name']} Satz {i}: {ex}", flush=True)
            stand["fertig"] += 1
        print("alle Testsätze gerechnet", flush=True)
    threading.Thread(target=rechnen, daemon=True).start()

    auswahl_pfad = os.path.join(aus, "auswahl.json")

    def auswahl() -> dict:
        try:
            with open(auswahl_pfad, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def seite() -> str:
        gut = auswahl()

        def audio(datei: str) -> str:
            if not os.path.exists(os.path.join(aus, datei)):
                return "<i>… rechnet</i>"
            return f'<audio controls preload="none" src="/a/{urllib.parse.quote(datei)}"></audio>'
        zeilen = []
        for e in eintraege:
            n = e["name"]
            knopf = "gut ✓" if gut.get(n) else "gut"
            zeilen.append(
                f'<div class="z{" g" if gut.get(n) else ""}"><div><b>{n}</b> {e["von_s"]}–{e["bis_s"]} s '
                f'({e["dauer_s"]} s) <button onclick="gut(\'{n}\')">{knopf}</button></div>'
                f'<div class="t">„{html.escape(e["text"])}“</div>'
                f'<div class="r"><span>Probe {audio(f"../lager/{n}/probe.wav")}</span>'
                + "".join(f"<span>Satz {i + 1} {audio(f'{n}_t{i}.wav')}</span>" for i in range(len(TESTSAETZE)))
                + "</div></div>")
        saetze = "".join(f"<li>{html.escape(t)}</li>" for t in TESTSAETZE)
        return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Probe zerlegen</title><style>body{{font-family:sans-serif;max-width:90rem;margin:1rem auto;padding:0 1rem}}
.z{{border-top:1px solid #ccc;padding:.5rem 0}}.g{{background:#eef6ee}}.t{{color:#555;margin:.2rem 0}}
.r{{display:flex;flex-wrap:wrap;gap:.6rem}}audio{{width:13rem;vertical-align:middle}}</style>
<h1>Probe „{html.escape(a.name)}“ zerlegt</h1><p>{len(eintraege)} Stücke, Testsätze gerechnet für
{stand['fertig']} davon (neu laden). Testsätze:</p><ol>{saetze}</ol>{"".join(zeilen)}
<script>async function gut(n){{await fetch("/gut",{{method:"POST",body:n}});location.reload();}}</script>"""

    class H(http.server.BaseHTTPRequestHandler):
        def _senden(self, code, daten, typ):
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(daten)))
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self):
            if self.path == "/":
                return self._senden(200, seite().encode(), "text/html; charset=utf-8")
            if self.path.startswith("/a/"):
                pfad = os.path.realpath(os.path.join(aus, urllib.parse.unquote(self.path[3:])))
                if pfad.startswith(os.path.realpath(aus)) and os.path.isfile(pfad):
                    with open(pfad, "rb") as f:
                        return self._senden(200, f.read(), "audio/wav")
            self._senden(404, b"weg", "text/plain")

        def do_POST(self):
            n = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if self.path == "/gut" and any(e["name"] == n for e in eintraege):
                g = auswahl()
                g[n] = not g.get(n)
                with open(auswahl_pfad, "w", encoding="utf-8") as f:
                    json.dump(g, f, indent=1)
                print(f"gut: {n} = {g[n]}", flush=True)
                return self._senden(200, b"{}", "application/json")
            self._senden(400, b"{}", "application/json")

        def log_message(self, *x):
            pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", a.port), H)
    print(f"Hörseite auf Port {a.port}, Ausgabe {aus}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
