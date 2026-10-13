"""Musik leiser, solange ein Turn laeuft (Ducking ueber eine Musik-Bridge).

Gegenstelle ist eine REST-Schnittstelle mit Lease-Semantik:

    POST {url}/api/v1/rooms/{raum}/duck    {"source", "level"?, "timeout_s"?}
         -> {"ducked": true, "lease_id": "…"} | {"ducked": false, "reason": "…"}
    POST {url}/api/v1/rooms/{raum}/unduck  {"lease_id"}

Die Bridge stellt nach ``timeout_s`` ohne erneutes duck selbst wieder her
(Absturzschutz). Ein laengerer Turn erneuert deshalb alle ``erneuern_s``.

Regeln:

- **Nie blockieren.** Die Hauptschleife setzt nur einen Wunsch
  (``zustand()``), die Aufrufe macht ein eigener Thread.
- **Gedaempft wird ab dem Wakewort und bis zum Leerlauf** (Antwort, Follow-up-
  Fenster, Rueckfragen eingeschlossen), danach unduck. Schon beim Ueberschreiten
  der Schwelle (``vorab()``), nicht erst beim Turn-Beginn: der kommt erst, wenn
  die Treffer-Serie abreisst, gemessen 0,62–0,78 s spaeter (2026-10-13, bei
  laufender Musik). Wird der Ruf danach doch verworfen (Pegel-Gate), gibt
  ``verworfen()`` sofort frei; jeden anderen Weg ohne Turn faengt
  ``_VORAB_FRIST`` ab.
- **Ansagen von aussen** (``voice_speak_text``) daempfen ebenfalls, solange sie
  laufen (``ansage()``). Der Timer nicht (Jochen, 2026-10-13): weder sein
  Klingeln noch „Der Timer ist abgelaufen“.
- **Nur Turns des Haupt-Mikrofons.** Ein Satelliten-Turn (Handy) hat keinen
  Raum (audio/satellit.py) und daempft nichts.
- **Erneuert wird nur, was das erste duck tatsaechlich gedaempft hat.** Lief
  beim Ruf keine Musik, bleibt es dabei — sonst wuerde die Musik gedaempft,
  die Gaston in diesem Turn gerade selbst gestartet hat.
- Ob ein zweites duck dieselbe Lease verlaengert oder eine neue anlegt, ist
  der Bridge ueberlassen: am Ende wird jede erhaltene ``lease_id`` freigegeben.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.request

from voice_assistant.state import STATE_LISTENING

log = logging.getLogger(__name__)

# So lange darf ein vorab gedaempfter Ruf auf seinen Turn warten. Die Serie
# nach dem Ueberschreiten der Schwelle dauerte gemessen hoechstens 0,8 s.
_VORAB_FRIST = 3.0


class MusikDucker:
    def __init__(self, url: str, token: str, raum: str, *, level: int | None = None,
                 lease_s: float = 30.0, erneuern_s: float = 15.0,
                 http_timeout: float = 1.0, post=None) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._raum = raum
        self._level = level
        self._lease_s = lease_s
        self._erneuern_s = erneuern_s
        self._http_timeout = http_timeout
        self._post = post or self._http_post
        self._cv = threading.Condition()
        self._wunsch = False          # soll gerade gedaempft sein? (Turn oder Ansage)
        self._turn = False            # ein Turn (oder vorab gemeldeter Ruf) laeuft
        self._ansagen = 0             # laufende Ansagen von aussen
        self._erstes = False          # das erste duck dieser Daempfung steht noch aus
        self._leases: list[str] = []  # erhaltene lease_ids des laufenden Turns
        self._letztes_duck = 0.0
        self._fehler_gemeldet = False
        self._zustand = STATE_LISTENING
        self._vorab_seit: float | None = None
        self._thread: threading.Thread | None = None

    # --- Hauptschleife -----------------------------------------------------

    def vorab(self) -> None:
        """Wakewort ueber der Schwelle (Haupt-Mikrofon), der Turn folgt gleich."""
        if self._zustand == STATE_LISTENING:
            self._vorab_seit = time.monotonic()
            self._setze(True)

    def verworfen(self) -> None:
        """Der vorab gemeldete Ruf wird doch kein Turn."""
        if self._vorab_seit is not None and self._zustand == STATE_LISTENING:
            self._vorab_seit = None
            self._setze(False)

    def zustand(self, state: int, satellit: bool) -> None:
        """Einmal je Schleifendurchlauf; entscheidet nur, ruft nichts auf."""
        alt, self._zustand = self._zustand, state
        if state == STATE_LISTENING:
            if alt != STATE_LISTENING:
                self._setze(False)
            elif (self._vorab_seit is not None
                  and time.monotonic() - self._vorab_seit > _VORAB_FRIST):
                self.verworfen()
            return
        self._vorab_seit = None
        if alt == STATE_LISTENING and not satellit:
            self._setze(True)

    @contextlib.contextmanager
    def ansage(self):
        """Gedaempft, solange der Block laeuft (Ansage von aussen)."""
        with self._cv:
            self._ansagen += 1
            self._aktualisieren()
        try:
            yield
        finally:
            with self._cv:
                self._ansagen -= 1
                self._aktualisieren()

    def _setze(self, turn: bool) -> None:
        with self._cv:
            self._turn = turn
            self._aktualisieren()

    def _aktualisieren(self) -> None:
        """Nur unter self._cv."""
        daempfen = self._turn or self._ansagen > 0
        if daempfen == self._wunsch:
            return
        self._wunsch = daempfen
        self._erstes = daempfen
        self._cv.notify()

    # --- Thread ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._lauf, name="musik-ducking", daemon=True)
        self._thread.start()

    def _lauf(self) -> None:
        while True:
            with self._cv:
                self._cv.wait(timeout=1.0)
                wunsch, erstes = self._wunsch, self._erstes
                self._erstes = False
            self.schritt(wunsch, erstes)

    def schritt(self, wunsch: bool, erstes: bool) -> None:
        """Ein Durchlauf des Threads (einzeln testbar)."""
        if wunsch:
            faellig = time.monotonic() - self._letztes_duck >= self._erneuern_s
            if erstes or (self._leases and faellig):
                self._duck()
        elif self._leases:
            leases, self._leases = self._leases, []
            for lease in dict.fromkeys(leases):
                self._aufruf("unduck", {"lease_id": lease})

    def _duck(self) -> None:
        self._letztes_duck = time.monotonic()
        body: dict = {"source": "gaston", "timeout_s": self._lease_s}
        if self._level is not None:
            body["level"] = self._level
        t0 = time.monotonic()
        antwort = self._aufruf("duck", body)
        if antwort is None:
            return
        if antwort.get("ducked") and antwort.get("lease_id"):
            erstes = not self._leases
            self._leases.append(str(antwort["lease_id"]))
            if erstes:
                log.info("Musik %s gedaempft (%.0f ms)", self._raum,
                         (time.monotonic() - t0) * 1000)
        elif not self._leases:
            log.debug("Musik %s nicht gedaempft: %s", self._raum, antwort.get("reason"))

    def _aufruf(self, was: str, body: dict) -> dict | None:
        try:
            antwort = self._post(f"{self._url}/api/v1/rooms/{self._raum}/{was}", body)
        except Exception as exc:
            # Musik-Bridge weg darf nie stoeren: einmal melden, dann still.
            if not self._fehler_gemeldet:
                log.warning("Musik-Ducking (%s) nicht erreichbar: %s", was, exc)
                self._fehler_gemeldet = True
            return None
        if self._fehler_gemeldet:
            log.info("Musik-Ducking wieder erreichbar")
            self._fehler_gemeldet = False
        return antwort

    def _http_post(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
            return json.loads(resp.read() or b"{}")
