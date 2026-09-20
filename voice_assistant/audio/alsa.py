"""Lokale Audio-Quelle und -Senke über ALSA (PyAudio + aplay)."""

from __future__ import annotations

import subprocess
import threading

import numpy as np
import pyaudio
from scipy.signal import resample_poly

from voice_assistant.config import CHANNELS, CHUNK_SIZE, LocalAudio


class AlsaSource:
    """Microphone-Quelle über PyAudio. Resampelt intern auf 16 kHz wenn nötig."""

    def __init__(self, cfg: LocalAudio) -> None:
        self.cfg = cfg
        self._pa: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None

    def start(self) -> None:
        print(
            f"🎙️  ALSA Mic: RATE_IN={self.cfg.rate_in}, "
            f"RESAMPLE={self.cfg.resample}, DEVICE_INDEX={self.cfg.device_index}"
        )
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=self.cfg.rate_in,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
            input_device_index=self.cfg.device_index,
        )

    def read_chunk(self) -> np.ndarray:
        assert self._stream is not None
        raw = self._stream.read(CHUNK_SIZE, exception_on_overflow=False)
        audio_in = np.frombuffer(raw, dtype=np.int16)
        if self.cfg.resample:
            return resample_poly(audio_in, 1, 3).astype(np.int16)
        return audio_in

    def flush(self) -> None:
        if self._stream:
            self._stream.stop_stream()
            self._stream.start_stream()

    def close(self) -> None:
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa:
            self._pa.terminate()
            self._pa = None


class AlsaSink:
    """Wiedergabe über `aplay`, Gerät aus Profil.

    Popen statt subprocess.run, damit stop() die laufende Wiedergabe mitten im
    Satz beenden kann (Barge-in). Verhalten ohne stop() ist unveraendert: die
    Methode kehrt erst zurueck, wenn aplay fertig ist.
    """

    def __init__(self, device: str | None) -> None:
        self.device = device
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        # Abbruch, der die Wiedergabe VOR dem Start erwischt: ohne diese Marke
        # wuerde ein stop(), das eine Mikrosekunde zu frueh kommt, ins Leere
        # laufen und der naechste Satz trotzdem anfangen zu spielen.
        self._stopped = False

    def play_wav(self, path: str) -> None:
        cmd = ["aplay", "-q"]
        if self.device:
            cmd += ["-D", self.device]
        cmd.append(path)
        with self._lock:
            if self._stopped:
                return
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._proc = proc
        try:
            proc.wait()
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception as e:
            print(f"⚠️  aplay-Abbruch fehlgeschlagen: {e}")

    def resume(self) -> None:
        """Abbruch-Marke loeschen — der naechste Turn darf wieder sprechen."""
        with self._lock:
            self._stopped = False
