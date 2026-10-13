"""Satelliten: weitere Mikrofone mit Lautsprecher neben dem Haupt-Geraet.

Gedacht fuer Faelle wie "ich liege in der Badewanne, da ist kein ReSpeaker":
das Handy laeuft mit Ava (Android-App, github.com/brownard/Ava), die sich im
Netz als ESPHome-Sprachsatellit ausgibt. Sie spricht dieselbe Native API wie
der ReSpeaker, deshalb steckt unter jedem Satelliten ein RespeakerClient mit
``satellit=True``.

Der Unterschied zum ReSpeaker, gemessen am 2026-10-13:

- **Kein Dauerstrom.** Ava hat keinen Start-Knopf; sie erkennt ihr Wakeword
  ("Hey Jarvis", "Okay Nabu" …) selbst auf dem Handy und oeffnet erst dann
  eine Session. Unser openwakeword sieht diesen Ton nie. Die Session ersetzt
  also den Trigger.
- **Die Session endet erst, wenn wir sie beenden.** Das Endpointing bleibt
  damit unseres (STATE_RECORDING), und ``zustand()`` schliesst die Session,
  sobald die Aufnahme vorbei ist.
- **Ansagen ueber die Announce-API**, nicht den Media-Player: sie meldet das
  Ende der Wiedergabe, und mit ``start_conversation`` hoert das Geraet danach
  von selbst zu. Darauf laufen Rueckfragen und Follow-ups.

**Der Turn gehoert dem Geraet, das ihn ausgeloest hat.** Ab der Session liest
die Hauptschleife nur noch vom Satelliten (das Haupt-Mikrofon wird verworfen),
bis sie wieder im Leerlauf ist. Ansagen gehen an das Geraet des letzten Turns
(``ziel``), auch spaetere von aussen (``voice_speak_text``, Timer) — bis ein
Turn vom Haupt-Mikrofon kommt oder der Satellit die Verbindung verliert.

Ein Satellit ist ortsungebunden (Jochen, 2026-10-13: "wo ich bin, muss ich dann
ggf. sagen"). Es gibt deshalb keinen Raum dazu.
"""

from __future__ import annotations

import logging

import numpy as np

from voice_assistant.audio.respeaker import RespeakerClient, RespeakerSink, get_client
from voice_assistant.config import FOLLOWUP_BEEP_PATH, RespeakerAudio, SatellitConfig
from voice_assistant.state import (
    STATE_FOLLOWUP,
    STATE_LISTENING,
    STATE_RECORDING,
)

log = logging.getLogger(__name__)

_AUFNAHME = (STATE_RECORDING, STATE_FOLLOWUP)


def _respeaker_cfg(sat: SatellitConfig) -> RespeakerAudio:
    return RespeakerAudio(
        host=sat.host,
        port=sat.port,
        encryption_key=sat.encryption_key,
        use_speaker=True,
        volume=sat.volume,
        http_port=sat.http_port,
        satellit=True,
    )


