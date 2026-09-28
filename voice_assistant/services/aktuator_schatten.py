"""Schattenbetrieb des Aktuators: ein zweiter Klassifikator urteilt mit, schaltet nie.

Anlass (2026-09-29): Laya, feinabgestimmt auf Saetze aus den eigenen Zielen,
schlug Gemma auf den 334 gelabelten Saetzen deutlich — aber diese Zahl ist
optimistisch, weil die Trainingsvorlagen nach dem Lesen des Test-Sets
entstanden. Belastbar ist erst ein Vergleich auf Saetzen, die danach
gesprochen werden. Dafuer laeuft Laya hier im Schatten mit: gleicher Satz,
gleiche Nachbearbeitung (_mehrzahl_gruppe, verdict), Ergebnis neben dem der
echten Kette in actuator_schatten.log. Auswertung:
tools/aktuator_vergleich.py --schatten.

Drei Eigenschaften, die man nicht wegkuerzen darf:

- Der Schatten startet ERST NACH der echten Entscheidung, in einem eigenen
  Thread. Er kann den Turn weder verzoegern noch beeinflussen.
- Er ruft nie execute(). Es gibt in diesem Modul keinen Weg zur Gegenstelle.
- Jeder Fehler (Dienst weg, Timeout) wird eine Log-Zeile mit `fehler`, keine
  Exception im Hauptloop — ein Schatten, der den Assistenten stoert, ist
  schlimmer als keiner.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from ..config import ACTUATOR_SCHATTEN_LOG_PATH
from .actuator import VERDICT_AUSFUEHRBAR, VERDICT_UNKLAR
from .laya_intent import als_intent, frage_laya


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


def _lauf(actuator, text: str, meta: dict) -> None:
    try:
        cfg = actuator.cfg
        with actuator._lock:
            digest = dict(actuator.digest or {})
        u = frage_laya(cfg.schatten_url, text, digest, timeout=cfg.schatten_timeout)
        intent = als_intent(u, digest, cfg.schatten_schwelle)
        if intent is not None:
            intent = actuator._mehrzahl_gruppe(text, intent, still=True)
        verdict, grund = actuator.verdict(intent, text)
        zeile = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "transcript": text,
            **meta,
            "laya": u.als_dict(),
            "laya_intent": intent,
            "laya_verdict": verdict,
            "laya_grund": grund,
            "laya_ausgang": ausgang(intent, verdict),
            "capabilities": actuator.version,
        }
        with open(ACTUATOR_SCHATTEN_LOG_PATH, "a") as f:
            f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
        if zeile["laya_ausgang"] != meta["gemma_ausgang"]:
            print(f"👥 Schatten: Laya {zeile['laya_ausgang']} ≠ Gemma {meta['gemma_ausgang']} "
                  f"({u.ms:.0f} ms)")
    except Exception as exc:  # der Schatten darf den Assistenten nie stoeren
        print(f"⚠️  Schatten: {exc}")


def aufwaermen(actuator) -> None:
    """Beim Start einmal fragen, im Hintergrund. Die erste Anfrage nach einem
    Container-Start kostet >1 s (torch baut einen Triton-Kernel), und das
    soll nicht als Latenz des ersten echten Turns im Log stehen. Meldet
    zugleich, ob der Schatten ueberhaupt antwortet — sonst faellt ein toter
    Dienst erst beim Auswerten auf."""
    def lauf():
        with actuator._lock:
            digest = dict(actuator.digest or {})
        u = frage_laya(actuator.cfg.schatten_url, "Mach das Licht an", digest, timeout=60)
        if u.fehler:
            print(f"⚠️  Aktuator-Schatten: {actuator.cfg.schatten_url} antwortet nicht ({u.fehler}) "
                  f"— jeder Turn wird als Laya-Ausfall geloggt")
        else:
            print(f"👥 Aktuator-Schatten aktiv: {actuator.cfg.schatten_url} ({u.ms:.0f} ms, nur Log)")
    threading.Thread(target=lauf, daemon=True, name="aktuator-schatten-warm").start()


def starten(actuator, text: str, wakeword: str | None, tor_urteil, intent: dict | None,
            verdict: str) -> None:
    """Nach der echten Entscheidung aufrufen; kehrt sofort zurueck."""
    meta = {
        "wakeword": wakeword,
        "gemma_tor": None if tor_urteil is None else {True: "ja", False: "nein"}.get(tor_urteil.ja, "ausfall"),
        "gemma_p_ja": None if tor_urteil is None or tor_urteil.p_ja is None else round(tor_urteil.p_ja, 4),
        "gemma_intent": intent,
        "gemma_verdict": verdict,
        # intent None heisst zweierlei: Tor sagte nein (classify lief nie) oder
        # classify fiel aus. Nur letzteres ist ein Ausfall.
        "gemma_ausgang": ausgang(intent, verdict) if intent is not None
                         else ("Ausfall→Brain" if tor_urteil is None or tor_urteil.ja else "Brain"),
    }
    threading.Thread(target=_lauf, args=(actuator, text, meta), daemon=True,
                     name="aktuator-schatten").start()
