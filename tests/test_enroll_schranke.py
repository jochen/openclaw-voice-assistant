"""Eine bestehende Stimm-Referenz überschreibt nur ein erkannter Sprecher.

Sonst könnte sich ein Fremder als "jochen" anlernen und danach die
Sprecher-Schranke passieren (Jochen, 2026-10-24). Jede Lücke im
Sprecher-Zustand muss als Verweigerung enden, nie als Erlaubnis.
"""
import json
import os
import tempfile
import unittest

from voice_assistant.services.enroll_server import ueberschreiben_verweigert


class UeberschreibenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "current_speaker.json")

    def tearDown(self):
        self.dir.cleanup()

    def _zustand(self, status, epoch=1000.0, label=None):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"status": status, "epoch": epoch, "label": label or status}, f)

    def test_bekannt_und_frisch_darf(self):
        self._zustand("bekannt", label="jochen")
        self.assertIsNone(ueberschreiben_verweigert(self.path, jetzt=1030.0))

    def test_unbekannt_darf_nicht(self):
        self._zustand("unbekannt")
        self.assertIn("unbekannt", ueberschreiben_verweigert(self.path, jetzt=1030.0))

    def test_ausgefallen_darf_nicht(self):
        self._zustand("ausgefallen", label="Erkennung ausgefallen")
        self.assertIsNotNone(ueberschreiben_verweigert(self.path, jetzt=1030.0))

    def test_alter_turn_darf_nicht(self):
        self._zustand("bekannt", label="jochen")
        self.assertIn("alt", ueberschreiben_verweigert(self.path, jetzt=2000.0))

    def test_fehlende_datei_darf_nicht(self):
        self.assertIsNotNone(ueberschreiben_verweigert(self.path, jetzt=1030.0))

    def test_kaputte_datei_darf_nicht(self):
        with open(self.path, "w") as f:
            f.write("{halb")
        self.assertIsNotNone(ueberschreiben_verweigert(self.path, jetzt=1030.0))


if __name__ == "__main__":
    unittest.main()


class MeldungTest(unittest.TestCase):
    """Jedes Anlernen geht in den Kanal, mit dem erkannten Sprecher (2026-11-11)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "current_speaker.json")

    def tearDown(self):
        self.dir.cleanup()

    def _zustand(self, **d):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_neu_nennt_namen_und_sprecher(self):
        from voice_assistant.services.enroll_server import meldung
        t = meldung("neu", "felix", "unbekannt")
        self.assertIn("felix", t)
        self.assertIn("unbekannt", t)
        self.assertIn("löschen", t)

    def test_sprecher_nicht_sicher_steht_dabei(self):
        from voice_assistant.services.enroll_server import _sprecher_jetzt
        self._zustand(status="bekannt", label="jochen", epoch=1000.0, sicher=False, aehnlichkeit=0.47)
        self.assertEqual(_sprecher_jetzt(self.path, jetzt=1010.0), "jochen (nicht sicher, 0.47)")

    def test_alter_oder_fehlender_zustand(self):
        from voice_assistant.services.enroll_server import _sprecher_jetzt
        self._zustand(status="bekannt", label="jochen", epoch=1000.0)
        self.assertEqual(_sprecher_jetzt(self.path, jetzt=2000.0), "kein frischer Sprach-Turn")
        self.assertIn("unbekannt", _sprecher_jetzt(os.path.join(self.dir.name, "fehlt"), jetzt=1.0))
