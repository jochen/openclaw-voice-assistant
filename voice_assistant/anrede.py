"""Steht das Wakewort im Transkript? — Beleg für einen Hinweis an den Brain.

Ein echter Ruf trägt das Wakewort im Pre-Roll der Aufnahme, die STT schreibt
es also mit hin ("Gaston, …", oder verhört "Gastau", "Kastronen"). Ein
Fehltrigger durch Fernseher, Hörspiel oder ein Gespräch im Raum hat es nicht:
gemessen am 2026-10-05 über 169 archivierte Aufnahmen stand in KEINEM der 11
Fernseh-/Raum-Clips eine Anrede, dagegen in 60 von 69 ausgeführten
Aktuator-Kommandos (Messung: tools/stt_vergleich.py, Messreihe 2026-10-05).

Daraus wird kein Filter, sondern ein Hinweis (Jochen, 2026-10-05): ein
Fehltrigger geht ohnehin zum Brain, und der kann am Inhalt besser urteilen als
eine Regel. Verworfen wird hier nichts.

Die Regel ist bewusst eng, denn ein falscher Hinweis kostet mehr als ein
fehlender: unscharf ähnlich zählt nur bei gleichem Anlaut (g/k/c, d/t, …) und
ab 5 Buchstaben — sonst gelten "hast", "ganz" und "Jackson" als Anrede, alle
drei kamen in Fernseh-Transkripten vor.
"""

from __future__ import annotations

import difflib
import re

_ANLAUT_KLASSEN = (frozenset("gkc"), frozenset("dt"), frozenset("bp"), frozenset("fvw"))
_WORT = re.compile(r"[^\W\d_]+")
_WOERTER = 4          # die Anrede steht vorn; weiter hinten ist "gestern" nur gestern
_AEHNLICH = 0.6
_MIN_LAENGE = 5


def _gleicher_anlaut(a: str, b: str) -> bool:
    return a[0] == b[0] or any(a[0] in k and b[0] in k for k in _ANLAUT_KLASSEN)


def anrede_im_text(text: str | None, bundle: str | None) -> bool:
    """True, wenn eines der ersten Wörter das Wakewort (oder ein Verhörer) ist.

    `bundle` ist der Name des Wakewort-Bundles ("gaston"); bei Namen aus
    mehreren Wörtern ("hey_jarvis") zählt das letzte. Ohne brauchbaren Namen
    gilt die Anrede als vorhanden — dann gibt es keinen Hinweis.
    """
    name = re.sub(r"[^a-zäöüß]", " ", (bundle or "").lower()).split()
    name = name[-1] if name else ""
    if len(name) < 4:
        return True
    for wort in _WORT.findall((text or "").lower())[:_WOERTER]:
        if wort.startswith(name[:3]):
            return True
        if (len(wort) >= _MIN_LAENGE and _gleicher_anlaut(wort, name)
                and difflib.SequenceMatcher(None, wort, name).ratio() >= _AEHNLICH):
            return True
    return False
