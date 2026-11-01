"""Clip-Aufträge für ein neues Wakewort (wakeword_studio/clips_erzeugen.py), ohne Piper."""

import tempfile
import unittest
from pathlib import Path

import yaml

from wakeword_studio.clips_erzeugen import auftraege, config_lesen, stimme_url

VORLAGE = Path(__file__).resolve().parent.parent / "wakeword_studio" / "clips_vorlage.yaml"


class ClipsTest(unittest.TestCase):
    def test_stimme_url(self):
        self.assertEqual(
            stimme_url("de_DE-thorsten_emotional-medium"),
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/"
            "thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx")
        self.assertTrue(stimme_url("en_US-lessac-high").endswith(
            "/en/en_US/lessac/high/en_US-lessac-high.onnx"))

    def test_vorlage_und_mengen(self):
        cfg = config_lesen(str(VORLAGE))
        jobs = auftraege(cfg, Path("voices"), Path("train_out/gaston"))
        soll = {}
        for j in jobs:
            k = j["ziel"].name
            soll[k] = soll.get(k, 0) + j["n"]
        # Ganzzahlige Teilung je Stimme: knapp unter der Vorgabe, nie darüber.
        for k, n in cfg["anzahl"].items():
            self.assertLessEqual(soll[k], n)
            self.assertGreater(soll[k], 0.99 * n)
        nasal = [j for j in jobs if j["tag"].startswith("nasal__")]
        self.assertTrue(all(j["phoneme"] for j in nasal))
        self.assertEqual(len({j["tag"] for j in jobs}), len(jobs))  # Tags eindeutig
        self.assertTrue(all(j["tag"].count("__") == 2 for j in jobs))  # pruefen gruppiert daran

    def test_anteile_muessen_eins_ergeben(self):
        cfg = yaml.safe_load(VORLAGE.read_text())
        cfg["positiv"][0]["anteil"] = 0.5
        p = Path(tempfile.mkdtemp()) / "c.yaml"
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True))
        with self.assertRaises(ValueError):
            config_lesen(str(p))


if __name__ == "__main__":
    unittest.main()
