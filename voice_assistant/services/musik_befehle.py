"""Einfache Musik-Befehle direkt an die Musik-Bridge (ohne Brain).

Der feste Erkenner (musik_parser.py) liest den Befehl, diese Klasse fuehrt
ihn aus:

    POST {url}/api/v1/rooms/{raum}/control
         {"action", "source": "gaston", "requested_by"?}
      -> {"ok": true, "state", "note"?, "queue_ended"?, "applies_after_conversation"?}
       | {"ok": false, "reason": "...", "playing_in"?, ...}

Raum: der gesprochene („in der Kueche“), aufgeloest gegen die Raumnamen der
Bridge (GET /api/v1/rooms); sonst der Raum des Haupt-Mikrofons. Der ist bei
der Bridge eine Gruppe, stop/pause/lauter/leiser wirken dort auf jeden
spielenden Player, next/previous nur, wenn genau einer spielt (Bridge
2026-10-18).

**Spielt im Raum nichts, aber in genau einem anderen**, wirken „stopp“ und
„Pause“ dort (Vorschlag pi51, Jochen 2026-10-18: „gute Idee“), aber nur ohne
gesprochenen Raum. Die Bridge waehlt das nicht selbst, sie nennt die anderen
Raeume in ``playing_in``. Fuer alles andere (weiter, naechstes, lauter)
bleibt es beim Brain: wer dort etwas will, sagt den Raum.

**Alles, was nicht glatt geht, liefert None, und der Satz geht wie bisher an
den Brain**: Bridge weg, Raum unbekannt, mehrere Raeume, nichts zum
Fortsetzen, Musik in einem anderen Raum. Der Brain kann nachfragen, das kann
der Schnellweg nicht. Ausnahmen mit eigener Antwort: Lautstaerke am Anschlag.
Die LMS-Player stehen absichtlich auf 100 (Grundpegel am Echo), „lauter“
endet deshalb meist bei volume_at_max (Bridge 2026-10-18).

requested_by fehlt bewusst: der Sprecher steht erst nach dem Turn fest
(_sprecher_nachtragen), und die Bridge nimmt sonst den letzten Namen im Raum
(10 min).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from voice_assistant.services.musik_parser import (
    LAUTER, LEISER, NEXT, PAUSE, PREVIOUS, RESUME, STOP, MusikBefehl,
)

log = logging.getLogger(__name__)

_QUELLE = "gaston"

_OK_TEXT = {
    STOP: "Musik ist aus.",
    PAUSE: "Musik pausiert.",
    RESUME: "Okay.",
    NEXT: "Okay.",
    PREVIOUS: "Okay.",
    LAUTER: "Lauter.",
    LEISER: "Leiser.",
}
# Aktion -> Zustaende, in denen ein anderer Raum sie annimmt (Bridge: stop
# braucht play oder pause, pause braucht play).
_ANDERER_RAUM = {STOP: ("play", "pause"), PAUSE: ("play",)}
_ANSCHLAG_TEXT = {
    "volume_at_max": "Lauter geht nur am Echo selbst.",
    "volume_at_min": "Leiser geht nicht.",
}


@dataclass
class MusikErgebnis:
    text: str                  # was Gaston sagt
    raum: str                  # Bridge-Raum, an den der Befehl ging
    antwort: dict              # Antwort der Bridge (fuers Log)
    ms: float


class MusikBefehle:
    def __init__(self, url: str, token: str, raum: str, *, timeout: float = 1.5,
                 post=None, get=None) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._raum = raum
        self._timeout = timeout
        self._post = post or self._http_post
        self._get = get or self._http_get

    def ausfuehren(self, befehl: MusikBefehl) -> MusikErgebnis | None:
        """None = nicht erledigt, der Satz gehoert dem Brain."""
        t0 = time.monotonic()
        raum = self._raum if befehl.raum is None else self._raum_von(befehl.raum)
        if raum is None:
            return None
        antwort = self._senden(raum, befehl.aktion)
        if antwort is None:
            return None
        anderer = self._anderer_raum(befehl, antwort)
        if anderer is not None:
            raum = anderer
            antwort = self._senden(raum, befehl.aktion)
            if antwort is None:
                return None
        ms = (time.monotonic() - t0) * 1000
        if antwort.get("ok"):
            text = _OK_TEXT[befehl.aktion]
            if antwort.get("queue_ended"):
                text = "Das war das letzte Lied, die Musik ist aus."
            return MusikErgebnis(text, raum, antwort, ms)
        grund = antwort.get("reason")
        if grund in _ANSCHLAG_TEXT:
            return MusikErgebnis(_ANSCHLAG_TEXT[grund], raum, antwort, ms)
        log.info("Musik-Befehl %s an %s abgelehnt (%s) → Brain", befehl.aktion, raum, grund)
        return None

    def _senden(self, raum: str, aktion: str) -> dict | None:
        try:
            return self._post(f"{self._url}/api/v1/rooms/{raum}/control",
                              {"action": aktion, "source": _QUELLE})
        except Exception as exc:
            log.warning("Musik-Befehl %s an %s: %s → Brain", aktion, raum, exc)
            return None

    @staticmethod
    def _anderer_raum(befehl: MusikBefehl, antwort: dict) -> str | None:
        """Der eine andere Raum fuer stop/pause, wenn hier nichts spielt."""
        if (befehl.raum is not None or befehl.aktion not in _ANDERER_RAUM
                or antwort.get("ok") or antwort.get("reason") != "not_playing"):
            return None
        kandidaten = [str(r.get("room")) for r in antwort.get("playing_in") or []
                      if r.get("state") in _ANDERER_RAUM[befehl.aktion]]
        return kandidaten[0] if len(kandidaten) == 1 else None

    def _raum_von(self, gesprochen: str) -> str | None:
        """Gesprochener Raum → Bridge-Raum-id, ueber Name oder id."""
        try:
            raeume = self._get(f"{self._url}/api/v1/rooms")
        except Exception as exc:
            log.warning("Musik-Raeume nicht lesbar: %s → Brain", exc)
            return None
        g = gesprochen.lower()
        treffer = [str(r.get("room")) for r in raeume
                   if g in (str(r.get("room", "")).lower(), str(r.get("name", "")).lower())]
        return treffer[0] if len(treffer) == 1 else None

    def _http_get(self, url: str):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read() or b"[]")

    def _http_post(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            # 400 unknown action o. ae.: als Ablehnung behandeln
            return {"ok": False, "reason": f"http_{exc.code}"}
