"""Ist auch die Antwort auf die Klärungs-Rückfrage des Aktuators unklar, geht
sie an den Brain — mit dem ersten Satz und dem Grund (locale.unklar_hinweis).

Anlass 2026-10-10: "Gaston macht den rechten Küchenrollo auf siebzig
Prozent." und die Antwort "den rechten Küchenrollo auf siebzig Prozent."
wurden beide von Regel A abgewiesen (Laya: Gruppe statt rechtes Rollo); der
Turn endete mit "wieder nicht verstanden", und der Brain traf es danach
sogar aus "Die Küche nur mal los auf siebzig Prozent."."""

import unittest

from voice_assistant.config import MAX_UNKLAR_ROUNDS, LocaleConfig, _parse_profile

_ERST = "Gaston macht den rechten Küchenrollo auf siebzig Prozent."
_GRUND = ("Gruppen-Ziel 'kuechenrollos' im Satz nicht belegt "
          "(braucht eines von: küche, küchenrollos, rollos)")


class UnklarHinweisTest(unittest.TestCase):
    def test_hinweis_traegt_ersten_satz_und_grund(self):
        h = LocaleConfig().unklar_hinweis_fuer(_ERST, _GRUND)
        self.assertIn(_ERST, h)
        self.assertIn(_GRUND, h)
        self.assertNotIn("{erst}", h)
        self.assertNotIn("{grund}", h)

    def test_geschweifte_klammern_im_transkript_brechen_nichts(self):
        h = LocaleConfig().unklar_hinweis_fuer("Rollo {links} auf", None)
        self.assertIn("Rollo {links} auf", h)
        self.assertIn("unbekannt", h)

    def test_aus_der_config_ueberschreibbar(self):
        p = _parse_profile("t", {"locale": {"unklar_hinweis": "[{erst} | {grund}]"}})
        self.assertEqual(p.locale.unklar_hinweis_fuer("a", "b"), "[a | b]")
        self.assertEqual(_parse_profile("t", {}).locale.unklar_hinweis,
                         LocaleConfig().unklar_hinweis)

    def test_genau_eine_rueckfrage_vor_dem_brain(self):
        # assistant.py: Runde 0 unklar → Rückfrage, Runde 1 unklar → Brain.
        self.assertEqual(MAX_UNKLAR_ROUNDS, 1)


class SatellitUnklarHinweisTest(unittest.TestCase):
    """Am Handy geht unklar ohne Rückfrage an den Brain (2026-11-15)."""

    def test_hinweis_traegt_grund(self):
        h = LocaleConfig().satellit_unklar_hinweis_fuer("kein Ziel erkannt")
        self.assertIn("kein Ziel erkannt", h)
        self.assertNotIn("{grund}", h)
        self.assertIn("unbekannt", LocaleConfig().satellit_unklar_hinweis_fuer(None))

    def test_aus_der_config_ueberschreibbar(self):
        p = _parse_profile("t", {"locale": {"satellit_unklar_hinweis": "[{grund}]"}})
        self.assertEqual(p.locale.satellit_unklar_hinweis_fuer("x"), "[x]")

    def test_zweig_in_assistant(self):
        # Der Zweig steht in der Hauptschleife, die sich nicht einzeln
        # aufrufen lässt: festhalten, dass Satelliten-Turns zum Brain gehen.
        import inspect
        from voice_assistant import assistant
        src = inspect.getsource(assistant)
        self.assertIn("or _satelliten_turn())", src)
        self.assertIn("satellit_unklar_hinweis_fuer", src)


if __name__ == "__main__":
    unittest.main()
