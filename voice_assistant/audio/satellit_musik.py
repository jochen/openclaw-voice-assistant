"""Musik am Handy: Ava spielt den Stream ihres Bridge-Raums (seit 2026-11-15).

Squeezelite auf dem Handy schaltete beim Pausieren die System-Medienlautstaerke
stumm, und damit Avas Sprache gleich mit (STREAM_ASSISTANT ist dort ein Alias
von STREAM_MUSIC; belegt per adb). Seither laeuft die Musik ueber Avas EIGENEN
Medien-Player: auf dem Musik-Rechner spielt ein Player ohne Ton fuer den Raum
(``satelliten[].musik_raum``), dessen Ton als Dauer-Stream ausgeliefert wird
(``satelliten[].musik_stream``). Der Stream liefert bei Pause Stille und reisst
nicht ab (gemessen 2026-11-15: drei Laeufe, Verbindung durchgehend, Ansagen
dazwischen ohne Luecke).

Ava haelt nur eine API-Verbindung, und die hat Gaston. Also gibt Gaston Ava
den Stream: ein Faden fragt alle ``_TAKT_S`` den Raumzustand der Bridge ab,
solange das Handy verbunden ist.

- Raum spielt, Avas Player nicht → Stream-URL schicken. Danach
  ``_NEU_SENDEN_S`` Ruhe, Ava braucht ~3 s bis zum Ton.
- Raum pausiert → nichts; der Stream liefert Stille. Das ist auch die Pause
  beim Daempfen (duck_mode pause). Erst nach ``_PAUSE_STOP_S`` STOP, damit
  ein vergessenes „Pause“ nicht stundenlang Stille ueber WLAN zieht.
- Raum gestoppt seit ``_STOP_NACH_S`` und Ava spielt → STOP.
- Handy neu verbunden → wie oben, der Stream kommt von selbst wieder.

Lautstaerke: nie Avas volume (ein Wert fuer Sprache UND Musik, VoiceOutput.kt),
nur die des Players auf dem Musik-Rechner (Jochen: drei Regler — Echo/BT fuer
alles, Ava fest auf voll, Musikserver fuer die Musik). Darum stoppt auch ein
Barge-in am Satelliten den Medien-Player nicht (RespeakerSink.stop).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import aioesphomeapi

log = logging.getLogger(__name__)

_TAKT_S = 2.0
_NEU_SENDEN_S = 15.0
_STOP_NACH_S = 30.0
_PAUSE_STOP_S = 600.0

_SPIELT = ("play", "starting")


class SatellitMusik:
    """Startet und stoppt den Raum-Stream auf dem Medien-Player eines Satelliten."""

    def __init__(self, name: str, client, raum: str, url: str,
                 raum_zustand: Callable[[str], str | None], *,
                 uhr: Callable[[], float] = time.monotonic) -> None:
        self._name = name
        self._client = client
        self._raum = raum
        self._url = url
        self._raum_zustand = raum_zustand
        self._uhr = uhr
        self._gesendet: float | None = None   # wann die URL zuletzt ging
        self._ruhe_seit: float | None = None  # seit wann Pause/Stop
        self._ruhe_art = ""
        self._thread: threading.Thread | None = None

    def _ava_spielt(self) -> bool:
        return self._client.player_zustand() == aioesphomeapi.MediaPlayerState.PLAYING

    def schritt(self) -> str | None:
        """Ein Takt. Liefert "start", "stop" oder None (fuer Log und Tests)."""
        jetzt = self._uhr()
        if not self._client.verbunden():
            self._gesendet = None
            return None
        zustand = self._raum_zustand(self._raum)
        if zustand is None:          # Bridge weg: nichts aendern
            return None
        if zustand in _SPIELT:
            self._ruhe_seit = None
            if self._ava_spielt():
                return None
            if self._gesendet is not None and jetzt - self._gesendet < _NEU_SENDEN_S:
                return None
            if self._client.musik_url(self._url):
                self._gesendet = jetzt
                print(f"📱 Satellit {self._name}: Musik im Raum {self._raum} → Stream an Ava")
                return "start"
            return None
        art = "pause" if zustand == "pause" else "stop"
        if self._ruhe_seit is None or self._ruhe_art != art:
            self._ruhe_seit, self._ruhe_art = jetzt, art
        frist = _PAUSE_STOP_S if art == "pause" else _STOP_NACH_S
        if self._ava_spielt() and jetzt - self._ruhe_seit >= frist:
            self._client.stop_playback()
            self._gesendet = None
            print(f"📱 Satellit {self._name}: Raum {self._raum} {zustand or 'unbekannt'} "
                  f"seit {jetzt - self._ruhe_seit:.0f} s → Stream bei Ava aus")
            return "stop"
        return None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._lauf, daemon=True,
                                        name=f"satellit-musik-{self._name}")
        self._thread.start()

    def _lauf(self) -> None:
        while True:
            try:
                self.schritt()
            except Exception as exc:
                log.warning("Satellit %s: Musik-Takt fehlgeschlagen: %s", self._name, exc)
            time.sleep(_TAKT_S)
