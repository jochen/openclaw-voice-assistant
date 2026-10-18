"""musik_befehle.MusikBefehle — ohne Netz: was erledigt der Schnellweg selbst,
und was geht an den Brain (None)."""

import unittest

from voice_assistant.services.musik_befehle import MusikBefehle
from voice_assistant.services.musik_parser import LAUTER, NEXT, PAUSE, RESUME, STOP, MusikBefehl

_RAEUME = [{"room": "wohnzimmer", "name": "Wohnzimmer"}, {"room": "kueche", "name": "Küche"},
           {"room": "kachelofen", "name": "Kachelofen"}]


def _bauen(antwort):
    aufrufe = []

    def post(url, body):
        aufrufe.append((url.rsplit("/rooms/", 1)[1], body))
        if isinstance(antwort, Exception):
            raise antwort
        return antwort
    m = MusikBefehle("http://bridge", "t", "wohnzimmer", post=post, get=lambda url: _RAEUME)
    return m, aufrufe


class MusikBefehleTest(unittest.TestCase):
    def test_stop_ohne_raum_geht_an_das_haupt_mikrofon(self):
        m, aufrufe = _bauen({"ok": True, "state": "stop"})
        e = m.ausfuehren(MusikBefehl(STOP))
        self.assertEqual(e.text, "Musik ist aus.")
        self.assertEqual(aufrufe, [("wohnzimmer/control", {"action": "stop", "source": "gaston"})])

    def test_gesprochener_raum_ueber_den_namen(self):
        m, aufrufe = _bauen({"ok": True})
        m.ausfuehren(MusikBefehl(STOP, "Küche"))
        self.assertEqual(aufrufe[0][0], "kueche/control")

    def test_unbekannter_raum_an_den_brain(self):
        m, aufrufe = _bauen({"ok": True})
        self.assertIsNone(m.ausfuehren(MusikBefehl(STOP, "Garage")))
        self.assertEqual(aufrufe, [])

    def test_ablehnung_an_den_brain(self):
        for grund in ("several_rooms_playing", "nothing_to_resume", "not_playing",
                      "music_is_in_another_room", "room_unavailable"):
            m, _ = _bauen({"ok": False, "reason": grund})
            self.assertIsNone(m.ausfuehren(MusikBefehl(NEXT)), grund)

    def _zwei_antworten(self, erste, zweite):
        aufrufe = []

        def post(url, body):
            aufrufe.append(url.rsplit("/rooms/", 1)[1])
            return erste if len(aufrufe) == 1 else zweite
        return MusikBefehle("http://bridge", "t", "wohnzimmer", post=post,
                            get=lambda url: _RAEUME), aufrufe

    def test_stopp_wirkt_im_einen_anderen_raum(self):
        m, aufrufe = self._zwei_antworten(
            {"ok": False, "reason": "not_playing",
             "playing_in": [{"room": "kueche", "state": "play"}]},
            {"ok": True, "state": "stop"})
        e = m.ausfuehren(MusikBefehl(STOP))
        self.assertEqual((e.raum, e.text), ("kueche", "Musik ist aus."))
        self.assertEqual(aufrufe, ["wohnzimmer/control", "kueche/control"])

    def test_pause_nur_wo_es_spielt(self):
        m, aufrufe = self._zwei_antworten(
            {"ok": False, "reason": "not_playing",
             "playing_in": [{"room": "kueche", "state": "pause"}]}, {"ok": True})
        self.assertIsNone(m.ausfuehren(MusikBefehl(PAUSE)))
        self.assertEqual(aufrufe, ["wohnzimmer/control"])

    def test_zwei_andere_raeume_an_den_brain(self):
        m, aufrufe = self._zwei_antworten(
            {"ok": False, "reason": "not_playing",
             "playing_in": [{"room": "kueche", "state": "play"},
                            {"room": "bad", "state": "play"}]}, {"ok": True})
        self.assertIsNone(m.ausfuehren(MusikBefehl(STOP)))
        self.assertEqual(len(aufrufe), 1)

    def test_naechstes_nicht_im_anderen_raum(self):
        m, aufrufe = self._zwei_antworten(
            {"ok": False, "reason": "not_playing",
             "playing_in": [{"room": "kueche", "state": "play"}]}, {"ok": True})
        self.assertIsNone(m.ausfuehren(MusikBefehl(NEXT)))
        self.assertIsNone(m.ausfuehren(MusikBefehl(STOP, "Bad")))   # Raum gesagt: nicht umleiten

    def test_bridge_weg_an_den_brain(self):
        m, _ = _bauen(TimeoutError("timed out"))
        self.assertIsNone(m.ausfuehren(MusikBefehl(RESUME)))

    def test_lauter_am_anschlag_sagt_es(self):
        m, _ = _bauen({"ok": False, "reason": "volume_at_max"})
        self.assertEqual(m.ausfuehren(MusikBefehl(LAUTER)).text, "Lauter geht nur am Echo selbst.")

    def test_letztes_lied(self):
        m, _ = _bauen({"ok": True, "queue_ended": True, "state": "stop"})
        self.assertIn("letzte Lied", m.ausfuehren(MusikBefehl(NEXT)).text)


if __name__ == "__main__":
    unittest.main()
