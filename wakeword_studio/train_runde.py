#!/usr/bin/env python3
"""Eine Nachtrainingsrunde für ein openWakeWord-Modell, auf dem GPU-Rechner.

Läuft dort im venv von openWakeWord (braucht nur stdlib, PyYAML, numpy,
onnxruntime und das CLI ``onnx2tf``), NICHT im ``ow-venv`` dieses Repos.
Anleitung, Begriffe und Lehren: ``WAKEWORD_TRAINING.md``.

    python train_runde.py --name r8 --paket gaston_r8_paket \\
        --rezept voll --rezept C=c_pos.txt,c_neg.txt --laeufe 2 \\
        --synth train_out/gaston --basis-config gaston.yaml --weiche llm

Was es tut, je Rezept:
1. Einen eigenen Ordner ``runden/<name>/<rezept>/`` anlegen: die synthetischen
   Clips als Symlinks (ohne die mit ``--praefix``, das sind Reste früherer
   Runden), dazu die echten Clips des Pakets ``--dupl``-mal verlinkt.
2. ``train.py --augment_clips`` (Merkmale berechnen, ~30 min, ein CPU-Kern).
3. ``--laeufe``-mal ``train.py --train_model`` in je eigenem Ordner
   ``l1``, ``l2``, … (Symlinks auf die Merkmale des Rezepts), danach FP/h auf
   dem generischen Validierungs-Set und die tflite per ``onnx2tf``.

Parallel: augments laufen gleichzeitig bis ``--augment-parallel`` (sie
belegen je einen CPU-Kern), Trainings gleichzeitig je eine pro GPU aus
``--gpus`` (``CUDA_VISIBLE_DEVICES``). Vorher schrieb ``train.py`` immer in
denselben ``output_dir``, deshalb ging nur eins nach dem anderen.

Was es bewusst NICHT tut: Modelle auswählen oder deployen. Die Schranke
steht vor dem Training fest, gemessen wird daheim gegen das Val-Set des
Pakets (``tools/wake_paket.py messen``). Das Skript liefert nur Modelle und
FP/h.

``--weiche NAME`` stoppt einen Podman-Container für die Dauer (Grafikspeicher)
und startet ihn am Ende im eigenen Scope wieder, auch bei Abbruch per
Signal. Nur Container, die vorher liefen.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

KLASSEN = ("positive_train", "negative_train", "positive_test", "negative_test")
SEITE_ZU_KLASSE = {"positive": "positive_train", "negative": "negative_train"}
# Gleiche Rechnung wie die Live-Erkennung: Trigger ab 3 Frames in Folge.
SCHWELLEN = (0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
FRAME_SEK = 0.08


# --------------------------------------------------------------------------
# Vorbereitung (ohne GPU, getestet in tests/test_train_runde.py)
# --------------------------------------------------------------------------

def rezept_lesen(text: str, paket: Path) -> tuple[str, dict[str, list[str]]]:
    """``voll`` = alles aus ``paket/train``; ``NAME=pos.txt,neg.txt`` = Listen.

    Die Listen enthalten Dateinamen aus ``paket/train/positive`` bzw.
    ``negative``, eine je Zeile. Ein fehlender Name ist ein Fehler, kein
    stilles Weglassen: sonst trainiert man ein anderes Rezept als gedacht.
    """
    if "=" not in text:
        if text != "voll":
            raise ValueError(f"Rezept {text!r}: entweder 'voll' oder NAME=pos.txt,neg.txt")
        return "voll", {s: sorted(f.name for f in (paket / "train" / s).glob("*.wav"))
                        for s in SEITE_ZU_KLASSE}
    name, _, listen = text.partition("=")
    teile = listen.split(",")
    if len(teile) != 2:
        raise ValueError(f"Rezept {text!r}: zwei Listen erwartet (positiv,negativ)")
    ergebnis = {}
    for seite, datei in zip(SEITE_ZU_KLASSE, teile):
        namen = [z.strip() for z in Path(datei).read_text().splitlines() if z.strip()]
        fehlt = [n for n in namen if not (paket / "train" / seite / n).exists()]
        if fehlt:
            raise ValueError(f"Rezept {name}: {len(fehlt)} Dateien fehlen in {seite}, z. B. {fehlt[0]}")
        ergebnis[seite] = namen
    return name, ergebnis


def config_schreiben(basis: dict, output_dir: Path, ziel: Path) -> None:
    import yaml
    cfg = dict(basis)
    cfg["output_dir"] = str(output_dir)
    ziel.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def rezept_vorbereiten(wurzel: Path, name: str, modell: str, synth: Path, paket: Path,
                       listen: dict[str, list[str]], dupl: int, praefix: str,
                       basis: dict) -> dict[str, int]:
    """Ordner eines Rezepts: synthetische Clips + echte Clips, alles als Symlinks."""
    d = wurzel / name
    g = d / modell
    zaehl = {}
    for k in KLASSEN:
        (g / k).mkdir(parents=True, exist_ok=True)
        n = 0
        quelle = synth / k
        for f in os.scandir(quelle):
            if f.name.endswith(".wav") and not f.name.startswith(praefix):
                os.symlink(os.path.realpath(f.path), g / k / f.name)
                n += 1
        zaehl[k] = n
    for seite, namen in listen.items():
        k = SEITE_ZU_KLASSE[seite]
        for n in namen:
            quelle = (paket / "train" / seite / n).resolve()
            for i in range(dupl):
                os.symlink(quelle, g / k / f"{praefix}{Path(n).stem}_d{i}.wav")
        zaehl[f"{k}_echt"] = len(namen) * dupl
    config_schreiben(basis, d, d / "config.yaml")
    return zaehl


def lauf_vorbereiten(rezept_dir: Path, n: int, modell: str, basis: dict) -> Path:
    """Eigener output_dir je Lauf, Merkmale und Clip-Ordner nur verlinkt.

    train.py liest positive_test (Eingabeform) und die *.npy aus
    ``output_dir/<modell>/`` und schreibt ``output_dir/<modell>.onnx``.
    """
    l = rezept_dir / f"l{n}"
    (l / modell).mkdir(parents=True, exist_ok=True)
    for e in os.scandir(rezept_dir / modell):
        ziel = l / modell / e.name
        if not ziel.exists():
            os.symlink(os.path.realpath(e.path), ziel)
    config_schreiben(basis, l, l / "config.yaml")
    return l


# --------------------------------------------------------------------------
# Ausführen
# --------------------------------------------------------------------------

class Runde:
    def __init__(self, args) -> None:
        self.a = args
        self.studio = Path(args.studio).expanduser().resolve()
        self.wurzel = self.studio / "runden" / args.name
        self.status = self.wurzel / "status.txt"
        self.lock = threading.Lock()
        self.gpus = [g for g in args.gpus.split(",") if g != ""]
        self.freie_gpus = list(self.gpus) or [""]   # ohne GPU: ein Training, CPU
        self.gpu_cv = threading.Condition()
        self.ergebnis: list[dict] = []
        self.gewichen: list[str] = []
        self.train_py = self.studio / args.train_py

    def log(self, text: str) -> None:
        zeile = f"{datetime.now().isoformat(timespec='seconds')} {text}"
        with self.lock:
            print(zeile, flush=True)
            with open(self.status, "a") as f:
                f.write(zeile + "\n")

    # -- GPU-Vergabe: ein Training je GPU ---------------------------------
    def gpu_holen(self) -> str:
        with self.gpu_cv:
            while not self.freie_gpus:
                self.gpu_cv.wait()
            return self.freie_gpus.pop(0)

    def gpu_frei(self, g: str) -> None:
        with self.gpu_cv:
            self.freie_gpus.append(g)
            self.gpu_cv.notify()

    def train_py_aufruf(self, config: Path, schritt: str, log: Path, gpu: str) -> int:
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu)
        with open(log, "w") as f:
            return subprocess.run([sys.executable, str(self.train_py), "--training_config",
                                   str(config), schritt] + (["--overwrite"] if schritt == "--augment_clips" else []),
                                  cwd=self.studio, env=env, stdout=f, stderr=subprocess.STDOUT).returncode

    # -- Container ---------------------------------------------------------
    def weichen(self) -> None:
        for c in self.a.weiche:
            r = subprocess.run(["podman", "inspect", "-f", "{{.State.Running}}", c],
                               capture_output=True, text=True)
            if r.stdout.strip() == "true":
                subprocess.run(["podman", "stop", c], capture_output=True)
                self.gewichen.append(c)
                self.log(f"Container {c} gestoppt")

    def zurueck(self) -> None:
        # Im eigenen Scope: sonst hängt die Port-Weiterleitung von rootless
        # Podman an dieser SSH-Sitzung und stirbt mit ihr.
        while self.gewichen:
            c = self.gewichen.pop()
            r = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect",
                                "podman", "start", c], capture_output=True, text=True)
            self.log(f"Container {c} gestartet rc={r.returncode}")

    # -- Schritte ----------------------------------------------------------
    def augment(self, name: str, idx: int) -> bool:
        d = self.wurzel / name
        gpu = self.gpus[idx % len(self.gpus)] if self.gpus else ""
        t0 = time.time()
        self.log(f"{name}: augment startet (GPU {gpu or '-'})")
        rc = self.train_py_aufruf(d / "config.yaml", "--augment_clips", d / "augment.log", gpu)
        ok = rc == 0 and (d / self.a.modell / "positive_features_train.npy").exists()
        self.log(f"{name}: augment rc={rc} {'ok' if ok else 'FEHLER'} in {(time.time()-t0)/60:.1f} min")
        return ok

    def lauf(self, name: str, n: int) -> None:
        l = lauf_vorbereiten(self.wurzel / name, n, self.a.modell, self.basis)
        onnx = l / f"{self.a.modell}.onnx"
        gpu = self.gpu_holen()
        t0 = time.time()
        self.log(f"{name} l{n}: train startet (GPU {gpu})")
        try:
            rc = self.train_py_aufruf(l / "config.yaml", "--train_model", l / "train.log", gpu)
        finally:
            self.gpu_frei(gpu)
        dauer = (time.time() - t0) / 60
        # rc=1 am Ende ist bekannt: train.py versucht die tflite mit onnx_tf
        # (Flag-Default ist der String "False", also wahr). Maßgeblich ist die onnx.
        if not onnx.exists() or onnx.stat().st_mtime < t0:
            self.log(f"{name} l{n}: train rc={rc}, KEINE onnx, siehe {l / 'train.log'}")
            return
        self.log(f"{name} l{n}: train fertig in {dauer:.1f} min (rc={rc})")
        fph = fp_pro_stunde(onnx, self.studio / self.basis["false_positive_validation_data_path"],
                            threads=self.a.eval_threads)
        (l / "fp_pro_stunde.json").write_text(json.dumps(fph, indent=1))
        tfl = l / "tflite"
        with open(l / "onnx2tf.log", "w") as f:
            subprocess.run([self._onnx2tf(), "-i", str(onnx), "-o", str(tfl), "-kat", "x"],
                           cwd=l, stdout=f, stderr=subprocess.STDOUT)
        tf = tfl / f"{self.a.modell}_float32.tflite"
        self.log(f"{name} l{n}: FP/h @{self.a.schwelle} (3 Frames) "
                 f"{fph.get(str(self.a.schwelle), {}).get('streak3', float('nan')):.2f}, tflite {'da' if tf.exists() else 'FEHLT'}")
        with self.lock:
            self.ergebnis.append({"rezept": name, "lauf": n, "train_min": round(dauer, 1),
                                  "fp_pro_stunde": fph, "tflite": str(tf) if tf.exists() else None})

    def _onnx2tf(self) -> str:
        neben = Path(sys.executable).parent / "onnx2tf"
        return str(neben) if neben.exists() else (shutil.which("onnx2tf") or "onnx2tf")

    # -- Ablauf -------------------------------------------------------------
    def run(self) -> int:
        import yaml
        a = self.a
        paket = (self.studio / a.paket).resolve()
        synth = (self.studio / a.synth).resolve()
        self.basis = yaml.safe_load((self.studio / a.basis_config).read_text())
        self.basis["model_name"] = a.modell
        rezepte = [rezept_lesen(r, paket) for r in a.rezept]
        if self.wurzel.exists():
            print(f"{self.wurzel} gibt es schon — anderen --name wählen", file=sys.stderr)
            return 2
        self.wurzel.mkdir(parents=True)
        self.log(f"Runde {a.name}: Paket {paket.name}, Rezepte {[r for r, _ in rezepte]}, "
                 f"{a.laeufe} Läufe, augment parallel {a.augment_parallel}, GPUs {self.gpus or '-'}")
        for name, listen in rezepte:
            z = rezept_vorbereiten(self.wurzel, name, a.modell, synth, paket, listen,
                                   a.dupl, a.praefix, self.basis)
            self.log(f"{name}: {z}")
        if a.trocken:
            self.log("trocken: nur vorbereitet")
            return 0

        def ende(signum, _frame):
            self.log(f"Signal {signum}, räume auf")
            self.zurueck()
            os._exit(128 + signum)
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(s, ende)

        t0 = time.time()
        try:
            self.weichen()
            with ThreadPoolExecutor(max(1, len(self.gpus))) as trains, \
                 ThreadPoolExecutor(a.augment_parallel) as augments:
                def rezept(idx, name):
                    if self.augment(name, idx):
                        return [trains.submit(self.lauf, name, n) for n in range(1, a.laeufe + 1)]
                    return []
                fs = [augments.submit(rezept, i, name) for i, (name, _) in enumerate(rezepte)]
                for f in fs:
                    for t in f.result():
                        t.result()
        finally:
            self.zurueck()
        self.ergebnis.sort(key=lambda e: (e["rezept"], e["lauf"]))
        (self.wurzel / "ergebnis.json").write_text(json.dumps(self.ergebnis, indent=1))
        self.log(f"Runde {a.name} fertig in {(time.time()-t0)/60:.0f} min")
        print(f"\n{'Rezept':10s} {'Lauf':>4s} {'train min':>9s}  FP/h 3 Frames je Schwelle "
              + " ".join(f"{s:>5}" for s in SCHWELLEN))
        for e in self.ergebnis:
            print(f"{e['rezept']:10s} {e['lauf']:4d} {e['train_min']:9.1f}  {'':27s}"
                  + " ".join(f"{e['fp_pro_stunde'][str(s)]['streak3']:5.2f}" for s in SCHWELLEN))
        return 0


def fp_pro_stunde(onnx: Path, val_npy: Path, threads: int = 4) -> dict:
    """FP/h auf dem generischen Validierungs-Set, roh und ab 3 Frames in Folge.

    Das Set (``validation_set_features.npy`` von openWakeWord, ~11 h Sprache
    und Geräusch ohne Wakewort) ist für jedes Wort gleich. Es misst nur die
    Grundneigung zum Fehlauslösen, nicht die Fehlauslöser im eigenen Raum.
    """
    import numpy as np
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    sess = ort.InferenceSession(str(onnx), so, providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    fenster = sess.get_inputs()[0].shape[1] or 16
    val = np.load(val_npy).astype(np.float32)
    win = np.ascontiguousarray(
        np.lib.stride_tricks.sliding_window_view(val, fenster, axis=0).transpose(0, 2, 1))
    s = np.empty(len(win), dtype=np.float32)
    buf = np.empty((1, fenster, val.shape[1]), dtype=np.float32)
    for i in range(len(win)):
        buf[0] = win[i]
        s[i] = sess.run(None, {iname: buf})[0][0, 0]
    stunden = len(val) * FRAME_SEK / 3600
    aus = {}
    for thr in SCHWELLEN:
        ueber = s > thr
        roh = int((ueber[1:] & ~ueber[:-1]).sum() + ueber[0])
        serie = np.convolve(ueber.astype(int), np.ones(3, dtype=int), "valid") >= 3
        ev = int((serie[1:] & ~serie[:-1]).sum() + serie[0])
        aus[str(thr)] = {"roh": round(roh / stunden, 2), "streak3": round(ev / stunden, 2)}
    return aus


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--name", required=True, help="Name der Runde, Ordner runden/<name>/")
    p.add_argument("--paket", required=True, help="Paket von tools/wake_paket.py (mit train/)")
    p.add_argument("--rezept", action="append", required=True,
                   help="'voll' oder NAME=pos.txt,neg.txt (mehrfach)")
    p.add_argument("--laeufe", type=int, default=2, help="Trainings je Rezept (Streuung!)")
    p.add_argument("--synth", default="train_out/gaston",
                   help="Ordner mit den synthetischen Clips (positive_train, …)")
    p.add_argument("--basis-config", default="gaston.yaml", help="Trainings-Config von openWakeWord")
    p.add_argument("--modell", default="gaston", help="model_name")
    p.add_argument("--studio", default=".", help="Arbeitsordner (openwakeword/, Merkmal-Dateien)")
    p.add_argument("--train-py", default="openwakeword/openwakeword/train.py")
    p.add_argument("--dupl", type=int, default=10, help="echte Clips so oft verlinken")
    p.add_argument("--praefix", default="real_",
                   help="Präfix echter Clips; solche im --synth-Ordner werden übersprungen")
    p.add_argument("--augment-parallel", type=int, default=2)
    p.add_argument("--gpus", default="0", help="z. B. '0,1': je GPU ein Training gleichzeitig")
    p.add_argument("--weiche", action="append", default=[], help="Podman-Container für die Dauer stoppen")
    p.add_argument("--schwelle", type=float, default=0.35, help="nur für die Statuszeile")
    p.add_argument("--eval-threads", type=int, default=4)
    p.add_argument("--trocken", action="store_true", help="nur Ordner vorbereiten")
    return Runde(p.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
