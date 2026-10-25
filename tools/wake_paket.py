#!/usr/bin/env python3
"""Trainingspaket für eine Nachtrainingsrunde des Wakeworts bauen und messen.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.wake_paket bauen  --vorrunde ~/wake-pakete/gaston_r5_paket \
        --out ~/wake-pakete/gaston_r6_paket
    ow-venv/bin/python -m tools.wake_paket messen --paket ~/wake-pakete/gaston_r6_paket
    ow-venv/bin/python -m tools.wake_paket messen --paket … --modell <bundle-verzeichnis>

Entstanden für Runde 6 (2026-10-25). In Runde 5 entstanden die Zusätze
(Auto-Negative, Timer-Sätze, eigene Val-Gruppen) von Hand in einem Skript,
das danach weg war; die Regeln standen nur im Paket-Manifest. Dieses Werkzeug
hält sie fest. Die Schranke, gegen die gemessen wird, steht in
WAKEWORD_PROCESS.md („Nachtraining Runde 6 — Schranke“), nicht hier.

Was ``bauen`` festschreibt
--------------------------
1. DIE VAL-SEITE DER VORRUNDE BLEIBT UNVERÄNDERT (``val_vorrunde/``, Kopie
   aus ``--vorrunde``). Nur so bleibt die Vorher-Zahl des laufenden Modells
   gültig: es hat diese Tage nie gesehen, das neue Modell auch nicht. Clips,
   die seither an diesen Tagen dazugekommen sind, gehen nirgendwohin
   (``an_vorrunde_val_tagen_ausgelassen`` im Manifest).
2. NEUE TAGE (ab ``--neu-ab``) werden nach Seed in ganze Tage geteilt,
   Ziel ``--val-anteil``, wie in ``wake_corpus paket`` (gleiche Begründung:
   Wiederholungs-Cluster). Ältere Tage gehen ins Training.
3. POSITIVE INS TRAINING NUR MIT HÖRBAREM WAKEWORT. Im geschnittenen Stück
   muss medium ODER Qwen (ohne Kontext) eine Gaston-Form hören, und eine
   Ohr-Notiz darf kein Geräusch und keine Musik nennen. Sonst
   ``val/positive_zweifel`` — gemessen, nicht trainiert. Anlass (Jochen,
   2026-10-25): viele gehörte „echte Rufe“ sind zweifelhaft, mit Geräuschen
   davor und danach; ein Klopfer im 1,8-s-Fenster lehrte das Modell, dass
   Klopfen zu „Gaston“ gehört. Die Val-Positive der neuen Tage bleiben
   ungefiltert: sie messen den Alltag, nicht die Auswahl.
4. GERÄUSCH-FEHLTRIGGER bekommen in Val eine eigene Gruppe
   (``val/negative_geraeusch``): Ohr-``herkunft`` geraeusch oder ein harter
   Fehltrigger im ruhigen Raum. Im Training sind sie gewöhnliche Negative.
5. AUTO-NEGATIVE nach der Regel von Runde 5, für Near-Misses nach dem Paket
   der Vorrunde: Peak ≥ 0,5, nur STT-Label (kein Ohr, kein Selbst), weder
   medium noch Qwen (mit Wakewort als Kontext, also strenger) hören eine
   Gaston-Form in Clip oder 6-s-Folge, Torfrage des Schattens „nein“. Die
   Auto-Negative der Vorrunde (``train/negative/auto_*``) werden übernommen,
   außer ein Clip hat seither ein Ohr-/Selbst-Label „echter Ruf“. Der Raum
   wird mitgeschrieben (``raum`` im Manifest): Kandidaten aus dem ruhigen
   Raum sind das Klapper-Material.
6. AUTO-POSITIVE DER VORRUNDE ENTFALLEN. Regel dort war „STT hört eine
   Gaston-Form“; 2 von 14 waren Gerede („Gastraum“, „Gastein“), und an
   Ohr-Rufen fehlt es nicht mehr.
7. TIMER-SÄTZE wie in der Vorrunde: deren ``train/positive/timer_*`` ins
   Training, ``val/positive_timer`` liegt in ``val_vorrunde/``.

Ruhiger Raum: gleitender Raum-Median unter 4 × Ruhe (``umgebung`` im
wake_events.log, seit 2026-10-17), für ältere Trigger der Median der
1-s-Fenster im ``*_vorlauf.wav`` ohne die letzten 3 s gegen Ruhe 24.

Was ``messen`` zählt
--------------------
Je Val-Gruppe, wie viele Clips ein Modell auslöst (``BundleScorer``, mehrere
Frame-Offsets, Live-Gate des Manifests). ``--modell`` nimmt ein
Bundle-Verzeichnis mit manifest.yaml (z. B. das neue Modell aus dem Fablab
neben dem alten Manifest). ``--paare`` zeigt je Gruppe die Clips, die
zwischen zwei Modellen kippen. Grenze: wie ``wake_corpus messen`` sieht das
die Kurz-Streak-Pfade nicht, und FP/h kommt aus ``eval_debounce.py`` im
Fablab.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime

_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.wake_paket", *sys.argv[1:]])

from voice_assistant.config import TRIGGER_AUDIO_DIR, WAKE_LOG_PATH  # noqa: E402
from tools.wake_corpus import (  # noqa: E402
    _WAKE_FORM, _korpus_dateien, _manifest_lesen, _positiv_stuecke,
    _wav_lesen, _wav_schreiben,
)
from tools.wake_rms_replay import _lade_review, labels_fuer_clips  # noqa: E402

WORKSPACE = os.path.dirname(WAKE_LOG_PATH)
SHADOW_PATH = os.path.join(WORKSPACE, "nearmiss_shadow.jsonl")
RUHE_DEFAULT = 24.0
RAUM_FAKTOR = 4.0
AUTO_PEAK_MIN = 0.5
ZWEIFEL_NOTIZ = ("klopf", "klapper", "geräusch", "gerausch", "musik", "knall", "abgestellt")


def _tag(audio: str) -> str:
    return audio[:8]


def _hoert_gaston(text: str | None) -> bool:
    for w in (text or "").lower().split():
        if w.strip(",.!?-:;„“\"'").startswith(_WAKE_FORM):
            return True
    return False


class _Ohren:
    """medium (Speaches, ohne Filter) und Qwen, je mit Cache je Datei+Schnitt."""

    def __init__(self):
        from voice_assistant.config import load_profile
        from voice_assistant.services.speaches import SpeachesState
        from voice_assistant.services.stt import LlamaCppAsrStt, SpeachesStt
        prof = load_profile()
        self.medium = SpeachesStt(SpeachesState(), prof.speaches_base, prof.speaches_stt_model)
        self.qwen = LlamaCppAsrStt(prof.stt_llamacpp_url, sprache=prof.stt_llamacpp_sprache or None)
        self.qwen_kontext = LlamaCppAsrStt(prof.stt_llamacpp_url, kontext=lambda: "Gaston",
                                           sprache=prof.stt_llamacpp_sprache or None)

    @staticmethod
    def _wav(roh: bytes, rate: int, breite: int, kan: int) -> bytes:
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(kan); w.setsampwidth(breite); w.setframerate(rate); w.writeframes(roh)
        return buf.getvalue()

    def texte(self, roh, rate, breite, kan, mit_kontext: bool) -> dict:
        wav = self._wav(roh, rate, breite, kan)
        try:
            m = (self.medium.transcribe_raw(wav).get("text") or "").strip()
        except Exception as e:
            m = None
            print(f"  ⚠️  medium: {e}")
        q = (self.qwen_kontext if mit_kontext else self.qwen).transcribe(wav)
        return {"medium": m, "qwen": q}


def _ruhe_und_raum(ev: dict, audio: str) -> tuple[float | None, bool | None]:
    """(Raum-Pegel, ruhig?) aus dem Log oder dem Vorlauf; None = unbekannt."""
    u = ev.get("umgebung") or {}
    if u.get("gleitend") is not None:
        ruhe = float(u.get("ruhe") or RUHE_DEFAULT)
        return float(u["gleitend"]), float(u["gleitend"]) < RAUM_FAKTOR * ruhe
    vl = os.path.join(TRIGGER_AUDIO_DIR, audio.replace("_wake.wav", "_vorlauf.wav"))
    if audio.endswith("_wake.wav") and os.path.exists(vl):
        import numpy as np
        from wakeword_studio.scoring import load_wav_16k
        x = load_wav_16k(vl).astype(np.float32)[:-3 * 16000]
        if len(x) >= 16000:
            r = [float(np.sqrt(np.mean(x[i * 16000:(i + 1) * 16000] ** 2))) for i in range(len(x) // 16000)]
            raum = float(np.median(r))
            return raum, raum < RAUM_FAKTOR * RUHE_DEFAULT
    return None, None


def _wake_events() -> dict[str, dict]:
    out = {}
    with open(WAKE_LOG_PATH, encoding="utf-8") as fh:
        for z in fh:
            if z.strip():
                r = json.loads(z)
                if r.get("result") in ("trigger", "nearmiss") and r.get("audio"):
                    out[r["audio"]] = r
    return out


def _shadow() -> dict[str, dict]:
    out = {}
    if os.path.exists(SHADOW_PATH):
        with open(SHADOW_PATH, encoding="utf-8") as fh:
            for z in fh:
                if z.strip():
                    r = json.loads(z)
                    out[r.get("nearmiss_audio", "")] = r
    return out


def run_bauen(args) -> int:
    from tools.verifier_probe import _split_days

    vor = os.path.expanduser(args.vorrunde)
    ziel = os.path.expanduser(args.out)
    if os.path.exists(ziel):
        print(f"Zielverzeichnis existiert schon: {ziel} — erst wegräumen.")
        return 1
    with open(os.path.join(vor, "paket_manifest.json"), encoding="utf-8") as fh:
        vor_manifest = json.load(fh)
    vor_val_tage = set(vor_manifest["val_tage"])
    vor_erstellt = vor_manifest["erstellt"]

    korpus = _korpus_dateien()
    kmanifest = _manifest_lesen()
    review = _lade_review()
    labels = labels_fuer_clips()
    events = _wake_events()
    shadow = _shadow()
    ohren = _Ohren()

    pos = sorted(a for a in korpus if kmanifest.get(a, {}).get("klasse") == "echter_ruf")
    neg = sorted(a for a in korpus if kmanifest.get(a, {}).get("klasse") == "rauschen")

    # Split: Vorrunden-Val-Tage fest, neue Tage nach Seed, Rest Training.
    neue_pos = [a for a in pos if _tag(a) >= args.neu_ab]
    neue_neg = [a for a in neg if _tag(a) >= args.neu_ab]
    neu_val, neu_train = _split_days(neue_pos, neue_neg, args.seed, args.val_anteil)

    def seite(audio: str) -> str | None:
        t = _tag(audio)
        if t in vor_val_tage:
            return None
        if t >= args.neu_ab:
            return "val" if t in neu_val else "train"
        return "train"

    os.makedirs(ziel)
    # 1. Val der Vorrunde unverändert
    shutil.copytree(os.path.join(vor, "val"), os.path.join(ziel, "val_vorrunde"))

    zaehl = Counter()
    info: dict[str, dict] = {}
    ausgelassen = []

    def kopiere(quelle: str, rel: str, meta: dict) -> None:
        dst = os.path.join(ziel, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(quelle, dst)
        zaehl[os.path.dirname(rel)] += 1
        info[rel] = meta

    # 2./3. Positive aus dem Korpus
    for n, audio in enumerate(pos, 1):
        s = seite(audio)
        if s is None:
            ausgelassen.append(audio)
            continue
        quelle = korpus[audio]
        notiz = (review.get(audio, {}).get("notiz") or "").lower()
        _, rate, breite, kan = _wav_lesen(quelle)
        for zusatz, pcm, schnitt in _positiv_stuecke(quelle, ohren.medium):
            name = os.path.splitext(audio)[0] + zusatz + ".wav"
            meta = {"quelle": labels.get(audio, {}).get("quelle"), **schnitt}
            if s == "val":
                rel = os.path.join("val", "positive", name)
            else:
                t = ohren.texte(pcm, rate, breite, kan, mit_kontext=False)
                meta.update(t)
                gut = (_hoert_gaston(t["medium"]) or _hoert_gaston(t["qwen"])) \
                    and not any(k in notiz for k in ZWEIFEL_NOTIZ)
                meta["zweifel"] = not gut
                rel = os.path.join("train" if gut else "val", "positive" if gut else "positive_zweifel", name)
            dst = os.path.join(ziel, rel)
            _wav_schreiben(dst, pcm, rate, breite, kan)
            zaehl[os.path.dirname(rel)] += 1
            info[rel] = meta
        if n % 50 == 0:
            print(f"  Positive {n}/{len(pos)}", flush=True)

    # 4. Negative aus dem Korpus, Geräusch-Gruppe in Val
    for audio in neg:
        s = seite(audio)
        if s is None:
            ausgelassen.append(audio)
            continue
        ev = events.get(audio, {})
        raum, ruhig = _ruhe_und_raum(ev, audio)
        herkunft = review.get(audio, {}).get("herkunft")
        geraeusch = herkunft == "geraeusch" or (audio.endswith("_wake.wav") and ruhig is True)
        gruppe = "negative_geraeusch" if (s == "val" and geraeusch) else "negative"
        kopiere(korpus[audio], os.path.join(s, gruppe, audio),
                {"quelle": labels.get(audio, {}).get("quelle"), "herkunft": herkunft,
                 "raum": raum, "ruhig": ruhig, "geraeusch": geraeusch})

    # 5. Auto-Negative: Vorrunde übernehmen …
    for f in sorted(os.listdir(os.path.join(vor, "train", "negative"))):
        if not f.startswith("auto_"):
            continue
        audio = f[len("auto_"):]
        l = labels.get(audio, {})
        if l.get("quelle") in ("ohr", "selbst") and l.get("klasse") == "echter_ruf":
            info[f"verworfen/{f}"] = {"grund": f"seit der Vorrunde {l['quelle']}: echter Ruf"}
            continue
        kopiere(os.path.join(vor, "train", "negative", f), os.path.join("train", "negative", f),
                {"herkunft": "vorrunde_auto"})
    # … und neue nach derselben Regel
    grenze = vor_erstellt.replace("-", "").replace("T", "_").replace(":", "")[:15]
    kandidaten = [a for a, e in events.items()
                  if e.get("result") == "nearmiss" and a[:15] >= grenze
                  and (e.get("peak") or 0) >= AUTO_PEAK_MIN
                  and labels.get(a, {}).get("quelle") == "stt"
                  and labels.get(a, {}).get("klasse") == "rauschen"
                  and os.path.exists(os.path.join(TRIGGER_AUDIO_DIR, a))]
    print(f"  Auto-Negativ-Kandidaten: {len(kandidaten)}", flush=True)
    for n, audio in enumerate(sorted(kandidaten), 1):
        s = seite(audio)
        if s is None:
            continue
        sh = shadow.get(audio)
        if not sh or sh.get("tor_ja") is not False:
            continue
        texte = {}
        for teil, datei in (("clip", audio), ("folge", audio.replace(".wav", "_folge.wav"))):
            pfad = os.path.join(TRIGGER_AUDIO_DIR, datei)
            if not os.path.exists(pfad):
                continue
            roh, rate, breite, kan = _wav_lesen(pfad)
            texte[teil] = ohren.texte(roh, rate, breite, kan, mit_kontext=True)
        if "clip" not in texte or any(_hoert_gaston(t) for x in texte.values() for t in x.values()):
            continue
        raum, ruhig = _ruhe_und_raum(events[audio], audio)
        kopiere(os.path.join(TRIGGER_AUDIO_DIR, audio),
                os.path.join(s, "negative" if s == "train" else "negative_auto", "auto_" + audio),
                {"herkunft": "auto", "peak": events[audio].get("peak"), "raum": raum,
                 "ruhig": ruhig, "texte": texte})
        if n % 50 == 0:
            print(f"  Auto-Kandidaten {n}/{len(kandidaten)}", flush=True)

    # 7. Timer-Sätze der Vorrunde
    for f in sorted(os.listdir(os.path.join(vor, "train", "positive"))):
        if f.startswith("timer_"):
            kopiere(os.path.join(vor, "train", "positive", f), os.path.join("train", "positive", f),
                    {"herkunft": "vorrunde_timer"})

    paket_manifest = {
        "erstellt": datetime.now().isoformat(timespec="seconds"),
        "werkzeug": "tools/wake_paket.py bauen",
        "vorrunde": vor, "vorrunde_erstellt": vor_erstellt,
        "seed": args.seed, "val_anteil_ziel": args.val_anteil, "neu_ab": args.neu_ab,
        "val_tage_vorrunde": sorted(vor_val_tage),
        "val_tage_neu": sorted(neu_val), "train_tage_neu": sorted(neu_train),
        "zaehlung": dict(sorted(zaehl.items())),
        "an_vorrunde_val_tagen_ausgelassen": ausgelassen,
        "clips": info,
    }
    with open(os.path.join(ziel, "paket_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(paket_manifest, fh, ensure_ascii=False, indent=1)
    print(f"\nPaket: {ziel}")
    for k, v in sorted(zaehl.items()):
        print(f"  {k:28s} {v:4d}")
    print(f"  Val-Tage neu: {sorted(neu_val)}")
    print(f"  an Vorrunden-Val-Tagen ausgelassen: {len(ausgelassen)}")
    return 0


def run_messen(args) -> int:
    from wakeword_studio.scoring import BundleScorer
    paket = os.path.expanduser(args.paket)
    modelle = [os.path.expanduser(m) for m in (args.modell or ["gaston"])]
    gruppen = []
    for seite in ("val_vorrunde", "val"):
        wurzel = os.path.join(paket, seite)
        if os.path.isdir(wurzel):
            gruppen += [os.path.join(seite, g) for g in sorted(os.listdir(wurzel))]
    ergebnis: dict[str, dict[str, set]] = {}
    for m in modelle:
        sc = BundleScorer(m)
        print(f"Modell {m}: threshold={sc.threshold}, min_hits={sc.min_hits}, min_peak={sc.min_peak}")
        ergebnis[m] = {}
        for g in gruppen:
            dateien = sorted(f for f in os.listdir(os.path.join(paket, g)) if f.endswith(".wav"))
            ergebnis[m][g] = {f for f in dateien if sc.score_wav(os.path.join(paket, g, f))["triggered"]}
            ergebnis[m][g + "#n"] = set(dateien)
    print()
    # Bundle-Verzeichnisse heißen alle wie das Bundle (…/v4/gaston): dann den Ordner darüber zeigen.
    def _name(m: str) -> str:
        if not os.path.isdir(m):
            return m
        teile = os.path.normpath(m).split(os.sep)
        return "/".join(teile[-2:])
    kopf = "".join(f"{_name(m)[-14:]:>16s}" for m in modelle)
    print(f"{'Gruppe':34s}{'n':>5s}{kopf}   (Anzahl ausgelöst)")
    for g in gruppen:
        n = len(ergebnis[modelle[0]][g + "#n"])
        print(f"{g:34s}{n:5d}" + "".join(f"{len(ergebnis[m][g]):16d}" for m in modelle))
    if args.paare and len(modelle) == 2:
        a, b = modelle
        print("\nKippende Clips (nur erstes / nur zweites Modell löst aus):")
        for g in gruppen:
            nur_a = sorted(ergebnis[a][g] - ergebnis[b][g])
            nur_b = sorted(ergebnis[b][g] - ergebnis[a][g])
            if nur_a or nur_b:
                print(f"  {g}: nur 1. {nur_a}  nur 2. {nur_b}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bauen", help="Paket bauen (Vorrunden-Val fest, neue Tage geteilt)")
    p.add_argument("--vorrunde", required=True, help="Paketverzeichnis der Vorrunde (mit paket_manifest.json)")
    p.add_argument("--out", required=True)
    p.add_argument("--neu-ab", dest="neu_ab", default="20261011", help="erster Tag nach der Vorrunde (JJJJMMTT)")
    p.add_argument("--seed", type=int, default=20260916)
    p.add_argument("--val-anteil", dest="val_anteil", type=float, default=0.3)
    p.set_defaults(fn=run_bauen)
    p = sub.add_parser("messen", help="Val-Gruppen eines Pakets gegen ein oder zwei Modelle")
    p.add_argument("--paket", required=True)
    p.add_argument("--modell", action="append", help="Bundle-Name oder -Verzeichnis (mehrfach möglich)")
    p.add_argument("--paare", action="store_true", help="bei zwei Modellen die kippenden Clips zeigen")
    p.set_defaults(fn=run_messen)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
