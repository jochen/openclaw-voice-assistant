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
hoeren wuerde — ueber Huelle (5-ms-Pegel) und Spektrum, nicht ueber die
Wellenform (Grund: siehe unten, "Taktdrift"):

    Versatz   wann setzt der Ton im Mitschnitt ein (Huellen-Korrelation)
    Drift     wie gleichmaessig der Versatz ueber die Datei wandert. Eine
              glatte Drift ist ein Taktunterschied und hoerbar nichts.
    Sprung    Versatz-Aenderung zwischen benachbarten 0,5-s-Fenstern, die die
              Drift nicht erklaert: Audio fehlt oder ist doppelt. Vermutlich
              die Signatur von "verstuemmelt".
    Luecke    Huellen-Werte, bei denen gesendet Sprache ist, im Mitschnitt
              aber (relativ zum Rest der Datei) fast nichts — Aussetzer
    Verzerrt  100-ms-Fenster, deren Spektrum weit unter dem Median der Datei
              zum Gesendeten passt. Nur bei "referenz" belastbar.
    STT       (--stt) beide Dateien durch die Speaches-STT des Profils,
              Wort-Uebereinstimmung. Verstuemmelte Sprache verhoert sich.
    Bild      (--bild) Spektrogramm gesendet ueber Mitschnitt als PNG neben
              die Dateien — zum Ansehen, wo es klemmt.

Taktdrift: der Mitschnitt laeuft gegenueber der Datei ~0,4 % schneller —
gleichmaessig, ohne Spruenge. Dazu nimmt der i2s_audio-Fork jeden dritten
48-kHz-Frame ohne Tiefpass (Aliasing). Beides liess eine Wellenform-
Korrelation ueber 7 s auf ~0 fallen, obwohl die Huellen in jedem Fenster mit
0,95-1,00 uebereinstimmten und die STT wortgleich war. Die erste Fassung
meldete deshalb an einer sauberen Aufnahme reihenweise Befunde.

Messreihe
---------

    2026-10-03  Heim-ReSpeaker, ESPHome 2026.9.1, kanal2_quelle referenz (5,0).
                Jochen: "hoerte sich gut an, ohne jede Verstuemmelung" —
                die Nulllinie. 8 Wiedergaben (0,3-7,8 s), 0 auffaellig.
                Versatz 0,20-0,25 s, Drift -3,1 bis -4,0 ‰ (ab 1,9 s
                messbar), Huellen-Korrelation 0,99, Spektral-Median
                0,87-0,94 (Sprache), 0,57 beim Follow-up-Beep (Ton, unter
                der Mindestmenge). STT gesendet = Mitschnitt in 7 von 8,
                der achte ist der Beep ("Beep!" / "Oh!").
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
# Alle Vergleiche laufen ueber Huelle (5-ms-Pegel) und Spektrum, nicht ueber
# die Wellenform: der Mitschnitt laeuft gegenueber der Datei ~0,4 % schneller
# (gleichmaessige Taktdrift, gemessen 2026-10-03), und der i2s_audio-Fork nimmt
# jeden dritten 48-kHz-Frame ohne Tiefpass — beides zerstoert eine
# Wellenform-Korrelation, ohne dass man etwas hoert.
_HUELLE_S = 0.005             # s je Huellen-Wert
_VERLAUF_FENSTER_S = 0.5      # Fenster fuer den Versatz-Verlauf, Schritt die Haelfte
_VERLAUF_SUCHE_S = 0.1        # so weit wird je Fenster um den Grobversatz gesucht
_VERLAUF_MIN_KORR = 0.6       # Fenster mit schwaecherer Huellen-Korrelation zaehlen nicht
_SPRUNG_S = 0.015             # Abweichung von der Drift-Geraden, ab der ein Sprung gemeldet wird
_SPRACHE_DB = -35.0           # gesendet: Huellen-Wert gilt als Sprache ab Pegel ueber Dateimaximum
_LUECKE_DB = -20.0            # Mitschnitt: so weit unter dem fuer diese Datei typischen Verhaeltnis
_LUECKE_MIN_S = 0.015         # kuerzere Einbrueche sind Rauschen der Messung
_SPEKTRUM_FENSTER_S = 0.1     # Fenster fuer den Spektral-Vergleich
_VERZERRT_ABSTAND = 0.25      # spektrale Korrelation so weit unter dem Median der Datei
# Mindestmaterial fuer ein Urteil. Am 2026-10-03 schlug ohne diese Grenze der
# 0,3-s-Follow-up-Beep als "verzerrt" an (ein Ton, zwei Fenster — der Median
# der Datei ist dann kein Massstab), und eine 1,2-s-Ansage zeigte aus drei
# Punkten -10 ‰ Drift statt der ueberall sonst gemessenen -4 ‰.
_MIN_SPEKTRUM_FENSTER = 5
_MIN_VERLAUF_PUNKTE = 4


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


def _db(x: np.ndarray) -> np.ndarray:
    return 10 * np.log10(np.maximum(x, 1e-12))


