"""Offline-Tests fuer das Sprecher-Urteil — die Trennung von "unbekannt" und "ausgefallen".

Laeuft OHNE Netz und ohne Speaches: der HTTP-Aufruf wird ersetzt.

Ausfuehren:
    ow-venv/bin/python tests/test_speaker_verdict.py

Warum es diese Datei gibt
-------------------------
Am 2026-09-18 hat der Assistent im Fablab Rechner ausgeschaltet, ohne dass ein
Sprecher erkannt war. Die Erkennung war nicht etwa unsicher — sie war TOT:
Speaches gab ab 20:01 HTTP 500. `diarize()` lieferte damals fuer JEDEN Fehler
schlicht None, und None wurde zum Label "unbekannt". Im Log stand also
"Sprecher: unbekannt", was aussieht wie "da sprach ein Fremder", tatsaechlich
aber hiess "wir haben gar nicht gemessen".

Genau diese Verschmelzung ist die Regressionsgefahr, und sie ist lautlos: ein
Ausfall und ein echter Fremder sehen danach identisch aus. Faellt die
Unterscheidung wieder weg, kann ein Dienstausfall erneut als harmloses
"unbekannt" durchlaufen — und das Gate, das auf `status` schaut, macht auf.

Die Tests unten sind entlang dieser Bruchlinie geschrieben: jeder Fehlerweg
muss `ausgefallen` ergeben, jeder gemessene Nicht-Treffer `unbekannt`.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.services import diarization as diar  # noqa: E402
from voice_assistant.services.diarization import (  # noqa: E402
    STATUS_AUSGEFALLEN,
    STATUS_BEKANNT,
    STATUS_NICHT_EINGERICHTET,
    STATUS_UNBEKANNT,
    SpeachesDiarizer,
    SpeakerVerdict,
    run_diarization,
    verdict_from_speaker,
)


def _wav(seconds: float = 6.0, rate: int = 16000) -> bytes:
    """Eine stille, aber formal gueltige Mono-WAV der gewuenschten Laenge."""
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _FakeResponse(io.BytesIO):
    """Minimales Stand-in fuer das Context-Manager-Objekt von urlopen()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DiarizeVerdictTest(unittest.TestCase):
    """Jeder Weg durch _diarize_pass fuehrt zum richtigen Status."""

    def setUp(self) -> None:
        self._orig_urlopen = diar.urllib.request.urlopen
        self._orig_speakers = diar._list_known_speakers
        # Eine Referenz, damit der Request gebaut wird wie im Betrieb.
        diar._list_known_speakers = lambda: [("jochen", _wav(2.0))]
        self.calls = 0

    def tearDown(self) -> None:
        diar.urllib.request.urlopen = self._orig_urlopen
        diar._list_known_speakers = self._orig_speakers

    def _install(self, handler) -> None:
        def fake_urlopen(req, timeout=None):
            self.calls += 1
            return handler(self.calls)

        diar.urllib.request.urlopen = fake_urlopen

    def _segments(self, speaker: str):
        body = json.dumps({"duration": 6.0, "segments": [
            {"start": 0.0, "end": 6.0, "speaker": speaker}
        ]}).encode()
        return _FakeResponse(body)

    # --- der Vorfall selbst -------------------------------------------------

    def test_http_500_ist_ausgefallen_nicht_unbekannt(self):
        """Der Fablab-Fall vom 2026-09-18: Speaches antwortet mit 500."""
        def handler(_n):
            raise urllib.error.HTTPError(
                "http://x/v1/audio/diarization", 500, "Internal Server Error",
                {}, io.BytesIO(b"Internal Server Error"),
            )

        self._install(handler)
        v = SpeachesDiarizer("http://x").diarize(_wav())
        self.assertEqual(v.status, STATUS_AUSGEFALLEN)
        self.assertIsNone(v.name)
        self.assertFalse(v.available)
        self.assertEqual(v.label, "Erkennung ausgefallen")

    def test_ausfall_wird_nicht_nachgetilt(self):
        """Nach einem Ausfall ist der Tile-Retry sinnlos — er verdoppelte nur die 500er.

        Im Fablab-Log vom 2026-09-18 standen deshalb zwei HTTP 500 pro Turn.
        """
        def handler(_n):
            raise urllib.error.HTTPError("http://x", 500, "kaputt", {}, io.BytesIO(b""))

        self._install(handler)
        SpeachesDiarizer("http://x").diarize(_wav(2.0))  # kurz genug zum Tilen
        self.assertEqual(self.calls, 1, "Ausfall darf keinen zweiten Request ausloesen")

    def test_netzfehler_ist_ausgefallen(self):
        """Timeout/Verbindungsabbruch ist ebenfalls 'nicht gemessen'."""
        def handler(_n):
            raise OSError("timed out")

        self._install(handler)
        v = SpeachesDiarizer("http://x").diarize(_wav())
        self.assertEqual(v.status, STATUS_AUSGEFALLEN)

    # --- echte Messungen ----------------------------------------------------

    def test_bekannter_sprecher(self):
        self._install(lambda _n: self._segments("jochen"))
        v = SpeachesDiarizer("http://x").diarize(_wav())
        self.assertEqual(v.status, STATUS_BEKANNT)
        self.assertEqual(v.name, "jochen")
        self.assertEqual(v.label, "jochen")
        self.assertTrue(v.available)

    def test_anonymer_cluster_ist_unbekannt(self):
        """SPEAKER_00 heisst: gemessen, aber niemandem zugeordnet."""
        self._install(lambda _n: self._segments("SPEAKER_00"))
        v = SpeachesDiarizer("http://x").diarize(_wav())
        self.assertEqual(v.status, STATUS_UNBEKANNT)
        self.assertIsNone(v.name)
        self.assertEqual(v.label, "unbekannt")
        self.assertTrue(v.available, "ein Nicht-Treffer ist eine gueltige Messung")

    def test_leere_segmente_sind_unbekannt(self):
        self._install(lambda _n: _FakeResponse(json.dumps({"segments": []}).encode()))
        v = SpeachesDiarizer("http://x").diarize(_wav())
        self.assertEqual(v.status, STATUS_UNBEKANNT)

    def test_retry_on_miss_bleibt_erhalten(self):
        """Kurzer Clip, erster Durchlauf ohne Treffer → getilt nochmal, dann Treffer."""
        def handler(n):
            return self._segments("SPEAKER_00" if n == 1 else "jochen")

        self._install(handler)
        v = SpeachesDiarizer("http://x").diarize(_wav(2.0))
        self.assertEqual(self.calls, 2)
        self.assertEqual(v.status, STATUS_BEKANNT)
        self.assertEqual(v.name, "jochen")


