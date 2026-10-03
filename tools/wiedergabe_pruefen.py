#!/usr/bin/env python3
"""Wiedergabe-Mitschnitte pruefen: kam beim Lautsprecher an, was gesendet wurde?

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.wiedergabe_pruefen                 # alle Mitschnitte
    ow-venv/bin/python -m tools.wiedergabe_pruefen --seit 2026-10-03T10:00
    ow-venv/bin/python -m tools.wiedergabe_pruefen --stt --bild    # + STT-Vergleich, Spektrogramme

Woher die Mitschnitte kommen
----------------------------
respeaker.mitschnitt: true im Profil legt bei jeder Wiedergabe unter
voice/wiedergabe/ ab: <stamm>_gesendet.wav (48 kHz Stereo, genau die Datei,
die der ESP geholt hat), <stamm>_kanal2.wav (16 kHz, zweiter Audiokanal
waehrenddessen) und <stamm>.json (Dauer, Anker, Ueberhang, Quelle).
Was im zweiten Kanal liegt, bestimmt respeaker.kanal2_quelle:

    referenz  das Signal, das der ESP an den Lautsprecher gibt, wie es der
              XVF3800 als Echo-Referenz sieht. Ist es hier schon kaputt, liegt
              der Fehler im ESP (Decoder, Puffer, Mixer) — nicht im Raum.
    roh       Rohmikrofon: was im Raum ankommt. Faengt auch Fehler hinter dem
              ESP (DAC, Verstaerker), dafuer mit Raumhall und Geraeusch.

Anlass: im Fablab-Test von ESPHome 2026.9.1 (2026-10-02) klang die Ausgabe
"teilweise verstuemmelt". Hoeren kann das Werkzeug nicht; es misst, was man
hoeren wuerde:

    Versatz   Kreuzkorrelation: wann setzt der Ton im Mitschnitt ein
    Luecken   20-ms-Fenster, in denen gesendet Sprache ist, im Mitschnitt
              aber (relativ zum Rest der Datei) fast nichts — Aussetzer
    Verzerrt  100-ms-Fenster mit Sprache, deren Korrelation zum Gesendeten
              weit unter dem Median der Datei liegt — Knacken, Stottern,
              verschobene Stuecke. Nur bei "referenz" belastbar; bei "roh"
              druecken Hall und Geraeusch die Korrelation ohnehin.
    STT       (--stt) beide Dateien durch die Speaches-STT des Profils,
              Wort-Uebereinstimmung. Verstuemmelte Sprache verhoert sich.
    Bild      (--bild) Spektrogramm gesendet ueber Mitschnitt als PNG neben
              die Dateien — zum Ansehen, wo es klemmt.

Die Schwellen sind Startwerte ohne Messreihe. Erst eine Aufnahme mit
bekannt sauberer Wiedergabe (Werkszustand) zeigt, wo die gesunden Werte
liegen; dann hier eintragen.

Messreihe
---------
(noch keine)
"""

from __future__ import annotations

import argparse
import difflib
import glob
import io
import json
import os
import re
import struct
import sys
import wave
import zlib

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.wiedergabe_pruefen", *sys.argv[1:]])

import numpy as np  # noqa: E402
from scipy.signal import resample_poly, stft  # noqa: E402

from voice_assistant.config import VOICE_DIR  # noqa: E402

_DIR = os.path.join(VOICE_DIR, "wiedergabe")
_RATE = 16000
_FENSTER_LUECKE = 0.020       # s
_FENSTER_KORR = 0.100         # s
_SPRACHE_DB = -35.0           # gesendet: Fenster gilt als Sprache ab Pegel ueber Dateimaximum
_LUECKE_DB = -20.0            # Mitschnitt: so weit unter dem fuer diese Datei typischen Verhaeltnis
_VERZERRT_ANTEIL = 0.5        # Korrelation unter Anteil x Median der Datei