def _huelle(x: np.ndarray) -> np.ndarray:
    w = int(_HUELLE_S * _RATE)
    k = len(x) // w
    return 10 * np.log10((x[:k * w].reshape(k, w) ** 2).mean(axis=1) + 1e-3)


def _intervalle(maske: np.ndarray, schritt: float, min_s: float = 0.0) -> list[tuple[float, float]]:
    out, start = [], None
    for k, m in enumerate(list(maske) + [False]):
        if m and start is None:
            start = k
        elif not m and start is not None:
            if (k - start) * schritt >= min_s - 1e-9:
                out.append((round(start * schritt, 2), round(k * schritt, 2)))
            start = None
    return out


def versatz_verlauf(eg: np.ndarray, em: np.ndarray) -> tuple[int, list[tuple[float, float, float]]]:
    """Grobversatz und Versatz je Fenster (in Huellen-Einheiten).

    Rueckgabe (grob, [(mitte, versatz, korrelation), ...]) — nur Fenster mit
    Sprache und ausreichend eindeutiger Lage.
    """
    a, b = eg - eg.mean(), em - em.mean()
    grob = int(np.argmax(np.correlate(b, a, "full"))) - (len(a) - 1)
    n = int(_VERLAUF_FENSTER_S / _HUELLE_S)
    such = int(_VERLAUF_SUCHE_S / _HUELLE_S)
    punkte = []
    for s0 in range(0, len(a) - n + 1, n // 2):
        if eg[s0:s0 + n].max() < eg.max() + _SPRACHE_DB:
            continue
        seg = a[s0:s0 + n]
        if seg.std() == 0:
            continue
        kurve = {}
        for d in range(-such, such + 1):
            b0 = s0 + grob + d
            if b0 < 0 or b0 + n > len(b) or b[b0:b0 + n].std() == 0:
                continue
            kurve[d] = float(np.corrcoef(seg, b[b0:b0 + n])[0, 1])
        if not kurve:
            continue
        d = max(kurve, key=kurve.get)
        if kurve[d] < _VERLAUF_MIN_KORR:
            continue
        # Unter-Raster-Lage per Parabel durch den Gipfel: die Huelle hat 5-ms-
        # Stufen, die Drift (~4 ms/s) aendert sich zwischen benachbarten
        # Fenstern aber nur um ~1 ms — ohne Verfeinerung waere sie unsichtbar.
        fein = 0.0
        if d - 1 in kurve and d + 1 in kurve:
            y0, y1, y2 = kurve[d - 1], kurve[d], kurve[d + 1]
            nenner = y0 - 2 * y1 + y2
            if nenner < 0:
                fein = float(np.clip(0.5 * (y0 - y2) / nenner, -0.5, 0.5))
        punkte.append((s0 + n / 2, grob + d + fein, round(kurve[d], 3)))
    return grob, punkte


def _drift(punkte: list) -> float:
    """Steigung des Versatz-Verlaufs (Huellen-Einheiten je Einheit), Theil-Sen.

    Ueber ALLE Punktpaare: robust gegen das Zittern einzelner Fenster
    (synthetisch +-10 ms), das die Drift je Schritt (~1 ms) ueberdeckt — ein
    Median nur benachbarter Steigungen lag dann bei 0. Ein echter Sprung
    verbiegt diese Schaetzung etwas; erkannt wird er trotzdem, weil die
    Sprungsuche benachbarte Fenster vergleicht (siehe analysiere).
    """
    if len(punkte) < 2:
        return 0.0
    t = np.array([p[0] for p in punkte])
    v = np.array([p[1] for p in punkte], dtype=float)
    return float(np.median([(v[j] - v[i]) / (t[j] - t[i])
                            for i in range(len(t)) for j in range(i + 1, len(t))]))


def analysiere(gesendet: np.ndarray, mitschnitt: np.ndarray) -> dict:
    if len(mitschnitt) == 0:
        return {"fehler": "Mitschnitt leer — zweiter Kanal kam nicht an"}
    eg, em = _huelle(gesendet), _huelle(mitschnitt)
    grob, punkte = versatz_verlauf(eg, em)
    steig = _drift(punkte) if len(punkte) >= _MIN_VERLAUF_PUNKTE else 0.0
    if punkte:
        tt = np.array([p[0] for p in punkte])
        vv = np.array([p[1] for p in punkte], dtype=float)
    else:
        tt, vv = np.array([0.0]), np.array([float(grob)])

    def lage(i: float) -> int:
        """Versatz an Huellen-Stelle i — entlang des GEMESSENEN Verlaufs
        (interpoliert), damit nach einem Sprung nicht der Rest der Datei
        falsch ausgerichtet ist."""
        return int(round(float(np.interp(i, tt, vv))))

    # Spruenge: Versatz-Aenderung zwischen benachbarten Fenstern, die nicht
    # durch die Drift erklaert ist — fehlendes oder doppeltes Audio. Eine
    # glatte Drift ist nur ein Taktunterschied und hoerbar nichts.
    spruenge = []
    for (t0, v0, _), (t1, v1, _) in zip(punkte, punkte[1:]):
        rest = (v1 - v0) - steig * (t1 - t0)
        if abs(rest) * _HUELLE_S > _SPRUNG_S:
            spruenge.append((round((t0 + t1) / 2 * _HUELLE_S, 2), int(round(rest * _HUELLE_S * 1000))))

    # Luecken: Huelle entlang der Geraden vergleichen
    sprache = eg > eg.max() + _SPRACHE_DB
    verh = np.full(len(eg), np.nan)
    for i in range(len(eg)):
        j = i + lage(i)
        if 0 <= j < len(em):
            verh[i] = em[j] - eg[i]
    gueltig = sprache & ~np.isnan(verh)
    typisch = float(np.median(verh[gueltig])) if gueltig.any() else 0.0
    luecke = gueltig & (verh < typisch + _LUECKE_DB)

    # Verzerrt: log-Spektren je 100 ms entlang der Geraden
    hop = 2 * int(_HUELLE_S * _RATE)              # 10 ms = 2 Huellen-Werte
    _, _, zg = stft(gesendet, fs=_RATE, nperseg=400, noverlap=400 - hop, boundary=None)
    _, _, zm = stft(mitschnitt, fs=_RATE, nperseg=400, noverlap=400 - hop, boundary=None)
    oben = int(7500 / (_RATE / 400))              # Bins bis 7,5 kHz
    sg, sm = _db(np.abs(zg[:oben]) ** 2), _db(np.abs(zm[:oben]) ** 2)
    w = int(round(_SPEKTRUM_FENSTER_S * _RATE / hop))
    korr, ist_sprache = [], []
    for h in range(0, sg.shape[1] - w + 1, w):
        i = 2 * h
        h2 = h + int(round(lage(i) / 2))
        if h2 < 0 or h2 + w > sm.shape[1]:
            korr.append(np.nan)
            ist_sprache.append(False)
            continue
        korr.append(float(np.corrcoef(sg[:, h:h + w].ravel(), sm[:, h2:h2 + w].ravel())[0, 1]))
        ist_sprache.append(bool(sprache[i:i + 2 * w].mean() > 0.5))
    korr, ist_sprache = np.array(korr), np.array(ist_sprache)
    med = float(np.nanmedian(korr[ist_sprache])) if ist_sprache.any() else 0.0
    verzerrt = ist_sprache & (korr < med - _VERZERRT_ABSTAND)
    if ist_sprache.sum() < _MIN_SPEKTRUM_FENSTER:
        verzerrt[:] = False

    return {
        "versatz_s": round(lage(0) * _HUELLE_S, 3),
        "drift_promille": round(steig * 1000, 2) if len(punkte) >= _MIN_VERLAUF_PUNKTE else None,
        "huelle_korr": round(float(np.median([p[2] for p in punkte])), 3) if punkte else None,
        "verlauf_fenster": len(punkte),
        "spektral_median": round(med, 3),
        "pegel_verhaeltnis_db": round(typisch, 1),
        "spruenge": spruenge,
        "luecken": _intervalle(luecke, _HUELLE_S, _LUECKE_MIN_S),
        "verzerrt": _intervalle(verzerrt, _SPEKTRUM_FENSTER_S),
        "sprache_s": round(float(sprache.sum()) * _HUELLE_S, 2),
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
        auffaellig = r["luecken"] or r["verzerrt"] or r["spruenge"]
        print(f"{r['stamm']}  {r['quelle']:12s} {r['dauer_s']:5.1f}s  "
              f"Versatz {r['versatz_s']:+.2f}s  Drift "
              f"{'—' if r['drift_promille'] is None else format(r['drift_promille'], '+.1f') + '‰'}  "
              f"Huelle {r['huelle_korr']}  Spektrum {r['spektral_median']:.2f}  "
              f"{'AUFFAELLIG' if auffaellig else 'ok'}")
        for t, ms in r["spruenge"]:
            print(f"    Sprung   bei {t:6.2f}s um {ms:+d} ms neben der Drift-Geraden")
        for a, b in r["luecken"]:
            print(f"    Luecke   {a:6.2f}–{b:.2f}s")
        for a, b in r["verzerrt"]:
            print(f"    verzerrt {a:6.2f}–{b:.2f}s")
        if "wort_uebereinstimmung" in r:
            print(f"    STT {r['wort_uebereinstimmung']:.2f}: gesendet  „{r['stt_gesendet']}“")
            print(f"             mitschnitt „{r['stt_mitschnitt']}“")
        if "bild" in r:
            print(f"    Bild: {r['bild']}  (oben gesendet, unten Mitschnitt)")

    n_auff = sum(1 for r in ergebnisse if r.get("luecken") or r.get("verzerrt") or r.get("spruenge"))
    n_leer = sum(1 for r in ergebnisse if "fehler" in r)
    print(f"\n{len(ergebnisse)} Mitschnitte, {n_auff} auffaellig, {n_leer} leer")
    if args.json:
        json.dump(ergebnisse, open(args.json, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
