"""Diagnose-Werte des ReSpeakers im Journal — ohne Netz und ohne Geraet.

Anlass 2026-10-28: ~30 Verbindungsabrisse pro Stunde, und es war nicht zu
sagen, ob der ESP neu startet oder nur das WLAN verliert. Der Kern ist die
Neustart-Erkennung: die Laufzeit muss einen Verbindungsabbruch ueberleben.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.audio.respeaker import RespeakerClient  # noqa: E402
from voice_assistant.config import RespeakerAudio  # noqa: E402


class DiagnoseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = RespeakerClient(RespeakerAudio(host="x"))

    def _logs(self, *werte) -> list[str]:
        with self.assertLogs("voice_assistant.audio.respeaker", "INFO") as cm:
            for name, wert in werte:
                self.c._diagnose(name, wert)
            self.c._diagnose("WLAN-Signal", -50.0)  # damit assertLogs nie leer ist
        return cm.output

    def test_neustart_nach_reconnect(self) -> None:
        self._logs(("Neustart-Grund", "Power On"), ("Laufzeit", 5000.0))
        self.c._reset_state()   # Verbindungsabbruch
        out = self._logs(("Neustart-Grund", "Brownout"), ("Laufzeit", 12.0))
        warn = [z for z in out if z.startswith("WARNING")]
        self.assertEqual(len(warn), 1)
        self.assertIn("Brownout", warn[0])
        self.assertIn("vorher Power On", " ".join(out))

    def test_nur_wlan_abriss_kein_neustart(self) -> None:
        self._logs(("Laufzeit", 5000.0))
        self.c._reset_state()
        out = self._logs(("Laufzeit", 5060.0))
        self.assertFalse([z for z in out if z.startswith("WARNING")])

    def test_signal_nur_bei_deutlicher_aenderung(self) -> None:
        out = self._logs(("WLAN-Signal", -60.0), ("WLAN-Signal", -62.0),
                         ("WLAN-Signal", -70.0))
        signal = [z for z in out if "WLAN-Signal" in z]
        # -60 (erst), -70 (Δ10), -50 (Δ20); -62 nicht
        self.assertEqual(len(signal), 3)

    def test_laufzeit_einmal_je_verbindung(self) -> None:
        out = self._logs(("Laufzeit", 100.0), ("Laufzeit", 160.0))
        self.assertEqual(len([z for z in out if "Laufzeit" in z]), 1)


if __name__ == "__main__":
    unittest.main()
