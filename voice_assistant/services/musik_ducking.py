"""Musik leiser, solange ein Turn laeuft (Ducking ueber eine Musik-Bridge).

Gegenstelle ist eine REST-Schnittstelle mit Lease-Semantik:

    POST {url}/api/v1/rooms/{raum}/duck    {"source", "lease_id"?, "level"?, "timeout_s"?}
         -> {"ducked": true, "lease_id": "…", "renewed"?, "already_ducked"?}
          | {"ducked": false, "reason": "…"}
    POST {url}/api/v1/rooms/{raum}/unduck  {"source"} | {"lease_id"} | {}

Die Bridge stellt nach ``timeout_s`` ohne erneutes duck selbst wieder her
(Absturzschutz). Ein laengerer Turn erneuert deshalb alle ``erneuern_s``, mit
der eigenen ``lease_id``: dann bleibt es eine Lease, auch wenn eine Antwort
verloren geht oder die Bridge dazwischen neu startet (sie speichert ihren
Ducking-Zustand seit 2026-10-18). Freigegeben wird nach ``source``, das
erfasst auch eine Lease, deren Antwort nie ankam.

Regeln:

- **Nie blockieren.** Die Hauptschleife setzt nur einen Wunsch
  (``zustand()``), die Aufrufe macht ein eigener Thread.
- **Gedaempft wird ab dem Wakewort und bis zum Leerlauf** (Antwort, Follow-up-
  Fenster, Rueckfragen eingeschlossen), danach unduck. **Ausser, waehrend der
  Brain denkt** (Jochen 2026-10-18): gebraucht wird die Daempfung nur fuer
  die Aufnahme und fuer unsere Ausgabe. In STATE_WAITING ohne Ausgabe
  (``tts_lock`` frei) geht die Musik nach ``_HALTEN`` wieder hoch und wird
  zum naechsten Satz neu gedaempft. Lebenszeichen („Einen Moment bitte.“,
  ``state.lebenszeichen``) daempfen nicht, auch das braucht es nicht (Jochen). Der tts_lock wird vor
  der Synthese genommen, die gibt dem Echo also Vorlauf. Anlass: 2026-10-18
  22:05, ein Soulseek-Auftrag, gut 60 s Denken bei leiser Musik. Preis: ein
  „Stopp Gaston“ waehrend des Denkens muss gegen die volle Musik an.
- **Schon beim Ueberschreiten der Schwelle** (``vorab()``), nicht erst beim
  Turn-Beginn: der kommt erst, wenn die Treffer-Serie abreisst, gemessen
  0,62–0,78 s spaeter (2026-10-13, bei laufender Musik). Wird der Ruf danach
  doch verworfen (Pegel-Gate), gibt ``verworfen()`` sofort frei; jeden
  anderen Weg ohne Turn faengt ``_VORAB_FRIST`` ab.
- **Ansagen von aussen** (``voice_speak_text``) daempfen ebenfalls, solange sie
  laufen (``ansage()``). Der Timer nicht (Jochen, 2026-10-13): weder sein
  Klingeln noch „Der Timer ist abgelaufen“.
- **Nur Turns des Haupt-Mikrofons.** Ein Satelliten-Turn (Handy) hat keinen
  Raum (audio/satellit.py) und daempft nichts.
- **Erneuert wird nur, was das erste duck tatsaechlich gedaempft hat.** Lief
  beim Ruf keine Musik, bleibt es dabei — sonst wuerde die Musik gedaempft,
  die Gaston in diesem Turn gerade selbst gestartet hat.
- **Hat der Turn die Musik gestartet, entfaellt der Follow-up**
  (``musik_gestartet()``, Jochen 2026-10-15). Sonst stand nach „spiel Queen“
  das Follow-up-Fenster mit voller Musik offen, ungedaempft, weil beim Ruf
  nichts lief. Verglichen wird, in welchen Raeumen beim Ruf und vor dem
  Follow-up Musik laeuft (GET /api/v1/rooms) — die Bridge sagt nicht, welcher
  Raum in welchem liegt, und Gaston spielt in Unterraeumen.
- **Warum Erneuern mit id und Freigeben nach source** (2026-10-18 22:06,
  Bridge-Log): die Bridge wurde mitten im Ducking zweimal neu gestartet
  (Deploys auf ihrer Seite). Sie vergass Leases und alte Lautstaerke,
  Gastons naechste Erneuerung war fuer sie ein erstes duck und merkte sich
  die gedaempfte Lautstaerke als die alte: am Ende „wiederhergestellt“ auf
  2 %. Damals legte jedes duck eine neue Lease an. Beides hat die Bridge am
  selben Abend behoben; bleibt eine Erneuerung trotzdem ohne ``renewed`` und
  ``already_ducked``, war unsere Lease weg, und das steht als Warnung im Log.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.request

from voice_assistant.state import STATE_LISTENING, STATE_WAITING

log = logging.getLogger(__name__)

# So lange darf ein vorab gedaempfter Ruf auf seinen Turn warten. Die Serie
# nach dem Ueberschreiten der Schwelle dauerte gemessen hoechstens 0,8 s.
_VORAB_FRIST = 3.0

# So zaehlt ein Raum als "Musik laeuft": spielt oder schaltet sich gerade ein.
_SPIELT = ("play", "starting")

# Unter diesem Namen daempft Gaston; unduck nach source gibt genau die frei.
_QUELLE = "gaston"

# So lange ohne eigene Ausgabe in STATE_WAITING, bis die Musik wieder hoch
# darf: haelt sie zwischen zwei Saetzen einer Antwort unten. Geschaetzt.
_HALTEN = 4.0


class MusikDucker:
    def __init__(self, url: str, token: str, raum: str, *, level: int | None = None,
                 lease_s: float = 30.0, erneuern_s: float = 15.0,
                 http_timeout: float = 1.0, post=None, get=None) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._raum = raum
        self._level = level
        self._lease_s = lease_s
        self._erneuern_s = erneuern_s
        self._http_timeout = http_timeout
        self._post = post or self._http_post
        self._get = get or self._http_get
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
        # Raeume, in denen beim Ruf Musik lief (None = nicht bekannt).
        self._lief_beim_ruf: set[str] | None = None
        self._denkt = False           # Turn wartet auf den Brain, wir sagen nichts
        self._turn_neu = False        # der Turn hat noch kein erstes duck gesehen
        self._turn_gedaempft = False  # das erste duck des Turns hat gedaempft
        self._duck_fehler = False     # ein duck ohne Antwort: Lease evtl. unbekannt
        self._ausgabe_zuletzt = 0.0
        self._thread: threading.Thread | None = None
        # Musik-Befehle an dieselbe Bridge (musik_befehle.py), wenn eingeschaltet.
        self.befehle = None

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

    def zustand(self, state: int, satellit: bool, spricht: bool = False) -> None:
        """Einmal je Schleifendurchlauf; entscheidet nur, ruft nichts auf.

        spricht: tts_lock gehalten (wir synthetisieren oder spielen gerade).
        """
        alt, self._zustand = self._zustand, state
        jetzt = time.monotonic()
        if spricht or state != STATE_WAITING or alt != STATE_WAITING:
            self._ausgabe_zuletzt = jetzt
        denkt = (state == STATE_WAITING and not spricht
                 and jetzt - self._ausgabe_zuletzt >= _HALTEN)
        if denkt != self._denkt:
            with self._cv:
                self._denkt = denkt
                self._aktualisieren()
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
            if turn and not self._turn:
                self._turn_neu = True
            self._turn = turn
            self._aktualisieren()

    def _aktualisieren(self) -> None:
        """Nur unter self._cv."""
        daempfen = (self._turn and not self._denkt) or self._ansagen > 0
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
                lage = self._lage()
            self.schritt(*lage)

    def _lage(self) -> tuple[bool, bool, bool, bool]:
        """Nur unter self._cv: was der Thread als Naechstes tun soll."""
        lage = (self._wunsch, self._erstes, self._turn_neu or not self._turn, self._turn)
        self._erstes = False
        if self._wunsch and lage[1]:
            self._turn_neu = False
        return lage

    def schritt(self, wunsch: bool, erstes: bool, neu: bool = True,
                turn: bool = False) -> None:
        """Ein Durchlauf des Threads (einzeln testbar).

        neu: die Daempfung beginnt einen Turn (oder eine Ansage ausserhalb),
        sonst kommt sie nach einer Denkpause desselben Turns wieder.
        turn: ein Turn laeuft noch (Denkpause, nicht Leerlauf).
        """
        if wunsch:
            faellig = time.monotonic() - self._letztes_duck >= self._erneuern_s
            if erstes and neu:
                self._duck()
                self._turn_gedaempft = bool(self._leases)
                self._lief_beim_ruf = self.spielende_raeume()
            elif erstes:
                # Nach der Denkpause nur, was der Turn selbst gedaempft hatte:
                # Musik, die Gaston in diesem Turn gestartet hat, bleibt laut.
                if self._turn_gedaempft:
                    self._duck()
            elif self._leases and faellig:
                self._duck()
        else:
            if not turn:
                self._lief_beim_ruf = None
                self._turn_gedaempft = False
            self._freigeben("Denkpause" if turn else "Ende")

    def _freigeben(self, anlass: str) -> None:
        leases, self._leases = self._leases, []
        fehler, self._duck_fehler = self._duck_fehler, False
        # Ein duck ohne Antwort kann trotzdem eine Lease angelegt haben.
        if not leases and not fehler:
            return
        t0 = time.monotonic()
        antwort = self._aufruf("unduck", {"source": _QUELLE})
        if leases and antwort and antwort.get("reason") == "not_ducked":
            log.warning("Musik %s: die Bridge kannte unsere Lease nicht mehr "
                        "— Musik bleibt womoeglich leise", self._raum)
        log.info("Musik %s freigegeben (%s, %.0f ms): %s", self._raum, anlass,
                 (time.monotonic() - t0) * 1000, antwort)

    def _duck(self) -> None:
        self._letztes_duck = time.monotonic()
        body: dict = {"source": _QUELLE, "timeout_s": self._lease_s}
        if self._leases:
            body["lease_id"] = self._leases[-1]
        if self._level is not None:
            body["level"] = self._level
        t0 = time.monotonic()
        antwort = self._aufruf("duck", body)
        if antwort is None:
            self._duck_fehler = True
            return
        if antwort.get("ducked") and antwort.get("lease_id"):
            erstes = not self._leases
            if not erstes and not (antwort.get("renewed") or antwort.get("already_ducked")):
                # Die Bridge nimmt die Erneuerung als erstes duck: unsere Lease
                # war weg, und sie merkt sich jetzt womoeglich die GEDAEMPFTE
                # Lautstaerke als die alte (previous_volume).
                log.warning("Musik %s: Erneuerung als neues duck, previous_volume=%s",
                            self._raum, antwort.get("previous_volume"))
            lease = str(antwort["lease_id"])
            if lease not in self._leases:
                self._leases.append(lease)
            if erstes:
                log.info("Musik %s gedaempft (%.0f ms)", self._raum,
                         (time.monotonic() - t0) * 1000)
        elif not self._leases:
            log.debug("Musik %s nicht gedaempft: %s", self._raum, antwort.get("reason"))

    def spielende_raeume(self, timeout: float | None = None) -> set[str] | None:
        """Raeume, in denen gerade Musik laeuft; None, wenn die Bridge nicht antwortet."""
        try:
            raeume = self._get(f"{self._url}/api/v1/rooms", timeout or self._http_timeout)
        except Exception as exc:
            log.debug("Musik-Raeume nicht lesbar: %s", exc)
            return None
        return {str(r.get("room")) for r in raeume if r.get("state") in _SPIELT}

    def musik_gestartet(self) -> bool:
        """Laeuft jetzt Musik in einem Raum, in dem beim Ruf keine lief?

        Aus der Hauptschleife vor dem Follow-up; kurze Frist, im Zweifel False
        (dann kommt der Follow-up wie bisher).
        """
        vorher = self._lief_beim_ruf
        if vorher is None:
            return False
        jetzt = self.spielende_raeume(timeout=0.5)
        return bool(jetzt and jetzt - vorher)

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

    def _http_get(self, url: str, timeout: float):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"[]")

    def _http_post(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
            return json.loads(resp.read() or b"{}")
