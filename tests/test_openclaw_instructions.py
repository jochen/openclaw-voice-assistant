"""Voice-Anweisung als "instructions", nicht im Transkript (seit 2026-10-24).

Im input stand sie bei jeder Sprachnachricht im Sitzungsverlauf, im
Gedächtnis-Index und im Dreaming. Rutscht sie dorthin zurück, merkt das im
Betrieb niemand — deshalb hier festgehalten.
"""
import unittest

from voice_assistant.config import _DEFAULT_VOICE_INSTRUCTION
from voice_assistant.services.openclaw import _payload


class PayloadTest(unittest.TestCase):
    def test_anweisung_im_instructions_feld(self):
        body = _payload("🎤 [Sprecher: jochen] Licht an", "s", "REGEL", True)
        self.assertEqual(body["instructions"], "REGEL")
        self.assertEqual(body["input"], "🎤 [Sprecher: jochen] Licht an")
        self.assertEqual(body["user"], "s")

    def test_alter_weg_haengt_an(self):
        body = _payload("🎤 [Sprecher: jochen] Licht an", "s", "REGEL", False)
        self.assertNotIn("instructions", body)
        self.assertEqual(body["input"], "🎤 [Sprecher: jochen] Licht an\n\nREGEL")

    def test_erinnerung_nur_auf_dem_neuen_weg(self):
        neu = _payload("🎤 x", "s", "REGEL", True, "[kurz]")
        self.assertEqual(neu["input"], "🎤 x\n\n[kurz]")
        alt = _payload("🎤 x", "s", "REGEL", False, "[kurz]")
        self.assertEqual(alt["input"], "🎤 x\n\nREGEL")

    def test_ohne_anweisung_kein_feld(self):
        body = _payload("hallo", "s", "", True)
        self.assertNotIn("instructions", body)
        self.assertEqual(body["input"], "hallo")

    def test_default_ohne_beispielnamen(self):
        # Beispielnamen landeten als "Gesprächsinhalt" im Dreaming (Katrin)
        self.assertNotIn("Katrin", _DEFAULT_VOICE_INSTRUCTION)
        self.assertNotIn("obige Zeile", _DEFAULT_VOICE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
