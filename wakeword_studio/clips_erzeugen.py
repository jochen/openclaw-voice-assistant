#!/usr/bin/env python3
"""Synthetische Clips für ein neues Wakewort, mit Piper-Stimmen.

Läuft auf dem GPU-Rechner im venv der Trainingsumgebung (``gpu_einrichten.sh``).
Alles Wortspezifische steht in einer YAML (Vorlage: ``clips_vorlage.yaml``):
Schreibweisen des Worts mit Anteil und Stimmen, ähnlich klingende Negative,
Mengen. Anleitung: ``WAKEWORD_TRAINING.md``, Teil 1.

Drei Schritte, in dieser Reihenfolge:

    python clips_erzeugen.py hoerproben clips.yaml     # je Schreibweise × Stimme 4 Clips
    python clips_erzeugen.py alle clips.yaml           # ~66k Clips, ~1 h auf 4 Kernen
    python clips_erzeugen.py pruefen clips.yaml runden/r1/synth/l1/NAME.onnx

``hoerproben`` zuerst: ein Mensch hört, welche Schreibweise und welche Stimme
das Wort so sagt, wie es im Haus gesagt wird, und streicht den Rest. Eine
schlechte Stimme im Pool erkennt das Modell später nicht, und sie verdünnt
die guten (gaston: die gedrosselte ``kerstin`` und ``mls`` erkannte das erste
Modell kaum).

``alle`` schreibt ins Layout, das ``train.py`` erwartet:
``train_out/<modell>/{positive,negative}_{train,test}/``, alles 16 kHz (Piper
medium/high liefert 22,05 kHz, das augment von openWakeWord verlangt 16 kHz).
Dateinamen: ``<variante>__<stimme>__<teil>_<n>.wav``, daran gruppiert ``pruefen``.

``pruefen`` misst nach dem ersten Training die Erkennung je Schreibweise und
Stimme auf rohen Test-Clips. Fällt eine Gruppe weit ab, ist meist die Stimme
schuld, nicht das Modell.

Fehlende Stimmen werden von Hugging Face geladen (rhasspy/piper-voices).
"""

from __future__ import annotations

import argparse
import glob
import multiprocessing as mp
import os
import shutil
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "3")

KLASSEN = ("positive_train", "positive_test", "negative_train", "negative_test")
STIMMEN_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{familie}/{sprache}/{name}/{qualitaet}/{stimme}.onnx"


def config_lesen(pfad: str) -> dict:
    import yaml
    cfg = yaml.safe_load(Path(pfad).read_text())
    for p in cfg["positiv"]:
        p.setdefault("phoneme", False)
    summe = sum(p["anteil"] for p in cfg["positiv"])
    if abs(summe - 1.0) > 1e-6:
        raise ValueError(f"Anteile der Schreibweisen ergeben {summe}, nicht 1")
    return cfg


def stimme_url(stimme: str) -> str:
    """``de_DE-thorsten_emotional-medium`` -> URL im piper-voices-Repo."""
    sprache, rest = stimme.split("-", 1)
    name, qualitaet = rest.rsplit("-", 1)
    return STIMMEN_URL.format(familie=sprache.split("_")[0], sprache=sprache,
                              name=name, qualitaet=qualitaet, stimme=stimme)


def stimmen_holen(cfg: dict, ordner: Path) -> None:
    ordner.mkdir(parents=True, exist_ok=True)
    alle = {s for p in cfg["positiv"] for s in p["stimmen"]} | set(cfg["negativ"]["stimmen"])
    for s in sorted(alle):
        for anhang in ("", ".json"):          # Modell und Pflicht-Sidecar
            ziel = ordner / f"{s}.onnx{anhang}"
            if not ziel.exists():
                print(f"lade {ziel.name}", flush=True)
                urllib.request.urlretrieve(stimme_url(s) + anhang, ziel)


