"""Selbst-Labels aus dem Wake-Log: ein Stopp-Wort labelt den Trigger nicht mehr
(nur Hörkandidat); ein Barge-in mit Stopp-Wort ist ein Ruf.

Anlass 2026-10-31: das Abbruch-Label landete auf dem Barge-in-Clip mit einem
echten "Stopp, Gaston" und ging so als Negativ ins Training.
"""

import unittest

from tools.wake_triage import ECHT, _selbst_labels, gestoppte_ohne_ohr

W = "20261012_220814_gaston_wake.wav"
B = "20261012_220824_gaston_bargein.wav"


def _ev(ts, result, audio, **kw):
    return {"ts": f"2026-10-12T{ts}", "result": result, "audio": audio, **kw}


class StoppImErstTurnTest(unittest.TestCase):
    # Regel 3 abgeschafft: per Ohr nur 16 von 29 richtig, 8 mit klarem Gaston.
    def test_stopp_in_erster_aufnahme_kein_label(self):
        ev = [_ev("22:08:14", "trigger", W),
              _ev("22:08:19", "outcome", W, ausgang="stopwort")]
        self.assertEqual(_selbst_labels(ev), {})
        self.assertEqual(gestoppte_ohne_ohr(ev, set()), [W])
        self.assertEqual(gestoppte_ohne_ohr(ev, {W}), [])

    def test_bargein_ist_kein_hoerkandidat(self):
        ev = [_ev("22:08:19", "outcome", W, ausgang="brain"),
              _ev("22:08:27", "outcome", W, ausgang="stopwort", via="bargein")]
        self.assertEqual(gestoppte_ohne_ohr(ev, set()), [])


class BargeinTest(unittest.TestCase):
    def test_bargein_clip_ist_echter_ruf_trigger_ohne_label(self):
        l = _selbst_labels([
            _ev("22:08:14", "trigger", W),
            _ev("22:08:19", "outcome", W, ausgang="brain"),
            _ev("22:08:24", "bargein", B),
            _ev("22:08:27", "outcome", B, ausgang="stopwort", via="bargein"),
        ])
        self.assertEqual(l[B]["klasse"], ECHT)
        self.assertNotIn(W, l)

    def test_alte_zeile_unter_trigger_audio(self):
        # Vor der eigenen Barge-in-Audio-Spalte: outcome trägt das Trigger-Audio.
        l = _selbst_labels([
            _ev("16:57:29", "trigger", W),
            _ev("16:57:36", "outcome", W, ausgang="brain"),
            _ev("16:57:52", "bargein", B),
            _ev("16:57:55", "outcome", W, ausgang="stopwort", via="bargein"),
        ])
        self.assertEqual(l[B]["klasse"], ECHT)
        self.assertNotIn(W, l)

    def test_altes_bargein_zu_weit_weg_kein_label(self):
        l = _selbst_labels([
            _ev("16:57:00", "bargein", B),
            _ev("16:57:30", "outcome", W, ausgang="stopwort", via="bargein"),
        ])
        self.assertEqual(l, {})

    def test_bargein_ohne_stopwort_kein_label(self):
        l = _selbst_labels([
            _ev("22:08:24", "bargein", B),
            _ev("22:08:27", "outcome", B, ausgang="bargein_still", via="bargein"),
        ])
        self.assertEqual(l, {})


class SpaeteresStoppTest(unittest.TestCase):
    def test_stopp_in_spaeterer_zeile_labelt_trigger_nicht(self):
        l = _selbst_labels([
            _ev("22:08:14", "trigger", W),
            _ev("22:08:19", "outcome", W, ausgang="brain"),
            _ev("22:08:40", "outcome", W, ausgang="stopwort"),
        ])
        self.assertNotIn(W, l)

    def test_aktuator_bleibt_echter_ruf(self):
        l = _selbst_labels([
            _ev("22:08:14", "trigger", W),
            _ev("22:08:17", "outcome", W, ausgang="aktuator", status="ausgefuehrt",
                ziel="x", aktion="an"),
        ])
        self.assertEqual(l[W]["klasse"], ECHT)


if __name__ == "__main__":
    unittest.main()
