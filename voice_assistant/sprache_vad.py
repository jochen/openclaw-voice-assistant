"""Sprach-Entscheid fürs Aufnahme-Ende: WebRTC-VAD, mit Silero als Einspruch im Raumpegel.

Anlass (Fablab, 2026-11-04 bis 07): Lief der Lasercutter mit seinen Lüftern,
endeten Aufnahmen nicht mehr nach der Sprechpause, sondern erst am Deckel
(30 s im Dialog). Der Grundpegel lag dann bei 700–1000 RMS, also über der
festen Schranke `vad_voice_rms_min: 900`, und WebRTC-VAD (Stufe 3, das
strengste) hielt 80–100 % der Chunks für Sprache. Gleichmäßiges,
breitbandiges Rauschen in diesem Pegel trennt es nicht von Sprache.

Die Regel (`vad_silero`): WebRTC entscheidet wie bisher. Sagt es "Sprache",
darf Silero-VAD widersprechen, aber nur, wenn der Chunk leiser ist als
`vad_silero_faktor` × Raumpegel vor dem Ruf (`umgebung.gleitend`). Lautes,
nahes Sprechen überstimmt Silero also nie. Ohne Raumpegel (Umgebungspegel
aus, zu kurz gemessen) gilt die alte Regel.

Gemessen 2026-11-13 über die archivierten Aufnahmen, Dialog-Endpointing
(2 s / 30 s), Wörter per Qwen bis zum alten und neuen Schnitt verglichen:

| | Aufnahmen | früher beendet | davon Wörter verloren |
|---|---|---|---|
| daheim | 483 | 12 | 0 (einige Befehle 1–4,6 s früher fertig) |
| Fablab | 42 | 4 | 0 bis zur Frage; weg ist das Gespräch im Raum danach |

Die beiden Lasercutter-Aufnahmen im Fablab-Archiv endeten nach 10,2 und
3,8 s statt am 30-s-Deckel. Silero ALLEIN (ohne den Pegel-Vorbehalt) war
zu streng: daheim 58 frühere Schnitte, darunter ein verlorenes "Stopp,
Stopp" und ein gekürzter Befehl. Schwelle 0,3 und Faktor 2 waren in diesem
Vergleich am schonendsten; 0,5 und 3 lagen dicht daneben. Grenze der
Messung: nur zwei laute Erst-Aufnahmen; Follow-ups werden im Fablab erst
seit diesem Einbau archiviert.

Rechenlast: 1,7 ms je 32-ms-Fenster auf dem Fablab-Pi (~5 % eines Kerns),
nur während einer Aufnahme.

Jede Entscheidung wird mitgezählt, damit `endpoint.log` sagt, wann die alte
Regel geendet hätte (`vad_alt_ende_s`) und wie viel Silero überstimmt hat.
"""
from __future__ import annotations

import os
from typing import Callable

import numpy as np

MODELL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "models", "silero_vad", "silero_vad.onnx")
_FENSTER = 512      # 32 ms bei 16 kHz, fest im Modell
_KONTEXT = 64


class SileroVad:
    """Silero-VAD (ONNX) über einen Strom beliebig großer Chunks."""

    def __init__(self, pfad: str = MODELL, rate: int = 16000) -> None:
        import onnxruntime as ort
        opt = ort.SessionOptions()
        opt.inter_op_num_threads = 1
        opt.intra_op_num_threads = 1
        opt.log_severity_level = 3
        self._s = ort.InferenceSession(pfad, sess_options=opt,
                                       providers=["CPUExecutionProvider"])
        self._sr = np.array(rate, np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), np.float32)
        self._ctx = np.zeros((1, _KONTEXT), np.float32)
        self._rest = np.zeros(0, np.float32)
        self._letzte = 0.0

    def prob(self, chunk: np.ndarray) -> float:
        """Höchste Sprach-Wahrscheinlichkeit der in diesem Chunk fertig
        gewordenen Fenster; ohne fertiges Fenster die des letzten."""
        x = np.concatenate([self._rest, chunk.astype(np.float32) / 32768.0])
        n = len(x) // _FENSTER
        best = None
        for i in range(n):
            inp = np.concatenate([self._ctx, x[None, i * _FENSTER:(i + 1) * _FENSTER]], axis=1)
            p, self._state = self._s.run(None, {"input": inp, "state": self._state, "sr": self._sr})
            self._ctx = inp[:, -_KONTEXT:]
            self._letzte = float(p[0][0])
            best = self._letzte if best is None else max(best, self._letzte)
        self._rest = x[n * _FENSTER:]
        return self._letzte if best is None else best


Stats = Callable[..., tuple[bool, float, int, int]]


class SprachEntscheider:
    """Je Chunk: ist das Sprache? Dazu der Schatten der alten Regel fürs Log."""

    def __init__(self, stats: Stats, vad, rms_min: float, silero: SileroVad | None,
                 schwelle: float = 0.3, faktor: float = 2.0) -> None:
        self._stats = stats
        self._vad = vad
        self._rms_min = rms_min
        self._silero = silero
        self.schwelle = schwelle
        self.faktor = faktor
        self.neuer_turn(None, aktiv=False)

    def neuer_turn(self, raum: float | None, aktiv: bool = True) -> None:
        """Vor jeder Aufnahme (Erst-Turn wie Follow-up). raum = Raumpegel vor
        dem Ruf; aktiv=False (z. B. Handy-Satellit) heißt: alte Regel."""
        self.raum = raum
        self.aktiv = bool(aktiv and self._silero is not None and raum)
        if self._silero is not None:
            self._silero.reset()
        self._chunks = 0
        self._alt_gesprochen = False
        self._alt_stille = 0
        self.alt_ende_chunk: int | None = None
        self.ueberstimmt = 0

    def chunk(self, audio_16: np.ndarray, nachlauf_chunks: int) -> tuple[bool, float, int, int]:
        """(ist_sprache, rms, sprach_frames, frames) wie `_chunk_speech_stats`.
        nachlauf_chunks: die gerade geltende Stille-Grenze, nur für den Schatten."""
        alt, rms, sf, tf = self._stats(self._vad, audio_16, self._rms_min)
        neu = alt
        if self.aktiv:
            p = self._silero.prob(audio_16)
            if alt and p < self.schwelle and rms < self.faktor * self.raum:
                neu = False
                self.ueberstimmt += 1
        self._chunks += 1
        if self.alt_ende_chunk is None:
            if alt:
                self._alt_gesprochen, self._alt_stille = True, 0
            elif self._alt_gesprochen:
                self._alt_stille += 1
                if self._alt_stille >= nachlauf_chunks:
                    self.alt_ende_chunk = self._chunks
        return neu, rms, sf, tf

    def als_log(self, chunk_sec: float) -> dict:
        if not self.aktiv:
            return {}
        return {
            "vad_silero": True,
            "vad_raum": round(self.raum, 1),
            "silero_ueberstimmt_s": round(self.ueberstimmt * chunk_sec, 2),
            # None: die alte Regel hätte bis hierher nicht geendet
            "vad_alt_ende_s": (round(self.alt_ende_chunk * chunk_sec, 2)
                               if self.alt_ende_chunk is not None else None),
        }
