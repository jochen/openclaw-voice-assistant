"""Dienst-Waechter: merkt, wenn ein Dienst des Assistenten wegbleibt, und
versucht ihn wieder anzuwerfen.

Anlass (2026-10-06): von 03:31 bis 18:50 waren Qwen-STT und Laya vom Host aus
nicht erreichbar (Port-Weiterleitung mit einer systemd-Unit gestorben, siehe
``services/container.py``). Der Assistent hat das korrekt abgefangen — jede
Stufe fiel auf die naechste zurueck, Parakeet statt Qwen, Gemma statt Laya —
und genau deshalb fiel es niemandem auf. Die Rueckfall-Ketten machen einen
Ausfall unsichtbar; dieser Waechter macht ihn wieder sichtbar.

Was er tut, je Dienst und Runde (Default alle 60 s):

1. ``/health`` abfragen. Die Dienste kommen aus dem Profil (STT, Laya,
   Klassifikations-LLM, Speaches) — es steht keine Adresse im Code.
2. Ist ein Dienst laenger als die Gnadenfrist weg: melden (Argus-Gruppe) und,
   wenn er lokal in einem Podman-Container laeuft, diesen stoppen und im
   eigenen Scope neu starten. Hoechstens ``max_heilversuche`` Mal, mit
   Abstand; danach nur noch melden. Das Ergebnis steht in derselben Meldung.
3. Kommt er wieder, steht auch das in der Gruppe.

Waehrend einer der ``ruhe_units`` laeuft (das Laya-Nachtraining stoppt Laya
und Qwen absichtlich, um die GPU frei zu haben), schweigt und heilt er nicht —
ein Heilversuch dort startete einen Container mitten ins Training und
verdraengte es aus dem Grafikspeicher. Die Gnadenfrist zaehlt danach neu.

In den stillen Stunden wird geheilt, aber nicht gesendet; die Meldungen gehen
gesammelt raus, sobald die stille Zeit vorbei ist.
"""

from __future__ import annotations

import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

from voice_assistant.services import container as _container
from voice_assistant.services.watcher import _in_stillen_stunden


@dataclass
class Dienst:
    name: str
    url: str            # Health-URL


@dataclass
class _Zustand:
    seit: float | None = None       # weg seit (erste fehlgeschlagene Pruefung)
    fehler: str = ""
    gemeldet: bool = False
    versuche: int = 0
    letzter_versuch: float | None = None
    aufgegeben: bool = False


def health_url(url: str) -> str:
    """``scheme://host:port/health`` aus einer beliebigen Dienst-URL."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/health"


def dienste_aus_profil(profile) -> list[Dienst]:
    """Jede Dienst-URL, die der Assistent benutzt, als Health-URL.

    Nicht dabei: das LLM des Ueberwachers (oft ein Cloud-Endpunkt ohne
    /health) und OpenClaw (hat seine eigene Fehlerbehandlung im Turn).
    """
    kandidaten = [("Speaches (STT/TTS)", profile.speaches_base),
                  ("Qwen-STT", profile.stt_llamacpp_url)]
    if profile.actuator.enabled:
        kandidaten += [("Klassifikations-LLM", profile.actuator.llm_url),
                       ("Laya", profile.actuator.laya_url)]
    dienste = [Dienst(name, health_url(url)) for name, url in kandidaten if url]
    dienste += [Dienst(name, url) for name, url in profile.dienstwaechter.dienste]
    return dienste


def pruefen(url: str, timeout: float = 5.0) -> str | None:
    """None = gesund, sonst ein kurzer Fehlertext."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status == 200:
                return None
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return str(e.reason)
    except Exception as e:  # Timeout, kaputte Antwort
        return f"{type(e).__name__}: {e}"


def unit_aktiv(unit: str) -> bool:
    r = subprocess.run(["systemctl", "--user", "is-active", unit],
                       text=True, capture_output=True)
    return r.stdout.strip() in ("active", "activating", "deactivating")


def _dauer(sek: float) -> str:
    if sek < 120:
        return f"{sek:.0f} s"
    if sek < 7200:
        return f"{sek / 60:.0f} min"
    return f"{sek / 3600:.1f} h"


