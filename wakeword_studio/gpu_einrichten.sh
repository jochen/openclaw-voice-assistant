#!/usr/bin/env bash
# Trainingsumgebung für openWakeWord auf einem GPU-Rechner einrichten.
#
#   wakeword_studio/gpu_einrichten.sh [ARBEITSORDNER]      (Default ~/wakeword-studio)
#
# Umgebung:
#   TORCH_INDEX   pip-Index für torch passend zur CUDA-Version des Treibers,
#                 Default https://download.pytorch.org/whl/cu130
#   PYTHON        Interpreter für den venv, Default python3 (getestet: 3.12)
#   OHNE_DATEN=1  nur venv und Code, keine Datensätze (zum Prüfen)
#
# Braucht: git, wget, unzip, ~25 GB Platz. Läuft einmal 1-2 h, fast alles
# davon Downloads. Jeder Schritt überspringt, was schon da ist; ein Abbruch
# kann also einfach wiederholt werden.
#
# Ergebnis im Arbeitsordner: venv/, openwakeword/ (gepatcht),
# piper-sample-generator/, die Merkmal-Dateien, mit_rirs/, audioset_16k/ und
# die Skripte train_runde.py, clips_erzeugen.py, training_vorlage.yaml.
# Anleitung: WAKEWORD_TRAINING.md, Teil 1.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
W="${1:-$HOME/wakeword-studio}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
PYTHON="${PYTHON:-python3}"
# Die Stände, mit denen das gaston-Modell trainiert wurde.
OWW_COMMIT=368c037
PSG_COMMIT=2971426
OWW_RELEASE=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
FEATURES=https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main

stufe() { echo; echo "===== [$(date +%H:%M:%S)] $1 ====="; }

mkdir -p "$W"
cd "$W"

stufe "1/6 venv, torch ($TORCH_INDEX), Pakete"
[ -x venv/bin/python ] || "$PYTHON" -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q torch==2.12.1 torchaudio==2.11.0 --index-url "$TORCH_INDEX"
venv/bin/pip install -q -r "$REPO/training_requirements.txt"

stufe "2/6 openWakeWord ($OWW_COMMIT) mit Kompat-Patch"
[ -d openwakeword ] || git clone -q https://github.com/dscripka/openWakeWord openwakeword
git -C openwakeword checkout -q "$OWW_COMMIT"
# torchaudio >= 2.9 lädt WAV nur noch über torchcodec/FFmpeg; der Patch nimmt soundfile.
if git -C openwakeword apply --check "$REPO/openwakeword-kompat.patch" 2>/dev/null; then
  git -C openwakeword apply "$REPO/openwakeword-kompat.patch"
else
  echo "Patch schon angewendet (oder passt nicht, dann: git -C openwakeword diff)"
fi
venv/bin/pip install -q --no-deps -e ./openwakeword
mkdir -p openwakeword/openwakeword/resources/models
for f in embedding_model.onnx embedding_model.tflite melspectrogram.onnx melspectrogram.tflite; do
  [ -s "openwakeword/openwakeword/resources/models/$f" ] || \
    wget -q -O "openwakeword/openwakeword/resources/models/$f" "$OWW_RELEASE/$f"
done

stufe "3/6 piper-sample-generator ($PSG_COMMIT)"
[ -d piper-sample-generator ] || git clone -q https://github.com/rhasspy/piper-sample-generator
git -C piper-sample-generator checkout -q "$PSG_COMMIT"
venv/bin/pip install -q --no-deps -e ./piper-sample-generator
# train.py importiert noch die alte API "generate_samples" aus dem Repo-Wurzelordner.
echo 'from piper_sample_generator.__main__ import generate_samples, generate_samples_onnx  # noqa: F401' \
  > piper-sample-generator/generate_samples.py

stufe "4/6 Skripte und Config-Vorlage"
cp "$REPO/train_runde.py" "$REPO/clips_erzeugen.py" "$REPO/hintergrund_laden.py" .
[ -e training_vorlage.yaml ] || cp "$REPO/training_vorlage.yaml" .
[ -e clips_vorlage.yaml ] || cp "$REPO/clips_vorlage.yaml" .

if [ "${OHNE_DATEN:-0}" = 1 ]; then
  echo "OHNE_DATEN=1: Datensätze übersprungen"
  exit 0
fi

stufe "5/6 Vorgerechnete Merkmale (ACAV100M ~17 GB, Validierung ~185 MB)"
for f in openwakeword_features_ACAV100M_2000_hrs_16bit.npy validation_set_features.npy; do
  wget -q -c -O "$f" "$FEATURES/$f"
done

stufe "6/6 Raumhall (MIT) und Hintergrund (AudioSet)"
venv/bin/python hintergrund_laden.py rirs audioset --ordner .

stufe "Prüfung"
venv/bin/python - <<'EOF'
import torch, openwakeword, piper_sample_generator  # noqa: F401
import acoustics  # noqa: F401  (scipy >= 1.15 bricht hier)
print("torch", torch.__version__, "CUDA", torch.cuda.is_available(), torch.cuda.device_count(), "GPU(s)")
EOF
ls -sh openwakeword_features_ACAV100M_2000_hrs_16bit.npy validation_set_features.npy
echo "mit_rirs: $(ls mit_rirs | wc -l), audioset_16k: $(ls audioset_16k | wc -l)"
command -v venv/bin/onnx2tf >/dev/null && echo "onnx2tf da"