def lies_mono16k(pfad: str) -> np.ndarray:
    with wave.open(pfad) as wf:
        rate, kan = wf.getframerate(), wf.getnchannels()
        roh = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    x = roh.astype(np.float32)
    if kan > 1:
        x = x.reshape(-1, kan)[:, 0]
    if rate != _RATE and len(x):
        from math import gcd
        g = gcd(rate, _RATE)
        x = resample_poly(x, _RATE // g, rate // g).astype(np.float32)
    return x - (x.mean() if len(x) else 0.0)


def versatz(gesendet: np.ndarray, mitschnitt: np.ndarray) -> tuple[int, float]:
    """Samples, um die der Mitschnitt spaeter beginnt, und die Spitzenkorrelation."""
    n = len(gesendet) + len(mitschnitt)
    nfft = 1 << (n - 1).bit_length()
    a = np.fft.rfft(mitschnitt, nfft)
    b = np.fft.rfft(gesendet, nfft)
    xc = np.fft.irfft(a * np.conj(b), nfft)
    xc = np.concatenate([xc[-(len(gesendet) - 1):], xc[:len(mitschnitt)]])
    i = int(np.argmax(np.abs(xc)))
    lag = i - (len(gesendet) - 1)
    norm = np.sqrt(np.sum(gesendet ** 2) * np.sum(mitschnitt ** 2)) or 1.0
    return lag, float(np.abs(xc[i]) / norm)


def _db(x: np.ndarray) -> np.ndarray:
    return 10 * np.log10(np.maximum(x, 1e-12))


def _intervalle(maske: np.ndarray, fenster: float) -> list[tuple[float, float]]:
    out, start = [], None
    for k, m in enumerate(list(maske) + [False]):
        if m and start is None:
            start = k
        elif not m and start is not None:
            out.append((round(start * fenster, 2), round(k * fenster, 2)))
            start = None
    return out


def analysiere(gesendet: np.ndarray, mitschnitt: np.ndarray) -> dict:
    if len(mitschnitt) == 0:
        return {"fehler": "Mitschnitt leer — zweiter Kanal kam nicht an"}
    lag, spitze = versatz(gesendet, mitschnitt)
    if lag >= 0:
        m = mitschnitt[lag:lag + len(gesendet)]
        g = gesendet[:len(m)]
    else:
        g = gesendet[-lag:-lag + len(mitschnitt)]
        m = mitschnitt[:len(g)]

    # Luecken: Energie je 20 ms
    w = int(_FENSTER_LUECKE * _RATE)
    k = len(g) // w
    eg = _db((g[:k * w].reshape(k, w) ** 2).mean(axis=1))
    em = _db((m[:k * w].reshape(k, w) ** 2).mean(axis=1))
    sprache = eg > eg.max() + _SPRACHE_DB
    verh = em - eg
    typisch = float(np.median(verh[sprache])) if sprache.any() else 0.0
    luecke = sprache & (verh < typisch + _LUECKE_DB)

    # Verzerrt: Korrelation je 100 ms
    w2 = int(_FENSTER_KORR * _RATE)
    k2 = len(g) // w2
    korr = np.zeros(k2)
    sprache2 = np.zeros(k2, dtype=bool)
    for j in range(k2):
        a, b = g[j * w2:(j + 1) * w2], m[j * w2:(j + 1) * w2]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        korr[j] = float(a @ b / (na * nb)) if na and nb else 0.0
        sprache2[j] = sprache[j * (w2 // w):(j + 1) * (w2 // w)].mean() > 0.5 if len(sprache) else False
    med = float(np.median(korr[sprache2])) if sprache2.any() else 0.0
    verzerrt = sprache2 & (korr < _VERZERRT_ANTEIL * med)

    return {
        "versatz_s": round(lag / _RATE, 3),
        "spitzenkorrelation": round(spitze, 3),
        "pegel_verhaeltnis_db": round(typisch, 1),
        "korrelation_median": round(med, 3),
        "luecken": _intervalle(luecke, _FENSTER_LUECKE),
        "verzerrt": _intervalle(verzerrt, _FENSTER_KORR),
        "sprache_s": round(sprache.sum() * _FENSTER_LUECKE, 2),
    }


def stt_vergleich(gesendet: str, mitschnitt: str) -> dict:
    from voice_assistant.config import load_profile
    from voice_assistant.services.speaches import SpeachesState
    from voice_assistant.services.stt import SpeachesStt, chunks_to_wav_bytes
    import contextlib
    p = load_profile()
    stt = SpeachesStt(SpeachesState(), p.speaches_base, p.speaches_stt_model)

    def text(pfad: str) -> str:
        x = lies_mono16k(pfad).astype(np.int16)
        with contextlib.redirect_stdout(io.StringIO()):
            wav = chunks_to_wav_bytes([x], normalize=True)
        return (stt.transcribe_raw(wav).get("text") or "").strip()

    a, b = text(gesendet), text(mitschnitt)
    wa = re.findall(r"\w+", a.lower())
    wb = re.findall(r"\w+", b.lower())
    return {"stt_gesendet": a, "stt_mitschnitt": b,
            "wort_uebereinstimmung": round(difflib.SequenceMatcher(None, wa, wb).ratio(), 2)}


def _png(pfad: str, bild: np.ndarray) -> None:
    """Graustufen-PNG ohne Bildbibliothek (bild: 2D uint8, oben = erste Zeile)."""
    h, w = bild.shape
    roh = b"".join(b"\x00" + bild[y].tobytes() for y in range(h))

    def chunk(typ: bytes, daten: bytes) -> bytes:
        return (struct.pack(">I", len(daten)) + typ + daten
                + struct.pack(">I", zlib.crc32(typ + daten) & 0xFFFFFFFF))
    with open(pfad, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(roh, 9)))
        fh.write(chunk(b"IEND", b""))


def spektrogramm(gesendet: np.ndarray, mitschnitt: np.ndarray, lag: int, pfad: str) -> None:
    def spek(x: np.ndarray) -> np.ndarray:
        _, _, z = stft(x, fs=_RATE, nperseg=512, noverlap=384)
        s = _db(np.abs(z) ** 2)
        s = np.clip((s - (s.max() - 70)) / 70, 0, 1)
        return (255 * (1 - s[::-1])).astype(np.uint8)      # dunkel = laut, tiefe Toene unten
    m = mitschnitt[max(lag, 0):]
    g = gesendet[max(-lag, 0):]
    n = min(len(g), len(m))
    a, b = spek(g[:n]), spek(m[:n])
    trenner = np.full((4, a.shape[1]), 128, dtype=np.uint8)
    _png(pfad, np.vstack([a, trenner, b]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=_DIR)
    ap.add_argument("--seit", help="nur Mitschnitte ab diesem Zeitpunkt (ISO, z.B. 2026-10-03T10:00)")
    ap.add_argument("--stt", action="store_true", help="beide Seiten durch die STT, Woerter vergleichen")
    ap.add_argument("--bild", action="store_true", help="Spektrogramm-PNG je Mitschnitt ablegen")
    ap.add_argument("--json", help="Ergebnis je Mitschnitt hierhin schreiben")
    args = ap.parse_args()

    seit = re.sub(r"[-:T]", "", args.seit)[:14].ljust(14, "0") if args.seit else None
    stämme = sorted(p[:-5] for p in glob.glob(os.path.join(args.dir, "*.json")))
    if seit:
        stämme = [s for s in stämme
                  if os.path.basename(s)[:15].replace("_", "") >= seit]
    if not stämme:
        print(f"Keine Mitschnitte in {args.dir}" + (f" seit {args.seit}" if args.seit else ""))
        return 1

    ergebnisse = []
    for stamm in stämme:
        meta = json.load(open(stamm + ".json", encoding="utf-8"))
        g = lies_mono16k(stamm + "_gesendet.wav")
        m = lies_mono16k(stamm + "_kanal2.wav")
        r = {"stamm": os.path.basename(stamm), "quelle": meta.get("kanal2_quelle"),
             "dauer_s": meta.get("dauer_s"), "ueberhang_s": meta.get("ueberhang_s"),
             **analysiere(g, m)}
        if args.stt and "fehler" not in r:
            r.update(stt_vergleich(stamm + "_gesendet.wav", stamm + "_kanal2.wav"))
        if args.bild and "fehler" not in r:
            spektrogramm(g, m, int(r["versatz_s"] * _RATE), stamm + "_spektrogramm.png")
            r["bild"] = stamm + "_spektrogramm.png"
        ergebnisse.append(r)

        if "fehler" in r:
            print(f"{r['stamm']}  {r['fehler']}")
            continue
        auffaellig = r["luecken"] or r["verzerrt"]
        print(f"{r['stamm']}  {r['quelle']:12s} {r['dauer_s']:5.1f}s  "
              f"Versatz {r['versatz_s']:+.2f}s  Korr {r['spitzenkorrelation']:.2f} "
              f"(Median 100ms {r['korrelation_median']:.2f})  "
              f"{'AUFFAELLIG' if auffaellig else 'ok'}")
        for a, b in r["luecken"]:
            print(f"    Luecke   {a:6.2f}–{b:.2f}s")
        for a, b in r["verzerrt"]:
            print(f"    verzerrt {a:6.2f}–{b:.2f}s")
        if "wort_uebereinstimmung" in r:
            print(f"    STT {r['wort_uebereinstimmung']:.2f}: gesendet  „{r['stt_gesendet']}“")
            print(f"             mitschnitt „{r['stt_mitschnitt']}“")
        if "bild" in r:
            print(f"    Bild: {r['bild']}  (oben gesendet, unten Mitschnitt)")

    n_auff = sum(1 for r in ergebnisse if r.get("luecken") or r.get("verzerrt"))
    n_leer = sum(1 for r in ergebnisse if "fehler" in r)
    print(f"\n{len(ergebnisse)} Mitschnitte, {n_auff} auffaellig, {n_leer} leer")
    if args.json:
        json.dump(ergebnisse, open(args.json, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
