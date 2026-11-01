"""Gewohnheiten als Hinweis an den Brain (seit 2026-10-31, Experiment).

Anlass 2026-10-30 07:19: „Gaston, mach die Rollos auf“ — Laya erkennt `auf`,
aber kein Ziel; der Aktuator fragt zurück, auf die Antwort fragt der Brain
noch einmal („alle Rollos oder die Rollos im ganzen Haus?“). Morgens war in
den letzten Wochen aber jedes Mal „alle Rollos“ gemeint.

Wer die Gewohnheiten bestimmt, steht nicht hier: eine Datei, die ein anderer
Agent aus der Schaltgeschichte rechnet (Pfad im Profil, `gewohnheiten.datei`).
Hier wird nur nachgeschlagen, fest und ohne Modell. Passt eine Gewohnheit,
geht der Satz statt in die Rückfrage mit einem Hinweis an den Brain; der
schaltet und sagt die Gruppe an, oder fragt selbst, wenn etwas dagegen
spricht (Jochen 2026-10-31: „Rückfrage weg, direkt schalten mit Ansage,
erst der enge Fall“).

Der enge Fall: Klassifikator sagt Kommando ohne Ziel (oder eine Gruppe, die
der Satz nicht belegt), die Aktion passt, im Satz steht ein Gerätewort am
Wortanfang („Rollos“, nicht „Küchenrollos“), die Uhrzeit liegt im Fenster.
Alles andere bleibt wie vorher. Eine Gewohnheit gilt nur, wenn die Datei
frisch ist und die Konfidenz (Anteil der Tage, an denen diese Gruppe gemeint
war) die Schwelle erreicht. Die Schwelle 0,8 ist geschätzt, nicht gemessen:
bisher gab es nur 1,0 (Jochen hätte 0,7 geschätzt).

Selbstverstärkung: der Brain schaltet mit `ziel_erschlossen=true`, die
ausführende Seite schreibt das mit, und der Rechner der Gewohnheiten zählt
solche Schaltungen nicht als Beleg für die Zielwahl (ACTUATOR_INTERFACE.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Schemata, deren Felder hier gelesen werden. Eine neue Version des Rechners
# kommt erst dazu, wenn jemand nachgesehen hat, dass die Felder gleich blieben.
SCHEMATA = {"hauswacht.gewohnheiten.v2", "hauswacht.gewohnheiten.v3"}
PFLICHT = ("id", "geraetetyp", "aktion", "gruppe", "fenster_beginn",
           "fenster_ende", "fenster_tz", "tage", "treffer", "konfidenz")


@dataclass
class Gewohnheit:
    id: str
    gruppe: str
    aktion: str
    tage: int
    treffer: int
    konfidenz: float
    fenster_beginn: str
    fenster_ende: str


def _norm(text: str) -> str:
    # Buchstaben-Dreier kürzen wie Regel A („Rolllos“ aus der STT)
    return re.sub(r"(.)\1\1+", r"\1\1", text.lower())


def geraet_im_satz(text: str, geraete: dict) -> str | None:
    """Gerätetyp, dessen Wort ein Wort des Satzes BEGINNT, sonst None.

    Nur am Wortanfang: „Küchenrollos“ nennt einen Ort und ist nicht gemeint.
    """
    woerter = re.findall(r"\w+", _norm(text))
    for typ, staemme in geraete.items():
        for stamm in staemme:
            s = _norm(stamm)
            if any(w.startswith(s) for w in woerter):
                return typ
    return None


def _im_fenster(jetzt: datetime, beginn: str, ende: str, tz: str) -> bool:
    lokal = jetzt.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    if beginn <= ende:
        return beginn <= lokal <= ende
    return lokal >= beginn or lokal <= ende   # über Mitternacht


def finden(cfg, text: str, intent: dict | None,
           jetzt: datetime | None = None) -> tuple[Gewohnheit | None, str]:
    """Passende Gewohnheit für diesen unklaren Befehl, mit Grund fürs Log.

    Jeder Fehler (Datei fehlt, kaputt, alt, fremdes Schema) heißt: keine
    Gewohnheit, also Verhalten wie vorher.
    """
    if not cfg.enabled or not cfg.datei:
        return None, "aus"
    if not intent or not intent.get("ist_kommando"):
        return None, "kein Kommando"
    typ = geraet_im_satz(text, cfg.geraete)
    if typ is None:
        return None, "kein Gerätewort"
    jetzt = jetzt or datetime.now(timezone.utc)
    try:
        with open(cfg.datei, encoding="utf-8") as f:
            daten = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"Datei nicht lesbar ({e.__class__.__name__})"
    if daten.get("schema") not in SCHEMATA:
        return None, f"Schema {daten.get('schema')!r} unbekannt"
    try:
        stand = datetime.fromisoformat(daten["letzte_berechnung"])
        alter = (jetzt - stand).total_seconds() / 86400
    except (KeyError, TypeError, ValueError):
        return None, "letzte_berechnung fehlt"
    if alter > cfg.max_alter_tage:
        return None, f"Datei {alter:.1f} Tage alt"
    ziel = intent.get("ziel")
    if ziel is not None and ziel not in (daten.get("gaston_gruppen") or {}):
        return None, f"Ziel {ziel} ist keine Gruppe der Gewohnheiten"
    grund = f"keine Gewohnheit für {typ}/{intent.get('aktion')} um diese Zeit"
    for g in daten.get("gewohnheiten") or []:
        if not isinstance(g, dict) or any(k not in g for k in PFLICHT):
            continue
        if g["geraetetyp"] != typ or g["aktion"] != intent.get("aktion"):
            continue
        try:
            if not _im_fenster(jetzt, g["fenster_beginn"], g["fenster_ende"], g["fenster_tz"]):
                continue
            konfidenz, tage = float(g["konfidenz"]), int(g["tage"])
        except (ValueError, KeyError):
            continue
        if tage < cfg.min_tage or konfidenz < cfg.min_konfidenz:
            grund = f"{g['id']}: {g['treffer']}/{tage} Tage, Konfidenz {konfidenz:.2f} zu schwach"
            continue
        return Gewohnheit(g["id"], g["gruppe"], g["aktion"], tage, int(g["treffer"]),
                          konfidenz, g["fenster_beginn"], g["fenster_ende"]), g["id"]
    return None, grund