class RunDiarizationTest(unittest.TestCase):
    """Auch ein Absturz des Workers darf nicht als 'unbekannt' erscheinen."""

    def test_worker_absturz_ist_ausgefallen(self):
        import queue

        class Boom:
            def diarize(self, _b):
                raise RuntimeError("kaputt")

        q: queue.Queue = queue.Queue()
        run_diarization(Boom(), b"", q)
        self.assertEqual(q.get_nowait().status, STATUS_AUSGEFALLEN)


class VerdictHelperTest(unittest.TestCase):
    def test_nicht_eingerichtet_ist_nicht_unbekannt(self):
        v = SpeakerVerdict(None, STATUS_NICHT_EINGERICHTET)
        self.assertFalse(v.available)
        self.assertEqual(v.label, "Erkennung nicht eingerichtet")

    def test_altes_aufrufschema(self):
        self.assertEqual(verdict_from_speaker("petra").status, STATUS_BEKANNT)
        self.assertEqual(verdict_from_speaker(None).status, STATUS_UNBEKANNT)


class SpeakerStateFileTest(unittest.TestCase):
    """Die Datei, die das Gate liest — Inhalt und Robustheit."""

    def test_schreibt_status_und_namen(self):
        from voice_assistant.services.speaker_state import write_current_speaker

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "current_speaker.json")
            write_current_speaker(SpeakerVerdict("jochen", STATUS_BEKANNT), "misterhandy", path=p)
            data = json.load(open(p, encoding="utf-8"))
            self.assertEqual(data["status"], STATUS_BEKANNT)
            self.assertEqual(data["name"], "jochen")
            self.assertEqual(data["wakeword"], "misterhandy")
            self.assertIsInstance(data["epoch"], int)

    def test_ausfall_wird_auch_geschrieben(self):
        """Gerade der Ausfall MUSS in der Datei stehen.

        Wuerde er uebersprungen, bliebe der letzte bekannte Sprecher stehen und
        das Gate machte auf — das waere der Fablab-Fall in Dateiform.
        """
        from voice_assistant.services.speaker_state import write_current_speaker

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "current_speaker.json")
            write_current_speaker(SpeakerVerdict("jochen", STATUS_BEKANNT), path=p)
            write_current_speaker(SpeakerVerdict(None, STATUS_AUSGEFALLEN), path=p)
            data = json.load(open(p, encoding="utf-8"))
            self.assertEqual(data["status"], STATUS_AUSGEFALLEN)
            self.assertIsNone(data["name"])

    def test_fehlendes_verzeichnis_wird_angelegt(self):
        from voice_assistant.services.speaker_state import write_current_speaker

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "voice", "current_speaker.json")
            write_current_speaker(SpeakerVerdict("jochen", STATUS_BEKANNT), path=p)
            self.assertTrue(os.path.exists(p))

    def test_unbeschreibbarer_pfad_bricht_den_turn_nicht_ab(self):
        """Schreibfehler werden geschluckt — und hinterlassen kein .tmp.

        Hier blockiert eine regulaere DATEI den Verzeichnisplatz, makedirs
        scheitert also wirklich. Der Aufruf darf trotzdem nicht werfen: ein
        misslungener Schreibvorgang soll den Sprach-Turn nicht kosten.
        """
        from voice_assistant.services.speaker_state import write_current_speaker

        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "blockiert")
            open(blocker, "w").close()
            write_current_speaker(
                SpeakerVerdict("jochen", STATUS_BEKANNT),
                path=os.path.join(blocker, "current_speaker.json"),
            )
            self.assertEqual(sorted(os.listdir(d)), ["blockiert"], "kein .tmp-Rest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