class DienstWaechter:
    def __init__(
        self,
        dienste: list[Dienst],
        melden: Callable[[str], None],
        intervall: float = 60.0,
        gnadenfrist: float = 180.0,
        heilen: bool = True,
        ruhe_units: tuple[str, ...] = (),
        quiet_start: int = 1,
        quiet_end: int = 7,
        max_heilversuche: int = 3,
        heil_abstand: float = 1800.0,
        heil_warte: float = 120.0,
        *,
        pruefe: Callable[[str], str | None] = pruefen,
        finde_container: Callable[[str], str | None] = _container.container_fuer_url,
        heile: Callable[[str], str] = _container.neu_starten,
        ruhe: Callable[[str], bool] = unit_aktiv,
        jetzt: Callable[[], float] = time.time,
        uhr: Callable[[], datetime] = datetime.now,
        schlafe: Callable[[float], None] = time.sleep,
    ) -> None:
        self.dienste = dienste
        self._senden = melden
        self.intervall = intervall
        self.gnadenfrist = gnadenfrist
        self.heilen = heilen
        self.ruhe_units = tuple(ruhe_units)
        self.quiet_start, self.quiet_end = quiet_start, quiet_end
        self.max_heilversuche = max_heilversuche
        self.heil_abstand = heil_abstand
        self.heil_warte = heil_warte
        self._pruefe, self._finde, self._heile = pruefe, finde_container, heile
        self._ruhe, self._jetzt, self._uhr, self._schlafe = ruhe, jetzt, uhr, schlafe
        self._zustand = {d.name: _Zustand() for d in dienste}
        self._gesammelt: list[str] = []

    # --- Melden -----------------------------------------------------------
    def _melden(self, text: str) -> None:
        print(f"🩺 {text}")
        if _in_stillen_stunden(self._uhr(), self.quiet_start, self.quiet_end):
            self._gesammelt.append(text)
            return
        self._senden(text)

    def _gesammeltes_senden(self) -> None:
        if not self._gesammelt or _in_stillen_stunden(
                self._uhr(), self.quiet_start, self.quiet_end):
            return
        self._senden("Dienst-Wächter, in der stillen Zeit gesammelt:\n\n"
                     + "\n\n".join(self._gesammelt))
        self._gesammelt = []

    # --- Eine Runde -------------------------------------------------------
    def schritt(self) -> None:
        self._gesammeltes_senden()
        aktiv = [u for u in self.ruhe_units if self._ruhe(u)]
        if aktiv:
            # Geplante Auszeit: vergessen, was bis hierher weg war — sonst
            # meldete die erste Runde danach den ganzen Trainingslauf.
            for z in self._zustand.values():
                if z.seit is not None and not z.gemeldet:
                    z.seit = None
            return
        for d in self.dienste:
            self._pruefe_dienst(d)

    def _pruefe_dienst(self, d: Dienst) -> None:
        z = self._zustand[d.name]
        jetzt = self._jetzt()
        fehler = self._pruefe(d.url)
        if fehler is None:
            if z.seit is not None and z.gemeldet:
                self._melden(f"✅ {d.name} wieder erreichbar (war {_dauer(jetzt - z.seit)} weg).")
            self._zustand[d.name] = _Zustand()
            return
        if z.seit is None:
            z.seit, z.fehler = jetzt, fehler
            return
        z.fehler = fehler
        if jetzt - z.seit < self.gnadenfrist:
            return

        kopf = (f"⚠️ {d.name} ({d.url}) seit {_dauer(jetzt - z.seit)} nicht erreichbar: "
                f"{fehler}. Der Assistent fällt solange auf die nächste Stufe zurück.")
        if not self.heilen:
            if not z.gemeldet:
                self._melden(kopf)
                z.gemeldet = True
            return
        if z.aufgegeben or (z.letzter_versuch is not None
                            and jetzt - z.letzter_versuch < self.heil_abstand):
            return
        name = self._finde(d.url)
        if name is None:
            if not z.gemeldet:
                self._melden(kopf + " Kein lokaler Container zuzuordnen — nicht neu gestartet.")
                z.gemeldet = True
            return

        z.versuche += 1
        z.letzter_versuch = jetzt
        was = self._heile(name)
        t0 = self._jetzt()
        rest = self._pruefe(d.url)
        while rest is not None and self._jetzt() - t0 < self.heil_warte:
            self._schlafe(3.0)
            rest = self._pruefe(d.url)
        if rest is None:
            self._melden(f"{kopf}\n→ Container {name}: {was} — nach "
                         f"{_dauer(self._jetzt() - t0)} wieder erreichbar ✅")
            self._zustand[d.name] = _Zustand()
            return
        z.gemeldet = True
        if z.versuche >= self.max_heilversuche:
            z.aufgegeben = True
            self._melden(f"{kopf}\n→ Container {name}: {was} — weiter nicht erreichbar "
                         f"({rest}). {z.versuche}. Versuch, ich gebe auf; bitte selbst ansehen.")
        else:
            self._melden(f"{kopf}\n→ Container {name}: {was} — weiter nicht erreichbar "
                         f"({rest}). Versuch {z.versuche}/{self.max_heilversuche}, "
                         f"nächster in {_dauer(self.heil_abstand)}.")

    # --- Thread -----------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._lauf, name="dienstwaechter", daemon=True).start()

    def _lauf(self) -> None:
        while True:
            try:
                self.schritt()
            except Exception as e:      # der Waechter darf den Dienst nie mitreissen
                print(f"⚠️  Dienst-Wächter: {type(e).__name__}: {e}")
            self._schlafe(self.intervall)
