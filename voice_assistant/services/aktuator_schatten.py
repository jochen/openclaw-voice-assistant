"""Zwei Klassifikator-Ketten fuer den Aktuator: eine entscheidet, die andere urteilt im Schatten.

    Gemma   tor() -> classify() (darin _mehrzahl_gruppe) -> verdict()
    Laya    laya_intent.frage_laya() -> als_intent() -> _mehrzahl_gruppe()
            -> Rueckfrage-Regel -> verdict()

Welche entscheidet, sagt actuator.klassifikator ("gemma", Default, oder
"laya"). Die andere laeuft — sofern eingerichtet — nach der echten
Entscheidung in einem eigenen Thread mit und schreibt ihr Ergebnis neben das
echte nach actuator_schatten.log (Auswertung: tools/aktuator_vergleich.py
--schatten).

Geschichte: Seit 2026-09-29 lief Laya im Schatten neben Gemma. Am
2026-10-01 entschied Jochen, die Rollen zu tauschen ("wir machen Gemma zum
Schatten"): im Schatten 11 von 11 Kommandos identisch, Laya ~20x schneller
(92 ms gegen 1,8 s), auf dem Test-Set weniger Falsch-Schaltungen.

Eigenschaften, die man nicht wegkuerzen darf:

- **Faellt Laya als Entscheider aus, entscheidet Gemma im selben Turn.**
  Laya-Container gestoppt (jedes Training!), Timeout, HTTP-Fehler — der
  Satz geht nicht still an den Brain, sondern durch die Gemma-Kette. Der
  Ausfall steht im Journal und im Log (`entscheider` = "gemma (Laya-Ausfall)").
- **Der Schatten startet erst nach der echten Entscheidung** im eigenen
  Thread und kann den Turn weder verzoegern noch beeinflussen. Er ruft nie
  execute(); dieses Modul hat keinen Weg zur Gegenstelle.
- **Jeder Fehler im Schatten wird eine Log-Zeile, keine Exception** — ein
  Schatten, der den Assistenten stoert, ist schlimmer als keiner.
- **Rueckfrage-Regel (Laya, laya_rueckfrage):** sagt das Tor ja, findet die
  ziel-Frage aber kein Geraet ("keins"), und faengt die Mehrzahl-Regel den
  Satz nicht auf, wird nachgefragt statt an den Brain gegeben. Entscheidung
  Jochen 2026-10-01: "Eine Rueckfrage ist billiger als ein Brain, der
  raet." Gemessen mit aktuator-v1: fragt bei 15 Befehlen ohne Ziel ("Rollo
  zu"), 5 verhoerten Befehlen mit Ziel und 3 Saetzen Kauderwelsch nach.
  Gemmas Kette bleibt unveraendert (dort regelt der Prompt "Rollo ohne
  Raum -> kein Kommando").
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..config import ACTUATOR_SCHATTEN_LOG_PATH
from .actuator import VERDICT_AUSFUEHRBAR, VERDICT_KEIN_KOMMANDO, VERDICT_UNKLAR
from .laya_intent import KEIN_ZIEL, als_intent, frage_laya


def ausgang(intent: dict | None, verdict: str | None) -> str:
    """Was waere passiert, in einem Wort: ziel/aktion[=wert], Rückfrage, Brain."""
    if verdict == VERDICT_AUSFUEHRBAR:
        w = f"={intent.get('wert')}" if intent.get("wert") is not None else ""
        return f"{intent.get('ziel')}/{intent.get('aktion')}{w}"
    if verdict == VERDICT_UNKLAR:
        return "Rückfrage"
    if intent is None:
        return "Ausfall→Brain"
    return "Brain"


@dataclass
class Entscheidung:
    """Ergebnis einer Kette. `wer` ist "gemma" oder "laya"; `ausfall` heisst:
    die Kette konnte nicht urteilen (dann ist intent None)."""
    wer: str
    intent: dict | None
    verdict: str
    grund: str | None
    ms: float
    ausfall: bool = False
    laya_ausfall: bool = False         # Gemma entschied, weil Laya ausfiel
    tor_urteil: object = None          # nur Gemma (TorUrteil), fuer actuator_tor.log
    laya: dict = field(default_factory=dict)   # nur Laya (LayaUrteil.als_dict())

    @property
    def ausgang(self) -> str:
        return ausgang(self.intent, self.verdict)

    def als_log(self) -> dict:
        p = self.wer
        d = {f"{p}_intent": self.intent, f"{p}_verdict": self.verdict, f"{p}_grund": self.grund,
             f"{p}_ausgang": self.ausgang, f"{p}_ms": round(self.ms)}
        if p == "gemma":
            u = self.tor_urteil
            d["gemma_tor"] = None if u is None else {True: "ja", False: "nein"}.get(u.ja, "ausfall")
            d["gemma_p_ja"] = None if u is None or u.p_ja is None else round(u.p_ja, 4)
        else:
            d["laya"] = self.laya
        return d


def kette_gemma(actuator, text: str) -> Entscheidung:
    t0 = time.time()
    tor_urteil = actuator.tor(text) if actuator.cfg.tor_enabled else None
    intent = None
    verdict, grund = VERDICT_KEIN_KOMMANDO, None
    ausfall = False
    if tor_urteil is None or tor_urteil.ja:
        intent = actuator.classify(text)
        ausfall = intent is None
        verdict, grund = actuator.verdict(intent, text)
    elif tor_urteil.ja is False:
        # Tor sagte nein: kein Ausfall, ein Urteil. intent als "kein Kommando",
        # damit Log und Vergleich es nicht mit einem Ausfall verwechseln.
        intent = {"ist_kommando": False, "aktion": None, "ziel": None, "wert": None, "einheit": None}
    else:
        ausfall = True   # Tor fiel aus -> wie bisher an den Brain
    return Entscheidung("gemma", intent, verdict, grund, (time.time() - t0) * 1000,
                        ausfall=ausfall, tor_urteil=tor_urteil)


def kette_laya(actuator, text: str, timeout: float | None = None,
               variante: str = "getrennt") -> Entscheidung:
    cfg = actuator.cfg
    with actuator._lock:
        digest = dict(actuator.digest or {})
    u = frage_laya(cfg.laya_url, text, digest, timeout=timeout or cfg.laya_timeout,
                   variante=variante)
    intent = als_intent(u, digest, cfg.laya_schwelle)
    if intent is not None:
        intent = actuator._mehrzahl_gruppe(text, intent, still=True)
        if (cfg.laya_rueckfrage and not intent.get("ist_kommando")
                and u.p_ja >= cfg.laya_schwelle and u.ziel == KEIN_ZIEL):
            # Schaltabsicht ja, Geraet unklar: verdict() macht daraus UNKLAR
            # ("kein Ziel erkannt") -> Rueckfrage statt Brain.
            intent = {"ist_kommando": True, "aktion": u.aktion, "ziel": None,
                      "wert": u.wert, "einheit": None}
    verdict, grund = actuator.verdict(intent, text)
    return Entscheidung("laya", intent, verdict, grund, u.ms, ausfall=intent is None,
                        laya=u.als_dict())


def entscheiden(actuator, text: str) -> Entscheidung:
    """Die echte Entscheidung dieses Turns, laut actuator.klassifikator.
    Laya mit Gemma als Rueckfall."""
    if actuator.cfg.klassifikator == "laya" and actuator.cfg.laya_url:
        e = kette_laya(actuator, text)
        if not e.ausfall:
            return e
        print(f"⚠️  Aktuator: Laya fiel aus ({e.laya.get('fehler')}) — Gemma entscheidet")
        g = kette_gemma(actuator, text)
        g.laya = e.laya          # der Ausfall gehoert ins Log
        g.laya_ausfall = True
        return g
    return kette_gemma(actuator, text)


def _lauf(actuator, text: str, echt: Entscheidung, wakeword: str | None) -> None:
    try:
        schatten = (kette_laya(actuator, text) if echt.wer == "gemma"
                    else kette_gemma(actuator, text))
        zeile = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "transcript": text,
            "wakeword": wakeword,
            "entscheider": echt.wer,
            **echt.als_log(),
            **schatten.als_log(),
            "capabilities": actuator.version,
        }
        with open(ACTUATOR_SCHATTEN_LOG_PATH, "a") as f:
            f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
        if schatten.ausgang != echt.ausgang:
            print(f"👥 Schatten: {schatten.wer} {schatten.ausgang} ≠ {echt.wer} {echt.ausgang} "
                  f"({schatten.ms:.0f} ms)")
    except Exception as exc:  # der Schatten darf den Assistenten nie stoeren
        print(f"⚠️  Schatten: {exc}")


def schatten_starten(actuator, text: str, wakeword: str | None, echt: Entscheidung) -> None:
    """Nach der echten Entscheidung aufrufen; kehrt sofort zurueck.

    Laya entscheidet -> Gemma im Schatten (wenn actuator.schatten). Gemma
    entscheidet -> Laya im Schatten (wenn laya_url gesetzt). Ist Laya als
    Entscheider ausgefallen, gibt es keinen Schatten: die Gemma-Entscheidung
    wird trotzdem geloggt, mit dem Laya-Fehler daneben."""
    cfg = actuator.cfg
    if echt.laya_ausfall:
        _log_ausfall(actuator, text, wakeword, echt)
        return
    if echt.wer == "laya" and not cfg.schatten:
        return
    if echt.wer == "gemma" and not cfg.laya_url:
        return
    threading.Thread(target=_lauf, args=(actuator, text, echt, wakeword), daemon=True,
                     name="aktuator-schatten").start()


def _log_ausfall(actuator, text, wakeword, echt: Entscheidung) -> None:
    try:
        zeile = {"ts": datetime.now().isoformat(timespec="seconds"), "transcript": text,
                 "wakeword": wakeword, "entscheider": "gemma (Laya-Ausfall)",
                 **echt.als_log(), "laya": echt.laya, "laya_ausgang": "Ausfall→Brain",
                 "capabilities": actuator.version}
        with open(ACTUATOR_SCHATTEN_LOG_PATH, "a") as f:
            f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠️  Schatten-Log: {exc}")


def aufwaermen(actuator) -> None:
    """Beim Start einmal Laya fragen, im Hintergrund. Die erste Anfrage nach
    einem Container-Start kostet >1 s (torch baut einen Triton-Kernel) —
    als Entscheider waere das ein Laya-Timeout und damit ein Gemma-Rueckfall
    im ersten echten Turn. Meldet zugleich, ob Laya ueberhaupt antwortet."""
    rolle = "entscheidet" if actuator.cfg.klassifikator == "laya" else "Schatten, nur Log"

    def lauf():
        with actuator._lock:
            digest = dict(actuator.digest or {})
        u = frage_laya(actuator.cfg.laya_url, "Mach das Licht an", digest, timeout=60)
        if u.fehler:
            print(f"⚠️  Aktuator-Laya: {actuator.cfg.laya_url} antwortet nicht ({u.fehler})"
                  + (" — Gemma entscheidet, bis Laya wieder da ist" if rolle == "entscheidet"
                     else " — jeder Turn wird als Laya-Ausfall geloggt"))
        else:
            print(f"👥 Aktuator-Laya aktiv ({rolle}): {actuator.cfg.laya_url} ({u.ms:.0f} ms)")
    threading.Thread(target=lauf, daemon=True, name="aktuator-laya-warm").start()
