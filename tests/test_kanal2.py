"""Kanal 2 laeuft im Gleichschritt mit den Chunks, die read_chunk liefert — ohne Geraet.

Grundlage fuer *_rec_kanal2.wav: die Datei muss genau die Chunks der
Aufnahme abdecken. Verrutscht die Zuordnung, vergleicht der STT-Vergleich
Kanal 0 gegen Kanal 1 zwei verschiedene Zeitabschnitte.
"""

import collections
import os
import queue
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio.respeaker import _SAMPLES_PER_CHUNK, RespeakerClient  # noqa: E402


def _client() -> RespeakerClient:
    c = RespeakerClient.__new__(RespeakerClient)        # ohne Verbindungs-Thread
    c._audio_q = queue.Queue()
    c._buf = b""
    c._buf2 = b""
    c._kanal2_ring = collections.deque(maxlen=2000)
    c._data2_gemeldet = True
    c._mitschnitt = None
    c._mitschnitt_lock = threading.Lock()
    return c


def _paket(wert1: int, wert2: int, n: int = 512) -> tuple[bytes, bytes]:
    """Eine ESPHome-Audionachricht: 512 Samples je Kanal (1024 Bytes, wie gemessen)."""
    return (np.full(n, wert1, dtype=np.int16).tobytes(),
            np.full(n, wert2, dtype=np.int16).tobytes())


class GleichschrittTest(unittest.TestCase):
    def test_kanal2_folgt_den_chunks(self):
        c = _client()
        # 5 Pakete à 512 Samples = 2560 Samples = 4 Chunks à 640
        for k in range(5):
            c._audio_q.put(_paket(0, 0))
        geliefert = [c.read_chunk() for _ in range(4)]
        self.assertTrue(all(len(x) == _SAMPLES_PER_CHUNK for x in geliefert))
        self.assertEqual(len(c.kanal2_letzte(4)), 4)
        self.assertEqual(len(c.kanal2_letzte(10)), 4)      # nicht mehr als geliefert

    def test_null_chunk_haelt_die_zuordnung(self):
        c = _client()
        c._audio_q.put((b"", b""))                          # EOS → Null-Chunk
        c.read_chunk()
        c._audio_q.put(_paket(0, 0, 640))
        c.read_chunk()
        self.assertEqual(len(c.kanal2_letzte(2)), 2)

    def test_inhalt_ist_kanal2(self):
        c = _client()
        # Kanal 1 = Rampe, Kanal 2 = Sinus — nach der Aufbereitung (Mittelwert
        # weg, x4) muss Kanal 2 erkennbar Kanal 2 sein, nicht Kanal 1.
        t = np.arange(640)
        k1 = (t - 320).astype(np.int16)
        k2 = (1000 * np.sin(2 * np.pi * t / 64)).astype(np.int16)
        c._audio_q.put((k1.tobytes(), k2.tobytes()))
        a = c.read_chunk()
        b = c.kanal2_letzte(1)[0]
        self.assertGreater(np.corrcoef(b, k2)[0, 1], 0.99)
        self.assertGreater(np.corrcoef(a, k1)[0, 1], 0.99)

    def test_ohne_zweiten_kanal_leer(self):
        c = _client()
        c._data2_gemeldet = False
        c._audio_q.put((np.zeros(640, dtype=np.int16).tobytes(), b""))
        c.read_chunk()
        self.assertEqual(c.kanal2_letzte(1), [])


if __name__ == "__main__":
    unittest.main()
