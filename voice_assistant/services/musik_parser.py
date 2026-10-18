"""Einfache Musik-Befehle fest aus dem Transkript lesen — ohne Modell.

Schnellweg wie beim Küchentimer (timer_parser.py): greift nur, wenn ein Satz
eindeutig ein einfacher Wiedergabe-Befehl ist (stopp, Pause, weiter,
nächstes/vorheriges Lied, lauter, leiser). Alles andere liefert None und geht
unverändert weiter an den Brain, der die Musik über das MCP-Werkzeug der
Bridge steuert. Ein übersehener Befehl ist also langsam, nicht falsch.
Gemessen über den Brain: 13–29 s für „Gaston, stopp die Musik.“
(2026-10-13..15). Entschieden (Jochen 2026-10-18): nicht über den Aktuator,
nicht über Node-RED.

Regeln, alle bewusst streng:

- **Anker**: „Musik“ oder „Lied“/„Titel“/„Song“ muss als eigenes Wort im
  Satz stehen. „Gaston, lauter.“ oder „weiter.“ allein kann auch Fernseher
  oder Rollo meinen, „Stopp alle Rollos!“ ist kein Musik-Befehl.
  „Musikprobe“ zählt nicht.
- **Genau eine Aktion.** Zwei verschiedene („stopp … nächstes Lied“) → None.
- **Keine Verneinung** („nicht“, „kein“): „die Musik nicht ausmachen“, und
  auch der Tadel „wenn du die Musik nicht ausmachst“ (2026-09-25).
- **Keine Frage nach etwas** (wie, wann, welche, ob, findest …): „wie heißt das
  nächste Lied?“. Ein „was“ allein reicht nicht zum Ablehnen: Qwen hörte
  „Gaston“ als „Was da“ („Was da stoppt die Musik?“, 2026-10-13).
- **Kurz**: höchstens ``_MAX_WOERTER`` Wörter. Lange Sätze sind Gespräch.
- **Raum** nur, wenn er im Satz steht („in der Küche“, „im Bad“), als
  gesprochenes Wort; aufgelöst wird gegen die Raumnamen der Bridge, im Code
  steht kein Raum. „im ganzen …“ → None (Gruppe, das kann der Brain).
- **Weiterspielen in einem Raum → None**: „Kannst du bitte die Musik in der
  Küche weiterspielen?“ hieß am 2026-10-16 „mitnehmen“ (der Brain startete
  dort neu), nicht „fortsetzen“. „Musik weiter in der Küche“ (ohne „spiel“)
  bleibt ein Fortsetzen dort.
- **„Pause“ allein** (höchstens Anrede + „Pause“) ist der eine Befehl ohne
  Anker: kurz genug, dass nichts anderes gemeint sein kann.
- „Das nächste Licht bitte.“ (14.10., verhörtes „Lied“) wird bewusst
  verpasst: ohne Anker kein Befehl, und Licht schaltet hier nie etwas.

Messung: tools/musik_parser_test.py (Vorlagen data/musik/de_saetze.jsonl,
echte Sätze privat in testsets/musik_steuerung/).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOP = "stop"
PAUSE = "pause"
RESUME = "resume"
NEXT = "next"
PREVIOUS = "previous"
LAUTER = "volume_up"
LEISER = "volume_down"


@dataclass
class MusikBefehl:
    aktion: str
    raum: str | None = None     # gesprochener Raum ("Küche"), None = Haupt-Mikrofon


_MAX_WOERTER = 12

_TOKEN = re.compile(r"[a-zäöüß]+")
_ANKER = {"musik", "lied", "liedchen", "titel", "song", "track"}
_VERNEINUNG = {"nicht", "kein", "keine", "keinen", "nie"}
_FRAGE = {"wie", "wann", "welche", "welcher", "welches", "welchen", "warum", "wieso",
          "weshalb", "ob", "findest", "wer", "wo", "heißt", "heisst"}

# Je Aktion: passt eines der Muster auf den kleingeschriebenen Satz?
_AKTIONEN: dict[str, re.Pattern] = {
    STOP: re.compile(r"\bstopp?\w*|\baus\b|\baus(machen|schalten|gemacht)\b"
                     r"|\bhör\w* auf\b|\baufhören\b|\bbeende\w*"),
    PAUSE: re.compile(r"\bpaus\w*"),
    RESUME: re.compile(r"\bweiter\w*|\bwieder (an|ein)\b"),
    NEXT: re.compile(r"\bnächst\w*|\bnaechst\w*|\büberspr\w*|\bskip\w*"),
    PREVIOUS: re.compile(r"\bvorherig\w*|\bdavor\b|\bzurück\b"),
    LAUTER: re.compile(r"\blauter\b"),
    LEISER: re.compile(r"\bleiser\b"),
}

_RAUM = re.compile(r"\b(?:in der|in dem|im|am|beim|auf der)\s+([A-Za-zÄÖÜäöüß]+)")
_KEIN_RAUM = {"ganzen", "ganze", "haus"}


def parse(text: str) -> MusikBefehl | None:
    if not text:
        return None
    klein = text.lower()
    woerter = _TOKEN.findall(klein)
    if not woerter or len(woerter) > _MAX_WOERTER:
        return None
    menge = set(woerter)
    if len(woerter) <= 2 and woerter[-1] == "pause":
        return MusikBefehl(PAUSE)
    if not menge & _ANKER:
        return None
    if menge & _VERNEINUNG or menge & _FRAGE:
        return None
    gefunden = [a for a, muster in _AKTIONEN.items() if muster.search(klein)]
    if len(gefunden) != 1:
        return None
    aktion = gefunden[0]
    raum = None
    m = _RAUM.search(text)
    if m:
        wort = m.group(1)
        if wort.lower() in _KEIN_RAUM:
            return None
        raum = wort
    if aktion == RESUME and raum is not None and any("spiel" in w for w in woerter):
        return None
    return MusikBefehl(aktion, raum)