def auftraege(cfg: dict, ordner: Path, ziel: Path) -> list[dict]:
    """Ein Auftrag je (Schreibweise oder negativ) × Stimme × Teil."""
    jobs = []
    for teil in ("train", "test"):
        n_pos = cfg["anzahl"][f"positive_{teil}"]
        for p in cfg["positiv"]:
            je = int(n_pos * p["anteil"]) // len(p["stimmen"])
            for stimme, tempi in p["stimmen"].items():
                jobs.append(dict(tag=f"{p['name']}__{stimme}__{teil}", ziel=ziel / f"positive_{teil}",
                                 text=p["text"], phoneme=p["phoneme"], stimme=ordner / f"{stimme}.onnx",
                                 n=je, tempi=tuple(tempi)))
        n_neg = cfg["anzahl"][f"negative_{teil}"]
        neg = cfg["negativ"]
        je = n_neg // len(neg["stimmen"])
        for stimme, tempi in neg["stimmen"].items():
            jobs.append(dict(tag=f"neg__{stimme}__{teil}", ziel=ziel / f"negative_{teil}",
                             text=list(neg["texte"]), phoneme=False, stimme=ordner / f"{stimme}.onnx",
                             n=je, tempi=tuple(tempi)))
    return jobs


def _sprechen(job: dict, rauschen: dict, tmp: Path) -> tuple[str, int, float]:
    from piper_sample_generator.__main__ import generate_samples_onnx
    t0 = time.time()
    d = tmp / job["tag"]
    d.mkdir(parents=True, exist_ok=True)
    job["ziel"].mkdir(parents=True, exist_ok=True)
    generate_samples_onnx(text=job["text"], output_dir=str(d), model=str(job["stimme"]),
                          max_samples=job["n"], phoneme_input=job["phoneme"],
                          length_scales=job["tempi"],
                          noise_scales=tuple(rauschen["noise_scales"]),
                          noise_scale_ws=tuple(rauschen["noise_scale_ws"]))
    n = 0
    for f in sorted(d.glob("*.wav")):
        _auf_16k(f, job["ziel"] / f"{job['tag']}_{f.name}")
        f.unlink()
        n += 1
    shutil.rmtree(d, ignore_errors=True)
    return job["tag"], n, time.time() - t0