class SatellitQuelle:
    """AudioSource ueber dem Haupt-Mikrofon und den Satelliten.

    Die Hauptschleife ruft ``zustand(state)`` einmal je Durchlauf vor
    ``read_chunk()`` und im Leerlauf ``satellit_trigger()``.
    """

    def __init__(self, haupt, clients: dict[str, RespeakerClient]) -> None:
        self._haupt = haupt
        self._clients = clients
        self._zustand = STATE_LISTENING
        # Geraet, dem der laufende Turn gehoert (None = Haupt-Mikrofon).
        self.aktiv: str | None = None
        # Geraet, an das Ansagen gehen (siehe Modul-Docstring).
        self._ziel: str | None = None
        self._trigger: tuple[str, str] | None = None

    @property
    def ziel(self) -> str | None:
        if self._ziel is not None and not self._clients[self._ziel].verbunden():
            log.info("Satellit %s weg — Ansagen wieder ueber das Haupt-Geraet", self._ziel)
            self._ziel = None
        return self._ziel

    # --- Steuerung durch die Hauptschleife ---------------------------------

    def zustand(self, state: int) -> None:
        alt, self._zustand = self._zustand, state
        if self.aktiv is not None:
            if alt in _AUFNAHME and state not in _AUFNAHME:
                self._clients[self.aktiv].sitzung_beenden()
            if state == STATE_LISTENING:
                log.info("Satellit %s: Turn vorbei", self.aktiv)
                self.aktiv = None
                self._haupt.flush()
        elif alt == STATE_LISTENING and state == STATE_RECORDING:
            # Trigger vom Haupt-Mikrofon: Antworten gehen wieder dorthin.
            self._ziel = None
        # Sessions, die gerade niemand brauchen kann, sofort schliessen: ein
        # Satellit, der gerufen wird, waehrend ein Turn laeuft, oder eine
        # Session des aktiven Geraets ausserhalb einer Aufnahme. Sonst stuende
        # das Handy auf "listening", ohne dass jemand zuhoert.
        for name, client in self._clients.items():
            if not client.in_session():
                continue
            frei = state == STATE_LISTENING and self.aktiv is None
            eigene_aufnahme = name == self.aktiv and state in _AUFNAHME
            if not (frei or eigene_aufnahme):
                log.info("Satellit %s: Session ohne Turn geschlossen", name)
                client.sitzung_beenden()

    def satellit_trigger(self) -> tuple[str, str] | None:
        """(Name, Wakeword-Phrase) eines neuen Satelliten-Turns, einmal."""
        neu, self._trigger = self._trigger, None
        return neu

    # --- AudioSource -------------------------------------------------------

    def start(self) -> None:
        self._haupt.start()

    def read_chunk(self) -> np.ndarray:
        if self.aktiv is None and self._zustand == STATE_LISTENING:
            for name, client in self._clients.items():
                phrase = client.sitzung_abholen()
                if phrase is not None:
                    self.aktiv = self._ziel = name
                    self._trigger = (name, phrase)
                    log.info("Satellit %s: Turn beginnt (Wakeword %r)", name, phrase)
                    break
        if self.aktiv is not None:
            client = self._clients[self.aktiv]
            # Die Session nach einer Ansage mit start_conversation meldet sich
            # ebenfalls als "neu" — hier ist sie Teil des laufenden Turns.
            client.sitzung_abholen()
            return client.read_chunk()
        return self._haupt.read_chunk()

    def flush(self) -> None:
        if self.aktiv is not None:
            self._clients[self.aktiv].flush()
        else:
            self._haupt.flush()

    def kanal2_letzte(self, n: int) -> list[np.ndarray]:
        if self.aktiv is not None:
            return []
        fn = getattr(self._haupt, "kanal2_letzte", None)
        return fn(n) if fn else []

    @property
    def beam_angle(self):
        if self.aktiv is not None:
            return None
        return getattr(self._haupt, "beam_angle", None)

    def close(self) -> None:
        self._haupt.close()


class SatellitSenke:
    """AudioSink: spielt beim Geraet des letzten Turns, sonst beim Haupt-Geraet."""

    def __init__(self, haupt, quelle: SatellitQuelle, senken: dict[str, RespeakerSink]) -> None:
        self._haupt = haupt
        self._quelle = quelle
        self._senken = senken

    def play_wav(self, path: str) -> None:
        name = self._quelle.ziel
        if name is None:
            self._haupt.play_wav(path)
            return
        # Der Follow-up-Beep heisst "ich hoere jetzt zu". Am Handy muss das
        # Mikrofon dafuer erst geoeffnet werden — das tut die Ansage selbst
        # (start_conversation), nur waehrend eines Turns dieses Geraets.
        mikro = path == FOLLOWUP_BEEP_PATH and self._quelle.aktiv == name
        self._senken[name].play_wav(path, mikro_danach=mikro)

    def stop(self) -> None:
        for senke in (self._haupt, *self._senken.values()):
            fn = getattr(senke, "stop", None)
            if fn is not None:
                try:
                    fn()
                except Exception as exc:
                    log.warning("Wiedergabe-Abbruch: %s", exc)

    def resume(self) -> None:
        for senke in (self._haupt, *self._senken.values()):
            fn = getattr(senke, "resume", None)
            if fn is not None:
                fn()


def mit_satelliten(satelliten: list[SatellitConfig], source, sink):
    """Haupt-Quelle/-Senke um Satelliten erweitern; ohne Satelliten unveraendert."""
    if not satelliten:
        return source, sink
    clients: dict[str, RespeakerClient] = {}
    senken: dict[str, RespeakerSink] = {}
    for sat in satelliten:
        cfg = _respeaker_cfg(sat)
        clients[sat.name] = get_client(cfg)
        senken[sat.name] = RespeakerSink(cfg)
        print(f"📱 Satellit {sat.name}: {sat.host}:{sat.port} (Ansagen über :{sat.http_port})")
    quelle = SatellitQuelle(source, clients)
    return quelle, SatellitSenke(sink, quelle, senken)
