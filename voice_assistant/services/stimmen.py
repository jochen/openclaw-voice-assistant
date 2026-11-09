"""Eigene Stimmen für den Antwort-Dienst (CosyVoice), lokal verwaltet.

Jeder Assistent hält seine Stimmen selbst (Jochen 2026-11-09: „die Verwaltung
lokal, die Stimme wird bei Auswahl an den Dienst gegeben“). Der Dienst ist nur
ein Rechenwerk: kennt er eine Stimme nicht (HTTP 404), lädt der Assistent sie
hoch und fragt noch einmal. Ein Neustart des Dienstes ist damit egal, und die
Häuser sind getrennt: jede Kennung beginnt mit dem Namensraum des Assistenten,
Gaston sieht nie die Stimmen aus dem Fablab.

Ablage je Stimme (Ordner ``<ordner>/<name>/``):

- ``probe.wav`` — die aufbereitete Probe (Lautheit, Länge; siehe stimm_probe)
- ``probe.txt`` — was in der Probe gesagt wird (CosyVoice braucht den Text)
- ``meta.json`` — optional: ``beschreibung``, ``quelle``, ``angelegt``,
  ``freigegeben`` (fehlt = freigegeben; ``false`` = nicht wählbar)

Die Kennung beim Dienst trägt einen Prüfwert über Probe und Text
(``gaston/gaston2-1a2b3c4d``): wer eine Probe ersetzt, bekommt beim nächsten
Satz von selbst die neue, ohne dass jemand den Dienst aufräumen muss.

Gelernt beim Bau (2026-11-08/09, Hörseiten): CosyVoice übernimmt Lautstärke,
Tempo und Gefühl der Probe. Eine gespielte Szene klingt „zu emotional“, eine
Probe mit −31 LUFS kommt 14 dB leiser heraus als thorsten. Deshalb gehört vor
das Anlegen eine Aufbereitung mit Hörprobe, nicht nur ein Kopieren.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.request

from voice_assistant.services.tts import FREMDE_MODELLE, SpeachesTts

log = logging.getLogger(__name__)


class StimmenLager:
    """Die eigenen Stimmen eines Assistenten und ihr Abgleich mit dem Dienst."""

    def __init__(self, ordner: str, namensraum: str, dienst_url: str, modell: str,
                 standard: str, timeout: float = 15.0) -> None:
        self.ordner = ordner
        self.namensraum = namensraum
        self.dienst_url = dienst_url.rstrip("/")
        self.modell = modell
        self.standard = standard
        self.timeout = timeout
        self._lock = threading.Lock()

    # --- lokal ----------------------------------------------------------

    def _pfad(self, name: str, datei: str) -> str:
        return os.path.join(self.ordner, name, datei)

    def _meta(self, name: str) -> dict:
        try:
            with open(self._pfad(name, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}

    def hat(self, name: str | None) -> bool:
        """Stimme liegt vollständig vor und ist freigegeben."""
        if not name or "/" in name or name.startswith("."):
            return False
        return (os.path.isfile(self._pfad(name, "probe.wav"))
                and os.path.isfile(self._pfad(name, "probe.txt"))
                and self._meta(name).get("freigegeben", True) is not False)

    def namen(self) -> list[str]:
        try:
            eintraege = sorted(os.listdir(self.ordner))
        except OSError:
            return []
        return [n for n in eintraege if self.hat(n)]

    def eintraege(self) -> list[dict]:
        return [{"name": n, "beschreibung": str(self._meta(n).get("beschreibung", ""))}
                for n in self.namen()]

    def wirksam(self, model: str | None, voice: str | None) -> str:
        """Die Stimme, die gerade spricht: die gewählte, wenn sie eine von
        unseren ist, sonst die Standard-Stimme des Profils. Eine Piper-Wahl
        (z. B. eine alte gespeicherte Vorliebe) zählt hier nicht als Wahl."""
        if model == self.modell and self.hat(voice):
            return voice  # type: ignore[return-value]
        return self.standard

    def dienst_id(self, name: str) -> str:
        h = hashlib.sha1()
        with open(self._pfad(name, "probe.wav"), "rb") as f:
            h.update(f.read())
        with open(self._pfad(name, "probe.txt"), "rb") as f:
            h.update(f.read())
        return f"{self.namensraum}/{name}-{h.hexdigest()[:8]}"

    # --- Dienst ---------------------------------------------------------

    def hochladen(self, name: str) -> str:
        """Stimme beim Dienst anlegen; gibt die Kennung zurück. Wirft bei Fehler."""
        if not self.hat(name):
            raise FileNotFoundError(f"Stimme {name!r} liegt nicht vor")
        kennung = self.dienst_id(name)
        with open(self._pfad(name, "probe.wav"), "rb") as f:
            wav = f.read()
        with open(self._pfad(name, "probe.txt"), encoding="utf-8") as f:
            text = f.read().strip()
        body = json.dumps({"id": kennung, "text": text,
                           "wav_b64": base64.b64encode(wav).decode("ascii")}).encode()
        req = urllib.request.Request(f"{self.dienst_url}/v1/voices", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self._lock, urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()
        log.info("Stimme %s beim Dienst angelegt (%s)", name, kennung)
        print(f"🗣️  Stimme {name} beim Antwort-Dienst angelegt ({kennung})")
        return kennung

    def vorab_hochladen(self, name: str) -> None:
        """Im Hintergrund, best effort: der erste Satz soll nicht warten."""
        def _lauf() -> None:
            try:
                self.hochladen(name)
            except Exception as e:  # noqa: BLE001 — beim ersten Satz noch einmal
                log.warning("Stimme %s vorab nicht hochgeladen: %s", name, e)
        threading.Thread(target=_lauf, name=f"stimme-{name}", daemon=True).start()


class AntwortTts(SpeachesTts):
    """Der Antwort-Dienst mit einer Stimme aus dem Lager.

    Welche, steht in voice_state (``model`` = Modell des Dienstes, ``voice`` =
    Name im Lager), sonst die Standard-Stimme. Ein 404 heißt hier „Stimme dem
    Dienst unbekannt“: hochladen, noch einmal fragen (SpeachesTts.synth_strom
    ruft dafür ``_modell_laden``)."""

    def __init__(self, lager: StimmenLager, state, timeout: float = 8.0) -> None:
        self._name: str | None = None
        super().__init__(state, lager.dienst_url, lager.modell, lager.standard,
                         fest=True, timeout=timeout)
        self.lager = lager
        FREMDE_MODELLE.add(lager.modell)

    @property
    def voice(self) -> str:  # für das Journal: wer gerade spricht
        return self._name or self.lager.standard

    @voice.setter
    def voice(self, _wert: str) -> None:
        pass

    def _parameter(self, text: str) -> tuple[str, str, float, str]:
        from voice_assistant.state import voice_state
        vm, vv, _ = voice_state.get()
        self._name = self.lager.wirksam(vm, vv)
        return self.model, self.lager.dienst_id(self._name), 1.0, text

    def _modell_laden(self, model: str) -> None:
        print(f"⚠️  Antwort-Dienst kennt die Stimme {self._name} nicht — lade sie hoch …")
        self.lager.hochladen(self._name or self.lager.standard)