def _auf_16k(quelle: Path, ziel: Path) -> None:
    import numpy as np
    import scipy.signal
    import soundfile as sf
    daten, sr = sf.read(str(quelle), dtype="float32", always_2d=True)
    mono = daten.mean(axis=1)
    if sr != 16000:
        g = np.gcd(sr, 16000)
        mono = scipy.signal.resample_poly(mono, 16000 // g, sr // g)
    sf.write(str(ziel), (np.clip(mono, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")


def _sprechen_mp(args):
    return _sprechen(*args)


def hoerproben(cfg: dict, studio: Path, n: int) -> None:
    ordner = studio / cfg.get("stimmen_ordner", "voices")
    stimmen_holen(cfg, ordner)
    aus = studio / "hoerproben" / cfg["modell"]
    jobs = []
    for p in cfg["positiv"]:
        for stimme, tempi in p["stimmen"].items():
            jobs.append(dict(tag=f"{p['name']}__{stimme}", ziel=aus / f"{p['name']}__{stimme}",
                             text=p["text"], phoneme=p["phoneme"], stimme=ordner / f"{stimme}.onnx",
                             n=n, tempi=tuple(tempi)))
    for j in jobs:
        tag, k, dt = _sprechen(j, cfg["rauschen"], aus / "tmp")
        print(f"{tag}: {k} Clips", flush=True)
    shutil.rmtree(aus / "tmp", ignore_errors=True)
    print(f"Anhören: {aus}/<schreibweise>__<stimme>/ — Stimmen, die das Wort falsch sagen, "
          "aus der YAML streichen", flush=True)


def alle(cfg: dict, studio: Path, prozesse: int) -> None:
    ordner = studio / cfg.get("stimmen_ordner", "voices")
    stimmen_holen(cfg, ordner)
    ziel = studio / "train_out" / cfg["modell"]
    for k in KLASSEN:
        if (ziel / k).exists() and any((ziel / k).iterdir()):
            raise SystemExit(f"{ziel / k} ist nicht leer — erst wegräumen")
    jobs = auftraege(cfg, ordner, ziel)
    soll = sum(j["n"] for j in jobs)
    print(f"{len(jobs)} Aufträge, {soll} Clips", flush=True)
    tmp = studio / "train_out" / f"tmp_{cfg['modell']}"
    fertig = 0
    with mp.Pool(prozesse) as pool:
        for tag, n, dt in pool.imap_unordered(_sprechen_mp, [(j, cfg["rauschen"], tmp) for j in jobs]):
            fertig += n
            print(f"[{fertig}/{soll}] {tag}: {n} Clips in {dt:.0f} s", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    for k in KLASSEN:
        print(k, len(list((ziel / k).glob("*.wav"))), flush=True)


def pruefen(cfg: dict, studio: Path, onnx: str, je_gruppe: int = 40) -> None:
    import numpy as np
    import soundfile as sf
    from openwakeword.model import Model
    m = Model(wakeword_models=[onnx], inference_framework="onnx")

    def bester(pfad: str) -> float:
        audio, _ = sf.read(pfad, dtype="int16")
        audio = np.concatenate([audio, np.zeros(1280 * 8, dtype=np.int16)])  # Wortende sehen
        m.reset()
        return max(list(m.predict(audio[i:i + 1280]).values())[0]
                   for i in range(0, len(audio) - 1280, 1280))

    gruppen = defaultdict(list)
    for f in glob.glob(str(studio / "train_out" / cfg["modell"] / "positive_test" / "*.wav")):
        teile = os.path.basename(f).split("__")
        if len(teile) >= 3:
            gruppen[(teile[0], teile[1])].append(f)
    rng = np.random.default_rng(0)
    print(f"{'Schreibweise / Stimme':<50}{'n':>4} {'>0,5':>6} {'>0,2':>6} {'Median':>7}")
    for (var, stimme), fl in sorted(gruppen.items()):
        s = np.array([bester(f) for f in rng.choice(fl, size=min(je_gruppe, len(fl)), replace=False)])
        print(f"{var + ' / ' + stimme:<50}{len(s):>4} {(s > 0.5).mean():>6.2f} {(s > 0.2).mean():>6.2f} "
              f"{np.median(s):>7.3f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("schritt", choices=["hoerproben", "alle", "pruefen"])
    ap.add_argument("config", help="YAML wie clips_vorlage.yaml")
    ap.add_argument("onnx", nargs="?", help="nur pruefen: das trainierte Modell")
    ap.add_argument("--studio", default=".", help="Arbeitsordner (piper-sample-generator/, train_out/)")
    ap.add_argument("--n", type=int, default=4, help="hoerproben: Clips je Schreibweise × Stimme")
    ap.add_argument("--prozesse", type=int, default=4, help="alle: gleichzeitige Stimmen")
    a = ap.parse_args(argv)
    studio = Path(a.studio).expanduser().resolve()
    sys.path.insert(0, str(studio / "piper-sample-generator"))
    # Der Klon heißt wie das Paket: ohne das fände "import openwakeword" den
    # Repo-Ordner im Arbeitsordner (Namespace ohne Inhalt) statt des Pakets darin.
    sys.path.insert(0, str(studio / "openwakeword"))
    cfg = config_lesen(a.config)
    if a.schritt == "hoerproben":
        hoerproben(cfg, studio, a.n)
    elif a.schritt == "alle":
        alle(cfg, studio, a.prozesse)
    else:
        if not a.onnx:
            ap.error("pruefen braucht das onnx-Modell")
        pruefen(cfg, studio, a.onnx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
