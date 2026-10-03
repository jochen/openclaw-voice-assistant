"""Ein Follow-up endet auch mit "Danke" — aber nur, wenn der ganze Satz ein Dank ist.

Die Faelle unter "echt" stammen aus dem Journal (Follow-up-Runden, 2026-08/09).
Ohne Netz, ohne Modelle: nur das Muster aus voice_assistant/assistant.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.assistant import _is_dank_abschluss, _is_stop_command  # noqa: E402


class DankAbschlussTest(unittest.TestCase):
    def test_echt_beendet(self):
        self.assertTrue(_is_dank_abschluss("Danke."))
        self.assertTrue(_is_dank_abschluss("Danke, wunderbar."))

    def test_echt_bleibt_auftrag(self):
        # Ein Dank mit Auftrag dahinter muss an den Brain.
        self.assertFalse(_is_dank_abschluss(
            "Wunderbar. Perfekter Tipp. Danke dir. Trag das gleich für morgen ein."))
        self.assertFalse(_is_dank_abschluss(
            "Danke. Sie muss Ihnen das Gefühl geben, wie sagt man, wieder jung zu sein?"))

    def test_formen(self):
        for satz in ("Vielen Dank!", "Dankeschön.", "Danke schön, Gaston.",
                     "Okay, danke dir.", "Super, danke, das war's.",
                     "Gastau, danke.", "Danke, Mister Handy.", "Ja, danke, passt."):
            with self.subTest(satz=satz):
                self.assertTrue(_is_dank_abschluss(satz))

    def test_kein_dank(self):
        for satz in ("", "Super.", "Okay.", "Das war's.",
                     "Danke, und mach noch das Küchenlicht an",
                     "Ich bin dir dankbar für die Hilfe",
                     "Wie warm ist es draußen"):
            with self.subTest(satz=satz):
                self.assertFalse(_is_dank_abschluss(satz))

    def test_stopp_unveraendert(self):
        # "Danke" erweitert nicht das Stopp-Muster selbst — das bleibt fuer
        # Barge-in und Rueckfrage, wo ein Dank nichts beenden soll.
        self.assertFalse(_is_stop_command("Danke.", 1))
        self.assertTrue(_is_stop_command("Stopp", 1))


if __name__ == "__main__":
    unittest.main()
