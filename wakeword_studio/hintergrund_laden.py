#!/usr/bin/env python3
"""Raumhall und Hintergrund-Audio für das openWakeWord-Training laden.

Läuft auf dem GPU-Rechner im venv der Trainingsumgebung (gerufen von
``gpu_einrichten.sh``). Ergebnis im Arbeitsordner:

- ``mit_rirs/``      ~270 Raum-Impulsantworten (MIT), 16 kHz mono
- ``audioset_16k/``  ~20 h AudioSet (bal_train, 4 Parquet-Teile), 16 kHz mono

Warum nicht wie im Notebook von openWakeWord: ``datasets`` lädt die RIRs nur
mit ``torchcodec`` (braucht FFmpeg-Bibliotheken), AudioSet liegt auf Hugging
Face seit 2026 als Parquet statt als tar, und das FMA-Musik-Set ist ein
Script-Dataset, das neue ``datasets``-Versionen ablehnen. AudioSet enthält
genug Musik und Geräusch, FMA entfällt.

    python hintergrund_laden.py [rirs] [audioset] [--ordner ~/wakeword-studio]
"""

from __future__ import annotations

import argparse
import io
import multiprocessing as mp
import os
import subprocess
from pathlib import Path

RIR_URL = "https://mcdermottlab.mit.edu/Reverb/IRMAudio/Audio.zip"
AUDIOSET_URL = "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/{teil}.parquet"
AUDIOSET_TEILE = ("00", "01", "02", "03")


def _als_16k(daten, sr, ziel: str) -> None:
    import numpy as np
    import scipy.signal
    import soundfile as sf
    mono = daten.mean(axis=1)
    if sr != 16000:
        g = np.gcd(sr, 16000)
        mono = scipy.signal.resample_poly(mono, 16000 // g, sr // g)
    sf.write(ziel, (np.clip(mono, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")


def _datei_16k(args) -> int:
    import soundfile as sf
    quelle, ziel = args
    try:
        daten, sr = sf.read(quelle, dtype="float32", always_2d=True)
        _als_16k(daten, sr, ziel)
        return 0
    except Exception as e:  # kaputte Einzeldateien überspringen
        print("übersprungen", quelle, e, flush=True)
        return 1


def _bytes_16k(args) -> int:
    import soundfile as sf
    blob, ziel = args
    try:
        daten, sr = sf.read(io.BytesIO(blob), dtype="float32", always_2d=True)
        _als_16k(daten, sr, ziel)
        return 0
    except Exception as e:
        print("übersprungen", ziel, e, flush=True)
        return 1


def _wget(url: str, ziel: Path) -> None:
    subprocess.run(["wget", "-q", "--tries=5", "--retry-on-http-error=429,503",
                    "--waitretry=30", "-c", "-O", str(ziel), url], check=True)


def rirs(ordner: Path, prozesse: int) -> None:
    aus = ordner / "mit_rirs"
    if aus.is_dir() and len(os.listdir(aus)) > 200:
        print("mit_rirs vorhanden", flush=True)
        return
    aus.mkdir(exist_ok=True)
    z = ordner / "mit_audio.zip"
    if not (z.exists() and z.stat().st_size > 10_000_000):
        _wget(RIR_URL, z)
    subprocess.run(["unzip", "-oq", str(z), "-d", str(ordner / "mit_audio")], check=True)
    jobs = [(str(f), str(aus / (f.stem + ".wav"))) for f in (ordner / "mit_audio").glob("**/*.wav")]
    with mp.Pool(prozesse) as pool:
        fehler = sum(pool.imap_unordered(_datei_16k, jobs, chunksize=16))
    print(f"mit_rirs: {len(jobs) - fehler} Dateien", flush=True)


def audioset(ordner: Path, prozesse: int) -> None:
    import pyarrow.parquet as pq
    aus = ordner / "audioset_16k"
    if aus.is_dir() and len(os.listdir(aus)) > 1000:
        print("audioset_16k vorhanden", flush=True)
        return
    aus.mkdir(exist_ok=True)
    (ordner / "audioset").mkdir(exist_ok=True)
    gesamt = 0
    for teil in AUDIOSET_TEILE:
        p = ordner / "audioset" / f"bal_train_{teil}.parquet"
        if not (p.exists() and p.stat().st_size > 100_000_000):
            print(f"lade AudioSet-Teil {teil} …", flush=True)
            _wget(AUDIOSET_URL.format(teil=teil), p)
        t = pq.read_table(p, columns=["video_id", "audio"])
        jobs = [(a["bytes"].as_py(), str(aus / f"{v.as_py()}.wav"))
                for v, a in zip(t["video_id"], t["audio"])]
        with mp.Pool(prozesse) as pool:
            fehler = sum(pool.imap_unordered(_bytes_16k, jobs, chunksize=8))
        gesamt += len(jobs) - fehler
        p.unlink()  # Platz: ~650 MB je Teil
    print(f"audioset_16k: {gesamt} Dateien", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stufen", nargs="*", default=["rirs", "audioset"], choices=["rirs", "audioset"])
    ap.add_argument("--ordner", default=".", help="Arbeitsordner der Trainingsumgebung")
    ap.add_argument("--prozesse", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = ap.parse_args(argv)
    ordner = Path(a.ordner).expanduser().resolve()
    for s in a.stufen:
        {"rirs": rirs, "audioset": audioset}[s](ordner, a.prozesse)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
