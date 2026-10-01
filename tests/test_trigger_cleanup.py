"""Das Trigger-Archiv räumt alte Clips weg — aber nicht die Marker-Clips.

Ausfuehren:
    ow-venv/bin/python tests/test_trigger_cleanup.py

Warum es diese Datei gibt: Am 2026-08-22 hat ein Neustart Audio zu Ohr-
Urteilen geloescht, seither sind die geschuetzt. Am 2026-10-01 stellte sich
heraus, dass die Marker-Clips des Rueckspul-Puffers (*_marker_rueckspul.wav)
es nicht waren — die einzigen Belege fuer Rufe, die nicht einmal ein
Near-Miss wurden, und Trainingsmaterial fuers Wakeword. Nach 30 Tagen waeren
sie beim naechsten Start still verschwunden.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant import assistant  # noqa: E402


class CleanupTest(unittest.TestCase):

    def test_alte_marker_bleiben_alte_trigger_gehen(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            alt = time.time() - 40 * 86400
            for name in ("20260801_120000_marker_rueckspul.wav", "20260801_120000_gaston_wake.wav",
                         "20260801_120000_gaston_rec.wav"):
                p = os.path.join(d, name)
                open(p, "wb").close()
                os.utime(p, (alt, alt))
            with mock.patch.object(assistant, "TRIGGER_AUDIO_DIR", d), \
                    mock.patch.object(assistant, "_geschuetzte_clips", return_value=set()):
                assistant._cleanup_trigger_audio()
            self.assertEqual(sorted(os.listdir(d)), ["20260801_120000_marker_rueckspul.wav"])


class KorpusSichernTest(unittest.TestCase):

    def test_fehler_beim_sichern_heisst_nichts_loeschen(self) -> None:
        """Faellt das Sichern aus, darf der Start NICHT aufraeumen — sonst
        waere der Schutz genau dann weg, wenn er gebraucht wird."""
        with mock.patch("tools.wake_corpus.run_sichern", side_effect=RuntimeError("Platte voll")):
            self.assertFalse(assistant._korpus_sichern())

    def test_erfolg(self) -> None:
        with mock.patch("tools.wake_corpus.run_sichern", return_value=0):
            self.assertTrue(assistant._korpus_sichern())


if __name__ == "__main__":
    unittest.main()
