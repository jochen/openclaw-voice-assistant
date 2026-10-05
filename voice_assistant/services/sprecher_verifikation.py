"""Sprecher-Verifikation: ein Stimm-Fingerabdruck je Aufnahme, auf der CPU.

Ersetzt (per Profil `sprecher_verifikation`) die Diarization über Speaches.
Die Frage hier ist nicht "welche Sprecher kommen in der Aufnahme vor?",
sondern "ist das eine der angelernten Stimmen?" — dafür braucht es keine
Segmentierung und kein Clustering, nur einen Fingerabdruck und einen Vergleich.

Gemessen am 2026-10-05 über 171 archivierte Aufnahmen
(tools/sprecher_verifikation_test.py, Labels in testsets/sprecher_labels.jsonl):
  - Diarization: live median ~2,0 s (zwei Durchläufe, wenn der erste niemanden
    fand), von 27 per Ohr bestätigten Jochen-Aufnahmen nur 2 erkannt.
  - Verifikation: ~50 ms, Schwelle 0,40 + Abstand 0,15 -> 128 von 129 richtig,
    0 falsch zugeordnet (auch kein Fernsehen: max 0,31, kein Mehrpersonen-Fall:
    0,38). Der eine Verpasste ist "Gaston." allein (0,39).
  - Wiederholen kurzer Aufnahmen (wie bei der Diarization) hilft hier nicht:
    bei 1 s Sprache schlechter, bei 1,5 s gemischt. Sehr kurze Sätze werden
    eher "unbekannt" — die sichere Richtung.

Modell und Rechenweg sind die von Speaches (onnx_diarization, WeSpeaker
ResNet34-LM), nachgebaut für den Ein-Sprecher-Fall — einschließlich der
Rundung, die den letzten Frame abschneidet, damit die gemessenen Zahlen gelten.

Referenzen: SPEAKERS_DIR/<name>.wav, wie sie das Anlernen über den Brain
(voice_enroll_speaker -> enroll_server) schreibt. Der Fingerabdruck einer
Referenz wird neu berechnet, sobald sich die Datei ändert — Anlernen und
Nachlernen wirken ohne Neustart.

Vertrag wie bei der Diarization (SPEAKER_STATE.md): jeder Fehlerweg ergibt
`ausgefallen`, nie `unbekannt`; ohne Referenzen `nicht_eingerichtet`.
"""

from __future__ import annotations

import io
import os
import threading
import wave

import numpy as np

from voice_assistant.config import SPEAKERS_DIR
from voice_assistant.services.diarization import (
    STATUS_AUSGEFALLEN,
    STATUS_BEKANNT,
    STATUS_NICHT_EINGERICHTET,
    STATUS_UNBEKANNT,
    SpeakerVerdict,
)

MODELL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODELL_DATEI = "voxceleb_resnet34_LM.onnx"
_RATE = 16000
_SHIFT_MS = 10


def _fbank(audio: np.ndarray):
    """80 Mel-Bänder wie onnx_diarization.fbank, Mittelwert je Band abgezogen."""
    import kaldi_native_fbank as knf

    o = knf.FbankOptions()
    o.mel_opts.num_bins = 80
    o.frame_opts.frame_length_ms = 25
    o.frame_opts.frame_shift_ms = _SHIFT_MS
    o.frame_opts.samp_freq = _RATE
    o.frame_opts.window_type = "hamming"
    o.frame_opts.dither = 0.0
    o.frame_opts.snip_edges = True
    o.frame_opts.round_to_power_of_two = True
    ex = knf.OnlineFbank(o)
    ex.accept_waveform(_RATE, (audio * (1 << 15)).astype(np.float32))
    ex.input_finished()
    f = np.array([ex.get_frame(j) for j in range(ex.num_frames_ready)], dtype=np.float32)
    f = f - np.mean(f, axis=0, keepdims=True)
    # Wie onnx_diarization: Frames -> Sekunden -> Frames, int() rundet ab und
    # kostet dabei manchmal den letzten Frame. Bewusst nachgebaut.
    dauer = f.shape[0] * (_SHIFT_MS / 1000.0)
    return f[: int(dauer / (_SHIFT_MS / 1000.0))]


def _wav_audio(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getframerate() != _RATE or w.getsampwidth() != 2:
            raise ValueError(f"erwarte 16 kHz/16 bit, habe {w.getframerate()} Hz/{8 * w.getsampwidth()} bit")
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            a = a.reshape(-1, 2).mean(axis=1)
    return a


class SprecherVerifikation:
    """`diarize(wav_bytes) -> SpeakerVerdict`, austauschbar mit SpeachesDiarizer."""

    def __init__(self, schwelle: float = 0.40, abstand: float = 0.15,
                 threads: int = 4, sprecher_dir: str = SPEAKERS_DIR) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        pfad = hf_hub_download(MODELL_REPO, MODELL_DATEI)
        self._sess = ort.InferenceSession(pfad, so, providers=["CPUExecutionProvider"])
        self.schwelle = schwelle
        self.abstand = abstand
        self.sprecher_dir = sprecher_dir
        self._lock = threading.Lock()
        self._refs: dict[str, tuple[float, np.ndarray]] = {}   # name -> (mtime, Fingerabdruck)

    def fingerabdruck(self, audio: np.ndarray) -> np.ndarray:
        f = _fbank(audio)
        e = self._sess.run(None, {"feats": f[None, :, :]})[0][0]
        return e / np.linalg.norm(e)

    def _referenzen(self) -> tuple[dict[str, np.ndarray], int]:
        """Fingerabdrücke aller angelernten Stimmen; neu nur, wenn die Datei sich
        änderte. Zweiter Wert: wie viele Referenz-Dateien es überhaupt gibt —
        Dateien da, aber kein Fingerabdruck ist ein Ausfall, kein fehlendes Setup."""
        if not os.path.isdir(self.sprecher_dir):
            return {}, 0
        aktuell = {}
        dateien = 0
        with self._lock:
            for fname in sorted(os.listdir(self.sprecher_dir)):
                if not fname.lower().endswith(".wav"):
                    continue
                dateien += 1
                name = os.path.splitext(fname)[0]
                pfad = os.path.join(self.sprecher_dir, fname)
                try:
                    mtime = os.path.getmtime(pfad)
                    alt = self._refs.get(name)
                    if alt is None or alt[0] != mtime:
                        with open(pfad, "rb") as fh:
                            self._refs[name] = (mtime, self.fingerabdruck(_wav_audio(fh.read())))
                        print(f"🎙  Sprecher-Referenz {'neu' if alt is None else 'aktualisiert'}: {name}")
                    aktuell[name] = self._refs[name][1]
                except Exception as e:      # eine kaputte Referenz soll die anderen nicht mitnehmen
                    print(f"⚠️  speakers/{fname}: {e}")
            for weg in set(self._refs) - set(aktuell):
                del self._refs[weg]
        return aktuell, dateien

    def urteil(self, aehnlichkeit: dict[str, float]) -> SpeakerVerdict:
        """Beste Stimme über der Schwelle UND mit Abstand zur zweitbesten."""
        if not aehnlichkeit:
            return SpeakerVerdict(None, STATUS_NICHT_EINGERICHTET)
        reihe = sorted(aehnlichkeit.items(), key=lambda kv: kv[1], reverse=True)
        name, best = reihe[0]
        zweit = reihe[1][1] if len(reihe) > 1 else -1.0
        if best >= self.schwelle and best - zweit >= self.abstand:
            return SpeakerVerdict(name, STATUS_BEKANNT)
        return SpeakerVerdict(None, STATUS_UNBEKANNT)

    def diarize(self, wav_bytes: bytes) -> SpeakerVerdict:
        try:
            refs, dateien = self._referenzen()
            if not refs:
                # Keine Datei: nichts angelernt. Dateien, aber keine nutzbar:
                # die Rechnung ist kaputt — das darf nicht wie "nicht
                # eingerichtet" aussehen (SPEAKER_STATE.md).
                return SpeakerVerdict(None, STATUS_AUSGEFALLEN if dateien else STATUS_NICHT_EINGERICHTET)
            e = self.fingerabdruck(_wav_audio(wav_bytes))
            aehnlich = {n: float(e @ r) for n, r in refs.items()}
            print("🎙  Verifikation: " + ", ".join(f"{n} {s:.2f}" for n, s in
                                                   sorted(aehnlich.items(), key=lambda kv: -kv[1])))
            return self.urteil(aehnlich)
        except Exception as e:
            # Jeder Fehler ist ein Ausfall, kein Fremder (SPEAKER_STATE.md).
            print(f"⚠️  Sprecher-Verifikation: {e}")
            return SpeakerVerdict(None, STATUS_AUSGEFALLEN)
